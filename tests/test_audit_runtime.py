"""Audited runtime defects in picture decoding (`image.py`) and in the isolated converter (`isolated.py`): each test is the
input that showed the defect, built in memory."""

from io import BytesIO

from PIL import Image, UnidentifiedImageError, features
import pytest

from opendockit import ConvertOptions, DocumentTooLargeError, convert
from opendockit.image import (
    _OPEN_FORMATS,
    HEIF_AVAILABLE,
    ImageTooLarge,
    ImageUnreadable,
    prepare_image,
    split_tiff_pages,
)


def _jpeg(width: int, height: int, *, progressive: bool) -> bytes:
    buffer = BytesIO()
    Image.new("L", (width, height), 128).save(buffer, format="JPEG", progressive=progressive)
    return buffer.getvalue()


class TestProgressiveJpegOverThePixelLimit:
    """A JPEG over the decode pixel limit is read reduced (draft), and only the reduced size was checked against the limit. A
    progressive JPEG keeps the coefficients of the whole picture in memory whatever the reduction: a 20000×20000 one (1.5 MB on
    disk) peaked at 946 MB where a baseline one took 148 MB. The tests lower the limit to take the same path with small pictures."""

    @pytest.fixture(autouse=True)
    def _small_pixel_limit(self, monkeypatch):
        monkeypatch.setattr("opendockit.image._MAX_DECODE_PIXELS", 1_000_000)

    def test_progressive_jpeg_over_the_limit_is_too_large(self):
        data = _jpeg(3200, 2400, progressive=True)  # 7.68 million pixels; 480 thousand at 1/4, which is under the limit

        with pytest.raises(ImageTooLarge):
            prepare_image(data, min_side=1, max_side=512)

    def test_uploaded_progressive_jpeg_over_the_limit_is_a_document_too_large(self):
        with pytest.raises(DocumentTooLargeError) as error:
            convert(_jpeg(3200, 2400, progressive=True), "scan.jpg")

        assert "The image is too large" in str(error.value)

    def test_baseline_jpeg_over_the_limit_is_still_read_reduced(self):
        image = prepare_image(_jpeg(3200, 2400, progressive=False), min_side=1, max_side=512)

        assert (image.width, image.height) == (512, 384)

    def test_progressive_jpeg_within_the_limit_is_read(self):
        result = convert(_jpeg(800, 600, progressive=True), "photo.jpg", options=ConvertOptions(image_max_side=512))

        assert [(image.width, image.height) for image in result.images] == [(512, 384)]


_EPS = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 10 10\n%%EndComments\nshowpage\n%%EOF\n"


def _picture(image_format: str) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (64, 64), (200, 30, 30)).save(buffer, format=image_format)
    return buffer.getvalue()


class TestPictureFormatAllowList:
    """`Image.open` tried every Pillow plugin on a picture taken out of a document. An EPS reached the PostScript plugin, which
    starts Ghostscript to draw it: an external interpreter run on untrusted input."""

    def test_eps_is_refused_without_reaching_the_postscript_plugin(self, monkeypatch):
        Image.init()
        factory, accept = Image.OPEN["EPS"]
        reached = []

        def watched(*arguments, **keywords):
            reached.append("EPS")
            return factory(*arguments, **keywords)

        monkeypatch.setitem(Image.OPEN, "EPS", (watched, accept))

        with pytest.raises(ImageUnreadable) as error:
            prepare_image(_EPS, min_side=1, max_side=2000)

        assert reached == []
        assert isinstance(error.value.__cause__, UnidentifiedImageError)

    def test_no_format_that_runs_another_program_or_is_not_a_picture_is_allowed(self):
        assert not {"EPS", "WMF", "FPX", "MIC", "MPEG", "BUFR", "GRIB", "HDF5", "FITS"} & set(_OPEN_FORMATS)

    def test_every_allowed_format_is_one_pillow_can_open(self):
        Image.init()

        assert set(_OPEN_FORMATS) <= set(Image.OPEN)  # a name Pillow does not know is a KeyError in `Image.open`

    @pytest.mark.parametrize("image_format", ["JPEG", "PNG", "WEBP", "GIF", "BMP", "TIFF", "AVIF", "ICO", "PCX", "PPM", "MPO"])
    def test_supported_format_is_still_read(self, image_format):
        image = prepare_image(_picture(image_format), min_side=1, max_side=2000)

        assert (image.width, image.height) == (64, 64)

    @pytest.mark.skipif(not features.check_codec("jpg_2000"), reason="this Pillow build has no JPEG 2000 codec")
    def test_jpeg_2000_is_still_read(self):
        image = prepare_image(_picture("JPEG2000"), min_side=1, max_side=2000)

        assert (image.width, image.height) == (64, 64)

    @pytest.mark.skipif(not HEIF_AVAILABLE, reason="the optional heic extra is not installed")
    def test_heic_is_still_read_with_the_optional_extra(self):
        image = prepare_image(_picture("HEIF"), min_side=1, max_side=2000)

        assert (image.width, image.height) == (64, 64)

    def test_multi_page_tiff_is_still_split(self):
        buffer = BytesIO()
        pages = [Image.new("L", (64, 48), shade) for shade in (0, 255)]
        pages[0].save(buffer, format="TIFF", save_all=True, append_images=pages[1:])

        assert len(split_tiff_pages(buffer.getvalue()).pages) == 2
