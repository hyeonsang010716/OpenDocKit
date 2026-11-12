"""Rows of a PDF three-rule table (horizontal lines only at the top, under the header row and at the bottom) whose values
sit at the vertical middle or bottom beside a first cell wrapped onto several lines."""

import pytest

from opendockit import convert
from tests.test_pdf import _FONT


def _three_rule_table(valign: str, *, before: str = "표 1. 운영 지표") -> bytes:
    """reportlab table: a three-rule table with rows whose first cell wraps onto three and two lines, and whose cell text is
    vertically aligned with `valign`."""
    from io import BytesIO

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
    style = ParagraphStyle("k", fontName=_FONT, fontSize=9, leading=12)
    data = [["구분", "1분기", "2분기"], ["생산량", "1,200", "1,310"],
            [Paragraph("판매량 (국내와 해외를 모두 합한 값)", style), "900", "950"], ["불량률", "0.8%", "0.5%"],
            [Paragraph("원가 (단위: 백만 원, 부가세 제외)", style), "450", "498"]]
    table = Table(data, colWidths=[90, 55, 55])
    table.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), _FONT, 9), ("LINEABOVE", (0, 0), (-1, 0), 1, colors.black),
                               ("LINEBELOW", (0, 0), (-1, 0), 1, colors.black), ("LINEBELOW", (0, -1), (-1, -1), 1, colors.black),
                               ("ALIGN", (1, 1), (-1, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), valign)]))
    buffer = BytesIO()
    SimpleDocTemplate(buffer, pagesize=A4).build([Paragraph(before, style), Spacer(1, 4), table, Spacer(1, 8),
                                                  Paragraph("표 아래 문단이다.", style)])
    return buffer.getvalue()


_ROWS = ("| 구분 | 1분기 | 2분기 |\n| --- | --- | --- |\n| 생산량 | 1,200 | 1,310 |\n"
         "| 판매량 (국내와<br>해외를 모두 합한<br>값) | 900 | 950 |\n| 불량률 | 0.8% | 0.5% |\n"
         "| 원가 (단위: 백만<br>원, 부가세 제외) | 450 | 498 |")


class TestWrappedFirstCellsInAThreeRuleTable:
    @pytest.mark.parametrize("valign", ["MIDDLE", "BOTTOM", "TOP"])
    def test_rows_with_a_wrapped_first_cell_stay_whole(self, valign):
        """When values are aligned to the vertical middle or bottom they sit at the height of the middle or last line of the
        first cell. In rows grouped by top coordinate the leading lines of the first cell were set apart, so the table was
        cut before '판매량' or '| 값) | 900 | 950 |' became the header row, and the remaining rows were scattered after the
        paragraph below the table as one paragraph per cell."""
        result = convert(_three_rule_table(valign), "a.pdf")

        assert _ROWS in result.text
        assert result.text.index("| 원가") < result.text.index("표 아래 문단이다.")

    def test_paragraph_right_above_the_table_stays_out_of_the_header(self):
        """A paragraph above the table, outside the ruling lines, is not attached to a header cell."""
        result = convert(_three_rule_table("BOTTOM", before="다음 표는 분기별 운영 지표를 정리한 것이다."), "a.pdf")

        assert "다음 표는 분기별 운영 지표를 정리한 것이다.\n\n| 구분 | 1분기 | 2분기 |" in result.text


def _spaced_three_rule_table(valign: str, *, leading: float, padding: float) -> bytes:
    """Three-rule table between paragraphs with wide line spacing (`leading`). The top and bottom cell padding is `padding`,
    and the first cell wraps onto two lines on every data row."""
    from io import BytesIO

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
    style = ParagraphStyle("k", fontName=_FONT, fontSize=10, leading=leading)
    prose = "재생용기 회수 체계를 구축해 온실가스 감축 효과를 산정하는 방법과 기준값을 정리한 문단이다. " * 3
    labels = ["r1 감축 회수 체계 보상 원료", "r2 산정 대비 제품 투입량", "r3 온실가스 회수 판매량", "r4 시장평균 판매량 기준값"]
    data = [["열0", "열1", "열2", "열3"]] + [[Paragraph(label, style), *(Paragraph(f"r{n}c{column}", style) for column in (1, 2, 3))]
                                           for n, label in enumerate(labels, 1)]
    table = Table(data, colWidths=[110, 70, 70, 70])
    table.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), _FONT, 10), ("LINEABOVE", (0, 0), (-1, 0), 1, colors.black),
                               ("LINEBELOW", (0, 0), (-1, 0), 1, colors.black), ("LINEBELOW", (0, -1), (-1, -1), 1, colors.black),
                               ("VALIGN", (0, 0), (-1, -1), valign), ("TOPPADDING", (0, 0), (-1, -1), padding),
                               ("BOTTOMPADDING", (0, 0), (-1, -1), padding)]))
    buffer = BytesIO()
    SimpleDocTemplate(buffer, pagesize=A4).build([Paragraph(prose, style), Spacer(1, 6), table, Spacer(1, 6),
                                                  Paragraph(prose, style)])
    return buffer.getvalue()


class TestWrappedFirstCellsWithWideLineSpacing:
    """In a table LibreOffice exports with a Korean font the gap between lines is 0.8 times the line height (9.7pt for 12pt
    text), so with the 'half the line height' criterion the two lines of the first cell were not joined, and a table whose
    values sit at the vertical middle or bottom was read column by column, like '열0, r1c0, …, 열1, r1c1, …'. The line gap
    of the paragraphs outside the table is used as the measure."""

    @pytest.mark.parametrize("valign", ["MIDDLE", "BOTTOM"])
    def test_rows_with_a_wrapped_first_cell_stay_whole(self, valign):
        result = convert(_spaced_three_rule_table(valign, leading=19, padding=8), "a.pdf")

        assert "| 열0 | 열1 | 열2 | 열3 |" in result.text
        assert all(f"| r{n}c1 | r{n}c2 | r{n}c3 |" in result.text for n in range(1, 5))

    def test_cells_without_vertical_padding(self):
        """With zero top and bottom cell padding the gap between rows equals the gap between lines, so rows cannot be told
        apart by the gap. They are separated by the position of the vertically centred values."""
        result = convert(_spaced_three_rule_table("MIDDLE", leading=19, padding=0), "a.pdf")

        assert "| 열0 | 열1 | 열2 | 열3 |" in result.text
        assert "| r1 감축 회수 체계<br>보상 원료 | r1c1 | r1c2 | r1c3 |" in result.text
        assert all(f"| r{n}c1 | r{n}c2 | r{n}c3 |" in result.text for n in range(2, 5))

    def test_bottom_aligned_cells_without_padding_do_not_take_the_next_row(self):
        """With bottom alignment the values are level with the last line of the first cell, so in a table with zero padding
        'two lines of the first cell + the first line of the next row' look like three lines with the value row in the
        middle. Taken as evidence of middle alignment, it would attach the first line of the next row to the cell of the
        row before."""
        result = convert(_spaced_three_rule_table("BOTTOM", leading=19, padding=0), "a.pdf")

        rows = [line for line in result.text.splitlines() if line.startswith("|")]
        assert not any(f"r{n}" in row and f"r{n + 1} " in row for row in rows for n in range(1, 4))
        assert all(f"r{n}c1" in result.text for n in range(1, 5))
