"""PDF defect from review round 47: in a borderless table whose number columns are right-aligned including the header row,
a bottom-aligned note that wraps on an early row made the header row and the first row fall out of the table."""

import pytest

from opendockit import ConvertOptions, convert
from tests.test_pdf import _FONT


def _monthly_cost_pdf(wrapped_row: int) -> bytes:
    """A heading, a borderless five-column table (항목 | 1월 | 2월 | 3월 | 비고 — number columns right-aligned including the
    header row, cells bottom-aligned by default), a unit paragraph below the table and the next section. The note
    '계약 갱신 예정' on row `wrapped_row` wraps onto two lines in its 60pt cell."""
    from io import BytesIO

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

    pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
    heading = ParagraphStyle("h", fontName=_FONT, fontSize=14, leading=18)
    body = ParagraphStyle("b", fontName=_FONT, fontSize=10, leading=14)
    note = ParagraphStyle("n", fontName=_FONT, fontSize=9, leading=11)
    rows = [["항목", "1월", "2월", "3월", "비고"], ["원재료비", "7,753", "5,408", "9,121", ""],
            ["인건비", "1,614", "5,233", "1,127", ""], ["외주용역비", "1,049", "5,477", "7,050", ""],
            ["운송비", "1,734", "7,254", "9,523", "2월 미집행"]]
    rows[wrapped_row][4] = Paragraph("계약 갱신 예정", note)
    table = Table(rows, colWidths=[90, 55, 55, 55, 60])
    table.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), _FONT, 9), ("ALIGN", (1, 0), (-2, -1), "RIGHT")]))
    story = [Paragraph("1. 월별 집행 현황", heading), table, Paragraph("※ 단위: 천원.", body), Paragraph("2. 다음 절", heading)]
    buffer = BytesIO()
    SimpleDocTemplate(buffer, pagesize=A4).build(story)
    return buffer.getvalue()


class TestWrappedNoteUnderARightAlignedMergedHeader:
    """In a table where pdfminer merged the right-aligned header cells '3월' and '비고' into one piece, when the leading line
    of the note on an early row ('계약 갱신') was set apart above the value row, the table found again after splitting the
    header was counted as holding only the cell joined with that leading line and not the original cell ('예정'), and was
    discarded. The header row became one paragraph per cell and the '원재료비' row became the header row (on the second row
    the 원재료비 values were scattered one per line as well)."""

    @pytest.mark.parametrize("wrapped_row", [1, 2])
    def test_header_and_rows_stay_in_one_table(self, wrapped_row):
        data = _monthly_cost_pdf(wrapped_row)

        text = convert(data, "a.pdf", ConvertOptions(page_markers=False)).text

        assert "| 항목 | 1월 | 2월 | 3월 | 비고 |" in text
        assert "| 원재료비 | 7,753 | 5,408 | 9,121 |" in text
        assert "계약 갱신<br>예정 |" in text
        assert sum(line.startswith("| --- ") for line in text.split("\n")) == 1
        assert text.index("운송비") < text.index("※ 단위") < text.index("2. 다음 절")
