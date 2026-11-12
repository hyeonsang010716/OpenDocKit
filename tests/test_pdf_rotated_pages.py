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

        assert "사업계획서" not in result.text
        assert "별표 1. 연도별 투자 계획" in result.sections[4].text
