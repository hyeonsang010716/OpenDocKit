"""Rendering of pictures and scans on rotated pages (/Rotate 90, 270), and running headers.

pdfplumber gives the character and picture coordinates and the page size of a rotated page as they are after rotation
(as seen). reportlab's `setPageRotation` rewrites the MediaBox as landscape, so in a test file drawn on it in portrait
coordinates the content falls outside the visible page — a 'cut-off picture' in such a file is exactly what is seen.
Here the content is drawn in landscape on a portrait MediaBox and only /Rotate is changed, giving a real landscape page.
"""

from io import BytesIO

from PIL import Image
import pytest

from opendockit import ConvertOptions, convert


def _landscape_pdf(rotation: int, draw, pages_before: int = 0) -> bytes:
    """PDF that draws landscape page (842×595) content on a portrait A4 MediaBox and rotates only the last page with
    /Rotate `rotation`."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    width, height = A4
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    for index in range(pages_before):
        pdf.setFont("HYSMyeongJo-Medium", 8)
        pdf.drawString(50, height - 30, "주식회사 한빛 | 2025 사업계획서")
        pdf.setFont("HYSMyeongJo-Medium", 11)
        pdf.drawString(60, height - 80, f"{index + 1}장 본문 내용입니다.")
        pdf.showPage()
    pdf.saveState()
    if rotation == 90:
        pdf.translate(width, 0)
        pdf.rotate(90)
    else:
        pdf.translate(0, height)
        pdf.rotate(-90)
    pdf.setFont("HYSMyeongJo-Medium", 12)
    draw(pdf, height, width)
    pdf.restoreState()
    pdf.save()
    data = buffer.getvalue()
    index = data.rindex(b"/Rotate 0")
    return data[:index] + b"/Rotate %d" % rotation + data[index + len(b"/Rotate 0"):]


def _picture(width: int, height: int) -> object:
    from reportlab.lib.utils import ImageReader

    image = Image.new("RGB", (width, height), (30, 120, 200))
    image.paste((255, 0, 0), (width * 3 // 4, 0, width, height))  # the band at the right edge is red: it disappears if the picture is cut off
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return ImageReader(BytesIO(buffer.getvalue()))


class TestRotatedPages:
    @pytest.mark.parametrize("rotation", [90, 270])
    def test_figure_on_the_right_of_a_landscape_page_is_rendered_whole(self, rotation):
        def draw(pdf, width, height):
            pdf.drawString(50, height - 60, "오른쪽에 그림이 있는 가로 쪽")
            pdf.drawImage(_picture(400, 200), 400, height - 300, 400, 200)

        result = convert(_landscape_pdf(rotation, draw), "a.pdf")

        figure = Image.open(BytesIO(result.images[0].data)).convert("RGB")
        assert figure.width == 2 * figure.height
        right = figure.getpixel((figure.width - 5, figure.height // 2))
        left = figure.getpixel((5, figure.height // 2))
        assert right[0] > 200 and right[2] < 60
        assert left[2] > 150

    @pytest.mark.parametrize("rotation", [90, 270])
    def test_landscape_scan_is_rendered_as_a_landscape_page(self, rotation):
        def draw(pdf, width, height):
            pdf.drawImage(_picture(1169, 827), 0, 0, width, height)

        result = convert(_landscape_pdf(rotation, draw), "a.pdf")

        page = Image.open(BytesIO(result.images[0].data)).convert("RGB")
        assert page.width > page.height
        assert page.getpixel((page.width - 5, page.height // 2))[0] > 200

    @pytest.mark.parametrize("rotation", [90, 270])
    def test_running_header_on_a_rotated_page_is_removed_like_on_the_other_pages(self, rotation):
        def draw(pdf, width, height):
            pdf.setFont("HYSMyeongJo-Medium", 8)
            pdf.drawString(50, height - 30, "주식회사 한빛 | 2025 사업계획서")
            pdf.setFont("HYSMyeongJo-Medium", 14)
            pdf.drawString(50, height - 70, "별표 1. 연도별 투자 계획")

        result = convert(_landscape_pdf(rotation, draw, pages_before=4), "a.pdf", ConvertOptions(extract_images=False))

        assert result.text.count("사업계획서") == 1
        assert "별표 1. 연도별 투자 계획" in result.sections[4].text


def _merged_cell_open_at_the_bottom(pdf, left: float) -> None:
    """A two-column ruled table whose bottom rule closes only the first column (the second cell of the last row is open)."""
    top, width, height = 400, 60, 20
    for y in (top, top - height):
        pdf.line(left, y, left + 2 * width, y)
    pdf.line(left, top - 2 * height, left + width, top - 2 * height)
    for x in (left, left + width, left + 2 * width):
        pdf.line(x, top, x, top - 2 * height)
    for column, row, text in ((0, 1, "가"), (1, 1, "나"), (0, 2, "다"), (1, 2, "라")):
        pdf.drawString(left + column * width + 3, top - row * height + 6, text)


def _rules_continuing_below_the_table(pdf, left: float) -> None:
    """A two-column ruled table whose vertical rules, drawn row by row, run on one row past its bottom rule (a row split at
    the page end)."""
    top, width, height = 400, 60, 20
    for row in range(3):
        pdf.line(left, top - row * height, left + 2 * width, top - row * height)
        for column in range(3):
            pdf.line(left + column * width, top - row * height, left + column * width, top - (row + 1) * height)
        for column in range(2):
            pdf.drawString(left + column * width + 3, top - (row + 1) * height + 6, f"칸{row}{column}")


_TABLES_AND_LAST_ROWS = [(_merged_cell_open_at_the_bottom, "| 다 | 라 |"), (_rules_continuing_below_the_table, "| 칸20 | 칸21 |")]


class TestTablesOffThePage:
    """Characters of a page turned with /Rotate are not filtered to the visible area, so rules and text drawn past the page
    edge make a table that lies wholly off the page. Searching it again cropped a box turned inside out (its left side cut to
    the page's right edge), pdfplumber raised, and the whole document failed as corrupted: every page was lost."""

    @pytest.mark.parametrize(("table", "last_row"), _TABLES_AND_LAST_ROWS)
    def test_table_past_the_edge_of_a_rotated_page_does_not_lose_the_document(self, table, last_row):
        def draw(pdf, width, height):
            pdf.drawString(60, height - 60, "가로 쪽 본문입니다.")
            pdf.setFont("HYSMyeongJo-Medium", 8)
            table(pdf, width + 60)

        result = convert(_landscape_pdf(90, draw, pages_before=1), "a.pdf", ConvertOptions(page_markers=False))

        assert "1장 본문 내용입니다." in result.sections[0].text
        assert "가로 쪽 본문입니다." in result.sections[1].text
        assert all(text in result.sections[1].text for text in last_row.strip("| ").split(" | "))

    @pytest.mark.parametrize(("table", "last_row"), _TABLES_AND_LAST_ROWS)
    def test_the_same_table_on_the_page_is_still_closed(self, table, last_row):
        def draw(pdf, width, height):
            pdf.setFont("HYSMyeongJo-Medium", 8)
            table(pdf, 300)

        assert last_row in convert(_landscape_pdf(90, draw), "a.pdf", ConvertOptions(page_markers=False)).text
