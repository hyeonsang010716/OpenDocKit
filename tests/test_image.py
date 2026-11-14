"""Image extraction, normalization and filling in descriptions (vision model).

The vision model is replaced by fake describers. The checks against the real OpenAI API are recorded in docs/design.md.
"""

import asyncio
from io import BytesIO
import re
import struct
import zlib

import olefile
from PIL import Image
import pytest

from opendockit import (
    ConvertOptions,
    CorruptedDocumentError,
    DocumentFormat,
    ImageKind,
    OpenAIImageDescriber,
    aconvert,
    convert,
    describe_images,
)
from opendockit.extractors import _pdf_figures
from opendockit.extractors.hwp import (
    TAG_BIN_DATA,
    TAG_CTRL_HEADER,
    TAG_PARA_HEADER,
    TAG_PARA_TEXT,
    TAG_SHAPE_PICTURE,
)
from opendockit.image import ImageSkipped, ImageUnreadable, prepare_image
from tests import builder
from tests.test_hwp import _OLE_MAGIC


class FakeDescriber:
    def __init__(self, answers=None, fail_ids=()):
        self.answers = answers or {}
        self.fail_ids = set(fail_ids)
        self.seen = []

    async def describe(self, image):
        self.seen.append(image)
        await asyncio.sleep(0)  # yields control like a real describer waiting on the network; without it everything finishes with no switch
        if image.id in self.fail_ids:
            raise RuntimeError("network error")
        return self.answers.get(image.id, f"설명 {image.id}\n세부 {image.id}")


def described(data, filename, describer=None, **options):
    return asyncio.run(aconvert(data, filename, ConvertOptions(**options), describer=describer or FakeDescriber()))


# Latin/digit fragments like those left in the text layer of a scanned Korean book (over 100 characters per page).
_LATIN_FRAGMENTS = ["2.1", "API", "GPT-4", "OpenAI API", "prompt = 'How much is 369 * 1235?'", "chat_completion(prompt)", "454965",
                    "gpt-3.5-turbo", "text-davinci-003", "https://platform.openai.com/docs/models", "LLM", "40"]


def _scan_with_text_layer(pages: list[list[str]], *, dense: bool = False, covered: list[bool] | None = None,
                          table_on_last: bool = False) -> bytes:
    """PDF with text-layer lines laid over a page-covering image (scan) on every page. `covered` can drop the covering image per page."""
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    width, height = A4
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    for index, lines in enumerate(pages):
        if covered is None or covered[index]:
            pdf.drawImage(ImageReader(BytesIO(builder.png_bytes(600, 800, seed=20 + index))), 0, 0, width=width, height=height)
        pdf.setFont("HYSMyeongJo-Medium", 10)
        step = 18 if dense else 60
        for row, line in enumerate(lines):
            pdf.drawString(72 if dense else 72 + (row % 3) * 120, height - 72 - row * step, line)

        if table_on_last and index == len(pages) - 1:
            pdf.grid([300, 380, 460], [140, 115, 90])
            for row, cells in enumerate((("A1", "B1"), ("A2", "B2"))):
                for column, cell in enumerate(cells):
                    pdf.drawString(310 + column * 80, 122 - row * 25, cell)
        pdf.showPage()

    pdf.save()
    return buffer.getvalue()


class TestPrepareImage:
    def test_skips_tiny_icon_and_thin_separator(self):
        with pytest.raises(ImageSkipped):
            prepare_image(builder.png_bytes(16, 16), min_side=64, max_side=2048)
        with pytest.raises(ImageSkipped):
            prepare_image(builder.png_bytes(2000, 70), min_side=64, max_side=2048)

    def test_downscales_long_side_and_reencodes_to_jpeg(self):
        data = builder.png_bytes(4000, 1000, image_format="BMP")

        image = prepare_image(data, min_side=64, max_side=2048)

        assert (image.width, image.height, image.mime_type) == (2048, 512, "image/jpeg")
        assert Image.open(BytesIO(image.data)).format == "JPEG"

    @pytest.mark.parametrize("mode", ["I;16", "I;16B", "I", "F"])
    def test_high_bit_depth_grayscale_is_converted_not_a_crash(self, mode):
        """For 16-bit grayscale (I;16) Pillow raised ValueError in the downscaling step and convert died with an exception outside
        ConversionError (long side 8192 or more). It is converted to 8-bit grayscale and then downscaled."""
        source = BytesIO()
        Image.linear_gradient("L").resize((9000, 1000)).convert("I").convert(mode).save(source, format="TIFF")

        image = prepare_image(source.getvalue(), min_side=64, max_side=2048)

        assert (image.width, image.height) == (2048, 228)
        assert Image.open(BytesIO(image.data)).getextrema() != (0, 0)

    def test_keeps_transparency_as_png(self):
        image = prepare_image(builder.png_bytes(mode="RGBA"), min_side=64, max_side=2048)

        assert image.mime_type == "image/png"

    def test_broken_png_is_unreadable_not_a_crash(self):
        """Pillow raises SyntaxError when it meets a corrupted PNG chunk while reading pixels. That was outside the list of caught
        exceptions, so convert died with an exception that is not a ConversionError (found by fuzzing that tampered with a
        picture embedded in a real HWP)."""
        data = builder.png_bytes(400, 300)
        start = data.index(b"IDAT") - 4
        (length,) = struct.unpack(">I", data[start:start + 4])
        payload, rest = data[start + 8:start + 8 + length], data[start + 12 + length:]

        def chunk(kind: bytes, body: bytes) -> bytes:
            return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))

        broken = data[:start] + chunk(b"IDAT", payload[:length // 2]) + chunk(b"IDA\x03", payload[length // 2:]) + rest

        with pytest.raises(ImageUnreadable):
            prepare_image(broken, min_side=64, max_side=2048)

    def test_undecodable_vector_image_is_unreadable(self):
        """Pillow cannot render the WMF/EMF of Hangul (HWP) documents on macOS or Linux."""
        with pytest.raises(ImageUnreadable):
            prepare_image(b"\xd7\xcd\xc6\x9a" + b"\x00" * 100, min_side=64, max_side=2048)


class TestStandaloneImage:
    def test_without_describer_keeps_image_and_placeholder(self):
        result = convert(builder.png_bytes(image_format="JPEG"), "사진.jpg")

        assert result.format is DocumentFormat.IMAGE
        assert result.text == "[Image]"
        assert [image.kind for image in result.images] == [ImageKind.STANDALONE]

    def test_wide_uploaded_image_is_not_dropped_as_separator(self):
        """Divider filtering (aspect ratio 20:1) applies only to pictures inside a document. A wide banner uploaded by a user
        (4208×180) used to be dropped."""
        result = convert(builder.png_bytes(4200, 180), "banner.png")

        assert len(result.images) == 1
        assert result.text == "[Image]"

    def test_describer_fills_text_as_image_block(self):
        result = described(builder.png_bytes(), "a.png")

        assert result.text == "> [Image] 설명 1\n> 세부 1"

    def test_empty_answer_for_uploaded_image_is_reported_as_failed(self):
        """When the describer gave a blank answer for an image uploaded on its own, the text was only '[Image]' yet the result
        came back as a success with no warning. The whole document is that one picture, so it is reported as a description
        failure, and calling again retries."""
        result = described(builder.png_bytes(), "a.png", FakeDescriber(answers={1: ""}))

        assert result.text == "[Image]"
        assert result.images[0].failed
        assert any("could not be described" in warning for warning in result.warnings)

    @pytest.mark.parametrize("data", [builder.png_bytes(200, 120)[:60], b"\xff\xd8\xff\xe0" + b"\x00" * 200],
                             ids=["truncated_png", "junk_jpeg"])
    def test_uploaded_image_that_cannot_be_decoded_is_rejected_as_corrupted(self, data):
        """When an image uploaded on its own could not be decoded, the text was only '[Image]' yet the result came back as a
        success with no warning at all. The whole document is that one picture, so it is rejected like any other corrupted file."""
        with pytest.raises(CorruptedDocumentError) as error:
            convert(data, "scan.png")

        assert isinstance(error.value.__cause__, ImageUnreadable)

    @pytest.mark.parametrize("size_field", [0, None], ids=["zero", "padded"])
    def test_bmp_is_detected_without_trusting_the_file_size_field(self, size_field):
        """A file was taken for a BMP only when the file size in the BMP header (bfSize) equaled the actual length, so files that
        wrote 0 there and files with trailing padding bytes were rejected as an "unsupported format". Detection uses 'BM' and
        the DIB header size."""
        data = bytearray(builder.png_bytes(200, 120, image_format="BMP"))
        if size_field is None:
            data += b"\x00" * 16
        else:
            data[2:6] = size_field.to_bytes(4, "little")

        result = convert(bytes(data), "scan.bmp")

        assert result.format is DocumentFormat.IMAGE
        assert len(result.images) == 1


class TestDocxImages:
    def test_places_description_after_text_with_context_and_alt(self):
        describer = FakeDescriber()

        result = described(builder.docx_with_images(), "a.docx", describer)

        assert result.text.startswith("차트 앞 문단입니다.\n\n> [Image] 설명 1\n> 세부 1")
        first = describer.seen[0]
        assert first.alt == "분기별 매출 차트"
        assert first.context == "차트 앞 문단입니다."

    def test_drops_tiny_icon_and_renders_cell_image_in_one_line(self):
        result = described(builder.docx_with_images(), "a.docx")

        assert "아이콘" in result.text and "[Image] 설명 2" not in result.text.split("아이콘")[1].split("|")[0]
        assert "| 사진 | [Image] 설명 3<br>세부 3 |" in result.text

    def test_repeated_logo_is_removed_as_decoration(self):
        result = convert(builder.docx_with_images(logo_count=3), "a.docx")

        assert len(result.images) == 2
        assert result.text.count("[Image") == 2

    def test_same_picture_is_decoded_once(self, monkeypatch):
        """When the same picture appeared several times (a stamp or logo on every page) it was decoded, downscaled and re-encoded
        for each reference. This is done only once."""
        from opendockit import converter

        calls = []
        monkeypatch.setattr(converter, "prepare_image",
                            lambda data, **options: calls.append(data) or prepare_image(data, **options))

        convert(builder.docx_with_images(logo_count=4), "a.docx")

        assert len(calls) == len(set(calls))

    def test_unreadable_picture_in_a_document_is_reported_in_warnings(self):
        """A picture that could not be decoded (WMF, EMF, corrupted) was left as just '[Image]' with no warning, so there was no way
        to tell that the picture content was missing."""
        import base64

        broken = base64.b64encode(builder.png_bytes(200, 120)[:60]).decode()
        html = f'<p>본문</p><img src="data:image/png;base64,{broken}"><img src="data:image/png;base64,{broken}">'

        result = convert(html.encode(), "a.html")

        assert result.images == []
        assert "[Image]" in result.text
        assert any("1 image(s) are in an unreadable format" in warning for warning in result.warnings)

    def test_extract_images_off_leaves_no_placeholder(self):
        result = convert(builder.docx_with_images(), "a.docx", ConvertOptions(extract_images=False))

        assert result.images == []
        assert "[Image" not in result.text


class TestOtherFormats:
    def test_pptx_picture_shape(self):
        result = described(builder.pptx_with_image(), "a.pptx")

        assert result.text == "## Slide 1: 구성도\n\n> [Image] 설명 1\n> 세부 1"

    def test_xlsx_sheet_image_goes_after_table(self):
        result = described(builder.xlsx_with_image(), "a.xlsx")

        assert result.text.endswith("| a | 1 |\n\n> [Image] 설명 1\n> 세부 1")

    def test_xlsx_that_is_large_only_because_of_its_picture_keeps_merges_and_picture(self):
        """Full load was decided by file size (2MB), so a workbook that was large only because of a picture fell to streaming and
        lost the picture and the merged-cell information. What uses memory is the cell data, so the decision uses the sheet XML
        size."""
        import random

        import openpyxl
        from openpyxl.drawing.image import Image as SheetImage

        noise = Image.frombytes("RGB", (1000, 1000), random.Random(1).randbytes(3_000_000))
        picture = BytesIO()
        noise.save(picture, format="PNG")
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        for row in (["권역", "지점"], ["수도권", "강남"], [None, "종로"]):
            sheet.append(row)
        sheet.merge_cells("A2:A3")
        sheet.add_image(SheetImage(BytesIO(picture.getvalue())), "D2")
        buffer = BytesIO()
        workbook.save(buffer)
        assert len(buffer.getvalue()) > 2 * 1024 * 1024

        result = convert(buffer.getvalue(), "a.xlsx")

        assert "| 수도권 | 종로 |" in result.text
        assert len(result.images) == 1

    def test_xlsx_pictures_are_extracted_even_when_streamed(self, monkeypatch):
        """A workbook with large cell data is read by streaming (a real 130,000-row sheet). Even then pictures are taken straight
        from the ZIP. The picture after "계산 모델은 다음 그림과 같다" (the calculation model is shown in the following figure)
        used to be dropped entirely."""
        from opendockit.extractors import spreadsheet

        monkeypatch.setattr(spreadsheet, "_FULL_LOAD_MAX_SHEET_BYTES", 0)

        result = described(builder.xlsx_with_image(), "a.xlsx")

        assert result.text.endswith("| a | 1 |\n\n> [Image] 설명 1\n> 세부 1")

    def test_hwpx_pictures_in_body_and_table_cell(self):
        """The '그림입니다…' ("This is a picture…") caption Hangul (HWP) adds automatically is not used as alt text."""
        describer = FakeDescriber()

        result = described(builder.hwpx_with_images(), "a.hwpx", describer)

        assert "그림 앞 문단\n\n> [Image] 설명 1" in result.text
        assert "| 도면 | [Image] 설명 2<br>세부 2 |" in result.text
        assert describer.seen[0].alt is None

    def test_html_data_uri_is_extracted_but_external_url_is_not_fetched(self):
        import base64

        encoded = base64.b64encode(builder.png_bytes()).decode()
        html = f'<p>본문</p><img src="data:image/png;base64,{encoded}"><img src="https://example.com/a.png" alt="외부 그림">'

        result = described(html.encode(), "a.html")

        assert result.text == "본문\n\n[Image: 외부 그림]\n\n> [Image] 설명 1\n> 세부 1"

    def test_hwp_picture_via_bin_data(self, monkeypatch):
        record = builder.hwp_record
        picture = b"\x00" * 71 + struct.pack("<H", 1) + b"\x00" * 18
        section = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text("앞 글", (11, "gso "))),
            record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("gso ")),
            record(TAG_SHAPE_PICTURE, 2, picture),
        ])
        bin_data = struct.pack("<HHH", 0x1, 1, 3) + "png".encode("utf-16-le")
        streams = {
            "FileHeader": builder.hwp_file_header(0),
            "DocInfo": record(TAG_BIN_DATA, 0, bin_data),
            "BodyText/Section0": section,
            "BinData/BIN0001.png": builder.png_bytes(),
        }
        _patch_ole(monkeypatch, streams)

        result = described(_OLE_MAGIC + b"\x00" * 504, "a.hwp")

        assert result.text == "앞 글\n\n> [Image] 설명 1\n> 세부 1"


class TestHwpRepeatedAndBrokenPictures:
    def _document(self, monkeypatch, picture_stream: bytes, references: int) -> bytes:
        """Compressed HWP that refers to one picture (BinItem 1) `references` times."""
        record = builder.hwp_record
        picture = b"\x00" * 71 + struct.pack("<H", 1) + b"\x00" * 18
        paragraph = b"".join([
            record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
            record(TAG_PARA_TEXT, 1, builder.hwp_text("앞 글", (11, "gso "))),
            record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("gso ")),
            record(TAG_SHAPE_PICTURE, 2, picture),
        ])
        bin_data = struct.pack("<HHH", 0x1, 1, 3) + "bmp".encode("utf-16-le")
        deflate = lambda data: zlib.compress(data)[2:-4]  # noqa: E731
        _patch_ole(monkeypatch, {
            "FileHeader": builder.hwp_file_header(1),
            "DocInfo": deflate(record(TAG_BIN_DATA, 0, bin_data)),
            "BodyText/Section0": deflate(paragraph * references),
            "BinData/BIN0001.bmp": picture_stream,
        })
        return _OLE_MAGIC + b"\x00" * 504

    def test_picture_referenced_many_times_is_read_and_counted_once(self, monkeypatch):
        """The same picture was read and inflated again for every reference, so a document with 16,384 references to one picture
        took 150 seconds and 1.4GB. The decompression limit was also consumed per reference, so a valid document could be
        rejected as a "document that becomes too large". The picture stream is read only once."""
        bitmap = builder.png_bytes(400, 300, image_format="BMP")
        data = self._document(monkeypatch, zlib.compress(bitmap)[2:-4], references=10)

        result = convert(data, "a.hwp", ConvertOptions(max_uncompressed_bytes=len(bitmap) * 3))

        assert result.text.count("앞 글") == 10

    def test_picture_that_cannot_be_inflated_is_skipped(self, monkeypatch):
        """A broken compression stream in one picture does not reject the whole document. Only that picture is skipped."""
        data = self._document(monkeypatch, b"\xff\xfe not deflate \x00\x01", references=1)

        result = convert(data, "a.hwp")

        assert result.text == "앞 글"
        assert not result.images


class TestPdfImages:
    def test_crops_figure_in_reading_position_and_ignores_icon(self):
        result = described(builder.pdf_with_images(), "a.pdf")

        page = result.sections[0].text
        assert page == "그림 위 문단입니다\n\n> [Image] 설명 1\n> 세부 1\n\n그림 아래 문단입니다"

    def test_figure_is_rendered_at_its_native_resolution(self):
        """For a 400×240 original placed at 200×120pt, rendering at 2x (400×240) is enough, but when the original is larger than
        its displayed size it is rendered at the original's pixel count (up to 4x). Reading of small text was unstable."""
        from io import BytesIO as Buffer

        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas

        buffer = Buffer()
        pdf = canvas.Canvas(buffer)
        pdf.drawImage(ImageReader(Buffer(builder.png_bytes(900, 400, seed=12))), 72, 400, width=300, height=133)
        pdf.drawString(72, 300, "text")
        pdf.save()

        result = convert(buffer.getvalue(), "a.pdf")

        assert result.images[0].width >= 898

    @pytest.mark.parametrize("boxes", [b"/CropBox [ 100 100 495 742 ] /MediaBox [ ", b"/MediaBox [ -50 -50 545 792 ] /Unused [ "],
                             ids=["cropbox", "mediabox-origin"])
    def test_figure_is_cropped_where_it_is_shown_when_page_boxes_are_offset(self, boxes):
        """In print PDFs the CropBox is smaller than the MediaBox, or the MediaBox does not start at 0. Text and picture
        coordinates are relative to the MediaBox while rendering draws only the visible area (CropBox), so instead of the
        picture, the blank paper next to it and body text were cropped and sent to the vision model."""
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas

        solid = BytesIO()
        Image.new("RGB", (400, 240), (200, 30, 30)).save(solid, format="PNG")
        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=(595, 842))
        pdf.drawString(172, 642, "text above")
        pdf.drawImage(ImageReader(solid), 172, 362, width=200, height=120)
        pdf.save()
        data = buffer.getvalue().replace(b"/MediaBox [ ", boxes, 1)
        assert boxes in data

        result = convert(data, "a.pdf")

        figure = Image.open(BytesIO(result.images[0].data)).convert("RGB")
        corners = [(5, 5), (figure.width - 6, 5), (5, figure.height - 6), (figure.width - 6, figure.height - 6)]
        assert all(figure.getpixel(point)[0] > 150 and figure.getpixel(point)[1] < 90 for point in corners)

    def test_page_of_vector_outlines_without_text_is_sent_whole_for_ocr(self):
        """A design PDF whose text was converted to outlines has neither text nor images, only vector shapes. It was not treated
        as a scanned page, so only an empty result came out. A page with many curves and no text at all is sent whole."""
        from reportlab.pdfgen import canvas

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer)
        for index in range(120):
            path = pdf.beginPath()
            path.moveTo(72, 780 - index * 6)
            path.curveTo(150, 790 - index * 6, 250, 770 - index * 6, 400, 780 - index * 6)
            pdf.drawPath(path)
        pdf.save()

        result = described(buffer.getvalue(), "a.pdf")

        assert [image.kind for image in result.images] == [ImageKind.PAGE]
        assert "설명 1" in result.text

    def test_page_with_only_a_frame_is_not_sent(self):
        """A blank page with only a frame and divider lines has no content. It is not sent to the vision model."""
        from reportlab.pdfgen import canvas

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer)
        pdf.rect(40, 40, 500, 760)
        pdf.line(40, 400, 540, 400)
        pdf.save()

        result = convert(buffer.getvalue(), "a.pdf")

        assert not result.images

    def test_image_only_page_is_sent_whole_for_ocr(self):
        describer = FakeDescriber(answers={2: "# 스캔 제목\n스캔 본문"})

        result = described(builder.pdf_with_images(), "a.pdf", describer)

        assert describer.seen[1].kind is ImageKind.PAGE
        assert result.sections[1].text == "<!-- Text read from image -->\n# 스캔 제목\n스캔 본문"
        assert not any("OCR" in warning for warning in result.warnings)

    def test_scanned_page_with_a_few_stamp_characters_is_still_sent_whole(self):
        """On a scanned page with a few characters in the text layer, such as a scanner-app stamp or a fax header, the whole
        image was filtered out as "background" and the content vanished entirely (with no warning). If there are only a few
        characters and an image covers the page, it is a scanned page."""
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas

        width, height = A4
        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=A4)
        pdf.drawImage(ImageReader(BytesIO(builder.png_bytes(1200, 1600, seed=3))), 0, 0, width=width, height=height)
        pdf.setFont("Helvetica", 9)
        pdf.drawString(width / 2, 20, "COPY")
        pdf.save()
        describer = FakeDescriber(answers={1: "스캔 본문"})

        result = described(buffer.getvalue(), "scan.pdf", describer)

        assert [image.kind for image in result.images] == [ImageKind.PAGE]
        assert "스캔 본문" in result.sections[0].text and "COPY" in result.sections[0].text

    def test_scan_cut_into_abutting_strips_under_a_korean_text_layer_is_one_background(self):
        """In a PDF whose scanner stored each page as 20 horizontal strips with an OCR text layer on top, every strip became a
        figure and 20 pictures per page (2,000 for 100 pages) were wedged into the text. When images drawn directly on the page
        abut with no gaps and cover the page, they are treated as one background and only the text layer is used."""
        lines = [f"한글 OCR 글자층 {row + 1}번째 줄: 스캔 문서를 문자 인식한 결과가 얹혀 있습니다." for row in range(12)]

        result = convert(builder.pdf_scanned_in_strips([lines, lines]), "scan.pdf")

        assert result.images == []
        assert result.text.count("문자 인식한 결과") == 24

    def test_scan_cut_into_abutting_strips_with_a_few_stamp_characters_is_sent_whole(self):
        """If a scanned page cut into strips has only a few characters (a stamp), the whole page is sent, as for a single-image scan."""
        result = convert(builder.pdf_scanned_in_strips([["COPY"]]), "scan.pdf")

        assert [image.kind for image in result.images] == [ImageKind.PAGE]

    def test_pictures_stacked_with_gaps_between_them_are_separate_figures(self):
        """If there are gaps between the strips (photos placed side by side), they are pictures, not a background."""
        result = convert(builder.pdf_scanned_in_strips([["사진 넉 장 아래 설명 글입니다."]], strips=4, gap=12), "a.pdf")

        assert [image.kind for image in result.images] == [ImageKind.FIGURE] * 4

    def test_scan_whose_text_layer_holds_only_latin_fragments_is_sent_whole(self):
        """The text layer of a PDF scanned from a Korean book held only Latin/digit fragments (over 100 characters per page). The
        pages were taken for pages with text and the page-covering scan image was dropped as background, so the Korean body
        vanished without a warning and only fragments such as 'API' and '2.1' came out. When the text layer of the pages with a
        covering image has no Hangul at all and characters are sparse, the text layer is not trusted and each whole page is
        handed over as an image."""
        data = _scan_with_text_layer([_LATIN_FRAGMENTS] * 3)
        describer = FakeDescriber(answers={1: "첫 쪽 한글 본문", 2: "둘째 쪽 한글 본문", 3: "셋째 쪽 한글 본문"})

        result = described(data, "scan.pdf", describer)

        assert [image.kind for image in result.images] == [ImageKind.PAGE] * 3
        assert all(f"{nth} 쪽 한글 본문" in section.text for nth, section in zip(("첫", "둘째", "셋째"), result.sections, strict=True))
        assert "chat_completion" in result.sections[0].text  # the text-layer fragments are not discarded
        assert any("no body text in their text layer" in warning and "3 PDF page(s) (1, 2, 3)" in warning for warning in result.warnings)

    def test_untrusted_text_layer_is_reported_when_pages_are_not_described(self):
        result = convert(_scan_with_text_layer([_LATIN_FRAGMENTS] * 3), "scan.pdf")

        assert [image.kind for image in result.images] == [ImageKind.PAGE] * 3
        assert any("3 page(s) whose text exists only as an image (1, 2, 3)" in warning for warning in result.warnings)

    def test_untrusted_pages_beyond_the_picture_budget_are_not_said_to_be_handed_over(self):
        """With `max_images` low, the render budget ran out before the last pages, yet the warning said every page with an
        untrusted text layer was handed over as an image. Only the rendered pages are named so; the others are named as read
        from their text layer only."""
        data = _scan_with_text_layer([_LATIN_FRAGMENTS] * 3)

        nothing = convert(data, "scan.pdf", ConvertOptions(max_images=0))
        some = convert(data, "scan.pdf", ConvertOptions(max_images=1))

        assert not nothing.images
        assert not any("handed over as an image" in warning for warning in nothing.warnings)
        assert any("3 PDF page(s) (1, 2, 3)" in warning and "only their text layer was read" in warning
                   for warning in nothing.warnings)
        # The render budget is twice `max_images`: pages 1 and 2 are rendered (the converter then keeps one), page 3 is not.
        handed = [warning for warning in some.warnings if "handed over as an image" in warning]
        assert len(handed) == 1 and "2 PDF page(s) (1, 2)" in handed[0]
        assert any("only their text layer was read" in warning and "(3)" in warning for warning in some.warnings)

    def test_text_layer_with_korean_is_trusted_even_on_pages_without_korean(self):
        """If the text layer has Hangul on even one page, it is a text layer that can hold Hangul (a scan with proper OCR, a
        document laid over a background picture). Pages with only Latin/digits (code, tables) are also read from the text layer
        as is, and the covering image is left as background."""
        data = _scan_with_text_layer([_LATIN_FRAGMENTS, _LATIN_FRAGMENTS, [*_LATIN_FRAGMENTS, "한글 본문이 들어 있는 줄입니다"]])

        result = convert(data, "scan.pdf")

        assert not result.images
        assert not any("no body text in their text layer" in warning for warning in result.warnings)

    def test_dense_text_layer_without_korean_is_trusted(self):
        """In an English scan with proper OCR applied, or a document with English body text typeset over a background picture,
        characters fill the page even without Hangul. The text layer is the body."""
        line = "The quick brown fox jumps over the lazy dog and keeps running along the river bank."
        data = _scan_with_text_layer([[line] * 40] * 3, dense=True)

        result = convert(data, "scan.pdf")

        assert not result.images
        assert result.text.count("quick brown fox") == 120

    def test_few_covered_pages_in_a_text_document_are_not_treated_as_scans(self):
        """A document where a picture covers only a few pages, such as the cover or divider pages of an English document, is not a
        scan. The document's text layer is doubted only when pages with a covering image are more than half of the pages with
        text."""
        line = "Plain body text of a digital document that has no background picture at all."
        data = _scan_with_text_layer([_LATIN_FRAGMENTS, [line] * 30, [line] * 30, [line] * 30], dense=True,
                                     covered=[True, False, False, False])

        result = convert(data, "report.pdf")

        assert not result.images
        assert not any("no body text in their text layer" in warning for warning in result.warnings)

    def test_untrusted_text_layer_page_with_a_ruled_table_keeps_its_table(self):
        """On a page where a ruled table is detected, the text layer is intact enough to form a table. That page is not handed over
        as a scanned page."""
        data = _scan_with_text_layer([_LATIN_FRAGMENTS] * 3, table_on_last=True)

        result = convert(data, "scan.pdf")

        assert [image.section_index for image in result.images if image.kind is ImageKind.PAGE] == [1, 2]
        assert "| A1 | B1 |" in result.sections[2].text

    def test_undescribed_image_page_is_reported(self):
        result = convert(builder.pdf_with_images(), "a.pdf")

        assert result.sections[1].text == ""
        assert any("1 page(s) whose text exists only as an image (2)" in warning for warning in result.warnings)


class TestDescribeImages:
    def test_failure_keeps_placeholder_and_warns(self):
        result = described(builder.png_bytes(), "a.png", FakeDescriber(fail_ids={1}))

        assert result.text == "[Image]"
        assert any("could not be described (the vision model call failed)" in warning for warning in result.warnings)

    def test_describing_again_retries_only_failed_images_and_clears_warning(self):
        """Only the failed images are asked about again. Previously, calling again re-invoked everything including the images that
        had succeeded, and the failure warning stayed."""
        first = described(builder.docx_with_images(), "a.docx", FakeDescriber(fail_ids={1}))
        retry = FakeDescriber(answers={1: "다시 읽은 차트"})

        second = asyncio.run(describe_images(first, retry))

        assert [image.failed for image in first.images] == [True, False]
        assert [image.id for image in retry.seen] == [1]
        assert "[Image] 다시 읽은 차트" in second.text and "[Image] 설명 3" in second.text
        assert not any("could not be described" in warning for warning in second.warnings)

    def test_skip_answer_removes_decorative_figure(self):
        """Checked with a picture that has no alt text (#3, in the table cell). With alt text it stays
        (TestDecorativePictureWithAltText)."""
        result = described(builder.docx_with_images(), "a.docx", FakeDescriber(answers={3: "SKIP"}))

        assert "| 사진 |  |" in result.text

    @pytest.mark.parametrize("answer", ["SKIP", "SKIP.", "(SKIP)", "`SKIP`", "skip", "**SKIP**", "(판독 불가)"])
    def test_skip_answer_variants_count_as_decorative(self, answer):
        """The real gpt-4o-mini once answered in a decorated form like '(SKIP)' and those characters went into the text."""
        result = described(builder.docx_with_images(), "a.docx", FakeDescriber(answers={3: answer}))

        assert "SKIP" not in result.text.upper()
        assert "| 사진 |  |" in result.text
        assert result.images[1].decorative

    @pytest.mark.parametrize("answer", ["", None, "```\n```"], ids=["empty", "none", "fence-only"])
    def test_completely_empty_answer_for_a_figure_is_a_failure_not_a_decoration(self, answer):
        """A blank answer (`""`, None) was treated as decoration like SKIP and the picture was removed without a warning. SKIP is the
        model's verdict, but a blank answer is not an answer (a truncated response, empty content). As for a standalone image
        (`test_empty_answer_for_uploaded_image_is_reported_as_failed`), it is left as a failure so that calling again retries.
        SKIP-like verdicts remain decoration."""
        result = described(builder.docx_with_images(), "a.docx", FakeDescriber(answers={3: answer}))

        assert result.images[1].failed and not result.images[1].decorative
        assert "| 사진 | [Image] |" in result.text
        assert any("could not be described" in warning for warning in result.warnings)

    @pytest.mark.parametrize("answer", ["SKIP", "(판독 불가)", "**SKIP**"])
    def test_skip_answer_for_an_uploaded_image_is_a_failure(self, answer):
        """A standalone image gets a prompt that does not allow SKIP, but when the model answered SKIP those characters became the
        text. Like a blank answer, it is a failure."""
        result = described(builder.png_bytes(), "a.png", FakeDescriber(answers={1: answer}))

        assert result.text == "[Image]"
        assert result.images[0].failed and not result.images[0].decorative
        assert any("could not be described" in warning for warning in result.warnings)

    def test_skip_answer_for_a_scanned_page_is_a_failure(self):
        """When SKIP came back for a scanned page, `<!-- Text read from image -->\nSKIP` became the text."""
        describer = FakeDescriber(answers={2: "SKIP"})

        result = described(builder.pdf_with_images(), "a.pdf", describer)

        assert describer.seen[1].kind is ImageKind.PAGE
        assert "SKIP" not in result.text
        assert result.images[1].failed
        assert any("could not be described" in warning for warning in result.warnings)

    def test_strips_code_fence_lines_inside_description(self):
        """The real gpt-4o-mini wrapped only the body after the first-line summary in ```. A code block inside a quote block is
        noise to the LLM."""
        answer = "영수증\n\n```\n합계 265,000\n```"

        result = described(builder.png_bytes(), "a.png", FakeDescriber(answers={1: answer}))

        assert result.text == "> [Image] 영수증\n>\n> 합계 265,000"

    def test_strips_code_fence_wrapping(self):
        result = described(builder.png_bytes(), "a.png", FakeDescriber(answers={1: "```markdown\n표 설명\n```"}))

        assert result.text == "> [Image] 표 설명"

    def test_describes_every_image_by_default(self):
        """The default is unlimited. Real Hangul (HWP) documents often had more than 30 pictures (51, 32), and the content of
        pictures cut off by the limit was lost entirely. Only callers that want to bound the cost pass max_images."""
        from docx import Document
        from docx.shared import Inches

        document = Document()
        for seed in range(1, 36):
            document.add_paragraph(f"문단 {seed}").add_run().add_picture(BytesIO(builder.png_bytes(seed=seed)),
                                                                        width=Inches(1))
        buffer = BytesIO()
        document.save(buffer)

        result = convert(buffer.getvalue(), "a.docx")

        assert len(result.images) == 35
        assert not any("selected for description" in warning for warning in result.warnings)

    def test_caps_number_of_images_with_warning(self):
        result = convert(builder.docx_with_images(), "a.docx", ConvertOptions(max_images=1))

        assert len(result.images) == 1
        assert any("only 1 were selected for description" in warning for warning in result.warnings)

    def test_char_count_includes_image_descriptions_once_described(self):
        """The character count includes the text inside pictures (descriptions). It is counted again once descriptions are filled in."""
        original = convert(builder.png_bytes(), "a.png")

        updated = asyncio.run(describe_images(original, FakeDescriber()))

        assert original.char_count == len("[Image]")
        assert updated.char_count == len(updated.text) > original.char_count

    def test_description_stops_once_the_character_limit_is_exceeded(self):
        """If the limit is exceeded while filling in picture descriptions, the remaining pictures are not sent to the vision model."""
        describer = FakeDescriber(answers=dict.fromkeys(range(1, 13), "가" * 200))

        result = asyncio.run(aconvert(_docx_with_pictures(12), "a.docx", ConvertOptions(max_chars=700),
                                      describer=describer, max_concurrency=1))

        assert result.exceeded
        assert result.char_count > 700
        assert len(describer.seen) < 12

    def test_placeholders_of_images_that_turn_out_decorative_do_not_count_against_the_limit(self):
        """When a picture is judged decorative, the `[Image]` in its place is removed and the character count goes down. Deciding
        "exceeded" from the length before description (placeholders included) or from a midway length blocks a document that is
        within the limit once everything is done (in the real-file check a document with N = L came out as exceeded)."""
        data = _docx_with_pictures(6)
        everything_decorative = dict.fromkeys(range(1, 7), "SKIP")
        final = asyncio.run(aconvert(data, "a.docx", ConvertOptions(max_chars=None),
                                     describer=FakeDescriber(answers=everything_decorative))).char_count
        before = convert(data, "a.docx", ConvertOptions(max_chars=None)).char_count
        describer = FakeDescriber(answers=everything_decorative)

        result = asyncio.run(aconvert(data, "a.docx", ConvertOptions(max_chars=final), describer=describer))

        assert final < before, "this test is meaningful only for a document that shrinks when placeholders are removed"
        assert not result.exceeded
        assert result.char_count == final
        assert len(describer.seen) == 6

    def test_retry_does_not_count_already_described_images_twice(self):
        """When only the failed pictures are described again, adding the characters of already-described pictures once more makes a
        document within the limit exceed it."""
        answers = dict.fromkeys(range(1, 7), "가" * 100)
        first = asyncio.run(aconvert(_docx_with_pictures(6), "a.docx", ConvertOptions(max_chars=1_000),
                                     describer=FakeDescriber(answers=answers, fail_ids={1})))

        second = asyncio.run(describe_images(first, FakeDescriber(answers=answers)))

        assert not first.exceeded
        assert not second.exceeded
        assert second.char_count == len(second.text) < 1_000

    def test_images_are_not_described_when_the_text_alone_exceeds_the_limit(self):
        describer = FakeDescriber()

        result = described(builder.docx_with_images(), "a.docx", describer, max_chars=5)

        assert result.exceeded
        assert describer.seen == []

    def test_picture_in_vertically_merged_cell_is_described_once(self):
        """Repeating the value of a vertically merged cell copied the picture placeholder into every row too, so the same description
        went in as many times as there were rows."""
        import base64

        encoded = base64.b64encode(builder.png_bytes(200, 120, seed=1)).decode()
        rows = "".join(f"<tr><td>행 {row}</td></tr>" for row in range(1, 4))
        html = f'<table><tr><td rowspan="4"><img src="data:image/png;base64,{encoded}"> 분류</td><td>행 0</td></tr>{rows}</table>'

        result = described(html.encode(), "a.html")

        assert result.text.count("설명 1") == 1
        assert result.text.count("분류") == 4

    @pytest.mark.parametrize("answer", [[{"type": "text", "text": "막대 "}, {"type": "image_url"}, "차트"]],
                             ids=["content_blocks"])
    def test_describer_answer_given_as_content_blocks_is_joined(self, answer):
        """The `content` of a LangChain chat model can be a list of text parts. It was taken as is and the whole conversion died with
        an AttributeError."""
        describer = FakeDescriber(answers={1: answer})

        result = described(builder.png_bytes(), "a.png", describer)

        assert result.text == "> [Image] 막대 차트"

    @pytest.mark.parametrize("answer", [42, {"text": "x"}, [object()]], ids=["number", "dict", "unknown_block"])
    def test_describer_answer_that_is_not_text_fails_only_that_image(self, answer):
        describer = FakeDescriber(answers={1: answer})

        result = described(builder.docx_with_images(), "a.docx", describer)

        assert [image.failed for image in result.images] == [True, False]
        assert "[Image] 설명 3" in result.text
        assert any("could not be described (the vision model call failed)" in warning for warning in result.warnings)

    def test_does_not_mutate_original_result(self):
        original = convert(builder.png_bytes(), "a.png")

        updated = asyncio.run(describe_images(original, FakeDescriber()))

        assert original.text == "[Image]"
        assert updated.text.startswith("> [Image] 설명 1")


class TestOpenAIImageDescriber:
    def test_sends_prompt_context_and_image_as_data_url(self):
        class FakeChatModel:
            def __init__(self):
                self.messages = None

            async def ainvoke(self, messages):
                from langchain_core.messages import AIMessage

                self.messages = messages
                return AIMessage(content="막대 차트")

        model = FakeChatModel()
        describer = OpenAIImageDescriber(model, detail="low")
        image = convert(builder.docx_with_images(), "a.docx").images[0]

        answer = asyncio.run(describer.describe(image))

        text_part, image_part = model.messages[0].content
        assert answer == "막대 차트"
        assert "차트 앞 문단입니다." in text_part["text"] and "분기별 매출 차트" in text_part["text"]
        assert image_part["image_url"]["url"].startswith("data:image/jpeg;base64,")
        assert image_part["image_url"]["detail"] == "low"


def _docx_with_pictures(count: int) -> bytes:
    from docx import Document
    from docx.shared import Inches

    document = Document()
    for seed in range(1, count + 1):
        document.add_paragraph(f"문단 {seed}").add_run().add_picture(BytesIO(builder.png_bytes(seed=seed)), width=Inches(1))
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _patch_ole(monkeypatch, streams):
    class FakeOle:
        def __init__(self, _source):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def listdir(self, streams=True, storages=False):
            return [name.split("/") for name in streams_map] + ([["BodyText"]] if storages else [])

        def exists(self, name):
            return name in streams_map

        def openstream(self, name):
            return BytesIO(streams_map[name])

    streams_map = streams
    monkeypatch.setattr(olefile, "OleFileIO", FakeOle)


def test_openai_describer_uses_separate_http_client_per_event_loop(monkeypatch):
    """The default async HTTP client of langchain_openai is a process-global cache, so it is bound to the event loop that first used
    it. In workers and scripts that call asyncio.run several times, the second call failed with 'Event loop is closed'."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    describer = OpenAIImageDescriber()

    async def current_model():
        return describer._chat_model()

    first, second = asyncio.run(current_model()), asyncio.run(current_model())

    assert first is not second
    assert first.http_async_client is not None
    assert first.http_async_client is not second.http_async_client


def test_openai_describer_closes_the_http_client_of_the_current_event_loop(monkeypatch):
    """There was no way to close the HTTP client created per event loop, so connections stayed open after the loop ended."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    describer = OpenAIImageDescriber()

    async def use_and_close():
        client = describer._chat_model().http_async_client
        await describer.aclose()
        return client, describer._chat_model().http_async_client

    closed, fresh = asyncio.run(use_and_close())

    assert closed.is_closed
    assert fresh is not closed and not fresh.is_closed


def test_closing_a_describer_built_around_a_given_model_leaves_that_model_alone():
    """The client of a model passed in (a LangChain chat model built by the caller) belongs to the caller, so it is not closed."""
    model = ScriptedChatModel("막대 차트")
    describer = OpenAIImageDescriber(model)

    asyncio.run(describer.aclose())

    assert describer._chat_model() is model


def _html_with_one_figure() -> bytes:
    """HTML with a single picture (a body figure). Used to run end to end (`describe_images`) with a stand-in model whose
    answers, including the text-check answer, are scripted in order."""
    import base64

    uri = "data:image/png;base64," + base64.b64encode(builder.png_bytes()).decode()
    return f'<html><body><p>분기 매출 차트</p><img src="{uri}"><p>끝</p></body></html>'.encode()


class ScriptedChatModel:
    """Stand-in chat model that returns scripted answers in order."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.prompts = []

    async def ainvoke(self, messages):
        from langchain_core.messages import AIMessage

        self.prompts.append(messages[0].content[0]["text"])
        return AIMessage(content=self.answers.pop(0))


class TestNoContentSecondOpinion:
    """The real gpt-4o-mini answered '(판독 불가)' (illegible) about a third of the time even for pictures of sharp text. Trusting
    that answer alone and removing the picture as decoration loses the content, so a "no information" answer is followed by
    one more text-only check query."""

    def _figure(self):
        return convert(builder.docx_with_images(), "a.docx").images[0]

    @pytest.mark.parametrize("first", ["(판독 불가)", "SKIP", "", "빈 이미지", '"빈 이미지."', "내용 없음", "Blank image",
                                       "비어 있는 이미지.\n\nSKIP", "이 이미지는 비어 있는 도식 또는 그래픽입니다."])
    def test_second_pass_recovers_text_after_false_no_content(self, first):
        """The real model also gave short answers claiming emptiness, such as '빈 이미지' (empty image), for pictures of text 1–2
        times out of 12."""
        model = ScriptedChatModel(first, "FIG 4411")

        answer = asyncio.run(OpenAIImageDescriber(model).describe(self._figure()))

        assert answer == "FIG 4411"
        assert len(model.prompts) == 2 and "NONE" in model.prompts[1]

    def test_both_passes_empty_means_decorative(self):
        model = ScriptedChatModel("SKIP", "NONE")

        answer = asyncio.run(OpenAIImageDescriber(model).describe(self._figure()))

        assert answer == "SKIP"

    @pytest.mark.parametrize("answers", [("", ""), ("   ", "\n"), ("SKIP", ""), ("(판독 불가)", ""), ("", "NONE"),
                                         ("", "```\n```")])
    def test_empty_answer_in_either_pass_is_a_failure_not_a_decoration(self, answers):
        """A blank answer is not an answer (a truncated response, empty content). When the first answer was blank and the text
        check was blank too, `is_no_content("")` made it SKIP and the picture was removed without a warning. Decoration is only
        for when both passes **answered** "no text"; if either is blank it is left as a failure so that calling again asks again
        (the same as the blank-answer rule of `describe_images`). The same goes when only the text check is blank — the
        "illegible" of the first answer was not confirmed."""
        model = ScriptedChatModel(*answers)
        result = convert(_html_with_one_figure(), "a.html")

        described_result = asyncio.run(describe_images(result, OpenAIImageDescriber(model)))

        image = described_result.images[0]
        assert image.failed and not image.decorative and image.description is None
        assert "[Image]" in described_result.text
        assert any("could not be described" in warning for warning in described_result.warnings)

    def test_empty_first_answer_with_text_found_by_the_second_pass_uses_that_text(self):
        model = ScriptedChatModel("", "FIG 4411")
        result = convert(_html_with_one_figure(), "a.html")

        described_result = asyncio.run(describe_images(result, OpenAIImageDescriber(model)))

        assert described_result.images[0].description == "FIG 4411"

    @pytest.mark.parametrize("answer", ["막대 차트", "빈 칸이 비어 있는 신청서 양식. " + "| 항목 | 값 |\n" * 8])
    def test_normal_answer_needs_no_second_pass(self, answer):
        """Even if a long description contains '비어 있' ("is empty") — a description of a blank form, say — no text check is made."""
        model = ScriptedChatModel(answer)

        asyncio.run(OpenAIImageDescriber(model).describe(self._figure()))

        assert len(model.prompts) == 1

    def test_substantive_first_answer_is_not_replaced_by_the_text_check(self):
        """A short chart description with just one value "(판독 불가)" (illegible) was replaced wholesale by the text check's list
        of text ("매출 120 135") and the chart summary was lost. If everything the text check found is already in the first
        answer, the first answer did read the picture."""
        first = "분기별 매출 막대 차트: 1분기 120, 2분기 135, 3분기 (판독 불가)"
        model = ScriptedChatModel(first, "매출\n120\n135")

        answer = asyncio.run(OpenAIImageDescriber(model).describe(self._figure()))

        assert answer == first

    def test_text_check_adds_what_a_substantive_first_answer_missed(self):
        """When the first answer read the picture (it shares text with the text check) but missed some text, both are kept.
        Dropping the first answer loses the summary; dropping the text check loses the text."""
        first = "신청서 양식: 성명 칸 비어 있음"
        model = ScriptedChatModel(first, "신청서\n성명\n접수번호 A-17")

        answer = asyncio.run(OpenAIImageDescriber(model).describe(self._figure()))

        assert answer == f"{first}\n\n신청서\n성명\n접수번호 A-17"


class TestResourceBounds:
    """Paths where memory and time balloon while hardly any text is produced. Resources run out before the character limit
    (`max_chars`) kicks in."""

    def test_hwpx_picture_referenced_many_times_is_read_once(self, monkeypatch):
        """The same picture was inflated from the ZIP again for every reference, so a 43KB document referring 80 times to one 40MB
        picture used 3.4GB."""
        import zipfile

        data = _hwpx_with_repeated_picture(references=20)
        reads = []
        original_read = zipfile.ZipFile.read

        def counting_read(self, name, *args, **kwargs):
            reads.append(name if isinstance(name, str) else name.filename)
            return original_read(self, name, *args, **kwargs)

        monkeypatch.setattr(zipfile.ZipFile, "read", counting_read)

        result = convert(data, "a.hwpx")

        assert result.text.count("앞 글") == 20
        assert reads.count("BinData/image1.bmp") == 1

    def test_hwpx_picture_that_cannot_be_inflated_is_skipped(self):
        """A broken compression stream in one picture does not reject the whole document (the same rule as HWP)."""
        data = bytearray(_hwpx_with_repeated_picture(references=1))
        start = data.find(b"BinData/image1.bmp") + len("BinData/image1.bmp")  # first occurrence is the local header; the compressed content follows
        data[start + 10:start + 30] = b"\xff" * 20

        result = convert(bytes(data), "a.hwpx")

        assert result.text == "앞 글"
        assert not result.images

    @pytest.mark.parametrize("streamed", [False, True], ids=["full-load", "streamed"])
    def test_xlsx_picture_referenced_many_times_is_read_once(self, monkeypatch, streamed):
        """A 56KB xlsx whose drawing XML referred to one picture 40 times used 1.2GB. Both openpyxl (full load) and direct reading
        (streaming) inflated the picture file again for each reference. In full load, if the size summed over all references
        exceeds the decompression limit, the workbook is read by streaming."""
        import zipfile

        from opendockit.extractors import spreadsheet

        if streamed:
            monkeypatch.setattr(spreadsheet, "_FULL_LOAD_MAX_SHEET_BYTES", 0)
        picture = builder.png_bytes(600, 600, image_format="BMP")
        data = _xlsx_with_repeated_picture(picture, references=12)
        reads = []
        original_read = zipfile.ZipFile.read

        def counting_read(self, name, *args, **kwargs):
            reads.append(name if isinstance(name, str) else name.filename)
            return original_read(self, name, *args, **kwargs)

        monkeypatch.setattr(zipfile.ZipFile, "read", counting_read)

        result = convert(data, "a.xlsx", ConvertOptions(max_uncompressed_bytes=len(picture) * 4))

        assert "| a | 1 |" in result.text
        assert reads.count("xl/media/image1.png") == 1

    @pytest.mark.parametrize("variant", ["single-quotes", "spaces", "char-reference", "drawing-elsewhere"])
    def test_xlsx_picture_reference_count_cannot_be_dodged(self, monkeypatch, variant):
        """References were counted only with a double-quote regex and the xl/drawings/ path, which could be dodged with single
        quotes, spaces, character references or a drawing file at another path (a 33KB file used 2.4GB). Like openpyxl, the
        sheet relationships are followed and attribute values are read by XML rules."""
        import zipfile

        picture = builder.png_bytes(600, 600, image_format="BMP")
        data = _xlsx_with_repeated_picture(picture, references=12)
        if variant == "drawing-elsewhere":
            data = _rename_part(data, "xl/drawings/drawing1.xml", "xl/pics/drawing1.xml")
        else:
            replacement = {"single-quotes": "r:embed='rId1'", "spaces": 'r:embed = "rId1"',
                           "char-reference": 'r:embed="rI&#100;1"'}[variant]
            data = builder.zip_with(data, edit={"xl/drawings/drawing1.xml": lambda xml: xml.replace('r:embed="rId1"', replacement)})
        reads = []
        original_read = zipfile.ZipFile.read

        def counting_read(self, name, *args, **kwargs):
            reads.append(name if isinstance(name, str) else name.filename)
            return original_read(self, name, *args, **kwargs)

        monkeypatch.setattr(zipfile.ZipFile, "read", counting_read)

        convert(data, "a.xlsx", ConvertOptions(max_uncompressed_bytes=len(picture) * 4))

        assert reads.count("xl/media/image1.png") <= 1

    def test_pdf_figure_on_a_huge_page_renders_only_its_own_area(self, monkeypatch):
        """To crop one picture the whole page was rendered at up to 4x, which used 9.4GB on an 8000pt page; on larger pages the
        picture was dropped without a warning."""
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas


        pixels = _record_rendered_pixels(monkeypatch)
        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=(2000, 2000))
        pdf.drawString(72, 1900, "text")
        pdf.drawImage(ImageReader(BytesIO(builder.png_bytes(800, 400, seed=13))), 300, 1500, width=200, height=100)
        pdf.save()

        result = convert(buffer.getvalue(), "a.pdf")

        assert len(result.images) == 1
        assert result.images[0].width >= 798, "the picture area is still rendered at the original pixel size"
        assert max(pixels) <= _pdf_figures._MAX_RENDER_PIXELS

    def test_pdf_scanned_huge_page_is_rendered_within_the_pixel_cap(self, monkeypatch):
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfgen import canvas


        pixels = _record_rendered_pixels(monkeypatch)
        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=(4000, 4000))
        pdf.drawImage(ImageReader(BytesIO(builder.png_bytes(800, 800, seed=14))), 0, 0, width=4000, height=4000)
        pdf.save()

        result = convert(buffer.getvalue(), "a.pdf")

        assert [image.kind for image in result.images] == [ImageKind.PAGE]
        assert max(pixels) <= _pdf_figures._MAX_RENDER_PIXELS

    def test_describing_many_images_does_not_reassemble_the_document_for_each(self, monkeypatch):
        """The whole document was reassembled to check the limit each time one picture finished, so it slowed down with the square
        of the number of pictures (25 seconds for 3,332 pictures). While there is certainly headroom to the limit, no assembly
        is done."""
        from opendockit import describe as describe_module

        data = _docx_with_pictures(60)
        calls = []
        original_render = describe_module.render

        def counting_render(result):
            calls.append(1)
            return original_render(result)

        monkeypatch.setattr(describe_module, "render", counting_render)

        result = asyncio.run(aconvert(data, "a.docx", ConvertOptions(max_chars=500_000), describer=FakeDescriber()))

        assert not result.exceeded
        assert result.text.count("> [Image] 설명") == 60
        assert len(calls) <= 3

    @pytest.mark.parametrize("limit", [400, 700, 1_000, 1_300])
    def test_description_stops_at_exactly_the_image_that_crosses_the_limit(self, limit):
        """Even when assembly is skipped the stopping point must be the same: it stops at the k-th picture, the first k for which
        describing the first k pictures and removing the rest goes over the limit (this test also passes on the code before the
        fix — it pins behavior that must not change)."""
        from dataclasses import replace

        from opendockit.converter import render

        data = _docx_with_pictures(12)
        answers = {image_id: "가" * (20 + 15 * image_id) for image_id in range(1, 13)}
        base = convert(data, "a.docx", ConvertOptions(max_chars=limit))
        expected = next(count for count in range(1, 13) if render(replace(base, images=[
            replace(image, description=answers[image.id]) if index < count else replace(image, decorative=True)
            for index, image in enumerate(base.images)])).exceeded)
        describer = FakeDescriber(answers=answers)

        result = asyncio.run(describe_images(base, describer, max_concurrency=1))

        assert result.exceeded
        assert sum(1 for image in result.images if image.description) == expected
        assert len(describer.seen) <= expected + 1, "the one next call started before the decision is cancelled"

    @pytest.mark.parametrize("limit", [700, 1_300, 2_000])
    def test_description_stops_at_the_same_image_when_a_placeholder_appears_more_than_once(self, limit):
        """When the placeholder of one picture appears several times, its description goes in that many times. Counting the bound
        on one picture's increase only once kept calling past the limit while assembly was skipped (the stopping point must be
        the same as when assembling every time)."""
        from dataclasses import replace

        from opendockit.converter import render

        converted = convert(_docx_with_pictures(12), "a.docx", ConvertOptions(max_chars=limit))
        pattern = re.compile(r"(⟦IMG:[0-9a-f]+:\d+⟧)")
        base = render(replace(converted, sections=[replace(section, template=pattern.sub(r"\1 \1 \1", section.template))
                                                   for section in converted.sections]))
        answers = {image_id: "가" * (20 + 15 * image_id) for image_id in range(1, 13)}
        expected = next(count for count in range(1, 13) if render(replace(base, images=[
            replace(image, description=answers[image.id]) if index < count else replace(image, decorative=True)
            for index, image in enumerate(base.images)])).exceeded)
        describer = FakeDescriber(answers=answers)

        result = asyncio.run(describe_images(base, describer, max_concurrency=1))

        assert result.exceeded
        assert sum(1 for image in result.images if image.description) == expected


def _record_rendered_pixels(monkeypatch) -> list[int]:
    import pypdfium2 as pdfium

    pixels = []
    original_render = pdfium.PdfPage.render

    def recording_render(self, *args, **kwargs):
        bitmap = original_render(self, *args, **kwargs)
        pixels.append(bitmap.width * bitmap.height)
        return bitmap

    monkeypatch.setattr(pdfium.PdfPage, "render", recording_render)
    return pixels


def _hwpx_with_repeated_picture(references: int) -> bytes:
    import zipfile

    picture = ('<hp:p><hp:run><hp:t>앞 글</hp:t><hp:pic><hc:img xmlns:hc="http://www.hancom.co.kr/hwpml/2011/core" '
               'binaryItemIDRef="image1"/></hp:pic></hp:run></hp:p>')
    source = zipfile.ZipFile(BytesIO(builder.hwpx_bytes(picture * references)))
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in source.namelist():
            content = source.read(name)
            if name == "Contents/content.hpf":
                content = content.replace(b"</opf:manifest>",
                                          b'<opf:item id="image1" href="BinData/image1.bmp"/></opf:manifest>')
            archive.writestr(name, content)
        archive.writestr("BinData/image1.bmp", builder.png_bytes(400, 300, image_format="BMP"))
    return buffer.getvalue()


def _xlsx_with_repeated_picture(picture: bytes, references: int) -> bytes:
    """xlsx whose drawing XML refers `references` times to one picture file (the `picture` bytes under the unchanged name
    xl/media/image1.png)."""
    import re
    import zipfile

    import openpyxl
    from openpyxl.drawing.image import Image as SheetImage

    workbook = openpyxl.Workbook()
    workbook.active.append(["a", 1])
    workbook.active.add_image(SheetImage(BytesIO(builder.png_bytes(100, 100))), "D2")
    source = BytesIO()
    workbook.save(source)
    buffer = BytesIO()
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in original.namelist():
            content = original.read(name)
            if name == "xl/media/image1.png":
                content = picture
            if name == "xl/drawings/drawing1.xml":
                anchor = re.search(rb"<oneCellAnchor>.*?</oneCellAnchor>", content, re.S).group(0)
                content = content.replace(anchor, anchor * references)
            archive.writestr(name, content)
    return buffer.getvalue()


def _rename_part(data: bytes, old: str, new: str) -> bytes:
    """Rename a part inside the ZIP and fix the relationship targets that point to it."""
    import zipfile

    old_target, new_target = old.split("/", 1)[1], new.split("/", 1)[1]  # path relative to xl/ → written in the form ../drawings/…
    buffer = BytesIO()
    with zipfile.ZipFile(BytesIO(data)) as source, zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            content = source.read(info.filename)
            name = info.filename
            if name == old:
                name = new
            elif name == old.replace("drawings/", "drawings/_rels/") + ".rels":
                name = new.replace("pics/", "pics/_rels/") + ".rels"
            if name.endswith(".rels") or name == "[Content_Types].xml":
                content = content.replace(old_target.encode(), new_target.encode()).replace(
                    old.encode(), new.encode())
            target.writestr(name, content)
    return buffer.getvalue()


class TestAuditedImageGaps:
    @pytest.mark.parametrize(("mode", "scale"), [("F", 1 / 255), ("I", 257), ("F", 1000.0)],
                             ids=["float-0-1", "int-0-65535", "float-0-255000"])
    def test_wide_range_grayscale_keeps_its_gradient(self, mode, scale):
        """32-bit integer (I) and float (F) TIFFs do not have a 0–255 value range, so on conversion to 8 bits a float 0–1 image
        became all black and an integer 0–65535 image almost white. The picture's actual minimum–maximum is stretched to 0–255
        for the conversion."""
        gradient = Image.linear_gradient("L").resize((512, 128)).convert("F").point(lambda value: value * scale)
        source = BytesIO()
        gradient.convert(mode).save(source, format="TIFF")

        image = prepare_image(source.getvalue(), min_side=64, max_side=2048)

        decoded = Image.open(BytesIO(image.data)).convert("L")
        low, high = decoded.getextrema()
        histogram = decoded.histogram()
        assert low < 20 and high > 235
        assert max(histogram) < decoded.width * decoded.height * 0.1, "the gradient remains instead of collapsing onto one value"

    def test_extract_images_off_does_not_send_an_uploaded_image_to_the_describer(self):
        """`extract_images=False` ignores images completely (docs/options.md). An image uploaded on its own was still sent to the
        describer."""
        describer = FakeDescriber()

        result = described(builder.png_bytes(), "a.png", describer, extract_images=False)

        assert result.images == []
        assert describer.seen == []
        assert result.text == ""
        assert any("extract_images" in warning for warning in result.warnings)

    def test_describer_that_raises_cancelled_error_itself_fails_only_that_image(self):
        """When the describer (or a library inside it) raised CancelledError by itself, the whole aconvert died with CancelledError
        although the outer task was not cancelled, losing the descriptions of the other pictures too. Unless the outside is
        being cancelled, only that picture is left as a failure."""
        class SelfCancelling(FakeDescriber):
            async def describe(self, image):
                await asyncio.sleep(0)
                if image.id == 2:
                    raise asyncio.CancelledError
                return f"설명 {image.id}"

        result = asyncio.run(aconvert(_docx_with_pictures(3), "a.docx", describer=SelfCancelling()))

        assert [(image.id, image.failed) for image in result.images] == [(1, False), (2, True), (3, False)]
        assert "[Image] 설명 3" in result.text
        assert any("could not be described" in warning for warning in result.warnings)

    @pytest.mark.timing
    def test_cancelling_aconvert_still_cancels_it(self):
        """The handling above must not swallow a real cancellation (cancel on the outer task) as well."""
        class Slow(FakeDescriber):
            async def describe(self, image):
                await asyncio.sleep(10)

        async def scenario():
            task = asyncio.create_task(aconvert(_docx_with_pictures(3), "a.docx", describer=Slow()))
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(asyncio.wait_for(scenario(), 5))


def _tiff_pages(*colors: str, size: tuple[int, int] = (200, 260)) -> bytes:
    """Multi-page TIFF with a different color on each page (a fax or scan bundle)."""
    frames = [Image.new("RGB", size, color) for color in colors]
    buffer = BytesIO()
    frames[0].save(buffer, format="TIFF", save_all=True, append_images=frames[1:], compression="tiff_deflate")
    return buffer.getvalue()


class TestMultiPageTiff:
    """Only the first page of a multi-page TIFF was read, with no warning. Each page is registered as one picture so that the
    describer (OCR) sees every page."""

    def test_every_page_becomes_an_image_in_its_own_page_section(self):
        result = convert(_tiff_pages("red", "blue", "green"), "fax.tif")

        assert [image.kind for image in result.images] == [ImageKind.STANDALONE] * 3
        assert [section.index for section in result.sections] == [1, 2, 3]
        assert result.text == "<!-- Page 1 -->\n\n[Image]\n\n<!-- Page 2 -->\n\n[Image]\n\n<!-- Page 3 -->\n\n[Image]"
        assert [image.section_index for image in result.images] == [1, 2, 3]
        assert {Image.open(BytesIO(image.data)).getpixel((5, 5)) for image in result.images} == {
            Image.open(BytesIO(convert(_tiff_pages(color), "a.tif").images[0].data)).getpixel((5, 5))
            for color in ("red", "blue", "green")}

    def test_describer_sees_every_page(self):
        describer = FakeDescriber()

        result = described(_tiff_pages("red", "blue"), "scan.tiff", describer)

        assert len(describer.seen) == 2
        assert "설명 1" in result.text and "설명 2" in result.text

    def test_max_images_still_limits_the_pages_sent_to_the_describer(self):
        describer = FakeDescriber()

        result = described(_tiff_pages("red", "blue", "green"), "scan.tif", describer, max_images=1)

        assert len(describer.seen) == 1
        assert any("only 1 were selected" in warning for warning in result.warnings)

    def test_pages_beyond_the_page_cap_are_dropped_with_a_warning(self, monkeypatch):
        from opendockit import image as image_module

        monkeypatch.setattr(image_module, "MAX_TIFF_PAGES", 2)

        result = convert(_tiff_pages("red", "blue", "green"), "scan.tif")

        assert len(result.images) == 2
        assert any("first 2 page(s)" in warning for warning in result.warnings)

    def test_page_that_is_too_large_to_decode_stops_reading_with_a_warning(self, monkeypatch):
        from opendockit import image as image_module

        frames = [Image.new("RGB", (200, 260), "red"), Image.new("RGB", (400, 520), "blue")]
        buffer = BytesIO()
        frames[0].save(buffer, format="TIFF", save_all=True, append_images=frames[1:])
        monkeypatch.setattr(image_module, "_MAX_DECODE_PIXELS", 200 * 260)

        result = convert(buffer.getvalue(), "scan.tif")

        assert len(result.images) == 1
        assert any("Page 2 of the TIFF" in warning for warning in result.warnings)

    def test_single_page_tiff_and_animated_gif_stay_one_image(self):
        """The frames of an animated GIF are not pages — only the first frame is read (deliberate)."""
        frames = [Image.new("RGB", (120, 120), color) for color in ("red", "blue")]
        gif = BytesIO()
        frames[0].save(gif, format="GIF", save_all=True, append_images=frames[1:])

        for data, name in ((_tiff_pages("red"), "a.tif"), (gif.getvalue(), "a.gif")):
            result = convert(data, name)

            assert result.text == "[Image]"
            assert len(result.images) == 1
            assert not result.warnings


class TestSamePictureTwice:
    @staticmethod
    def _deck(slides=2) -> bytes:
        from pptx import Presentation
        from pptx.util import Inches

        presentation = Presentation()
        picture = builder.png_bytes(seed=7)
        for index in range(slides):
            slide = presentation.slides.add_slide(presentation.slide_layouts[5])
            slide.shapes.title.text = f"조직도 {index + 1}"
            slide.shapes.add_picture(BytesIO(picture), Inches(1), Inches(2))
        buffer = BytesIO()
        presentation.save(buffer)
        return buffer.getvalue()

    def test_picture_used_twice_is_described_once_and_shown_at_both_places(self):
        """For the same picture appearing twice, below the repetition threshold (3 times), the second place vanished entirely from
        the text and sections. The description is obtained only once and put in both places."""
        describer = FakeDescriber(answers={1: "대표 아래 3개 본부가 있는 조직도"})

        result = described(self._deck(), "a.pptx", describer)

        assert len(describer.seen) == 1
        assert [section.text for section in result.sections] == ["> [Image] 대표 아래 3개 본부가 있는 조직도"] * 2
        assert result.text.count("대표 아래 3개 본부") == 2
        assert result.char_count == len(result.text)

    def test_picture_used_twice_keeps_both_placeholders_without_a_describer(self):
        result = convert(self._deck(), "a.pptx")

        assert len(result.images) == 1
        assert [section.text for section in result.sections] == ["[Image]", "[Image]"]

    def test_picture_repeated_up_to_the_threshold_is_still_removed_as_a_logo(self):
        result = convert(self._deck(slides=3), "a.pptx")

        assert result.images == []
        assert "[Image" not in result.text

    @pytest.mark.parametrize("limit_offset", [-1, 0, 1])
    def test_limit_is_exact_when_a_described_picture_appears_twice(self, limit_offset):
        full = described(self._deck(), "a.pptx", FakeDescriber(answers={1: "가" * 300}), max_chars=None)
        limit = full.char_count + limit_offset

        result = described(self._deck(), "a.pptx", FakeDescriber(answers={1: "가" * 300}), max_chars=limit)

        assert result.exceeded is (full.char_count > limit)


class TestDecorativePictureWithAltText:
    def test_alt_text_is_kept_when_the_picture_turns_out_decorative(self):
        """When the describer said decoration (SKIP), even the alt text the author had attached was removed. Alt text is information
        written by a person."""
        result = described(builder.docx_with_images(), "a.docx", FakeDescriber(answers={1: "SKIP"}))

        assert result.text.startswith("차트 앞 문단입니다.\n\n[Image: 분기별 매출 차트]\n\n아이콘")

    def test_decorative_picture_without_alt_text_is_removed(self):
        result = described(builder.docx_with_images(), "a.docx", FakeDescriber(answers={3: "SKIP"}))

        assert "| 사진 |  |" in result.text

    @pytest.mark.parametrize("answer", ["SKIP", "짧", "가" * 40])
    @pytest.mark.parametrize("limit_offset", [-1, 0, 1])
    def test_limit_is_exact_when_a_picture_with_alt_text_gets_any_answer(self, answer, limit_offset):
        """A picture with alt text keeps '[Image: …]' even when judged decorative, and a short description can be shorter than
        that. Either way, the floor used for the limit decision during description must not be larger than the real value."""
        answers = {1: answer, 3: "SKIP"}
        full = described(builder.docx_with_images(), "a.docx", FakeDescriber(answers=answers), max_chars=None)
        limit = full.char_count + limit_offset

        result = described(builder.docx_with_images(), "a.docx", FakeDescriber(answers=answers), max_chars=limit)

        assert result.exceeded is (full.char_count > limit)


def _docx_with_alt_text(alt: str) -> bytes:
    """Copy of `builder.docx_with_images` with the alt text of the first picture replaced. Line breaks are written as `&#xA;`,
    as Word does."""
    escaped = alt.replace("\n", "&#xA;")
    return builder.zip_with(builder.docx_with_images(),
                            {"word/document.xml": lambda xml: xml.replace('descr="분기별 매출 차트"', f'descr="{escaped}"')})


class TestOfficeAutoGeneratedAltText:
    """The alt text Office attaches automatically when a picture is inserted ("…이(가) 표시된 사진 / 자동 생성된 설명", the Korean
    form of "A picture containing … / Description automatically generated") is not information written by the author. It was
    handled as author alt text: put into the describer prompt, and left in the text as '[Image: …]' even after a decorative
    verdict."""

    @pytest.mark.parametrize("alt", [
        "로고, 폰트, 그래픽, 상징이(가) 표시된 사진\n\n자동 생성된 설명",
        "텍스트, 스크린샷, 디자인이(가) 표시된 사진\n\nAI 생성 콘텐츠는 정확하지 않을 수 있습니다.",
        "A picture containing text, logo\n\nDescription automatically generated",
        "A close-up of a sign\n\nAI-generated content may be incorrect.",
        "A screenshot of a computer\n\nDescription generated with very high confidence",
        "텍스트, 도표, 폰트, 스크린샷이(가) 표시된 사진",  # saved without the trailer (a real proposal DOCX)
        "지도이(가) 표시된 그림",
        "A picture containing text, logo",
        "A picture containing person, indoor",
        "A picture containing text, screenshot, font",
        "텍스트, 스크린샷, 소프트웨어, 컴퓨터 아이콘이(가) 표시된 사진",
        "그래픽, 폰트, 디자인이(가) 표시된 사진\n\nAI가 생성한 콘텐츠는 부정확할 수 있습니다.",  # a different trailer from a real DOCX
    ])
    def test_auto_generated_alt_text_is_dropped(self, alt):
        describer = FakeDescriber(answers={1: "SKIP"})

        result = described(_docx_with_alt_text(alt), "a.docx", describer)

        assert describer.seen[0].alt is None
        assert result.text.startswith("차트 앞 문단입니다.\n\n아이콘")

    @pytest.mark.parametrize("alt", ["홍길동 팀장이 표시된 사진", "A picture of the new office lobby",
                                     "도표이(가) 표시된 사진\n2팀 신설 후 조직도",
                                     "A picture containing our CEO John Smith at the launch",
                                     "A picture containing the 2025 sales team",
                                     "신제품 발표회 무대에서 연설하는 홍길동 대표의 모습이(가) 표시된 사진"])
    def test_author_alt_text_that_resembles_the_office_phrase_is_kept(self, alt):
        """An Office phrase without the trailer is dropped only when it is the single line '…이(가) 표시된 사진/그림' or
        'A picture containing …'. Text where the author added a line is kept. The part before it is dropped only when it has
        the Office shape (short words listed with commas; lowercase for English) — a sentence written by a person
        ('A picture containing our CEO …') is kept."""
        describer = FakeDescriber(answers={1: "SKIP"})

        result = described(_docx_with_alt_text(alt), "a.docx", describer)

        assert describer.seen[0].alt == " ".join(alt.split())
        assert f"[Image: {' '.join(alt.split())}]" in result.text

    def test_author_alt_text_that_mentions_a_description_is_kept(self):
        describer = FakeDescriber(answers={1: "SKIP"})

        result = described(_docx_with_alt_text("자동 생성된 설명 예시 화면"), "a.docx", describer)

        assert describer.seen[0].alt == "자동 생성된 설명 예시 화면"
        assert "[Image: 자동 생성된 설명 예시 화면]" in result.text


def _markdown_with_a_data_uri_picture(*, title: str = "", alt: str = "결과 그래프") -> bytes:
    import base64

    encoded = base64.b64encode(builder.png_bytes()).decode()
    return (f"# 실험 보고서\n\n결과는 아래 그림과 같다.\n\n![{alt}](data:image/png;base64,{encoded}{title})\n\n"
            "## 결론\n\n매출이 12% 늘었다.\n").encode()


class TestMarkdownDataUriPictures:
    """A data: URI picture embedded in Markdown (`![alt](data:image/png;base64,…)`) was not extracted as a picture and became
    body text, base64 and all (50,000 characters for one picture — the picture used up the character limit). Like a data: URI
    in HTML, it is registered as a picture and its alt text is passed along."""

    @pytest.mark.parametrize("title", ["", ' "그림 1"'])
    def test_data_uri_picture_is_extracted_with_its_alt_text(self, title):
        describer = FakeDescriber()

        result = described(_markdown_with_a_data_uri_picture(title=title), "report.md", describer)

        assert result.format is DocumentFormat.MARKDOWN
        assert result.text == ("# 실험 보고서\n\n결과는 아래 그림과 같다.\n\n> [Image] 설명 1\n> 세부 1\n\n## 결론\n\n매출이 12% 늘었다.")
        assert describer.seen[0].alt == "결과 그래프"
        assert "base64" not in result.text

    def test_without_picture_extraction_only_the_alt_text_is_left(self):
        result = convert(_markdown_with_a_data_uri_picture(), "report.md", ConvertOptions(extract_images=False))

        assert result.text == "# 실험 보고서\n\n결과는 아래 그림과 같다.\n\n[Image: 결과 그래프]\n\n## 결론\n\n매출이 12% 늘었다."
        assert not result.images

    def test_data_uri_inside_a_code_block_is_left_as_code(self):
        """Deliberate: what is inside a code block is example code — it is left as is."""
        data = b"```markdown\n![a](data:image/png;base64,iVBORw0KGgo=)\n```\n\nafter\n"

        result = convert(data, "a.md")

        assert result.text == "```markdown\n![a](data:image/png;base64,iVBORw0KGgo=)\n```\n\nafter"
        assert not result.images


class TestHeicWithoutTheOptionalExtra:
    """pillow-heif is an optional extra (`opendockit[heic]`) because its wheels bundle GPLv2/LGPLv3 libraries. Without it an
    uploaded HEIC was rejected as 'corrupted', which does not tell the user that one install command fixes it."""

    _HEIC = b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic" + b"\x00" * 64

    def test_uploaded_heic_names_the_extra_to_install(self, monkeypatch):
        from opendockit import UnsupportedFormatError

        monkeypatch.setattr("opendockit.image.HEIF_AVAILABLE", False)

        with pytest.raises(UnsupportedFormatError, match=r'pip install "opendockit\[heic\]"'):
            convert(self._HEIC, "photo.heic")

    def test_unreadable_heic_with_the_extra_installed_is_still_corrupted(self, monkeypatch):
        from opendockit import CorruptedDocumentError

        monkeypatch.setattr("opendockit.image.HEIF_AVAILABLE", True)

        with pytest.raises(CorruptedDocumentError):
            convert(self._HEIC, "photo.heic")

    def test_avif_does_not_need_the_extra(self, monkeypatch):
        from io import BytesIO

        from PIL import Image

        monkeypatch.setattr("opendockit.image.HEIF_AVAILABLE", False)
        buffer = BytesIO()
        Image.new("RGB", (300, 200), (200, 30, 30)).save(buffer, "AVIF")

        assert len(convert(buffer.getvalue(), "photo.avif").images) == 1



class TestOutOfMemoryWhileReadingAPicture:
    """A MemoryError while decoding or re-encoding a picture was taken for an unreadable picture: an uploaded photo was
    'corrupted' and a picture inside a document a format warning, so `IsolatedConverter` never reported 'too large or too
    complex'. Running out of address space under the worker's memory limit is simulated by making the re-encode fail."""

    @pytest.fixture
    def no_memory(self, monkeypatch):
        import opendockit.image as image_module

        def out_of_memory(*_args, **_kwargs):
            raise MemoryError

        monkeypatch.setattr(image_module, "_encoded", out_of_memory)

    def test_uploaded_picture_is_too_complex_not_corrupted(self, no_memory):
        from opendockit import TOO_COMPLEX_MESSAGE, DocumentTooLargeError

        with pytest.raises(DocumentTooLargeError, match=re.escape(TOO_COMPLEX_MESSAGE)):
            convert(builder.png_bytes(400, 300), "photo.png")

    def test_picture_inside_a_document_is_too_complex_not_a_format_warning(self, no_memory):
        from opendockit import TOO_COMPLEX_MESSAGE, DocumentTooLargeError
        from opendockit.isolated import _run

        with pytest.raises(DocumentTooLargeError, match=re.escape(TOO_COMPLEX_MESSAGE)):
            _run(builder.docx_with_images(), "a.docx", None)

    def test_memory_error_is_not_an_unreadable_picture(self, no_memory):
        with pytest.raises(MemoryError):
            prepare_image(builder.png_bytes(400, 300), min_side=1, max_side=2048)

    def test_memory_error_deeper_in_the_chain_is_too_complex(self, monkeypatch):
        """A parser that wraps the MemoryError in its own exception, wrapped again by the converter, was a corrupted document:
        only the direct cause was looked at."""
        from opendockit import TOO_COMPLEX_MESSAGE, DocumentTooLargeError
        import opendockit.isolated as isolated

        def convert_that_runs_out(*_args):
            try:
                try:
                    raise MemoryError
                except MemoryError as inner:
                    raise ValueError("parser") from inner
            except ValueError as outer:
                raise CorruptedDocumentError("Could not parse the file format. The file may be corrupted.") from outer

        monkeypatch.setattr(isolated, "convert", convert_that_runs_out)

        with pytest.raises(DocumentTooLargeError, match=re.escape(TOO_COMPLEX_MESSAGE)):
            isolated._run(b"x", "a.txt", None)


def _tiff_with_a_tag_of_the_wrong_count() -> bytes:
    """A readable TIFF whose PlanarConfiguration tag holds two values where one is expected: Pillow reads it with a UserWarning."""
    from PIL import ImageDraw

    image = Image.new("RGB", (300, 160), "white")
    ImageDraw.Draw(image).text((20, 60), "INVOICE 4411", fill="black")
    buffer = BytesIO()
    image.save(buffer, format="TIFF")
    data = bytearray(buffer.getvalue())
    directory = struct.unpack_from("<I", data, 4)[0]
    for index in range(struct.unpack_from("<H", data, directory)[0]):
        entry = directory + 2 + 12 * index
        if struct.unpack_from("<H", data, entry)[0] == 284:
            struct.pack_into("<IHH", data, entry + 4, 2, 1, 1)
    return bytes(data)


class TestPillowWarnings:
    """Pillow's UserWarning about a tag it could not take as written leaked out of the single-picture path, so a program that
    turns warnings into errors had a readable picture rejected as 'corrupted' (and left as placeholder text inside a document)."""

    def test_uploaded_picture_with_a_metadata_warning_is_read_with_warnings_as_errors(self):
        import warnings

        data = _tiff_with_a_tag_of_the_wrong_count()

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            result = convert(data, "scan.tiff")

        assert len(result.images) == 1

    def test_picture_with_a_metadata_warning_inside_a_document_gives_no_warning(self):
        import warnings

        from docx import Document

        document = Document()
        document.add_paragraph("Body")
        document.add_picture(BytesIO(_tiff_with_a_tag_of_the_wrong_count()))
        buffer = BytesIO()
        document.save(buffer)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = convert(buffer.getvalue(), "a.docx")

        assert len(result.images) == 1
        assert not result.warnings
        assert not [warning for warning in caught if "PIL" in (warning.filename or "")]


class TestTransparencyOfEveryMode:
    """Only RGBA, LA and P counted as transparent, so a PA picture (palette with alpha, as TIFF stores it) became a JPEG and
    its transparent background black."""

    @pytest.mark.parametrize(("mode", "image_format"), [("PA", "TIFF"), ("LA", "PNG"), ("RGBA", "WEBP")])
    def test_transparent_background_stays_transparent(self, mode, image_format):
        picture = Image.new("RGBA", (300, 120), (0, 0, 0, 0))
        picture.paste((0, 0, 0, 255), (20, 20, 280, 100))
        picture = picture.convert(mode) if mode != "PA" else picture.convert("RGBA").convert("PA")
        buffer = BytesIO()
        picture.save(buffer, format=image_format)

        result = prepare_image(buffer.getvalue(), min_side=1, max_side=2048)

        decoded = Image.open(BytesIO(result.data)).convert("RGBA")
        assert result.mime_type == "image/png"
        assert decoded.getpixel((2, 2))[3] == 0
        assert decoded.getpixel((150, 60))[3] == 255

    def test_grey_png_with_a_transparent_level_stays_transparent(self):
        picture = Image.new("L", (300, 120), 0)
        picture.paste(255, (20, 20, 280, 100))
        buffer = BytesIO()
        picture.save(buffer, format="PNG", transparency=0)

        result = prepare_image(buffer.getvalue(), min_side=1, max_side=2048)

        assert result.mime_type == "image/png"
        assert Image.open(BytesIO(result.data)).convert("RGBA").getpixel((2, 2))[3] == 0


class TestScannedPagesLeftOutByTheImageLimit:
    """A scanned page image that `max_images` left out leaves its page empty, yet the warning said it was 'left as a
    placeholder' and the page was named nowhere."""

    def test_page_left_out_is_named_as_empty(self):
        data = builder.pdf_scanned_in_strips([[], [], []], strips=1)

        result = convert(data, "s.pdf", ConvertOptions(max_images=1))

        assert [image.section_index for image in result.images] == [1]
        assert "There are many images, so only 1 were selected for description (scanned pages first). 1 page(s) whose text " \
               "exists only as an image (2) were left out and are empty." in result.warnings
        assert not any("left as placeholders only" in warning for warning in result.warnings)

    def test_pictures_left_out_beside_pages_are_counted_apart(self):
        from opendockit.converter import _prepare_images
        from opendockit.models import Extraction, Section, SectionKind, marker_key

        extraction = Extraction()
        with marker_key("k"):
            page = extraction.add_image(builder.png_bytes(400, 300, seed=1), kind=ImageKind.PAGE)
            figure = extraction.add_image(builder.png_bytes(400, 300, seed=2))
        sections = [Section(SectionKind.PAGE, 1, "", None, page), Section(SectionKind.PAGE, 2, "", None, figure)]
        warnings: list[str] = []

        images, _ = _prepare_images(extraction.images, sections, ConvertOptions(max_images=0), warnings, "k")

        assert images == []
        assert warnings == ["There are many images, so only 0 were selected for description (scanned pages first). 1 page(s) "
                            "whose text exists only as an image (1) were left out and are empty, and 1 other image(s) were "
                            "left as placeholders only."]
