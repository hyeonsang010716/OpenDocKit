"""Ruled PDF form grids: tables whose 'label | value' rows and the item rows below have different column boundaries."""


from opendockit import convert
from tests.test_pdf import _korean_pdf


def _cell(pdf, x: float, y: float, width: float, height: float, text: str = "") -> None:
    pdf.rect(x, y, width, height)
    if text:
        pdf.drawString(x + 3, y + height / 2 - 3, text)


class TestFormGridHeader:
    """Because of the vertical lines of the item rows below, the '등록번호' cell spanned two grid columns and looked merged, and
    the '상호' row under it was folded in as the lower row of a two-level header, giving
    '등록번호 상호 | 123-45-67890 (주)라마바 | …'. A two-level header splits into two or more cells under the merged cell."""

    def test_label_rows_of_a_form_grid_are_not_merged_into_a_two_level_header(self):
        def draw(pdf, index):
            left, top, height = 50, 742, 18
            _cell(pdf, left, top - height, 80, height, "등록번호")
            _cell(pdf, left + 80, top - height, 340, height, "123-45-67890")
            for x, width, text in [(0, 80, "상호"), (80, 140, "(주)라마바"), (220, 60, "성명"), (280, 140, "박대표")]:
                _cell(pdf, left + x, top - 2 * height, width, height, text)
            _cell(pdf, left, top - 3 * height, 80, height, "주소")
            _cell(pdf, left + 80, top - 3 * height, 340, height, "서울시 강남구 테헤란로 1")
            edges = [left, left + 40, left + 200, left + 300, left + 420]
            rows = [["월일", "품목", "수량", "금액"], ["09-30", "부품 A", "100", "5,000,000"], ["09-30", "부품 B", "100", "7,000,000"]]
            for number, row in enumerate(rows):
                for column, text in enumerate(row):
                    _cell(pdf, edges[column], top - (4 + number) * height, edges[column + 1] - edges[column], height, text)

        text = convert(_korean_pdf(draw), "a.pdf").sections[0].text

        assert "등록번호 상호" not in text
        rows = [line for line in text.splitlines() if line.startswith("|")]
        assert rows[0].startswith("| 등록번호 |") and "123-45-67890" in rows[0]
        assert any(row.startswith("| 상호 |") and "(주)라마바" in row and "박대표" in row for row in rows)
