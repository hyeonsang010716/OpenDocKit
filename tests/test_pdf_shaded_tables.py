"""Shaded PDF tables: striped and header-shaded tables whose cell boundaries come only from cell fill rectangles (filled
rectangles with no border)."""


import pytest

from opendockit import ConvertOptions, convert
from tests.test_pdf import _FONT, _korean_pdf


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

    `fills` is {row number: colour}, `rules` the row boundary numbers at which a table-wide horizontal line is drawn
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
        import time

        import opendockit.extractors._pdf_shaded_tables as shaded

        regridded, spent = shaded._regridded_fragments, [0.0]

        def timed(*args):
            started = time.perf_counter()
            try:
                return regridded(*args)
            finally:
                spent[0] += time.perf_counter() - started

        monkeypatch.setattr(shaded, "_regridded_fragments", timed)
        text = convert(_long_zebra_with_groups_pdf(400), "a.pdf", ConvertOptions(page_markers=False)).text

        assert "| G199 | 399-0 abcdef | 399-1 abcdef | 399-2 abcdef |" in text
        assert spent[0] < 1.5  # about 4 seconds before


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
