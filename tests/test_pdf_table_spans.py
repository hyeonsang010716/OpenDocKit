"""Ruled PDF tables written with the spans of their merged cells (`_pdf_ruled_tables._merged_cells`, `_spanned_text`)."""

import math

from opendockit import ConvertOptions, convert
from opendockit.extractors._pdf_model import _Table
from opendockit.extractors._pdf_ruled_tables import _merged_cells, _spanned_text
from opendockit.markdown import TableGrid, render_table, restore_spans, span_budget
from opendockit.models import image_marker
from tests.test_pdf import _korean_pdf
from tests.test_pdf_table_columns import _merged_cells_pdf


_XS = [60, 140, 260, 360, 460]


def _text(cells: list[tuple[int, int, int, int, str]], rows: int, xs: list[float] = _XS) -> str:
    return convert(_merged_cells_pdf(xs, cells, rows), "a.pdf", ConvertOptions(page_markers=False)).text


class TestMergedCells:
    def test_cells_over_several_columns_and_rows_are_written_with_their_spans(self):
        """pdfplumber states each cell box, and the places a merged cell hides were written as empty cells: '구분' stood in
        the first of its two columns, '합계' too, and nothing said which columns they cover. DOCX, HWPX, ODF and HTML write
        such a table as HTML with its spans, and so does the PDF reader now."""
        cells = [(0, 0, 1, 2, "구분"), (0, 2, 1, 1, "2024년"), (0, 3, 1, 1, "2025년"),
                 (1, 0, 2, 1, "자산"), (1, 1, 1, 1, "유동"), (1, 2, 1, 1, "100"), (1, 3, 1, 1, "110"),
                 (2, 1, 1, 1, "비유동"), (2, 2, 1, 1, "300"), (2, 3, 1, 1, "320"),
                 (3, 0, 1, 2, "합계"), (3, 2, 1, 2, "830")]

        assert _text(cells, 4) == ('<table>\n<tr><th colspan="2">구분</th><th>2024년</th><th>2025년</th></tr>\n'
                                   '<tr><td rowspan="2">자산</td><td>유동</td><td>100</td><td>110</td></tr>\n'
                                   "<tr><td>비유동</td><td>300</td><td>320</td></tr>\n"
                                   '<tr><td colspan="2">합계</td><td colspan="2">830</td></tr>\n</table>\n\n표 아래 본문입니다.')

    def test_a_table_merged_only_down_its_rows_is_written_as_before(self):
        """Without a cell over several columns the table stays Markdown, a merged value repeated on every row it covers."""
        cells = [(0, 0, 1, 1, "구분"), (0, 1, 1, 1, "항목"), (0, 2, 1, 1, "금액"),
                 (1, 0, 2, 1, "수입"), (1, 1, 1, 1, "매출"), (1, 2, 1, 1, "100"), (2, 1, 1, 1, "이자"), (2, 2, 1, 1, "5")]

        assert _text(cells, 3, _XS[:4]) == ("| 구분 | 항목 | 금액 |\n| --- | --- | --- |\n| 수입 | 매출 | 100 |\n| 수입 | 이자 | 5 |\n\n"
                                            "표 아래 본문입니다.")

    def test_a_title_over_every_column_alone_leaves_the_table_as_before(self):
        """A title row over all columns is taken out above the table, and no other cell covers more than one column: the
        table is Markdown as it was."""
        cells = [(0, 0, 1, 3, "사업비 내역"), (1, 0, 1, 1, "항목"), (1, 1, 1, 1, "금액"), (1, 2, 1, 1, "비고"),
                 (2, 0, 1, 1, "인건비"), (2, 1, 1, 1, "100"), (2, 2, 1, 1, "-")]

        assert _text(cells, 3, _XS[:4]) == ("사업비 내역\n\n| 항목 | 금액 | 비고 |\n| --- | --- | --- |\n| 인건비 | 100 | - |\n\n"
                                            "표 아래 본문입니다.")


class TestHeaderCarriedOver:
    def test_a_header_put_on_the_rows_clears_their_spans(self):
        """A borderless table at the foot of a page goes on in a ruled table of the same columns on the next: the header of
        the first is put on top of the second (`_carry_table_headers`), and the spans found in its rows stood one row too
        high — '합계' lost its two columns and '가' was written over them."""
        xs = [50, 150, 250, 350, 450]

        def draw(pdf, index):
            pdf.setLineWidth(0.6)
            if index == 0:
                pdf.drawString(60, 780, "본문 첫 줄입니다.")
                rows = [["구분", "항목", "2024년", "2025년"], ["갑", "을", "10", "20"], ["병", "정", "30", "40"], ["무", "기", "50", "60"]]
                for number, row in enumerate(rows):
                    for x, text in zip(xs, row, strict=False):
                        pdf.drawString(x + 10, 160 - 18 * number, text)
                return
            ys = [790, 770, 750]
            for row, column, height, width, text in [(0, 0, 1, 1, "가"), (0, 1, 1, 1, ""), (0, 2, 1, 1, "1"), (0, 3, 1, 1, "2"),
                                                     (1, 0, 1, 2, "합계"), (1, 2, 1, 1, "3"), (1, 3, 1, 1, "4")]:
                pdf.rect(xs[column], ys[row + height], xs[column + width] - xs[column], ys[row] - ys[row + height])
                if text:
                    pdf.drawString(xs[column] + 5, (ys[row] + ys[row + height]) / 2 - 4, text)
            pdf.drawString(60, 700, "표 아래 본문입니다.")

        text = convert(_korean_pdf(draw, pages=2), "a.pdf", ConvertOptions(page_markers=False)).text

        assert "| 구분 | 항목 | 2024년 | 2025년 |\n| --- | --- | --- | --- |\n| 가 | | 1 | 2 |\n| 합계 | | 3 | 4 |" in text
        assert "colspan" not in text


def _box(row: int, column: int, rows: int = 1, columns: int = 1) -> tuple[float, float, float, float]:
    """The box of a cell in a grid of 10pt squares from the origin."""
    return (column * 10.0, row * 10.0, (column + columns) * 10.0, (row + rows) * 10.0)


_SPANS = ((0.0, 10.0), (10.0, 20.0), (20.0, 30.0))
_BANDS = ((0.0, 10.0), (10.0, 20.0), (20.0, 30.0))


class TestMergedCellsFromTheBoxes:
    def test_each_box_over_more_than_one_place_is_one_merge(self):
        boxes = (_box(0, 0, 1, 2), _box(0, 2), _box(1, 0, 2), _box(1, 1), _box(1, 2), _box(2, 1, 1, 2))
        rows = [["a", "", "b"], ["c", "d", "e"], ["c", "f", ""]]

        assert _merged_cells(rows, boxes, _SPANS, _BANDS) == ((0, 0, 1, 2), (1, 0, 2, 1), (2, 1, 1, 2))

    def test_merges_only_down_the_rows_are_none(self):
        boxes = (_box(0, 0), _box(0, 1), _box(0, 2), _box(1, 0, 2), _box(1, 1), _box(1, 2), _box(2, 1), _box(2, 2))
        rows = [["a", "b", "c"], ["d", "e", "f"], ["d", "g", "h"]]

        assert _merged_cells(rows, boxes, _SPANS, _BANDS) == ()

    def test_a_box_over_another_is_no_merge(self):
        """A cell that holds the cells of a table inside it lies over them: written with its span, the cell placed first
        hid the others and their text was lost (a title block of a drawing). Such a table is written as before."""
        boxes = (_box(0, 0, 3, 3), _box(1, 1), _box(1, 2), _box(2, 1), _box(2, 2))
        rows = [["a", "", ""], ["", "b", "c"], ["", "d", "e"]]

        assert _merged_cells(rows, boxes, _SPANS, _BANDS) == ()

    def test_rows_that_are_not_the_rows_of_the_bands_are_none(self):
        """A two-level header merged into one row leaves one row fewer than the bands."""
        boxes = (_box(0, 0, 1, 2), _box(0, 2), _box(1, 0), _box(1, 1), _box(1, 2), _box(2, 0), _box(2, 1), _box(2, 2))
        rows = [["a b", "a c", "d"], ["e", "f", "g"]]

        assert _merged_cells(rows, boxes, _SPANS, _BANDS) == ()
        assert _merged_cells([["a", "", "b"], ["c", "d"], ["e", "f", "g"]], boxes, _SPANS, _BANDS) == ()

    def test_a_box_over_no_middle_or_of_no_number_is_none(self):
        rows = [["a", "", "b"], ["c", "d", "e"], ["f", "g", "h"]]
        thin = (12.0, 0.0, 14.0, 10.0)  # between two middles
        for odd in (thin, (math.nan, 0.0, 20.0, 10.0)):
            boxes = (_box(0, 0, 1, 2), odd, _box(1, 0), _box(1, 1), _box(1, 2), _box(2, 0), _box(2, 1), _box(2, 2))
            assert _merged_cells(rows, boxes, _SPANS, _BANDS) == ()
        assert _merged_cells(rows, (_box(0, 0, 1, 2),), _SPANS, ((0.0, 10.0), (10.0, math.inf), (math.inf, math.inf))) == ()

    def test_a_box_whose_edge_lies_on_a_middle_does_not_cover_that_row_or_column(self):
        """A place is covered when its middle lies strictly inside the box."""
        rows = [["a", "", "b"], ["c", "d", "e"], ["f", "g", "h"]]
        # from the middle of row 0 to the middle of row 2: that is row 1 alone
        boxes = (_box(0, 0, 1, 2), _box(0, 2), (0.0, 5.0, 10.0, 25.0), _box(1, 1), _box(1, 2), _box(2, 1), _box(2, 2))
        assert _merged_cells(rows, boxes, _SPANS, _BANDS) == ((0, 0, 1, 2),)
        # from the middle of column 0 to that of column 2: column 1 alone
        boxes = ((5.0, 0.0, 25.0, 10.0), _box(0, 0), _box(0, 2), _box(1, 0, 1, 2), _box(1, 2), _box(2, 0), _box(2, 1), _box(2, 2))
        assert _merged_cells(rows, boxes, _SPANS, _BANDS) == ((1, 0, 1, 2),)


def _table(rows: list[list[str]], merges: tuple[tuple[int, int, int, int], ...], **fields: object) -> _Table:
    fields.setdefault("bands", tuple((10.0 * row, 10.0 * row + 10.0) for row in range(len(rows))))
    return _Table(top=0.0, text="as before", bottom=30.0, x0=0.0, x1=30.0, rows=rows, spans=_SPANS[:len(rows[0])],
                  merges=merges, **fields)


class TestSpannedText:
    def test_the_table_is_written_with_its_spans_and_its_text_is_left_as_it_was(self):
        """`text` is what the page's characters are counted from (`pdf._read_chars`): it stays the table without spans."""
        table = _table([["구분", "", "값"], ["가", "나", "1"], ["가", "다", "2"]], ((0, 0, 1, 2), (1, 0, 2, 1)))

        assert _spanned_text(table) == ('<table>\n<tr><th colspan="2">구분</th><th>값</th></tr>\n'
                                        '<tr><td rowspan="2">가</td><td>나</td><td>1</td></tr>\n<tr><td>다</td><td>2</td></tr>\n</table>')
        assert table.text == "as before"

    def test_text_in_a_place_a_merged_cell_covers_leaves_the_table_as_before(self):
        """Written with the span, the text there would be hidden."""
        assert _spanned_text(_table([["구분", "숨은 값", "값"], ["가", "나", "1"]], ((0, 0, 1, 2),))) == "as before"
        # down the rows, a copy of the merged cell's text in its first column is its own; any other text is not
        last = ["나", "다", "2"]  # a row that keeps the second column, so the span is still over two columns
        assert _spanned_text(_table([["가", "", "값"], ["가", "", "1"], last], ((0, 0, 2, 2),))).startswith("<table>")
        assert _spanned_text(_table([["가", "", "값"], ["나", "", "1"], last], ((0, 0, 2, 2),))) == "as before"
        assert _spanned_text(_table([["가", "", "값"], ["", "가", "1"], last], ((0, 0, 2, 2),))) == "as before"

    def test_a_span_over_columns_that_hold_no_text_leaves_the_table_as_before(self):
        """With the empty columns left out the cell covers one column: there is no span to write."""
        assert _spanned_text(_table([["가", "", "값"], ["나", "", "1"]], ((0, 0, 1, 2),))) == "as before"

    def test_rows_written_again_after_the_search_leave_the_table_as_before(self):
        rows = [["구분", "", "값"], ["가", "나", "1"]]
        merges = ((0, 0, 1, 2),)
        assert _spanned_text(_table(rows, merges)).startswith("<table>")
        assert _spanned_text(_table(rows, merges, continued=True)) == "as before"
        assert _spanned_text(_table(rows, merges, from_the_right=True)) == "as before"
        assert _spanned_text(_table([*rows, ["다", "라", "2", "3"]], merges)) == "as before"  # a row of another width
        assert _spanned_text(_table(rows, ())) == "as before"
        # rows no longer as many as the bands they were found with, or a band of no position (a header carried over)
        assert _spanned_text(_table(rows, merges, bands=((0.0, 10.0),))) == "as before"
        assert _spanned_text(_table(rows, merges, bands=((-math.inf, -math.inf), (0.0, 10.0)))) == "as before"

    def test_a_merge_past_the_rows_or_columns_leaves_the_table_as_before(self):
        rows = [["구분", "", "값"], ["가", "나", ""]]
        assert _spanned_text(_table(rows, ((0, 0, 1, 2),))).startswith("<table>")
        assert _spanned_text(_table(rows, ((0, 0, 1, 2), (1, 1, 2, 2)))) == "as before"
        assert _spanned_text(_table(rows, ((0, 0, 1, 2), (1, 1, 1, 3)))) == "as before"

    def test_white_space_in_a_place_a_merged_cell_covers_is_no_text(self):
        assert _spanned_text(_table([["구분", " ", "값"], ["가", "나", "1"]], ((0, 0, 1, 2),))).startswith("<table>")

    def test_rows_the_grid_budget_would_leave_out_leave_the_table_as_before(self):
        """The drawing charges the conversion's grid budget; past it the bottom rows are left out, which `text` keeps."""
        rows = [["A", "", "B", "C"], ["x", "y", "", ""], *([f"w{row}" if column == row % 4 else "" for column in range(4)]
                                                          for row in range(8))]
        table = _Table(top=0.0, text=render_table(rows), bottom=100.0, x0=0.0, x1=40.0, rows=rows,
                       spans=((0.0, 10.0), (10.0, 20.0), (20.0, 30.0), (30.0, 40.0)),
                       bands=tuple((10.0 * row, 10.0 * row + 10.0) for row in range(len(rows))), merges=((0, 0, 1, 2),))
        assert _spanned_text(table).startswith("<table>")
        with span_budget():
            restore_spans((1_000_000, 5))
            assert _spanned_text(table) == table.text

    def test_a_picture_in_a_merged_cell_stays_in_it_once(self):
        """A picture is put in the first place of the cell it lies in (`_place_images_in_cells`): written with the span,
        it is in the cell once, as in the other formats."""
        placeholder = image_marker(0)
        rows = [[f"로고\n{placeholder}", "", "값"], ["가", "나", "1"]]

        text = _spanned_text(_table(rows, ((0, 0, 1, 2),)))

        assert text.count(placeholder) == 1 and '<th colspan="2">로고<br>' in text


class TestSpanningHtml:
    def test_html_only_while_a_cell_spans_columns(self):
        grid = TableGrid()
        grid.place(0, 0, "a", col_span=2)
        grid.place(1, 0, "b")
        grid.place(1, 1, "c")
        assert grid.spanning_html() == grid.render() == '<table>\n<tr><th colspan="2">a</th></tr>\n<tr><td>b</td><td>c</td></tr>\n</table>'

        down = TableGrid()
        down.place(0, 0, "a", row_span=2)
        down.place(0, 1, "b")
        down.place(1, 1, "c")
        assert down.spanning_html() is None and down.render().startswith("| a | b |")
