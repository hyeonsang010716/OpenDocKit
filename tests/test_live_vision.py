"""End-to-end tests against a real vision model. They cost money, so they run only when asked for:

    OPENDOCKIT_LIVE_TESTS=1 OPENAI_API_KEY=... uv run pytest -m live

`OPENAI_BASE_URL` may point at any OpenAI-compatible endpoint (an Azure OpenAI `/openai/v1/` URL works with the Azure key),
and `OPENDOCKIT_LIVE_MODEL` overrides the model (default: `describe.DEFAULT_MODEL`).

Every picture carries a number drawn as pixels. The tests check that the number comes back in the right place of the
Markdown — between the paragraphs around the picture, inside the table cell, on the slide or sheet it was on.
"""

import asyncio
import base64
from io import BytesIO
import os

from docx import Document
from docx.shared import Inches
import openpyxl
from openpyxl.drawing.image import Image as SheetImage
from PIL import Image, ImageDraw, ImageFont
from pptx import Presentation
from pptx.util import Inches as SlideInches
import pytest
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas

from opendockit import OpenAIImageDescriber, aconvert, convert, describe_images
from opendockit.describe import DEFAULT_MODEL


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("OPENDOCKIT_LIVE_TESTS") != "1" or not os.environ.get("OPENAI_API_KEY"),
                       reason="live vision tests need OPENDOCKIT_LIVE_TESTS=1 and OPENAI_API_KEY"),
]
MODEL = os.environ.get("OPENDOCKIT_LIVE_MODEL", DEFAULT_MODEL)


def _picture(*lines: str, size: tuple[int, int] = (640, 360), image_format: str = "PNG") -> bytes:
    image = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([8, 8, size[0] - 8, size[1] - 8], outline="black", width=4)
    for index, line in enumerate(lines):
        draw.text((40, 50 + index * 90), line, fill="black", font=ImageFont.load_default(size=64))
    buffer = BytesIO()
    image.save(buffer, image_format)
    return buffer.getvalue()


def _described(data: bytes, filename: str):
    async def run():
        describer = OpenAIImageDescriber(model_name=MODEL)
        try:
            return await aconvert(data, filename, describer=describer)
        finally:
            await describer.aclose()

    result = asyncio.run(run())
    assert not any("could not be described" in warning for warning in result.warnings), result.warnings
    return result


def _between(text: str, before: str, code: str, after: str) -> bool:
    return before in text and after in text and text.index(before) < text.index(code) < text.index(after)


class TestPicturesInsideDocuments:
    def test_docx_picture_is_described_between_its_paragraphs_and_inside_a_table_cell(self):
        document = Document()
        document.add_paragraph("Paragraph before the picture.")
        document.add_picture(BytesIO(_picture("CODE 4821")), width=Inches(4))
        document.add_paragraph("Paragraph after the picture.")
        table = document.add_table(rows=2, cols=2)
        table.cell(0, 0).text, table.cell(0, 1).text, table.cell(1, 0).text = "Item", "Label", "Sticker"
        table.cell(1, 1).paragraphs[0].add_run().add_picture(BytesIO(_picture("CODE 7306")), width=Inches(2))
        buffer = BytesIO()
        document.save(buffer)

        text = _described(buffer.getvalue(), "a.docx").text

        assert _between(text, "Paragraph before the picture.", "4821", "Paragraph after the picture.")
        row = next(line for line in text.split("\n") if line.startswith("| Sticker"))
        assert "7306" in row and "[Image]" in row

    def test_pptx_picture_is_described_on_its_slide(self):
        deck = Presentation()
        first = deck.slides.add_slide(deck.slide_layouts[5])
        first.shapes.title.text = "First"
        second = deck.slides.add_slide(deck.slide_layouts[5])
        second.shapes.title.text = "Second"
        second.shapes.add_picture(BytesIO(_picture("CODE 5519")), SlideInches(1), SlideInches(2), width=SlideInches(5))
        buffer = BytesIO()
        deck.save(buffer)

        result = _described(buffer.getvalue(), "a.pptx")

        assert "5519" in result.sections[1].text and "5519" not in result.sections[0].text

    def test_xlsx_picture_is_described_on_its_sheet(self):
        workbook = openpyxl.Workbook()
        workbook.active.title = "Data"
        workbook.active.append(["Name", "Value"])
        workbook.active.append(["alpha", 1])
        photos = workbook.create_sheet("Photos")
        photos.append(["Photo sheet"])
        photos.add_image(SheetImage(BytesIO(_picture("CODE 6642"))), "B3")
        buffer = BytesIO()
        workbook.save(buffer)

        result = _described(buffer.getvalue(), "a.xlsx")

        assert "6642" in result.sections[1].text and "6642" not in result.sections[0].text

    def test_html_data_uri_picture_is_described_in_place(self):
        uri = "data:image/png;base64," + base64.b64encode(_picture("CODE 3907")).decode()
        html = f'<html><body><p>Before.</p><img src="{uri}" alt=""><p>After.</p></body></html>'

        assert _between(_described(html.encode(), "a.html").text, "Before.", "3907", "After.")

    def test_pdf_figure_is_described_on_its_page_and_a_scanned_page_is_transcribed(self):
        buffer = BytesIO()
        page = canvas.Canvas(buffer, pagesize=A4)
        page.setFont("Helvetica", 12)
        page.drawString(72, 780, "A text page with one figure below this line of ordinary body text.")
        page.drawImage(ImageReader(BytesIO(_picture("CODE 2284"))), 72, 480, width=320, height=180)
        page.drawString(72, 440, "Text after the figure, still on the first page.")
        page.showPage()
        scan = _picture("SCANNED PAGE", "REF 9153", size=(1240, 1754))
        page.drawImage(ImageReader(BytesIO(scan)), 0, 0, width=A4[0], height=A4[1])
        page.save()

        result = _described(buffer.getvalue(), "a.pdf")

        assert "2284" in result.sections[0].text and "9153" not in result.sections[0].text
        assert "9153" in result.sections[1].text and "<!-- Text read from image -->" in result.sections[1].text


class TestImageFiles:
    @pytest.mark.parametrize("image_format, extension", [("PNG", "png"), ("JPEG", "jpg"), ("WEBP", "webp"), ("GIF", "gif"),
                                                         ("BMP", "bmp"), ("TIFF", "tiff"), ("AVIF", "avif")])
    def test_uploaded_image_is_described(self, image_format, extension):
        code = str(1000 + sum(map(ord, image_format)))

        result = _described(_picture(f"CODE {code}", image_format=image_format), f"photo.{extension}")

        assert code in result.text

    def test_long_picture_sent_as_tiles_is_read_from_top_to_bottom(self):
        image = Image.new("RGB", (800, 7200), "white")
        draw = ImageDraw.Draw(image)
        for index, code in enumerate(("TOP 1175", "MIDDLE 4460", "BOTTOM 8832")):
            draw.text((60, 200 + index * 3300), code, fill="black", font=ImageFont.load_default(size=72))
        buffer = BytesIO()
        image.save(buffer, "PNG")

        result = _described(buffer.getvalue(), "long.png")

        assert len(result.images[0].tiles) >= 2
        positions = [result.text.index(code) for code in ("1175", "4460", "8832")]
        assert positions == sorted(positions)


class TestJudgement:
    def test_chart_values_are_transcribed_and_nothing_is_made_up(self):
        image = Image.new("RGB", (700, 420), "white")
        draw = ImageDraw.Draw(image)
        draw.text((30, 20), "Units sold", fill="black", font=ImageFont.load_default(size=36))
        for index, (label, value) in enumerate((("Jan", 37), ("Feb", 52), ("Mar", 81))):
            x = 90 + index * 200
            draw.rectangle([x, 380 - value * 3, x + 110, 380], fill="#2f6fb5")
            draw.text((x + 30, 380 - value * 3 - 40), str(value), fill="black", font=ImageFont.load_default(size=30))
            draw.text((x + 30, 385), label, fill="black", font=ImageFont.load_default(size=26))
        buffer = BytesIO()
        image.save(buffer, "PNG")

        text = _described(buffer.getvalue(), "chart.png").text

        assert all(value in text for value in ("37", "52", "81", "Jan", "Feb", "Mar"))

    def test_decorative_picture_inside_a_document_does_not_fail(self):
        """A flat colour block carries no information. The model may drop it (SKIP) or describe it briefly; either way the
        conversion must finish without a failure warning and without the literal word SKIP in the text."""
        block = Image.new("RGB", (400, 300), "#d9e2ec")
        buffer = BytesIO()
        block.save(buffer, "PNG")
        document = Document()
        document.add_paragraph("Above the decoration.")
        document.add_picture(BytesIO(buffer.getvalue()), width=Inches(3))
        document.add_paragraph("Below the decoration.")
        saved = BytesIO()
        document.save(saved)

        result = _described(saved.getvalue(), "a.docx")

        assert "SKIP" not in result.text
        assert "Above the decoration." in result.text and "Below the decoration." in result.text


class TestFailures:
    def test_wrong_key_leaves_placeholders_and_a_warning_and_a_retry_with_the_right_key_fills_them(self, monkeypatch):
        data = _picture("CODE 6071")
        offline = convert(data, "photo.png")

        async def with_key(key: str | None):
            if key is not None:
                monkeypatch.setenv("OPENAI_API_KEY", key)
            describer = OpenAIImageDescriber(model_name=MODEL, max_retries=0)
            try:
                return await describe_images(offline if key else failed, describer)
            finally:
                await describer.aclose()

        real_key = os.environ["OPENAI_API_KEY"]
        failed = asyncio.run(with_key("sk-this-key-is-wrong"))
        assert failed.text == "[Image]" and any("could not be described" in warning for warning in failed.warnings)

        monkeypatch.setenv("OPENAI_API_KEY", real_key)
        retried = asyncio.run(with_key(None))
        assert "6071" in retried.text and not any("could not be described" in warning for warning in retried.warnings)
