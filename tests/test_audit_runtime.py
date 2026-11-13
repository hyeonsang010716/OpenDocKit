"""Audited runtime defects in picture decoding (`image.py`) and in the isolated converter (`isolated.py`): each test is the
input that showed the defect, built in memory."""

from io import BytesIO

from PIL import Image
import pytest

from opendockit import ConvertOptions, DocumentTooLargeError, convert
from opendockit.image import ImageTooLarge, prepare_image


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
