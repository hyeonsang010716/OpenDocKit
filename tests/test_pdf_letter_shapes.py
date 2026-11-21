"""Text a page draws as shapes and not as text (`_pdf_artifacts._letter_shapes`): it cannot be read back, and the page is
named in a warning so that the missing characters are not missed silently."""

from io import BytesIO

from opendockit import convert


_FONT = "HYSMyeongJo-Medium"
_WARNING = "draw some of their characters as shapes and not as text"
# Each line as the text before and after a character that is drawn as a shape and not written
_LINES = [("시행령안 제", "조에 따라 인공지능 장비전력이"), ("킬로와트 이상인 시설은 제", "조의 기준을 따르며"),
          ("장관이 고시한 날부터", "개월 이내에 신청서를 낸다"), ("위원회는", "일 이내에 심의하고 그 결과를"),
          ("신청인에게 서면으로 통지하되 제", "항의 경우는 예외로 한다")]


def _pdf(shapes: int, *, bullets: bool = False) -> bytes:
    """A page of five lines of Korean, each with a gap of one letter in it; in the gap of the first `shapes` lines stands
    a filled curve of the size of a letter, as a digit drawn as an outline does. With `bullets`, a filled circle stands
    before each line."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(595, 842))
    pdf.setFont(_FONT, 11)
    for number, (before, after) in enumerate(_LINES):
        y = 760 - number * 18
        gap = 90 + pdfmetrics.stringWidth(before, _FONT, 11) + 2
        pdf.drawString(90, y, before)
        pdf.drawString(gap + 9, y, after)
        if bullets:
            pdf.circle(80, y + 4, 2.5, stroke=0, fill=1)
        if number < shapes:
            path = pdf.beginPath()
            path.moveTo(gap + 3, y)
            path.curveTo(gap + 7, y, gap + 7, y + 8, gap + 3, y + 8)
            path.curveTo(gap, y + 8, gap, y, gap + 3, y)
            pdf.drawPath(path, stroke=0, fill=1)
    pdf.save()
    return buffer.getvalue()


class TestLetterShapes:
    def test_a_page_with_letter_sized_shapes_between_its_letters_is_named(self):
        result = convert(_pdf(5), "a.pdf")

        assert [warning for warning in result.warnings if _WARNING in warning] == [
            "1 PDF page(s) (1) draw some of their characters as shapes and not as text, so those characters "
            "(often digits or Latin letters) are missing."]
        assert "시행령안 제" in result.text and "조에 따라 인공지능 장비전력이" in result.text  # what is text is read as before

    def test_a_shape_or_two_is_a_drawing_and_bullets_before_the_lines_are_bullets(self):
        for data in (_pdf(2), _pdf(0, bullets=True), _pdf(0)):
            assert not any(_WARNING in warning for warning in convert(data, "a.pdf").warnings)
