"""Tables inside PDF tables: an inner table in an outer cell detected as one table with the outer table, or separately."""


from opendockit import ConvertOptions, convert
from tests.test_pdf import _FONT, _korean_pdf


_SCHOOLS = [["기간", "학교명", "전공", "구분"], ["2009~2015", "한국대학교", "컴퓨터공학", "졸업"],
            ["2015~2017", "한국대학교 대학원", "인공지능", "석사"]]
_JOBS = [["기간", "회사명", "직위", "담당업무"], ["2017~2020", "(주)가나다", "연구원", "추천 시스템 개발"],
         ["2020~현재", "(주)라마바", "선임", "검색 품질 개선"]]


def _resume_pdf(inset: tuple[float, float, float, float], open_last_row: bool = False, note: str = "") -> bytes:
    """Résumé-style outer table (name and contact rows + inner tables inside the '학력' and '경력' cells). `inset` is how far
    the inner table is set in from the outer cell (left, top, right, bottom). With right 0 the vertical lines of the inner
    table touch the outer border and both are detected as one table. `open_last_row` leaves out the line under the last
    row of the inner table (the inner table is only partly detected). `note` is a line written inside the outer cell,
    below the inner table."""
    def draw(pdf, index):
        xs = [50, 130, 290, 370, 545]
        ys = [780, 758, 736, 666, 596]
        pdf.setLineWidth(0.6)
        for y in ys:
            pdf.line(xs[0], y, xs[-1], y)
        for x in (xs[0], xs[1], xs[-1]):
            pdf.line(x, ys[0], x, ys[-1])
        for x in xs[2:4]:
            pdf.line(x, ys[0], x, ys[2])
        pdf.setFont(_FONT, 10)
        for row, values in enumerate([["성명", "홍길동", "생년월일", "1990. 1. 1."], ["연락처", "010-1234-5678", "이메일", "hong@example.com"]]):
            for column, value in enumerate(values):
                pdf.drawString(xs[column] + 5, ys[row] - 15, value)

        left, top, right, bottom = inset
        for number, (label, rows) in enumerate([("학력", _SCHOOLS), ("경력", _JOBS)]):
            outer_top, outer_bottom = ys[2 + number], ys[3 + number]
            pdf.setFont(_FONT, 10)
            pdf.drawString(xs[0] + 5, (outer_top + outer_bottom) / 2 - 4, label)
            x0, x1, y0, y1 = xs[1] + left, xs[-1] - right, outer_top - top, outer_bottom + bottom
            height = (y0 - y1) / len(rows)
            columns = [x0 + (x1 - x0) * share for share in (0, 0.25, 0.55, 0.75, 1)]
            pdf.setLineWidth(0.4)
            for line in range(len(rows) + (0 if open_last_row else 1)):
                pdf.line(x0, y0 - line * height, x1, y0 - line * height)
            for x in columns:
                pdf.line(x, y0, x, y0 - height * (len(rows) - (1 if open_last_row else 0)))
            pdf.setFont(_FONT, 9)
            if note:
                pdf.drawString(x0, outer_bottom + 4, note)
            for row, values in enumerate(rows):
                for column, value in enumerate(values):
                    pdf.drawString(columns[column] + 3, y0 - (row + 1) * height + 6, value)

    return _korean_pdf(draw)


class TestNestedTables:
    def test_inner_table_caught_as_cells_of_the_outer_table_is_not_repeated(self):
        """When the inner table touches the outer border, pdfplumber returns the outer cell (the cell holding the whole inner
        table) and the inner cells as one table. The outer cell was taken for a vertical merge, so the whole text of the
        inner table was repeated on every inner row (LibreOffice résumé, 430 → 1,196 characters)."""
        result = convert(_resume_pdf((5, 14, 0, 0)), "a.pdf", ConvertOptions(page_markers=False))

        assert result.text.count("한국대학교 대학원") == 1
        assert result.text.count("검색 품질 개선") == 1
        rows = [line for line in result.text.splitlines() if line.startswith("|")]
        assert any(row.startswith("| 학력 |") and "2009~2015" in row and "컴퓨터공학" in row for row in rows)
        assert any(row.startswith("| 경력 |") and "2020~현재" in row and "검색 품질 개선" in row for row in rows)

    def test_inner_table_found_apart_keeps_the_outer_label_on_its_rows(self):
        """When the inner table was detected separately, the outer table was discarded as 'a box holding another table', so
        '성명' and '홍길동' were scattered as paragraphs and '학력' came apart from its table. The outer table is kept and the
        rows of the inner table are spread out in place of the outer cell."""
        result = convert(_resume_pdf((6, 6, 6, 6)), "a.pdf", ConvertOptions(page_markers=False))

        assert "| 성명 | 홍길동 | 생년월일 | 1990. 1. 1. |" in result.text
        assert "| 학력 | 2015~2017 | 한국대학교 대학원 | 인공지능 | 석사 |" in result.text
        assert "| 경력 | 기간 | 회사명 | 직위 | 담당업무 |" in result.text
        assert result.text.count("검색 품질 개선") == 1

    def test_inner_table_found_only_in_part_is_kept_as_the_outer_cell_text(self):
        """When only some rows of the inner table are detected as a table, the inner table is not used and the content stays
        as the text of the outer cell — the undetected rows used to be scattered as paragraphs."""
        result = convert(_resume_pdf((6, 6, 6, 6), open_last_row=True), "a.pdf", ConvertOptions(page_markers=False))

        assert result.text.count("2015~2017") == 1
        row = next(line for line in result.text.splitlines() if line.startswith("| 학력 |"))
        assert "2009~2015" in row and "2015~2017 한국대학교 대학원 인공지능 석사" in row

    def test_unit_line_under_the_inner_table_becomes_a_row_after_it(self):
        """A '(단위: 천원)' line below the inner table inside the outer cell means the inner table does not hold all of the
        cell's text; the table structure must not be lost because of it."""
        result = convert(_resume_pdf((6, 6, 6, 16), note="(단위: 천원)"), "a.pdf", ConvertOptions(page_markers=False))

        assert ("| 학력 | 2015~2017 | 한국대학교 대학원 | 인공지능 | 석사 |\n| 학력 | (단위: 천원) | | | |\n"
                "| 경력 | 기간 | 회사명 | 직위 | 담당업무 |") in result.text
