"""PDF defects from review round 45: wrapped rows with middle/bottom alignment in a borderless table, a row with a single
filled cell outside the first column (subtotal), a two-row table next to a short paragraph, two adjoining tables with
different column counts, and the wrapped line of the last row of a striped table."""

import pytest

from opendockit import ConvertOptions, convert
from tests.test_pdf import _FONT


_WRAPPED = "탄소 포집 저장 기술의 상용화"


def _story_pdf(story) -> bytes:
    from io import BytesIO

    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate

    buffer = BytesIO()
    SimpleDocTemplate(buffer, pagesize=A4).build(story)
    return buffer.getvalue()


def _style():
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
    return ParagraphStyle("k", fontName=_FONT, fontSize=10, leading=14)


def _table(rows: list[list[str]], widths: list[float], commands: list[tuple] = ()):
    from reportlab.platypus import Paragraph, Table, TableStyle

    style = _style()
    table = Table([[Paragraph(cell, style) for cell in row] for row in rows], colWidths=widths)
    table.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), _FONT, 10), *commands]))
    return table


def _text(story) -> str:
    return convert(_story_pdf(story), "a.pdf", ConvertOptions(page_markers=False)).text


def _project_table_pdf(wraps: str, valign: str) -> bytes:
    """Borderless four-column table (번호 | 과제명 | 담당 | 예산). On the rows marked 'w' in `wraps` the project name wraps onto
    two lines, and the cell text is vertically aligned with `valign`. A caption above the table, the paragraph '이상.' below."""
    from reportlab.platypus import Paragraph, Spacer

    style = _style()
    rows = [["번호", "과제명", "담당", "예산"]]
    rows += [[str(number), _WRAPPED if wrap == "w" else f"과제 {number}", f"담당{number}", f"{number},000"]
             for number, wrap in enumerate(wraps, 1)]
    table = _table(rows, [40, 130, 80, 80], [("VALIGN", (0, 0), (-1, -1), valign)])
    return _story_pdf([Paragraph("표 1. 과제 목록", style), Spacer(1, 6), table, Spacer(1, 10), Paragraph("이상.", style)])


class TestWrappedRowsOfABorderlessTable:
    """A row of a borderless table with cell padding in which one cell's text wraps onto two lines and the other cells are
    vertically aligned to the middle or the bottom. With bottom alignment (the default when Excel exports to PDF) the leading
    line was set apart above the value row, so the table split at that row and the first row of the second piece became a
    header row (when it was the first data row, the header row fell out as paragraphs); with middle alignment the last row
    was scattered cell by cell and the paragraph below the table got in between."""

    @pytest.mark.parametrize(("wraps", "valign"), [("ssssw", "MIDDLE"), ("ssssw", "BOTTOM"), ("sswss", "MIDDLE"),
                                                   ("sswss", "BOTTOM"), ("wssss", "BOTTOM")])
    def test_wrapped_row_stays_one_row_of_one_table(self, wraps, valign):
        data = _project_table_pdf(wraps, valign)

        text = convert(data, "a.pdf", ConvertOptions(page_markers=False)).text

        number = wraps.index("w") + 1
        assert f"| {number} | 탄소 포집 저장 기술의<br>상용화 | 담당{number} | {number},000 |" in text
        assert sum(line.startswith("| --- ") for line in text.split("\n")) == 1
        assert text.startswith("표 1. 과제 목록\n\n| 번호 | 과제명 | 담당 | 예산 |")
        assert text.endswith("| 5 | " + ("탄소 포집 저장 기술의<br>상용화" if number == 5 else "과제 5") + " | 담당5 | 5,000 |\n\n이상.")

    def test_short_line_set_apart_above_a_row_is_not_joined_to_its_cell(self):
        """A one-piece line set apart by the gap between rows is not the leading line of a cell in the row below (it is a
        paragraph that ends the table)."""
        from reportlab.platypus import Paragraph, Spacer

        style = _style()
        rows = [["번호", "과제명", "담당", "예산"], *([str(number), f"과제 {number}", f"담당{number}", f"{number},000"]
                                               for number in range(1, 4))]
        later = [["번호", "과제명", "담당", "예산"], ["9", "과제 9", "담당9", "9,000"], ["8", "과제 8", "담당8", "8,000"]]
        story = [_table(rows, [40, 130, 80, 80]), Spacer(1, 4), Paragraph("둘째 표", style), Spacer(1, 4),
                 _table(later, [40, 130, 80, 80])]

        text = _text(story)

        assert "둘째 표<br>" not in text
        assert "둘째 표" in text.split("\n")


class TestSubtotalRowInsideABorderlessTable:
    """When a row with a single filled cell outside the first column (a subtotal amount) comes in the middle of a borderless
    table, the table split in two, the amount fell out as a paragraph, and the first data row of the second piece became a
    header row. Just as a first-column value was already accepted as a divider row, a cell aligned with the cells of its
    own column is accepted as a row of the table."""

    @pytest.mark.parametrize("column", [1, 2, 3])
    def test_row_with_one_value_in_a_later_column_stays_in_the_table(self, column):
        from reportlab.platypus import Paragraph, Spacer

        style = _style()
        rows = [["품목", "수량", "단가", "금액"], ["사과", "10", "1,000", "10,000"], ["배", "5", "2,000", "10,000"],
                ["감", "3", "500", "1,500"], ["", "", "", ""], ["포도", "2", "3,000", "6,000"], ["수박", "1", "15,000", "15,000"],
                ["귤", "20", "300", "6,000"]]
        rows[4][column] = "21,500"
        story = [Paragraph("주문 내역", style), Spacer(1, 6), _table(rows, [112.5] * 4), Spacer(1, 10), Paragraph("이상.", style)]

        text = _text(story)

        cells = ["", "", "", ""]
        cells[column] = "21,500"
        assert "|" + "".join(f" {cell} |" if cell else " |" for cell in cells) in text
        assert sum(line.startswith("| --- ") for line in text.split("\n")) == 1
        assert "| 감 | 3 | 500 | 1,500 |" in text and "| 포도 | 2 | 3,000 | 6,000 |" in text
        assert text.endswith("| 귤 | 20 | 300 | 6,000 |\n\n이상.")


class TestTwoRowTableNextToAShortParagraph:
    """When a short one-line paragraph with the same left edge as the first cell comes right below or above a borderless
    two-row table (with cell padding), the first cells looked like a column of text running up and down and were not joined
    into rows. The table was scattered column by column and the paragraph below got in between the first and second
    columns. A text column is assumed only when it continues beyond that line or continues on both sides."""

    @pytest.mark.parametrize("side", ["below", "above"])
    def test_rows_are_read_row_by_row(self, side):
        from reportlab.platypus import Paragraph, Spacer

        style = _style()
        padding = [("TOPPADDING", (0, 0), (-1, -1), 6), ("BOTTOMPADDING", (0, 0), (-1, -1), 6)]
        table = _table([["구분", "담당자", "연락처", "비고"], ["총무팀", "김철수", "1234", "내선"]], [112.5] * 4, padding)
        story = [table, Spacer(1, 10), Paragraph("아래 문단.", style)] if side == "below" else [
            Paragraph("위 문단입니다.", style), Spacer(1, 6), table]

        text = _text(story)

        assert "구분 담당자 연락처 비고" in text and "총무팀 김철수 1234 내선" in text
        if side == "below":
            assert text.index("총무팀 김철수 1234 내선") < text.index("아래 문단.")


class TestBorderlessTablesWithDifferentColumnsNextToEachOther:
    """When two borderless tables with different column counts sit a row gap apart they were merged into one table, and the
    cells of the second table went into the wrong columns of the first ('| 부서 | 인원 | | 비고 |'). If none of the cells of
    the second table line up with the cells of the same column in the rows above, the table is cut there."""

    def test_second_table_keeps_its_own_columns(self):
        from reportlab.platypus import Spacer

        first = [["품목", "수량", "단가", "금액"], ["사과", "10", "1,000", "10,000"], ["배", "5", "2,000", "10,000"],
                 ["감", "3", "500", "1,500"]]
        second = [["부서", "인원", "비고"], ["총무", "3", "본사"], ["영업", "12", "지사"], ["개발", "20", "연구소"]]

        text = _text([_table(first, [112.5] * 4), Spacer(1, 10), _table(second, [150.0] * 3)])

        assert "| 감 | 3 | 500 | 1,500 |\n\n| 부서 | 인원 | 비고 |\n| --- | --- | --- |\n| 총무 | 3 | 본사 |" in text
        assert "| 개발 | 20 | 연구소 |" in text


def _zebra_projects_pdf(valign: str, start: int) -> bytes:
    """Borderless table shaded on alternate rows (from row `start`). The project-name column is wide and the second and
    fourth project names wrap onto two lines."""
    from reportlab.lib import colors
    from reportlab.platypus import Paragraph, Spacer

    style = _style()
    rows = [["번호", "과제명", "담당", "예산"], ["1", "차세대 배터리 소재 개발 및 실증 사업 추진", "김철수", "1,200"],
            ["2", "수소 충전소", "이영희", "800"], ["3", "탄소 포집 저장 기술의 상용화를 위한 파일럿 플랜트 구축", "박민수", "2,500"],
            ["4", "폐플라스틱 열분해", "최지우", "650"], ["5", "지열 에너지 활용 냉난방 시스템 설계와 시범 운영", "정하늘", "900"]]
    commands = [("VALIGN", (0, 0), (-1, -1), valign),
                ("ROWBACKGROUNDS", (0, start), (-1, -1), [colors.HexColor("#EEEEEE"), colors.white])]
    table = _table(rows, [40, 220, 80, 80], commands)
    return _story_pdf([Paragraph("표 1. 과제 목록", style), Spacer(1, 6), table, Spacer(1, 10), Paragraph("이상.", style)])


class TestWrappedLastRowOfAStripedTable:
    """In a striped table that shades whole rows, the wrapped line of the last row ('운영') became a paragraph below the table
    even though it lies inside the shaded band of that row. The shaded band tells the extent of the row, so a wrapped line
    inside the same band is joined to its cell."""

    def test_wrapped_line_inside_the_band_of_the_last_row_stays_in_its_cell(self):
        data = _zebra_projects_pdf("TOP", 1)

        text = convert(data, "a.pdf", ConvertOptions(page_markers=False)).text

        assert "| 5 | 지열 에너지 활용 냉난방 시스템 설계와 시범<br>운영 | 정하늘 | 900 |\n\n이상." in text

    @pytest.mark.parametrize("start", [0, 1])
    def test_bottom_aligned_striped_table_keeps_its_rows(self, start):
        data = _zebra_projects_pdf("BOTTOM", start)

        text = convert(data, "a.pdf", ConvertOptions(page_markers=False)).text

        assert "| 3 | 탄소 포집 저장 기술의 상용화를 위한 파일럿<br>플랜트 구축 | 박민수 | 2,500 |" in text
        assert "| 5 | 지열 에너지 활용 냉난방 시스템 설계와 시범<br>운영 | 정하늘 | 900 |\n\n이상." in text
