"""Lines wrapped by the cell width inside a ruled PDF table cell (not line breaks)."""


from opendockit import ConvertOptions, convert
from tests.test_pdf import _FONT, _korean_pdf


def _ruled_table_pdf(rows, widths, size=10):
    """Ruled-table PDF whose cells are filled with paragraphs (reportlab Paragraph, wrapped by word at the cell width).
    A `<br/>` in the cell text is a real line break."""
    def draw(pdf, index):
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.platypus import Paragraph, Table, TableStyle

        style = ParagraphStyle("cell", fontName=_FONT, fontSize=size, leading=size * 1.4)
        pdf.setFont(_FONT, 10)
        pdf.drawString(72, 790, "규정 개정 대비표")
        table = Table([[Paragraph(text, style) for text in row] for row in rows], colWidths=widths)
        table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, (0, 0, 0)), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        _, height = table.wrapOn(pdf, 0, 0)
        table.drawOn(pdf, 72, 770 - height)
        pdf.drawString(72, 750 - height, "이상.")

    return _korean_pdf(draw)


class TestSoftWrappedLinesInRuledCells:
    """A `<br>` was inserted every time the text of a ruled-table cell wrapped at the cell width, so one cell of an
    old-vs-new provisions comparison table was cut on every line, like '…임직원이 업무를 수<br>행함에…'. Lines wrapped by the
    cell width are joined with a space; real line breaks (a line that still had room for the first word of the next
    line, list items) are kept."""

    _OLD = "제1조(목적) 이 규정은 회사의 임직원이 업무를 수행함에 있어 준수하여야 할 사항을 정함을 목적으로 한다."
    _NEW = ("제1조(목적) 이 규정은 회사의 임직원 및 협력사 직원이 업무를 수행함에 있어 준수하여야 할 윤리 기준과 절차를 "
            "정함을 목적으로 한다. Article 1 applies to all employees and contractors of the company.")

    def test_lines_wrapped_by_the_cell_width_are_joined_with_a_space(self):
        result = convert(_ruled_table_pdf([["현 행", "개 정 안"], [self._OLD, self._NEW]], [200, 200]), "a.pdf",
                         ConvertOptions(page_markers=False))

        assert f"| {self._OLD} | {self._NEW} |" in result.text.splitlines()

    def test_real_line_breaks_and_list_items_stay_apart(self):
        cell = "1) 신청서 제출<br/>2) 서류 심사 및 현장 확인을 거쳐 최종 선정 결과를 개별 통보한다<br/>3) 협약 체결"

        result = convert(_ruled_table_pdf([["구분", "절차"], ["진행", cell]], [80, 200]), "a.pdf",
                         ConvertOptions(page_markers=False))

        row = next(line for line in result.text.splitlines() if line.startswith("| 진행"))
        assert row.startswith("| 진행 | 1) 신청서 제출<br>2) 서류 심사 및")
        assert row.endswith("개별 통보한다<br>3) 협약 체결 |")

    def test_one_letter_per_line_label_is_read_as_one_word(self):
        """A category name stacked vertically one letter per line in a narrow cell ('인/적/사/항') is read as one word."""
        result = convert(_ruled_table_pdf([["인<br/>적<br/>사<br/>항", "성명: 홍길동<br/>생년월일: 1990. 1. 1."]], [30, 300]),
                         "a.pdf", ConvertOptions(page_markers=False))

        assert "| 인적사항 | 성명: 홍길동<br>생년월일: 1990. 1. 1. |" in result.text.splitlines()

    def test_centred_lines_of_a_cell_stay_apart(self):
        """Lines of a centred cell (the '위와 같이 … / date / applicant' closing of an application form) are real line
        breaks, not wrapped lines."""
        cell = "위와 같이 휴가를 신청합니다.<br/>2024년 7월 25일<br/>신청인 홍길동 (인)"

        def draw(pdf, index):
            from reportlab.lib.enums import TA_CENTER
            from reportlab.lib.styles import ParagraphStyle
            from reportlab.platypus import Paragraph, Table, TableStyle

            style = ParagraphStyle("cell", fontName=_FONT, fontSize=10, leading=14, alignment=TA_CENTER)
            table = Table([[Paragraph("구분", style), Paragraph("내용", style)], [Paragraph("확인", style), Paragraph(cell, style)]],
                          colWidths=[60, 220])
            table.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.5, (0, 0, 0))]))
            _, height = table.wrapOn(pdf, 0, 0)
            table.drawOn(pdf, 72, 770 - height)

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert "| 확인 | 위와 같이 휴가를 신청합니다.<br>2024년 7월 25일<br>신청인 홍길동 (인) |" in result.text.splitlines()
