"""PDF lines with widened character spacing (Tc): pdfminer put a space after every character, splitting numbers and words."""


from opendockit import ConvertOptions, convert
from tests.test_pdf import _FONT, _korean_pdf


def _spaced_lines(spacing: float, lines: list[tuple[float, float, str]], size: float = 10) -> str:
    """Text of a page on which each (x, y, text) line is written with a character spacing of `spacing` pt."""
    def draw(pdf, index):
        for x, y, text in lines:
            spaced = pdf.beginText(x, y)
            spaced.setFont(_FONT, size)
            spaced.setCharSpace(spacing)
            spaced.textOut(text)
            pdf.drawText(spaced)

    return convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False)).text


class TestWideCharacterSpacing:
    """When the character spacing exceeded 10% of the font size it tripped pdfminer's word-gap threshold (word_margin 0.1)
    and the text became '1 , 2 3 4'."""

    def test_line_with_uniform_wide_spacing_keeps_its_words(self):
        text = _spaced_lines(1.5, [(50, 780, "총 사업비는 1,234,567천원이며 기간은 2025.01.01~2025.12.31이다.")])

        assert "총 사업비는 1,234,567천원이며 기간은 2025.01.01~2025.12.31이다." in text

    def test_borderless_table_cells_with_wide_spacing_keep_their_numbers(self):
        rows = [("인건비", "45,600", "3%"), ("운영비", "12,300", "10%"), ("합계", "57,900", "17%")]
        lines = [(x, 700 - index * 16, cell) for index, row in enumerate(rows) for x, cell in zip((50, 250, 400), row, strict=True)]

        text = _spaced_lines(1.5, lines)

        assert "| 인건비 | 45,600 | 3% |" in text and "| 합계 | 57,900 | 17% |" in text

    def test_line_with_ordinary_spacing_is_unchanged(self):
        assert "총 사업비는 1,234,567천원이다." in _spaced_lines(0.3, [(50, 780, "총 사업비는 1,234,567천원이다.")])
