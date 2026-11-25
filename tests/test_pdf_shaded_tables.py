"""Shaded PDF tables: striped and header-shaded tables whose cell boundaries come only from cell fill rectangles (filled
rectangles with no border)."""


import pytest

from opendockit import ConvertOptions, convert
from tests.test_pdf import _FONT, _korean_pdf
from tests.timing import Fastest


_ROWS = [["지점", "매출(백만원)", "전년 대비", "담당"], ["서울 본점", "1,250", "+12%", "김민수"], ["부산 지점", "830", "-3%", "이정훈"],
         ["대구 지점", "610", "+5%", "박지영"], ["광주 지점", "540", "+8%", "최서연"], ["대전 지점", "495", "-1%", "정우성"],
         ["합계", "3,725", "+6%", ""]]
_WIDTHS = [120, 150, 120, 100]
_TABLE = ("| 지점 | 매출(백만원) | 전년 대비 | 담당 |\n| --- | --- | --- | --- |\n| 서울 본점 | 1,250 | +12% | 김민수 |\n"
          "| 부산 지점 | 830 | -3% | 이정훈 |\n| 대구 지점 | 610 | +5% | 박지영 |\n| 광주 지점 | 540 | +8% | 최서연 |\n"
          "| 대전 지점 | 495 | -1% | 정우성 |\n| 합계 | 3,725 | +6% | |")


def _shaded_pdf(rows=_ROWS, fills=(), rules=(), verticals=(), header_rule_cells=False, before="지점별 실적",
                after="전 지점 목표 달성률은 102%이다.", row_height=22.0, right_aligned=False, number_drop=0.0):
    """Table whose shading is drawn with a separately filled rectangle per cell (no border), as in PDFs printed from a
    browser or Word.

    `fills` is {row number: colour}, a hex string or a grey level (a float), `rules` the row boundary numbers at which a table-wide horizontal line is drawn
    (0 = top of the table), `verticals` vertical lines as (column boundary number, first row, last row + 1). With
    `header_rule_cells` a thin filled rectangle (0.7pt) of the cell width is drawn per cell at every row boundary (Chrome
    border-bottom). With `right_aligned` the cells from the second column on below the header row are right-aligned and
    written with the baseline lowered by `number_drop` (Word number columns).
    """
    def draw(pdf, index):
        from reportlab.lib import colors

        left, top = 60.0, 740.0
        edges = [left]
        for width in _WIDTHS:
            edges.append(edges[-1] + width)
        pdf.setFont(_FONT, 12)
        pdf.drawString(left, top + 24, before)
        pdf.setFont(_FONT, 10)

        for row_index, row in enumerate(rows):
            y = top - row_index * row_height  # top of the row (PDF coordinates)
            colour = dict(fills).get(row_index)
            if colour:
                if isinstance(colour, float):
                    pdf.setFillGray(colour)  # DeviceGray ('0.85 g'), which pdfminer gives as a bare number
                else:
                    pdf.setFillColor(colors.HexColor(colour))
                for x0, x1 in zip(edges, edges[1:], strict=False):
                    pdf.rect(x0, y - row_height, x1 - x0, row_height, stroke=0, fill=1)
            if header_rule_cells and row_index > 0:
                pdf.setFillColor(colors.HexColor("#CCCCCC"))
                for x0, x1 in zip(edges, edges[1:], strict=False):
                    pdf.rect(x0, y - row_height - 0.35, x1 - x0, 0.7, stroke=0, fill=1)
            pdf.setFillColor(colors.white if colour in ("#1F3864",) else colors.black)
            for column, (x0, x1, text) in enumerate(zip(edges, edges[1:], row, strict=False)):
                if right_aligned and row_index > 0 and column > 0:
                    pdf.drawRightString(x1 - 6, y - 15 - number_drop, text)
                else:
                    pdf.drawString(x0 + 6, y - 15, text)

        pdf.setFillColor(colors.black)
        for boundary in rules:
            pdf.line(edges[0], top - boundary * row_height, edges[-1], top - boundary * row_height)
        for column, first, last in verticals:
            pdf.line(edges[column], top - first * row_height, edges[column], top - last * row_height)
        pdf.drawString(left, top - len(rows) * row_height - 26, after)

    return _korean_pdf(draw)


class TestShadedTables:
    """The edges of the per-cell filled rectangles were used as table ruling lines, so only the shaded rows became a ruled
    table and the unshaded rows became separate paragraphs or tables; the row order was mixed up and the numbers were
    scattered (Chrome striped tables, Word 'Light Shading' and 'Medium Shading 1' table styles)."""

    def test_zebra_rows_shaded_cell_by_cell_stay_one_table_in_order(self):
        data = _shaded_pdf(fills={0: "#1F3864", 1: "#E8EEF7", 3: "#E8EEF7", 5: "#E8EEF7"})

        result = convert(data, "a.pdf", ConvertOptions(page_markers=False))

        assert _TABLE in result.text
        assert result.text.index(_TABLE) < result.text.index("전 지점 목표")

    def test_zebra_rows_shaded_in_device_grey_stay_one_table(self):
        """pdfminer gives a DeviceGray fill ('0.85 g') as a bare number, not a tuple, and such a fill was not seen as shading:
        each shaded row became a table of its own and the other rows fell apart into one paragraph per cell."""
        data = _shaded_pdf(fills={0: 0.25, 1: 0.85, 3: 0.85, 5: 0.85})

        result = convert(data, "a.pdf", ConvertOptions(page_markers=False))

        assert _TABLE in result.text

    def test_banded_rows_between_top_and_bottom_rules_stay_one_table(self):
        """Word 'Light Shading' style: only horizontal lines at the top of the table, under the header row and at the bottom,
        plus banded row shading; no vertical lines."""
        data = _shaded_pdf(fills={1: "#D3DFEE", 3: "#D3DFEE", 5: "#D3DFEE"}, rules=(0, 1, 7))

        result = convert(data, "a.pdf", ConvertOptions(page_markers=False))

        assert _TABLE in result.text

    def test_unshaded_rows_of_a_boxed_table_are_split_into_the_columns_of_the_other_rows(self):
        """Word 'Medium Shading 1' style: only the outer border and the vertical lines of the header row exist, so each
        unshaded row was merged into one cell."""
        data = _shaded_pdf(fills={0: "#4F81BD", 1: "#D3DFEE", 3: "#D3DFEE", 5: "#D3DFEE"}, rules=(0, 1, 7),
                           verticals=((0, 0, 7), (1, 0, 1), (2, 0, 1), (3, 0, 1), (4, 0, 7)))

        result = convert(data, "a.pdf", ConvertOptions(page_markers=False))

        assert _TABLE in result.text

    def test_unshaded_header_over_banded_rows_with_right_aligned_numbers_stays_one_table(self):
        """Word 'Light Shading' / Chrome stripes: the left-aligned text of the unshaded header row did not line up with the
        right-aligned number columns, so widening the table was given up altogether; only the shaded rows became separate
        tables and the row order was mixed up."""
        data = _shaded_pdf(fills={1: "#D3DFEE", 3: "#D3DFEE", 5: "#D3DFEE"}, rules=(0, 1, 7), right_aligned=True)

        result = convert(data, "a.pdf", ConvertOptions(page_markers=False))

        assert _TABLE in result.text

    def test_numbers_that_barely_touch_the_next_row_do_not_merge_its_cells(self):
        """Word 'Medium Shading 1': the glyph boxes of the numbers touched the band of the row below by 0.1pt and were counted
        as text of that band, so the unshaded row was merged into one cell. A character that reaches into a band by less
        than `_BAND_SLACK_RATIO` of its height is text of the neighbouring row."""
        data = _shaded_pdf(fills={0: "#4F81BD", 1: "#D3DFEE", 3: "#D3DFEE", 5: "#D3DFEE"}, rules=(0, 1, 7),
                           verticals=((0, 0, 7), (1, 0, 1), (2, 0, 1), (3, 0, 1), (4, 0, 7)), right_aligned=True,
                           number_drop=4.5)

        result = convert(data, "a.pdf", ConvertOptions(page_markers=False))

        assert _TABLE in result.text

    def test_dark_header_over_rows_ruled_by_thin_filled_cells_stays_one_table(self):
        """Chrome: shaded header cells + a bottom border on data rows (a 0.7pt filled rectangle per cell). The last row ends
        above its bottom border."""
        data = _shaded_pdf(fills={0: "#1F3864"}, header_rule_cells=True)

        result = convert(data, "a.pdf", ConvertOptions(page_markers=False))

        assert _TABLE in result.text

    def test_shaded_header_bar_does_not_swallow_the_prose_below_it(self):
        """A body paragraph or note below shaded header cells does not fit the cell boundaries, so it does not become a
        table row."""
        rows = [["구분", "내용", "비고", "담당"]]
        data = _shaded_pdf(rows=rows, fills={0: "#1F3864"},
                           after="이 문단은 표 아래에 놓인 본문으로 여러 칸의 경계를 가로질러 길게 이어지는 문장입니다.")

        result = convert(data, "a.pdf", ConvertOptions(page_markers=False))

        assert "\n\n이 문단은 표 아래에 놓인 본문으로 여러 칸의 경계를 가로질러 길게 이어지는 문장입니다." in result.text
        assert "| 이 문단" not in result.text

    def test_note_in_the_first_column_below_a_zebra_table_is_not_a_row(self):
        """A note right below a striped table that lies only in the first column is not a table row."""
        data = _shaded_pdf(rows=_ROWS[:6], fills={0: "#1F3864", 1: "#E8EEF7", 3: "#E8EEF7", 5: "#E8EEF7"}, after="※ 잠정치")

        result = convert(data, "a.pdf", ConvertOptions(page_markers=False))

        assert "| 대전 지점 | 495 | -1% | 정우성 |" in result.text
        assert "| ※ 잠정치" not in result.text

    def test_list_under_two_coloured_title_boxes_is_not_taken_for_a_last_row(self):
        """The first line of a list that follows at line spacing below two title boxes shaded side by side (a one-row,
        two-cell table) is not the last row of the table."""
        def draw(pdf, index):
            from reportlab.lib import colors

            pdf.setFillColor(colors.HexColor("#4472C4"))
            for x in (60, 300):
                pdf.rect(x, 700, 240, 24, stroke=0, fill=1)
            pdf.setFillColor(colors.white)
            pdf.drawString(66, 708, "현재 문제점")
            pdf.drawString(306, 708, "개선 방안")
            pdf.setFillColor(colors.black)
            for number in range(4):
                pdf.drawString(66, 684 - number * 16, f"- 문제 {number + 1} 설명")
                pdf.drawString(306, 684 - number * 16, f"- 개선 {number + 1} 내용")

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert "| - 문제 1" not in result.text
        assert "- 문제 1 설명\n- 문제 2 설명" in result.text


def _zebra_with_merged_cells_pdf(header_fill: str | None) -> bytes:
    """Striped table as printed by Chrome (each cell of the even tr rows is shaded) with two vertically merged cells
    ('영업본부' over 3 rows — shaded because it starts on a shaded row; '기술본부' over 2 rows — unshaded) and a '합계' row merged
    across two columns. Without `header_fill` the header row has only a bottom border (thin filled rectangles)."""
    edges, row_height, top = [34.0, 196.0, 327.0, 457.0, 578.0], 20.0, 712.0
    data = [("1팀", "1,200", "1,350"), ("2팀", "980", "1,010"), ("3팀", "760", "-"), ("개발팀", "2,300", "2,410"),
            ("품질팀", "540", "600")]

    def draw(pdf, index):
        from reportlab.lib import colors

        def shade(colour, x0, x1, upper, lower):
            pdf.setFillColor(colors.HexColor(colour))
            pdf.rect(x0, lower, x1 - x0, upper - lower, stroke=0, fill=1)

        def row_top(row):  # 0 = header row, 1.. = data rows
            return top - row * row_height

        pdf.setFont(_FONT, 12)
        pdf.drawString(34, top + 28, "나. 본부별 인원 현황")
        if header_fill:
            for x0, x1 in zip(edges, edges[1:], strict=False):
                shade(header_fill, x0, x1, row_top(0), row_top(1))
        else:
            for x0, x1 in zip(edges, edges[1:], strict=False):
                shade("#333333", x0, x1, row_top(1) + 0.75, row_top(1) - 0.75)
        shade("#DDE6F2", edges[0], edges[1], row_top(1), row_top(4))  # 영업본부 (starts on a shaded row)
        for row in (1, 3, 5):
            for x0, x1 in zip(edges[1:], edges[2:], strict=False):
                shade("#DDE6F2", x0, x1, row_top(row), row_top(row + 1))
        pdf.setFillColor(colors.white if header_fill else colors.black)
        pdf.setFont(_FONT, 10)
        for x0, text in zip(edges, ("본부", "팀", "2024", "2025"), strict=False):
            pdf.drawString(x0 + 6, row_top(0) - 14, text)
        pdf.setFillColor(colors.black)
        pdf.drawString(40, row_top(2) - 14, "영업본부")  # middle of rows 1–3
        pdf.drawString(40, row_top(4) - 24, "기술본부")  # middle of rows 4–5 (straddles the row boundary)
        for row, (team, first, second) in enumerate(data, 1):
            pdf.drawString(202, row_top(row) - 14, team)
            pdf.drawRightString(451, row_top(row) - 14, first)
            pdf.drawRightString(572, row_top(row) - 14, second)
        pdf.drawString(40, row_top(6) - 14, "합계")
        pdf.drawRightString(451, row_top(6) - 14, "5,780")
        pdf.drawRightString(572, row_top(6) - 14, "5,370")
        pdf.drawString(34, row_top(7) - 30, "본부별 인원은 전년 대비 소폭 감소하였다.")

    return _korean_pdf(draw)


_MERGED_TABLE = ("| 본부 | 팀 | 2024 | 2025 |\n| --- | --- | --- | --- |\n| 영업본부 | 1팀 | 1,200 | 1,350 |\n"
                 "| 영업본부 | 2팀 | 980 | 1,010 |\n| 영업본부 | 3팀 | 760 | - |\n| 기술본부 | 개발팀 | 2,300 | 2,410 |\n"
                 "| 기술본부 | 품질팀 | 540 | 600 |\n| 합계 | | 5,780 | 5,370 |")


class TestShadedTablesWithMergedCells:
    """A vertically merged cell of a striped table takes the shading of the row it starts on, so a four-column piece whose
    first column is one large shaded cell and a three-column piece without the first column became separate tables. Either
    the table widened from the three-column piece overlapped the four-column piece and rows came out twice (header with a
    bottom border), or it could not be widened, the '2팀' and '개발팀' rows fell out of the table and '합계' was pushed behind
    the body text (shaded header)."""

    def test_zebra_table_with_merged_cells_under_a_ruled_header_is_one_table(self):
        text = convert(_zebra_with_merged_cells_pdf(None), "a.pdf", ConvertOptions(page_markers=False)).text

        assert _MERGED_TABLE in text
        assert text.count("1,200") == 1 and text.index("5,370") < text.index("본부별 인원은")

    def test_zebra_table_with_merged_cells_under_a_shaded_header_is_one_table(self):
        text = convert(_zebra_with_merged_cells_pdf("#4A6FA5"), "a.pdf", ConvertOptions(page_markers=False)).text

        assert _MERGED_TABLE in text
        assert text.count("1,200") == 1 and text.index("5,370") < text.index("본부별 인원은")


def _striped_with_a_centred_merge(rows: list[list[str | None]], merged: tuple[int, int], *, rules: bool, box: bool) -> bytes:
    """A striped table (header and odd rows shaded cell by cell) whose cell `merged` (row, column) spans that row and the next,
    with the shading of its first row and its text centred on the two rows (Chrome `vertical-align: middle`). `None` marks
    the cell it covers. With `rules` a rule is drawn under every row except across the merged cell; with `box` only the outer
    border is drawn."""
    def draw(pdf, index):
        from reportlab.lib import colors

        edges, top, height = [60.0, 160.0, 260.0, 360.0], 740.0, 24.0
        first, column = merged
        pdf.setFont(_FONT, 9)
        for row_index, row in enumerate(rows):
            if row_index == 0 or row_index % 2:
                pdf.setFillColor(colors.HexColor("#B0C4DE" if row_index == 0 else "#DDE4F0"))
                for col, text in enumerate(row):
                    if text is not None:
                        span = 2 if (row_index, col) == merged else 1
                        pdf.rect(edges[col], top - (row_index + span) * height, edges[col + 1] - edges[col], span * height,
                                 stroke=0, fill=1)
        pdf.setFillColor(colors.black)
        for row_index, row in enumerate(rows):
            for col, text in enumerate(row):
                if text is not None:
                    y = top - (row_index + 1) * height - 3 if (row_index, col) == merged else top - (row_index + 1) * height + 6
                    pdf.drawString(edges[col] + 4, y, text)
        if rules:
            for boundary in range(len(rows) + 1):
                right = edges[column] if boundary == first + 1 else edges[-1]
                pdf.line(edges[0], top - boundary * height, right, top - boundary * height)
        if box:
            pdf.rect(edges[0], top - len(rows) * height, edges[-1] - edges[0], len(rows) * height, stroke=1, fill=0)
        pdf.drawString(edges[0], top - len(rows) * height - 30, "Closing paragraph after the table.")

    return _korean_pdf(draw)


class TestCentredTextOfAMergedCell:
    """A browser centres the text of a cell merged over two rows on both, so it sticks out of each. The band of the row beside
    it was rejected as 'text sticking out of the band', and that row kept the cell pdfplumber made of its columns without
    fills: '| South Lee | | shared |'."""

    @pytest.mark.parametrize("box", [False, True], ids=["ruled", "ruled-and-boxed"])
    def test_cells_beside_a_merged_cell_stay_apart(self, box):
        rows = [["Region", "Owner", "Note"], ["North", "Kim", "shared"], ["South", "Lee", None], ["East", "Park", "solo"]]

        text = convert(_striped_with_a_centred_merge(rows, (1, 2), rules=True, box=box), "a.pdf",
                       ConvertOptions(page_markers=False)).text

        assert ("| Region | Owner | Note |\n| --- | --- | --- |\n| North | Kim | shared |\n| South | Lee | shared |\n"
                "| East | Park | solo |") in text


class TestOneShadedRowInABorderlessTable:
    """A shaded fragment could be grown by only one open band above and one below, so a borderless table with one or two
    shaded data rows was cut at the shaded row: the rows beyond it became a second table, or lines scattered column by
    column, and the header fell apart into paragraphs. A fragment that stops short of aligned rows is dropped and the
    region is read by the borderless search, as if it had no shading."""

    @pytest.mark.parametrize("fills", [{1: "#E8EEF7"}, {2: "#E8EEF7"}, {3: "#E8EEF7"}, {2: "#E8EEF7", 4: "#E8EEF7"}])
    def test_table_reads_as_if_it_had_no_shading(self, fills):
        options = ConvertOptions(page_markers=False)
        plain = convert(_shaded_pdf(), "a.pdf", options).text

        text = convert(_shaded_pdf(fills=fills), "a.pdf", options).text

        assert _TABLE in plain
        assert text == plain


class TestShadedHeaderRowAndFirstColumn:
    """A borderless table whose header row and one column are each shaded with one rectangle: the sides of the two
    rectangles gave pdfplumber a grid of two rows and two columns, and the whole table was read as two cells, a column of
    names over a cell of every number ('120 135 160<br>45 52 60<br>…'). A cell whose lines are split into columns by one
    blank strip is not a cell of the table's grid, and the table is read as if it had no shading."""

    _ROWS = [["지역", "2022", "2023", "2024"], ["서울", "120", "135", "160"], ["부산", "45", "52", "60"],
             ["대구", "30", "33", "41"], ["인천", "55", "58", "61"]]

    def _pdf(self, shading):
        def draw(pdf, index):
            from reportlab.platypus import Table, TableStyle

            pdf.setFont(_FONT, 10)
            pdf.drawString(72, 790, "지역별 실적 (단위: 건)")
            table = Table(self._ROWS, colWidths=[70, 60, 60, 60])
            table.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), _FONT, 10),
                                       *(("BACKGROUND", start, end, (0.85, 0.85, 0.85)) for start, end in shading)]))
            _, height = table.wrapOn(pdf, 0, 0)
            table.drawOn(pdf, 72, 770 - height)
            pdf.drawString(72, 740 - height, "이상.")

        return _korean_pdf(draw)

    @pytest.mark.parametrize("column", [0, 3], ids=["first-column", "last-column"])
    def test_table_reads_as_if_it_had_no_shading(self, column):
        options = ConvertOptions(page_markers=False)
        plain = convert(self._pdf([]), "a.pdf", options).text

        text = convert(self._pdf([((0, 0), (-1, 0)), ((column, 1), (column, -1))]), "a.pdf", options).text

        assert "| 지역 | 2022 | 2023 | 2024 |\n| --- | --- | --- | --- |\n| 서울 | 120 | 135 | 160 |" in plain
        assert text == plain


def _ruled_shaded_pdf(rows: list[tuple], edges: list[float], totals: list[tuple], grid: bool = True) -> bytes:
    """A table whose every row is shaded with one rectangle (two band colours) and, with `grid`, ruled with a full grid.
    A cell is a list of lines; the columns after the second are right-aligned. `totals` are lines without rules or shading
    one row height apart below the table, in the same columns."""
    def draw(pdf, index):
        from reportlab.lib import colors

        pdf.setFont("Helvetica", 10)
        pdf.drawString(60, 800, "Quotation for office equipment.")
        tops = [760.0]
        for row in rows:
            tops.append(tops[-1] - (6 + 12 * max(len(cell) for cell in row)))
        for number, row in enumerate(rows):
            pdf.setFillColor(colors.HexColor("#B8CCE4" if number == 0 else ("#DCE6F2", "#F2F2F2")[number % 2]))
            pdf.rect(edges[0], tops[number + 1], edges[-1] - edges[0], tops[number] - tops[number + 1], stroke=0, fill=1)
            pdf.setFillColor(colors.black)
            for column, cell in enumerate(row):
                for line, text in enumerate(cell):
                    if column >= 2:
                        pdf.drawRightString(edges[column + 1] - 6, tops[number] - 13 - 12 * line, text)
                    else:
                        pdf.drawString(edges[column] + 4, tops[number] - 13 - 12 * line, text)
        if grid:
            pdf.setLineWidth(0.5)
            for y in tops:
                pdf.line(edges[0], y, edges[-1], y)
            for x in edges:
                pdf.line(x, tops[0], x, tops[-1])
        for number, line in enumerate(totals):
            for column, text in enumerate(line):
                if column >= 2:
                    pdf.drawRightString(edges[column + 1] - 6, tops[-1] - 13 - 18 * number, text)
                else:
                    pdf.drawString(edges[column] + 4, tops[-1] - 13 - 18 * number, text)
        pdf.drawString(60, tops[-1] - 100, "Prices exclude tax.")

    return _korean_pdf(draw)


class TestRuledGridWithShadedRows:
    """A table with a full grid of ruling lines whose rows are all shaded, followed by total lines without rules. The table
    was dropped as a shaded fragment that stops short of aligned rows and read again by the borderless search, which knows
    nothing of the rules: wrapped cells lost their line breaks and a number beside an empty cell moved into the next
    column. A grid that is drawn is read as the ruled table, and the lines below stay outside it."""

    _EDGES = [60, 170, 330, 400, 500]
    _ROWS = [(["Product"], ["Description"], ["Qty"], ["Amount"]),
             (["Laser printer"], ["Duplex, network,", "two paper trays"], ["2"], ["1,250"]),
             (["Toner"], ["Black, high yield"], [], ["980"]),
             (["Service plan"], ["On-site repair,", "three years,", "next business day"], ["1"], ["2,680"]),
             (["Cable"], [], ["4"], ["77"])]
    _TOTALS = [("", "", "Subtotal", "4,987"), ("", "", "Tax", "498"), ("", "", "Total", "5,485")]
    _TABLE = ("| Product | Description | Qty | Amount |\n| --- | --- | --- | --- |\n"
              "| Laser printer | Duplex, network,<br>two paper trays | 2 | 1,250 |\n| Toner | Black, high yield | | 980 |\n"
              "| Service plan | On-site repair,<br>three years,<br>next business day | 1 | 2,680 |\n| Cable | | 4 | 77 |")

    @pytest.mark.parametrize("totals", [2, 3])
    def test_grid_is_read_as_the_ruled_table_with_total_lines_below_it(self, totals):
        text = convert(_ruled_shaded_pdf(self._ROWS, self._EDGES, self._TOTALS[:totals]), "a.pdf",
                       ConvertOptions(page_markers=False)).text

        assert self._TABLE + "\n\n" in text
        assert "Subtotal" in text and "4,987" in text and "498" in text

    def test_table_is_the_same_as_without_the_total_lines(self):
        options = ConvertOptions(page_markers=False)
        alone = convert(_ruled_shaded_pdf(self._ROWS, self._EDGES, []), "a.pdf", options).text

        text = convert(_ruled_shaded_pdf(self._ROWS, self._EDGES, self._TOTALS), "a.pdf", options).text

        assert self._TABLE in alone
        assert [line for line in text.splitlines() if line.startswith("|")] == [line for line in alone.splitlines() if line.startswith("|")]

    def test_shaded_rows_without_a_grid_are_still_left_to_the_borderless_search(self):
        """The same page without the ruling lines: the total lines join the table (the rule this class narrows,
        `TestOneShadedRowInABorderlessTable`)."""
        text = convert(_ruled_shaded_pdf(self._ROWS, self._EDGES, self._TOTALS, grid=False), "a.pdf",
                       ConvertOptions(page_markers=False)).text

        assert "| | | Subtotal | 4,987 |\n| | | Tax | 498 |\n| | | Total | 5,485 |" in text


@pytest.mark.timing
def test_many_bands_on_a_page_with_many_letters_take_linear_time():
    """Every band scanned all the characters of the page, so a large page with many shaded bands and tables (a 1500pt
    square, 50,000 characters and 196 tables) took 48 seconds."""
    from io import BytesIO
    import time

    import pdfplumber
    from reportlab.pdfgen import canvas

    from opendockit.extractors._pdf_shaded_tables import _band_chars

    buffer = BytesIO()
    page = canvas.Canvas(buffer, pagesize=(1500, 1500))
    page.setFont("Helvetica", 6)
    for y in range(20, 1480, 8):
        page.drawString(20, y, "w12 x34 " * 40)
    page.save()
    with pdfplumber.open(BytesIO(buffer.getvalue())) as document:
        first = document.pages[0]
        objects = first.chars  # parsing time is not measured

        started = time.perf_counter()
        for top in range(0, 1500, 2):
            _band_chars(first, (top, top + 8), [20.0, 300.0, 600.0])
        elapsed = time.perf_counter() - started

    assert len(objects) > 40000 and elapsed < 4


def _striped_pages_pdf(pages: int) -> bytes:
    """`pages` pages, each with a table whose every second row is shaded cell by cell (no rules)."""
    def draw(pdf, index):
        from reportlab.lib import colors

        edges = [60.0]
        for width in _WIDTHS:
            edges.append(edges[-1] + width)
        for row, cells in enumerate(_ROWS):
            y = 740.0 - row * 22
            if row % 2:
                pdf.setFillColor(colors.HexColor("#E8EEF7"))
                for x0, x1 in zip(edges, edges[1:], strict=False):
                    pdf.rect(x0, y - 22, x1 - x0, 22, stroke=0, fill=1)
            pdf.setFillColor(colors.black)
            for x0, text in zip(edges, cells, strict=False):
                pdf.drawString(x0 + 6, y - 15, text)
        pdf.drawString(60, 200, f"{index + 1}쪽 본문")

    return _korean_pdf(draw, pages=pages)


class TestCharactersByTop:
    """The page's characters sorted by top (`_chars_by_top`) were kept on the pdfplumber page by themselves: every page read
    held its characters until the end of the document, and a page read again got the old characters."""

    def test_the_list_is_dropped_when_the_page_tables_have_been_read(self, monkeypatch):
        import opendockit.extractors.pdf as pdf_module

        read, holding = pdf_module._read_page, []

        def spy(page, *args, **kwargs):
            result = read(page, *args, **kwargs)
            holding.append(any(key in page.__dict__ for key in ("_chars_by_top", "_page_indexes")))
            return result

        monkeypatch.setattr(pdf_module, "_read_page", spy)
        result = convert(_striped_pages_pdf(3), "a.pdf", ConvertOptions(page_markers=False))

        assert holding == [False, False, False]
        assert result.text.count("| 서울 본점 | 1,250 | +12% | 김민수 |") == 3

    def test_a_page_read_again_gets_its_new_characters(self):
        from io import BytesIO

        import pdfplumber

        from opendockit.extractors._pdf_shaded_tables import _chars_by_top

        with pdfplumber.open(BytesIO(_striped_pages_pdf(1))) as document:
            page = document.pages[0]
            old = _chars_by_top(page)[1][0]
            page.close()
            fresh = min((char for char in page.chars if char["text"].strip()), key=lambda char: char["top"])

            assert _chars_by_top(page)[1][0] is fresh
            assert fresh is not old


def _grid_beside_bars_pdf(size: int, bars: int) -> bytes:
    """A ruled `size` × `size` grid of small cells and, to its right, `bars` small filled squares (a bar chart)."""
    from io import BytesIO

    from reportlab.lib import colors
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(1200, 1200))
    pdf.setFont("Helvetica", 5)
    for line in range(size + 1):
        pdf.line(20, 20 + line * 10, 20 + size * 10, 20 + line * 10)
        pdf.line(20 + line * 10, 20, 20 + line * 10, 20 + size * 10)
    for row in range(size):
        for column in range(size):
            pdf.drawString(22 + column * 10, 23 + row * 10, str((row * size + column) % 10))
    pdf.setFillColor(colors.HexColor("#3366cc"))
    for bar in range(bars):
        pdf.rect(700 + (bar % 60) * 7, 20 + (bar // 60) * 7, 4, 4, stroke=0, fill=1)
    pdf.save()
    return buffer.getvalue()


class TestShadingOutsideTheTable:
    """Whether a ruled table has cell shading was measured against every shading rectangle of the page for every cell (and
    measured twice), so a 60 × 60 grid beside 3,000 small filled bars spent 10 seconds there."""

    def test_rectangles_away_from_a_table_are_not_measured_for_its_cells(self, monkeypatch):
        import opendockit.extractors._pdf_shaded_tables as shaded

        fill_cover, measured = shaded._fill_cover, [0]

        def counting(cell, fills):
            measured[0] += len(fills)
            return fill_cover(cell, fills)

        monkeypatch.setattr(shaded, "_fill_cover", counting)
        text = convert(_grid_beside_bars_pdf(20, 600), "a.pdf", ConvertOptions(page_markers=False)).text

        assert measured[0] < 20_000  # 480,000 before: 400 cells × 600 bars, twice
        assert "| 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 0 | 1 |" in text

    def test_fill_cover_of_the_touching_rectangles_equals_the_full_sum(self):
        import random

        from opendockit.extractors._pdf_shaded_tables import _by_area, _fill_cover, _touching_each

        rng = random.Random(7)
        rects = _by_area([{"x0": x, "x1": x + rng.uniform(1, 40), "top": y, "bottom": y + rng.uniform(1, 40)}
                          for x, y in ((rng.uniform(0, 300), rng.uniform(-50, 300)) for _ in range(400))]
                         + [{"x0": -1e9, "x1": 1e9, "top": 0.0, "bottom": float("inf")}])
        cells = [(x, y, x + rng.uniform(0.5, 60), y + rng.uniform(0.5, 60))
                 for x, y in ((rng.uniform(0, 300), rng.uniform(0, 300)) for _ in range(300))] + [None]

        assert [_fill_cover(cell, touching) if cell else None for cell, touching in _touching_each(cells, rects)] == [
            _fill_cover(cell, rects) if cell else None for cell in cells]


def _long_zebra_with_groups_pdf(rows: int) -> bytes:
    """A striped table of `rows` rows (shaded cell by cell on even rows, no rules) whose first column has a group cell
    merged over two rows starting on every shaded row, so the table found from the fill rectangles has holes on the unshaded
    rows (`_regridded_fragments`)."""
    from io import BytesIO

    from reportlab.lib import colors
    from reportlab.pdfgen import canvas

    edges, height = [40.0, 140.0, 240.0, 340.0, 440.0], 18.0
    size = rows * height + 120
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(600, size))
    pdf.setFont("Helvetica", 10)
    for row in range(rows):
        y = size - 60 - row * height
        pdf.setFillColor(colors.HexColor("#DDDDDD"))
        if row % 2 == 0:
            pdf.rect(edges[0], y - 2 * height, edges[1] - edges[0], 2 * height, stroke=0, fill=1)
            for x0, x1 in zip(edges[1:], edges[2:], strict=False):
                pdf.rect(x0, y - height, x1 - x0, height, stroke=0, fill=1)
        pdf.setFillColor(colors.black)
        if row % 2 == 0:
            pdf.drawString(edges[0] + 5, y - height - 4, f"G{row // 2}")
        for column, x0 in enumerate(edges[1:4]):
            pdf.drawString(x0 + 5, y - 13, f"{row}-{column} abcdef")
    pdf.drawString(40, 30, "end.")
    pdf.save()
    return buffer.getvalue()


class TestLongZebraTableWithMergedGroups:
    """Joining the fragments of a long striped table with merged groups looked at every character of the table for every row
    boundary and column, and compared every pair of characters of neighbouring columns: 14 seconds for 700 rows."""

    def test_rows_stay_in_order_with_their_groups(self):
        text = convert(_long_zebra_with_groups_pdf(40), "a.pdf", ConvertOptions(page_markers=False)).text

        assert text.startswith("| G0 | 0-0 abcdef | 0-1 abcdef | 0-2 abcdef |\n| --- | --- | --- | --- |\n"
                               "| G0 | 1-0 abcdef | 1-1 abcdef | 1-2 abcdef |\n| G1 | 2-0 abcdef |")
        assert "| G19 | 39-0 abcdef | 39-1 abcdef | 39-2 abcdef |" in text

    @pytest.mark.timing
    def test_fragments_of_a_long_table_are_joined_in_linear_time(self, monkeypatch):
        """Joining the fragments takes a fifth of the time of the rest of the conversion, and five times that time before.
        Measured against the rest of the same conversion, on the same machine: a bound of 1.5 seconds failed on a macOS
        runner."""
        import math
        import time

        import opendockit.extractors._pdf_shaded_tables as shaded
        from tests.timing import Fastest

        regridded, spent = shaded._regridded_fragments, [0.0]

        def timed(*args):
            started = time.perf_counter()
            try:
                return regridded(*args)
            finally:
                spent[0] += time.perf_counter() - started

        monkeypatch.setattr(shaded, "_regridded_fragments", timed)
        data, joining, rest, timer = _long_zebra_with_groups_pdf(400), math.inf, math.inf, Fastest()
        for _ in timer:
            spent[0] = 0.0
            with timer:
                started = time.perf_counter()
                text = convert(data, "a.pdf", ConvertOptions(page_markers=False)).text
                whole = time.perf_counter() - started
            joining, rest = min(joining, spent[0]), min(rest, whole - spent[0])

        assert "| G199 | 399-0 abcdef | 399-1 abcdef | 399-2 abcdef |" in text
        assert joining < rest


def test_band_whose_only_text_lies_on_its_edge_does_not_end_the_text():
    """Characters of no height lying exactly on the top edge of a band are the band's text for `_band_cells` but have no
    middle inside it, so `_ends_the_text` took the end of an empty list."""
    from opendockit.extractors._pdf_shaded_tables import _band_cells, _ends_the_text

    class Page:
        objects: dict = {}

        def __init__(self, chars):
            self.chars = chars

    page = Page([{"text": text, "x0": x, "x1": x + 5, "top": 10.0, "bottom": 10.0, "size": 10} for text, x in (("a", 10), ("b", 60))])

    assert _band_cells(page, (10.0, 20.0), [0.0, 50.0, 100.0]) == [(10, 15), (60, 65)]
    assert _ends_the_text(page, (10.0, 20.0), 1, 0.0, 100.0, 10.0) is False
    assert _ends_the_text(page, (10.0, 20.0), -1, 0.0, 100.0, 10.0) is False


def _separate_shaded_tables_pdf(count: int) -> bytes:
    """One tall page of `count` separate tables of three columns, each a shaded header row over an unshaded value row."""
    from io import BytesIO

    from reportlab.lib import colors
    from reportlab.pdfgen import canvas

    height = count * 50 + 100
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(600, height))
    pdf.setFont("Helvetica", 9)
    edges = [50, 200, 350, 500]
    for index in range(count):
        y = height - 50 - index * 50
        pdf.setFillColor(colors.HexColor("#DDE4F0"))
        for x0, x1 in zip(edges, edges[1:], strict=False):
            pdf.rect(x0, y - 14, x1 - x0, 14, stroke=0, fill=1)
        pdf.setFillColor(colors.black)
        for column, x0 in enumerate(edges[:-1]):
            pdf.drawString(x0 + 3, y - 10, f"head{index} c{column}")
            pdf.drawString(x0 + 3, y - 24, f"val{index} c{column}")
    pdf.save()
    return buffer.getvalue()


class TestManySeparateShadedTables:
    """Every shaded table grown by bands cropped the whole page to search its region again, `_ends_the_text` scanned every
    character of the page, and every table measured the box and column boundaries of every other table again (pdfplumber
    measures `table.bbox` from all the cells at every access): one page of 400 separate shaded tables took 14 seconds."""

    def test_separate_shaded_tables_read_as_before(self):
        text = convert(_separate_shaded_tables_pdf(3), "a.pdf", ConvertOptions(page_markers=False)).text

        assert text == "\n\n".join(f"| head{n} c0 | head{n} c1 | head{n} c2 |\n| --- | --- | --- |\n| val{n} c0 | val{n} c1 | val{n} c2 |"
                                   for n in range(3))

    @pytest.mark.timing
    def test_many_shaded_tables_take_linear_time(self):
        from io import BytesIO

        import pdfplumber

        from opendockit.extractors._pdf_ruled_tables import _TABLE_SETTINGS
        from opendockit.extractors._pdf_shaded_tables import _with_shaded_rows

        spent = []
        for count in (75, 300):
            data = _separate_shaded_tables_pdf(count)
            timer = Fastest()
            for _ in timer:
                with pdfplumber.open(BytesIO(data)) as document:  # anew: the page keeps the lists made for its tables
                    page = document.pages[0]
                    fragments = page.find_tables(_TABLE_SETTINGS)  # the page's own table search is not measured
                    with timer:
                        tables = _with_shaded_rows(page, fragments)
                    assert len(tables) == count and all(len(table.rows) == 2 for table in tables)
            spent.append(timer.seconds)
        small, large = spent

        assert large < 8 * small + 0.5  # 16 times as long when every table cropped the whole page

    def test_cropped_page_holds_the_objects_of_a_crop_of_the_whole_page(self):
        from io import BytesIO
        import random

        import pdfplumber

        from opendockit.extractors._pdf_model import _TABLE_SETTINGS, _crop_for_table_search, _cropped

        rng = random.Random(7)
        with pdfplumber.open(BytesIO(_separate_shaded_tables_pdf(40))) as document:
            page = document.pages[0]
            left, top, right, bottom = page.bbox
            for _ in range(60):
                y0 = rng.choice([top, rng.uniform(top, bottom), float(rng.randrange(0, 2000, 64))])
                box = (rng.uniform(left, 300), y0, rng.uniform(300, right), min(bottom, y0 + rng.choice([1.0, 14.0, 64.0, 300.0, 1e6])))
                lines = {"explicit_vertical_lines": [box[0] + 10, box[2] - 10]}
                whole, near = _crop_for_table_search(page, box), _cropped(page, box)

                assert near.objects == whole.objects and near.bbox == whole.bbox
                assert ([table.cells for table in near.find_tables({**_TABLE_SETTINGS, **lines})]
                        == [table.cells for table in whole.find_tables({**_TABLE_SETTINGS, **lines})])

    @pytest.mark.parametrize("seed", range(6))
    def test_text_end_is_the_end_a_scan_finds(self, seed):
        import random

        from opendockit.extractors._pdf_shaded_tables import _ends_the_text, _ends_the_text_by_scanning

        class Page:
            objects: dict = {}

            def __init__(self, chars):
                self.chars = chars

        rng = random.Random(seed)
        odd = [float("nan"), float("inf"), -float("inf")]
        chars = []
        for _ in range(800):
            x, top = rng.uniform(0, 400), rng.choice([rng.uniform(0, 600), float(rng.randrange(0, 600, 12))])
            height = rng.choice([10.0, 10.0, 6.0, 0.0, -3.0])
            chars.append({"text": rng.choice(["a", "a", " "]), "x0": x, "x1": x + 5,
                          "top": rng.choice([top] * 8 + odd), "bottom": rng.choice([top + height] * 8 + odd)})
        page = Page(chars)
        verdicts = set()
        for _ in range(400):
            band_top = rng.choice([rng.uniform(0, 600), float(rng.randrange(0, 600, 12))])
            band = (band_top, band_top + rng.choice([12.0, 24.0, 1.0]))
            left = rng.uniform(0, 300)
            arguments = (band, rng.choice([1, -1]), left, left + rng.uniform(5, 200), rng.choice([12.0, 24.0, 2.0]))
            verdict = _ends_the_text(page, *arguments)
            assert verdict == _ends_the_text_by_scanning(page, *arguments)
            verdicts.add(verdict)
        assert verdicts == {True, False}

    @pytest.mark.parametrize("seed", range(4))
    def test_open_columns_are_those_of_scanning_the_edges(self, seed):
        import random
        from types import SimpleNamespace

        from opendockit.extractors._pdf_ruled_tables import _TableLookups

        rng = random.Random(seed)
        edges = [{"top": rng.choice([rng.uniform(0, 300), float(rng.randrange(0, 300, 10)), float("nan"), float("inf")]),
                  "x0": rng.uniform(0, 100), "x1": rng.uniform(100, 200)} for _ in range(500)]
        lookups = _TableLookups(SimpleNamespace(chars=[], horizontal_edges=edges), [])
        for y in [rng.uniform(0, 300) for _ in range(200)] + [float(rng.randrange(0, 300, 10)) + 1.5 for _ in range(50)]:
            found = lookups.edges_at(y)
            assert found == [edge for edge in edges if abs(edge["top"] - y) <= 1.5]
            assert [id(edge) for edge in found] == [id(edge) for edge in edges if abs(edge["top"] - y) <= 1.5]


# The table of a disease in a release, row by row: (label over both label columns, or (group, label) with the group cell
# going on from the row above when it is None; then three values, or one value across the three columns)
_DISEASE_ROWS = [("구분", "가형", "나형", "다형"), (("발생", "국가"), "콩고", "수단", "우간다"), ((None, "동향"), "산발 유행", "소규모", "드묾"),
                 ("병원체", "자이레형", "수단형", "분디부교형"), ("증상", "발열과 두통이 나타남"), ("치명률", "60%", "41%", "30%"),
                 (("치료", "약"), "승인 치료제", "없음", "없는 상태"), ((None, "백신"), "어베보", "미승인", "미승인 상태"),
                 ("잠복기", "2일에서 21일"), (("경로", "동물"), "박쥐와 접촉"), ((None, "사람"), "체액에 접촉")]


def _open_shaded_pdf(*, right_border_rows: int = 1, shaded: bool = True, open_left: bool = False) -> bytes:
    """A table as a Korean release draws it: rules across every row, the label cells shaded, the columns ruled off only
    where a row has cells of its own, and the right border on the header row alone (`right_border_rows` rows from the top)."""
    columns, top, height = [57.0, 86.0, 125.0, 264.0, 397.0, 530.0], 760.0, 24.0
    rows = _DISEASE_ROWS

    def y(line: int) -> float:
        return top - line * height

    def draw(pdf, index):
        grouped = [isinstance(row[0], tuple) for row in rows]
        if shaded:
            pdf.setFillColorRGB(0.85, 0.85, 0.85)
            pdf.rect(columns[0], y(len(rows)), columns[2] - columns[0], height * len(rows), stroke=0, fill=1)
            pdf.setFillColorRGB(0, 0, 0)
        for line in range(len(rows) + 1):
            # a rule stops at the group cell that goes on under it
            start = columns[1] if line < len(rows) and grouped[line] and rows[line][0][0] is None else columns[0] - 1
            pdf.line(start, y(line), columns[-1] + 1, y(line))
        if not open_left:
            pdf.line(columns[0], y(0), columns[0], y(len(rows)))
        pdf.line(columns[2], y(0), columns[2], y(len(rows)))
        for row, cells in enumerate(rows):
            if grouped[row]:
                pdf.line(columns[1], y(row), columns[1], y(row + 1))
            if len(cells) == 4:
                for x in columns[3:5]:
                    pdf.line(x, y(row), x, y(row + 1))
            label = cells[0]
            if grouped[row]:
                if label[0]:
                    pdf.drawString(columns[0] + 3, y(row + 1) + 8, label[0])
                pdf.drawString(columns[1] + 3, y(row + 1) + 8, label[1])
            else:
                pdf.drawString(columns[0] + 6, y(row + 1) + 8, label)
            for column, value in enumerate(cells[1:]):
                pdf.drawString(columns[2 + column] + 6, y(row + 1) + 8, value)
        pdf.line(columns[-1], y(0), columns[-1], y(right_border_rows))
        pdf.drawString(57, 420, "표 아래의 문단이다.")

    return _korean_pdf(draw)


class TestShadedTableOpenAtASide:
    """The table of a disease in a press release: the right border is drawn on the header row only, and the label cells of
    the first column are shaded. It was found whole with its open end given as a border, and then, because it has shaded
    cells, searched again from the rules of the page alone: the cells of the last column and every value that spans to the
    open end were gone from the table, and their text came out as paragraphs under a table of empty cells."""

    _WHOLE = ("| 구분 | | 가형 | 나형 | 다형 |\n| --- | --- | --- | --- | --- |\n| 발생 | 국가 | 콩고 | 수단 | 우간다 |\n"
              "| 발생 | 동향 | 산발 유행 | 소규모 | 드묾 |\n| 병원체 | | 자이레형 | 수단형 | 분디부교형 |\n"
              "| 증상 | | 발열과 두통이 나타남 | | |\n| 치명률 | | 60% | 41% | 30% |\n| 치료 | 약 | 승인 치료제 | 없음 | 없는 상태 |\n"
              "| 치료 | 백신 | 어베보 | 미승인 | 미승인 상태 |\n| 잠복기 | | 2일에서 21일 | | |\n| 경로 | 동물 | 박쥐와 접촉 | | |\n"
              "| 경로 | 사람 | 체액에 접촉 | | |\n\n표 아래의 문단이다.")

    def test_last_column_and_the_values_that_span_to_the_open_end_stay_in_the_table(self):
        assert convert(_open_shaded_pdf(), "a.pdf").sections[0].text == self._WHOLE

    def test_same_table_without_shading_is_whole_as_before(self):
        """Without a shaded cell the table is not searched again."""
        assert convert(_open_shaded_pdf(shaded=False), "a.pdf").sections[0].text == self._WHOLE

    def test_same_table_closed_at_the_right_is_whole_as_before(self):
        assert convert(_open_shaded_pdf(right_border_rows=len(_DISEASE_ROWS)), "a.pdf").sections[0].text == self._WHOLE

    def test_table_without_any_right_border_is_whole_too(self):
        assert convert(_open_shaded_pdf(right_border_rows=0), "a.pdf").sections[0].text == self._WHOLE


    def test_table_found_with_and_without_its_open_side_is_kept_once(self):
        """The table is found twice where the ends of its rules stand a point from its borders: with its open side given
        as a border, and from the rules alone. Both grow into the whole table, and kept twice each was a box around the other:
        both were dropped and the text went to the search for tables without rules."""
        from io import BytesIO

        import pdfplumber

        from opendockit.extractors._pdf_model import _TABLE_SETTINGS
        from opendockit.extractors._pdf_open_tables import _open_sided_tables
        from opendockit.extractors._pdf_shaded_tables import _with_shaded_rows

        page = pdfplumber.open(BytesIO(_open_shaded_pdf())).pages[0]
        twice = [*_open_sided_tables(page), *page.find_tables(_TABLE_SETTINGS)]

        found = _with_shaded_rows(page, twice)

        assert len(twice) == 2 and len(found) == 1
        assert sum(cell is not None for cell in found[0].cells) > max(sum(cell is not None for cell in table.cells) for table in twice)


    @staticmethod
    def _grown(monkeypatch, second=None, closed: bool = False):
        """`_grown_shaded_table` of the built table found with its open side given, with the searches it makes counted;
        `second(first table, page)` gives what the second search finds in place of the real result."""
        from io import BytesIO

        import pdfplumber

        from opendockit.extractors import _pdf_shaded_tables as shaded
        from opendockit.extractors._pdf_open_tables import _open_sided_tables

        page = pdfplumber.open(BytesIO(_open_shaded_pdf(right_border_rows=len(_DISEASE_ROWS) if closed else 1))).pages[0]
        table = (page.find_tables(shaded._TABLE_SETTINGS) if closed else _open_sided_tables(page))[0]
        searches = []
        cropped = shaded._cropped

        class Region:
            def __init__(self, region):
                self._region = region

            def find_tables(self, settings):
                found = self._region.find_tables(settings)
                searches.append(found)
                return second(searches[0][0], page) if second is not None and len(searches) == 2 else found

        monkeypatch.setattr(shaded, "_cropped", lambda page, box: Region(cropped(page, box)))
        grown = shaded._grown_shaded_table(page, table, [table], shaded._horizontal_rules(page))
        return grown, searches

    def test_table_that_keeps_its_ends_is_searched_once(self, monkeypatch):
        """The same table with its right border drawn: found again from the rules, it has every cell, and that is the result."""
        grown, searches = self._grown(monkeypatch, closed=True)

        assert grown is not None and len(searches) == 1 and grown[0] is searches[0][0]

    def test_table_that_lost_its_ends_is_searched_again_with_them(self, monkeypatch):
        grown, searches = self._grown(monkeypatch)

        assert len(searches) == 2 and grown[0] is searches[1][0]
        assert sum(cell is not None for cell in grown[0].cells) > sum(cell is not None for cell in searches[0][0].cells)

    @staticmethod
    def _closed(page, *more: float) -> list:
        """The table of the page found with both its ends given as borders, and a border more at each of `more`."""
        return page.find_tables({"vertical_strategy": "lines", "horizontal_strategy": "lines",
                                 "explicit_vertical_lines": [56.7, 530.5, *more]})

    @pytest.mark.parametrize("second", [
        lambda first, page: [],
        lambda first, page: TestShadedTableOpenAtASide._closed(page) * 2,
        lambda first, page: TestShadedTableOpenAtASide._closed(page, 330.0),
        lambda first, page: page.find_tables({"vertical_strategy": "lines", "horizontal_strategy": "lines"}),
    ], ids=["nothing", "two-tables", "a-column-more", "ends-still-gone"])
    def test_second_search_is_taken_only_when_it_is_the_one_table_with_its_ends(self, monkeypatch, second):
        """Otherwise the table stays as the first search left it, as before."""
        grown, searches = self._grown(monkeypatch, second)

        assert len(searches) == 2 and grown[0] is searches[0][0]

    def test_tables_the_second_search_is_refused_for_would_hold_the_ends(self):
        """What makes the two cases above cases of their own: each is a table with every end cell."""
        from io import BytesIO

        import pdfplumber

        from opendockit.extractors._pdf_open_tables import _open_sided_tables
        from opendockit.extractors._pdf_shaded_tables import _column_edges, _end_cells, _holds_the_ends

        page = pdfplumber.open(BytesIO(_open_shaded_pdf())).pages[0]
        table = _open_sided_tables(page)[0]
        edges = _column_edges(table)
        ends = _end_cells([table], edges[0], edges[-1])

        assert len(self._closed(page)) == 1 and _holds_the_ends(self._closed(page)[0], ends)
        wider = self._closed(page, 330.0)
        assert len(wider) == 1 and _holds_the_ends(wider[0], ends) and len(_column_edges(wider[0])) == len(edges) + 1


class TestEndCells:
    """`_end_cells` and `_holds_the_ends`: the cells at the ends of a table, and whether a table found again still has them."""

    class _Table:
        def __init__(self, *cells):
            self.cells = list(cells)

    def test_cells_at_the_ends_are_listed_with_the_end_they_stand_at(self):
        from opendockit.extractors._pdf_shaded_tables import _end_cells

        table = self._Table((57, 100, 125, 120), (125, 100, 530, 120), None, (125, 120, 264, 140), (264, 120, 530.6, 140),
                            (59.5, 140, 264, 160), (264, 140, 527.5, 160))

        assert _end_cells([table], 57, 530) == [(57, 100, 120), (530, 100, 120), (530, 120, 140)]

    @pytest.mark.parametrize(("cells", "held"), [
        ([(57, 100, 125, 120), (397, 100, 530, 120)], True),
        ([(57, 100, 125, 120), (397, 100, 530, 109), (397, 109, 530, 120)], True),
        ([(57, 100, 125, 120), (264, 100, 397, 120)], False),
        ([(57, 100, 125, 120), (397, 60, 530, 105)], False),
        ([(57, 100, 125, 120), (397, 100, 531.4, 120)], True),
        ([(57, 100, 125, 120), (397, 100, 533, 120)], False),
        ([(57, 100, 125, 120), None, (397, 100, float("nan"), 120)], False),
    ], ids=["still-there", "split-into-two-rows", "a-column-short", "another-row-only", "a-point-off", "three-points-off",
            "no-number"])
    def test_table_found_again_holds_the_ends_or_not(self, cells, held):
        from opendockit.extractors._pdf_shaded_tables import _holds_the_ends

        assert _holds_the_ends(self._Table(*cells), [(57, 100, 120), (530, 100, 120)]) is held

    def test_end_that_is_no_number_is_not_held(self):
        from opendockit.extractors._pdf_shaded_tables import _holds_the_ends

        assert not _holds_the_ends(self._Table((57, 100, 530, 120)), [(float("inf"), 100, 120)])
        assert not _holds_the_ends(self._Table((57, 100, 530, 120)), [(530, float("nan"), 120)])
        assert _holds_the_ends(self._Table((57, 100, 530, 120)), [])
