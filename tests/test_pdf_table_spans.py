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
        assert _spanned_text(_table(rows, merges, continued=True)).startswith("<table>")  # continued: spans kept
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


def _spanned_grid(pdf, rows: list[list[str]], top: float, widths: list[float], spans: list[tuple[int, int, int, int]],
                  height: float = 20) -> None:
    """A ruled table drawn downwards from `top` (rows of `height`, 10pt text), with cells merged over (column, row) to
    (column, row) as reportlab's SPAN takes them."""
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle

    from tests.test_pdf import _FONT

    table = Table(rows, colWidths=widths, rowHeights=height)
    table.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), _FONT, 10), ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                               *(("SPAN", (left, first), (right, last)) for left, first, right, last in spans)]))
    _, height = table.wrapOn(pdf, 0, 0)
    table.drawOn(pdf, 60, top - height)


_HEADER = ["ID", "요구사항명", "상세 내용", "우선순위"]
_WIDTHS = [90, 90, 200, 95]


def _continued(second: list[list[str]], spans: list[tuple[int, int, int, int]]) -> list[str]:
    """The pages of a two-page document: a ruled table of a header and one row at the foot of the first page, and its rows
    going on in a table of the same columns on the second, with merged cells. A table of fewer than three rows gives the
    next page no header (`_carry_table_headers`): the rows there are read as the table's own, continued."""
    from tests.test_pdf import _korean_pdf
    from tests.test_pdf_table_columns import _ruled_table

    def draw(pdf, index):
        if index == 0:
            _ruled_table(pdf, [_HEADER, ["REQ-001", "기능 1", "조회 화면에서 조건을 입력한다", "상"]], 80, _WIDTHS)
        else:
            _spanned_grid(pdf, second, 800, _WIDTHS, spans)

    return [section.text for section in convert(_korean_pdf(draw, pages=2), "a.pdf", ConvertOptions(page_markers=False)).sections]


class TestContinuedTables:
    def test_a_table_continued_with_the_same_columns_is_written_with_its_spans(self):
        second = [["REQ-100", "기능 100", "결과를 내려받을 수 있어야 한다", "중"], ["REQ-101", "기능 101 과 102 를 함께 처리", "", "하"],
                  ["REQ-103", "기능 103", "이력을 조회할 수 있어야 한다", "하"]]

        pages = _continued(second, [(1, 1, 2, 1)])

        assert pages[1] == ('<table>\n<tr><th>REQ-100</th><th>기능 100</th><th>결과를 내려받을 수 있어야 한다</th><th>중</th></tr>\n'
                            '<tr><td>REQ-101</td><td colspan="2">기능 101 과 102 를 함께 처리</td><td>하</td></tr>\n'
                            "<tr><td>REQ-103</td><td>기능 103</td><td>이력을 조회할 수 있어야 한다</td><td>하</td></tr>\n</table>")

    def test_the_piece_of_a_row_split_at_the_page_end_is_no_title(self):
        """The top row of a table continued from the previous page is a title only when it is a merged cell over the other
        columns: the piece of a row split at the end of the page, one cell with text among empty ones, became a
        paragraph above the table, as it did in Markdown before `merged_titles_only`."""
        second = [["", "", "있어야 한다.", ""], ["REQ-100", "기능 100 과 101 을 함께 처리", "", "중"],
                  ["REQ-102", "기능 102", "이력을 조회할 수 있어야 한다", "하"]]

        pages = _continued(second, [(1, 1, 2, 1)])

        assert pages[1].startswith("<table>\n<tr><th></th><th></th><th>있어야 한다.</th><th></th></tr>\n")

    def test_a_merged_top_row_of_a_continued_table_is_still_a_title(self):
        second = [["요구사항 목록 (계속)", "", "", ""], ["REQ-100", "기능 100 과 101 을 함께 처리", "", "중"],
                  ["REQ-102", "기능 102", "이력을 조회할 수 있어야 한다", "하"]]

        pages = _continued(second, [(0, 0, 3, 0), (1, 1, 2, 1)])

        assert pages[1].startswith("요구사항 목록 (계속)\n\n<table>\n<tr><th>REQ-100</th>")

    def test_the_rule_of_a_merged_title(self):
        from opendockit.extractors._pdf_ruled_tables import _merged_title

        rows = [["제목", "", "", ""], ["a", "b", "c", "d"]]
        assert _merged_title(rows, ((0, 0, 1, 4),))
        assert not _merged_title(rows, ((0, 0, 1, 3),))  # the last column is an empty cell of its own
        assert _merged_title([["제목", "", "", ""], ["a", "b", "c", ""]], ((0, 0, 1, 3),))  # a column without text
        assert not _merged_title([["제목", "", "끝", ""], ["a", "b", "c", "d"]], ((0, 0, 1, 2),))  # two cells with text
        assert not _merged_title([["", "", "", ""], ["a", "b", "c", "d"]], ())
        assert not _merged_title([["A", "", "B", ""], ["a", "b", "c", "d"]], ((0, 0, 1, 2), (0, 2, 1, 2)))
        assert not _merged_title([["조각", "", "", ""], ["x", "", "", ""], ["p", "q", "", ""]], ((1, 0, 1, 2),))  # another row's
        assert not _merged_title([["제목", "", "", ""], ["a", "", "c", ""]], ((0, 0, 1, 2), (0, 2, 1, 2)))  # a start

    def test_under_a_merged_title_no_row_is_a_title(self):
        """Markdown takes no row under the title of a continued table for a title (`merged_titles_only`): a unit row
        merged over every column stays the table's first row."""
        rows = [["요구사항 목록 (계속)", "", "", ""], ["단위: 건", "", "", ""], ["REQ-1", "기능 1 과 2", "", "상"], ["REQ-3", "기능 3", "x", "하"]]
        merges = ((0, 0, 1, 4), (1, 0, 1, 4), (2, 1, 1, 2))
        table = _Table(top=0.0, text="as before", bottom=40.0, x0=0.0, x1=40.0, rows=rows, continued=True, merges=merges,
                       spans=((0.0, 10.0), (10.0, 20.0), (20.0, 30.0), (30.0, 40.0)),
                       bands=tuple((10.0 * row, 10.0 * row + 10.0) for row in range(4)))

        assert _spanned_text(table).startswith('요구사항 목록 (계속)\n\n<table>\n<tr><th colspan="4">단위: 건</th></tr>')

    def test_a_table_not_continued_takes_its_titles_as_any_html_table(self):
        """A title with a unit note at the top of a table that is not continued is taken out above it, as for any table."""
        rows = [["2024년 매출 현황", "", "", "(단위: 천원)"], ["구분", "1분기", "", "합계"], ["매출", "10", "20", "30"]]
        table = _Table(top=0.0, text="as before", bottom=30.0, x0=0.0, x1=40.0, rows=rows, merges=((1, 1, 1, 2),),
                       spans=((0.0, 10.0), (10.0, 20.0), (20.0, 30.0), (30.0, 40.0)),
                       bands=tuple((10.0 * row, 10.0 * row + 10.0) for row in range(3)))

        assert _spanned_text(table).startswith("2024년 매출 현황 (단위: 천원)\n\n<table>")


class TestPicturesInMergedCells:
    def test_a_picture_in_a_cell_over_rows_keeps_the_spans(self):
        """The picture goes into the first place of the cell it lies in (`_place_images_in_cells`), and the rows under it
        hold the copy of its text from before: taken for text of their own, the table was written without its spans."""
        from tests.test_pdf import _korean_pdf
        from tests.test_pdf_images import _image

        rows = [["구분", "제품", "", "단가"], ["세트", "마우스", "무선", "25,000"], ["", "키보드", "유선", "89,000"],
                ["단품", "모니터", "27인치", "320,000"]]

        def draw(pdf, index):
            _spanned_grid(pdf, rows, 800, [90, 90, 90, 90], [(1, 0, 2, 0), (0, 1, 0, 2)], height=30)
            pdf.drawImage(_image(120, 120, seed=1), 82, 714, 45, 45)  # inside the cell over two rows

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert len(result.images) == 1
        assert result.text == ('<table>\n<tr><th>구분</th><th colspan="2">제품</th><th>단가</th></tr>\n'
                               '<tr><td rowspan="2">[Image]<br>세트</td><td>마우스</td><td>무선</td><td>25,000</td></tr>\n'
                               "<tr><td>키보드</td><td>유선</td><td>89,000</td></tr>\n"
                               "<tr><td>단품</td><td>모니터</td><td>27인치</td><td>320,000</td></tr>\n</table>")

    def test_a_copy_is_the_text_without_its_picture_lines_only(self):
        from opendockit.extractors._pdf_ruled_tables import _without_pictures
        from opendockit.models import image_marker

        assert _without_pictures(f"{image_marker(0)}\n세트") == "세트"
        assert _without_pictures(f"세트 {image_marker(0)}") == f"세트 {image_marker(0)}"  # not a line of its own
        assert _without_pictures(f"{image_marker(0)} 세트") == f"{image_marker(0)} 세트"
        assert _without_pictures(f"세트\n{image_marker(0)}\n상품") == "세트\n상품"  # the lines of the text stay lines
        rows = [[f"세트\n{image_marker(0)}", "", "값"], ["세트", "", "1"], ["나", "다", "2"]]
        assert _spanned_text(_table(rows, ((0, 0, 2, 2),))).startswith("<table>")
        assert _spanned_text(_table([rows[0], ["세트 외", "", "1"], rows[2]], ((0, 0, 2, 2),))) == "as before"
