"""Tables drawn with ruling lines or shading: which found tables are real, cells and merged cells, shaded rows without
rules, tables open at the sides, text wrapped inside a cell, and the highlights and glyph outlines that look like rules.
"""

from bisect import bisect_left, bisect_right, insort
from collections import Counter
import math

from pdfplumber.table import TableFinder, TableSettings

from opendockit.extractors._pdf_cell_text import _cell_rows, _text_outside
from opendockit.extractors._pdf_chars import _drawn_text
from opendockit.extractors._pdf_clip_tables import _clip_tables
from opendockit.extractors._pdf_model import (
    _CAPTION_LABEL,
    _CHECKBOX_SIDE,
    _TABLE_ROW_TOLERANCE,
    _TABLE_SETTINGS,
    _BoxIndex,
    _cropped,
    _encloses,
    _inside_any,
    _page_index,
    _PointGrid,
    _region_on_page,
    _Table,
    _text_by_middle,
)
from opendockit.extractors._pdf_open_tables import _merge_split_header, _open_sided_tables
from opendockit.extractors._pdf_pdfminer import _INFLATE_BUDGET
from opendockit.extractors._pdf_shaded_tables import _column_edges, _is_inner_part, _mark_filled_cells, _rule_line, _with_shaded_rows
from opendockit.extractors._pdf_table_filters import _dropping_page_indexes
from opendockit.markdown import TableGrid, render_table
from opendockit.models import IMAGE_MARKER


# Share of the page area for a table to count as a page frame. In slide PDFs the whole page is often a frame box. A table this
# large is dropped as a frame (layout box) only when it has at most _FRAME_MAX_ROWS rows or a single column. When only the area was
# considered, a requirements table filling the page (29 rows × 6 columns) was dropped and its cell text scattered column by column
# (Pinned by: test_pdf.py::TestRuledTables::
# test_grid_filling_the_whole_page_is_a_table_not_a_page_frame, test_page_frame_split_into_two_boxes_is_still_layout).
_PAGE_FRAME_RATIO = 0.8
# In an outer table taken as a form that contains tables within a table, the maximum text length of the label cell in the same row
# as the cell holding the inner table (`_with_inner_tables`).
_NESTED_LABEL_MAX = 20


_FRAME_MAX_ROWS = 2


# Most crossings of horizontal and vertical ruling lines (after pdfplumber's snapping) and most pairs of them a page may have for
# its ruled tables to be looked for (`_too_many_rules`). A table of R rows × C columns has (R + 1) × (C + 1) crossings, summed
# over the tables of the page. The table finder's time grows with the square of the crossings and with how the cells are
# merged: just under the bound an empty 91 × 91 grid takes about 1 second, a filled sheet 2-3 seconds, and the costliest pages
# found (every cell merged over two rows, 1,400 rows of five cells on one page, 280 separate tables) 5-7 seconds; at 12,000
# crossings those take 8-13 seconds and an empty 150 × 150 grid (22,801) 14 seconds. The bound is the lowest round number
# that keeps the sheets seen in practice: a Gantt chart of 100 tasks × 52 weeks has 5,406 crossings and a year planner of
# 20 people × 366 days 8,096 — at 5,000 they lost their tables. A page of 800 small separate tables has 7,200 crossings and
# 5.8 million pairs (Pinned by: test_pdf_resource_limits.py::TestTooManyRules).
_MAX_RULE_CROSSINGS = 8_500
_MAX_RULE_PAIRS = 4_000_000


# Number of rows that must continue with the same boundaries in each group when splitting touching ruled tables at column
# boundaries (`_split_column_sections`).
_SECTION_MIN_ROWS = 2


# Width (pt, `_place_images_in_cells`) a picture may spill outside the cell box and still count as inside a ruled table cell. About
# the cell border thickness plus rounding.
_CELL_IMAGE_SLACK = 2.0


def _column_spans(cells: list[tuple], columns: int) -> tuple[tuple[float, float], ...]:
    """Horizontal range of each column, split at the vertical boundaries of a ruled table's cell boxes (`cells`). An empty tuple if
    the number of boundaries does not match the number of columns (`columns`)."""
    edges: list[float] = []
    for x in sorted(edge for cell in cells if cell for edge in (cell[0], cell[2])):
        if not edges or x - edges[-1] > 1.0:
            edges.append(x)
    spans = tuple(zip(edges, edges[1:], strict=False))
    return spans if len(spans) == columns else ()


class _TableLookups:
    """What `_ruled_table` and `_with_open_below` look up on the page for each table: the non-blank characters by the grid cell
    of their centre and the pictures by the grid cell of their top-left corner (`_PointGrid`), and the horizontal edges by
    their top, each built once per page the first time a table asks. Every cell holding a picture scanned all the characters
    of the page, and every cell all the pictures: a ruled grid with a picture and a label in each of 3,600 cells took 30
    seconds (Pinned by: test_pdf_images.py::TestManyPicturesInRuledCells). Every column of every table listed the page's
    horizontal edges again (pdfplumber filters them at every access of `page.horizontal_edges`) (Pinned by:
    test_pdf_shaded_tables.py::TestManySeparateShadedTables::test_open_columns_are_those_of_scanning_the_edges)."""

    def __init__(self, page: object, figures: list[tuple[float, float, float, float, float]]) -> None:
        self._page, self._figures = page, figures
        self._char_grid: _PointGrid | None = None
        self._figure_grid: _PointGrid | None = None
        self._edges: tuple[list[float], list[tuple[int, dict]]] | None = None

    def holds_figure(self, box: tuple[float, float, float, float]) -> bool:
        """Whether a picture lies in the cell box (`_in_cell`). A picture whose sides are turned inside out is always tested:
        only for the others does lying in the cell put the corner in the cell widened by `_CELL_IMAGE_SLACK`."""
        if self._figure_grid is None:
            self._figure_grid = _PointGrid(list(self._figures), lambda figure: (
                (figure[0], figure[1]) if figure[0] <= figure[2] and figure[1] <= figure[3] else (math.nan, math.nan)))
        near = self._figure_grid.within(box[0] - _CELL_IMAGE_SLACK, box[1] - _CELL_IMAGE_SLACK, box[2] + _CELL_IMAGE_SLACK,
                                        box[3] + _CELL_IMAGE_SLACK)
        return any(_in_cell(figure[:4], box) for figure in near)

    def tops(self, box: tuple[float, float, float, float]) -> list[float]:
        """The top edges of the non-blank characters whose centre lies in the cell box (`_inside_any`), sorted."""
        if self._char_grid is None:
            self._char_grid = _PointGrid([char for char in self._page.chars if char["text"].strip()], lambda char: (
                (char["x0"] + char["x1"]) / 2, (char["top"] + char["bottom"]) / 2))
        return sorted(char["top"] for char in self._char_grid.within(*box[:4]) if _inside_any(char, [box]))

    def edges_at(self, y: float) -> list[dict]:
        """The page's horizontal edges whose top lies within 1.5pt of `y`, in page order. Those are found among the edges
        with a finite top within 4pt of it: an edge that is no number away passes no such test."""
        if self._edges is None:
            ordered = sorted(((edge["top"], position, edge) for position, edge in enumerate(self._page.horizontal_edges)
                              if math.isfinite(edge["top"])), key=lambda item: item[0])
            self._edges = [top for top, _, _ in ordered], [(position, edge) for _, position, edge in ordered]
        tops, edges = self._edges
        near = edges[bisect_left(tops, y - 4.0):bisect_right(tops, y + 4.0)] if math.isfinite(y) else []
        return [edge for _, edge in sorted(near, key=lambda item: item[0]) if abs(edge["top"] - y) <= 1.5]


def _ruled_table(bbox: tuple[float, float, float, float], rows: list[list[str | None]], boxes: list[tuple],
                 text: _TableLookups, header: tuple[list[str], list[str]] | None = None) -> _Table:
    """Turns one ruled table (a result of `_real_tables`) into a `_Table`. For a cell holding a picture the line heights of the cell
    text are stored as well (`_place_images_in_cells`), from the page's characters and pictures in `text`. `header` holds
    the two rows of a two-level header merged into the top row of `rows` (`_merge_split_header`), as they were found."""
    cells = [[cell and _drawn_text(cell) for cell in row] for row in rows]
    boxes = tuple(box for box in boxes if box)
    cell_lines = {}

    for index, box in enumerate(boxes):
        if text.holds_figure(box):
            tops: list[float] = []

            for top in text.tops(box):
                if not tops or top - tops[-1] > _TABLE_ROW_TOLERANCE:
                    tops.append(top)

            cell_lines[index] = tuple(tops)
    spans, bands = _column_spans(boxes, len(cells[0])), _row_bands(boxes, len(cells))
    merges, unmerged = _merged_cells(cells, boxes, spans, bands), ()
    if header is not None and not merges:
        unmerged = (tuple(cells[0]), *(tuple(_drawn_text(cell) for cell in row) for row in header))
        whole = [list(unmerged[1]), list(unmerged[2]), *cells[1:]]
        merges = _merged_cells(whole, boxes, spans, _row_bands(boxes, len(whole)))
    return _Table(top=bbox[1], text=render_table(cells), bottom=bbox[3], x0=bbox[0], x1=bbox[2], rows=cells, spans=spans,
                  cells=boxes, bands=bands, cell_lines=cell_lines, merges=merges, unmerged_header=unmerged)


def _merged_cells(rows: list[list[str]], boxes: tuple[tuple, ...], spans: tuple[tuple[float, float], ...],
                  bands: tuple[tuple[float, float], ...]) -> tuple[tuple[int, int, int, int], ...]:
    """The cell boxes of a ruled table that cover more than one row or column, as (row, column, rows, columns) in `rows`:
    a box covers the rows and columns whose middle lies strictly inside it. Nothing when no box covers more than one
    column, and nothing when a place is covered by two boxes (a cell that holds the cells of a table inside it, a box laid
    over others), when a box covers no row or column, or when the rows are not the rows of the bands (a two-level
    header merged into one row, `_merge_split_header`, which `_ruled_table` searches again in the two rows as they were
    found; a row of a cell holding a table left out, `_cell_rows`). A place
    no box covers is written as a cell of its own.

    pdfplumber states each cell box and gives None for the places a merged cell hides, and those were written as empty
    cells: the text of a cell over three columns stood in the column its middle falls in, between empty ones, and no reader
    could tell which columns it covers ('| 15:00~15:05 | | 오프닝 영상 | |'), where DOCX, HWPX, ODF and HTML write such a
    table as HTML with its spans (Pinned by: test_pdf_table_spans.py::TestMergedCells). The places are found by bisection
    over the middles, and a place is visited once: a box over a place already taken ends the search."""
    if not spans or not bands or len(bands) != len(rows) or any(len(row) != len(spans) for row in rows):
        return ()
    row_middles = [(top + bottom) / 2 for top, bottom in bands]
    column_middles = [(left + right) / 2 for left, right in spans]
    if not all(map(math.isfinite, [*row_middles, *column_middles])):
        return ()
    taken: set[tuple[int, int]] = set()
    merged: list[tuple[int, int, int, int]] = []
    for x0, top, x1, bottom in boxes:
        first_row, end_row = bisect_right(row_middles, top), bisect_left(row_middles, bottom)
        first_column, end_column = bisect_right(column_middles, x0), bisect_left(column_middles, x1)
        if first_row >= end_row or first_column >= end_column:
            return ()  # a box over no middle, or one that is no number
        for row in range(first_row, end_row):
            for column in range(first_column, end_column):
                if (row, column) in taken:
                    return ()
                taken.add((row, column))
        if end_row - first_row > 1 or end_column - first_column > 1:
            merged.append((first_row, first_column, end_row - first_row, end_column - first_column))
    return tuple(merged) if any(columns > 1 for _, _, _, columns in merged) else ()


def _spanned_text(table: _Table) -> str:
    """The text of the table as it is written: with the spans of its merged cells (`merges`) when one still covers more
    than one column once the empty rows and columns are left out — HTML, as `TableGrid` writes a table of any format with
    such a cell — and otherwise `text`, the table as it was written before.

    The spans are kept only while the rows are the rows they were found in: same count, same width, and every place a
    merged cell covers empty or, in the first column of a cell over several rows, the copy of its text `_cell_rows` put
    there (its text from before a picture was put in its first place, `_without_pictures`). Any other text there would
    be hidden by the span, so the table is written as before; and so it is when a step after the search wrote the rows
    again — a header carried over to a table continued from the previous page, which
    clears `merges`, one read from the right (`from_the_right`) — and when the rows are no longer as many as the bands
    found with them. A table continued from the previous page with the same columns (`continued`) keeps its spans: the
    text of an open cell carried into its top rows (`_carry_open_cells`) is, where a cell over rows covers them, that
    cell's copy, which the check above takes, and its top row is a title only by the rule its Markdown follows
    (`_merged_title`). A table read from the tags of a tagged PDF (`stated`) has the spans its tree states
    (`_pdf_tagged_tables._trusted_cells`), and no cell boxes or bands to hold them to: its rows are as wide as each other
    as they were built. The table is written as before too when the drawing would leave out rows for the
    conversion's grid budget (`TableGrid.omitted_rows`): `text` holds them, and nothing would say they are gone. A
    table whose two-level header was merged into one row (`unmerged_header`) is written from the two rows as they were
    found, while its top row is still the one merged and it is not continued from the previous page: '문의 1월 | 문의 2월'
    stood for a header the other formats write with '문의' over the two columns of '1월' and '2월', and the merged row
    stays its Markdown (Pinned by: test_pdf_table_spans.py::TestTwoLevelHeaders). Known accepted gaps: those tables,
    and borderless tables, keep the place of a merged cell's text in the column its middle falls in."""
    rows, bands = table.rows, table.bands
    if not table.merges or table.from_the_right or not rows:
        return table.text
    if table.unmerged_header:
        if table.continued or tuple(rows[0]) != table.unmerged_header[0]:
            return table.text
        rows = [list(row) for row in table.unmerged_header[1:]] + rows[1:]
        bands = _row_bands(table.cells, len(rows))
    width = len(rows[0]) if table.stated else len(table.spans)
    if any(len(row) != width for row in rows):
        return table.text
    if not table.stated and (len(bands) != len(rows) or not all(math.isfinite(edge) for band in bands for edge in band)):
        return table.text
    starts: dict[tuple[int, int], tuple[int, int]] = {}
    covered: set[tuple[int, int]] = set()
    for row, column, row_span, column_span in table.merges:
        if row + row_span > len(rows) or column + column_span > width:
            return table.text
        first = rows[row][column] or ""
        copied = _without_pictures(first)
        for below in range(row, row + row_span):
            for right in range(column, column + column_span):
                if (below, right) == (row, column):
                    continue
                text = rows[below][right] or ""
                if text.strip() and not (right == column and text in (first, copied)):
                    return table.text
                covered.add((below, right))
        starts[row, column] = (row_span, column_span)
    grid = TableGrid()
    for row, cells in enumerate(rows):
        for column, text in enumerate(cells):
            if (row, column) not in covered:
                row_span, column_span = starts.get((row, column), (1, 1))
                grid.place(row, column, text or "", row_span=row_span, col_span=column_span)
    html = grid.spanning_html(titles=(1 if _merged_title(rows, table.merges) else 0) if table.continued else None)
    return html if html is not None and not grid.omitted_rows() else table.text


def _without_pictures(text: str) -> str:
    """Cell text without the lines a picture placeholder was put in as (`_pdf_paragraphs._with_pictures`): the text the
    rows under the first of a cell over rows hold, copied there before its picture went into the first (`_cell_rows`).
    With them taken for text of their own, a table holding a picture in a cell over rows was written without its spans
    (Pinned by: test_pdf_table_spans.py::TestPicturesInMergedCells)."""
    return "\n".join(line for line in text.split("\n") if not IMAGE_MARKER.fullmatch(line))


def _merged_title(rows: list[list[str]], merges: tuple[tuple[int, int, int, int], ...]) -> bool:
    """Whether the top row of a table continued from the previous page is a title: one cell holds text, and every other
    column that holds text anywhere is covered there by a merged cell — the rule `_pdf_paragraphs._table_text` writes such a
    table by (`merged_titles_only`), which takes no row under it for a title. The piece of a row split at the end of the
    page is one cell with text among empty ones, and taken for a title it became a paragraph above the table (Pinned by:
    test_pdf_table_spans.py::TestContinuedTables::test_the_piece_of_a_row_split_at_the_page_end_is_no_title)."""
    filled = [column for column, text in enumerate(rows[0]) if (text or "").strip()]
    if len(filled) != 1:
        return False
    covered = {column for row, start, height, width in merges if row == 0
               for column in range(start, start + width) if column != start}
    used = {column for row in rows for column, text in enumerate(row) if (text or "").strip()}
    return all(column in covered for column in used - set(filled))


def _with_open_below(table: _Table, lookups: _TableLookups) -> _Table:
    """The table with those of its columns put in `open_below` whose bottom cell has, at its bottom edge, a horizontal ruling line
    over less than 80% of the cell width (modified in place). Measuring at each column's own bottom cell keeps the other columns
    from being seen as open in a table that `_closed_at_the_bottom` closed by extending only the merged-cell column down to the end
    of its text (if every column is open it is a split row, `_carry_open_cells`). The page's horizontal edges come from
    `lookups`."""
    open_columns = []

    for column, (x0, x1) in enumerate(table.spans):
        bottom = max((box[3] for box in table.cells if box[0] < (x0 + x1) / 2 < box[2]), default=table.bottom)
        edges = lookups.edges_at(bottom)
        covered = sum(max(0.0, min(edge["x1"], x1) - max(edge["x0"], x0)) for edge in edges)
        if x1 > x0 and covered < (x1 - x0) * 0.8:
            open_columns.append(column)
    table.open_below = tuple(open_columns)
    return table


def _in_cell(box: tuple[float, float, float, float], cell: tuple[float, float, float, float]) -> bool:
    """Whether the box (x0, top, x1, bottom) lies inside the cell box (it may spill out by `_CELL_IMAGE_SLACK`)."""
    return (cell[0] - _CELL_IMAGE_SLACK <= box[0] and box[2] <= cell[2] + _CELL_IMAGE_SLACK
            and cell[1] - _CELL_IMAGE_SLACK <= box[1] and box[3] <= cell[3] + _CELL_IMAGE_SLACK)


def _row_bands(cells: list[tuple], rows: int) -> tuple[tuple[float, float], ...]:
    """Vertical range of each row, split at the horizontal boundaries of a ruled table's cell boxes (`cells`). An empty tuple if the
    number of boundaries does not match the number of rows (`rows`)."""
    edges: list[float] = []
    for y in sorted(edge for cell in cells if cell for edge in (cell[1], cell[3])):
        if not edges or y - edges[-1] > 1.0:
            edges.append(y)
    bands = tuple(zip(edges, edges[1:], strict=False))
    return bands if len(bands) == rows else ()


def _is_caption_box(cells: list[list[str | None]]) -> bool:
    """Whether a ruled 'table' is a caption set in a box: one row of two cells, the first a figure label ('Diagram 4',
    'Figure 6', `_CAPTION_LABEL`) and nothing more.

    Deliberate: a report that sets each figure's label in a coloured badge beside its title gave a table
    '| Diagram 4 | Distribution of Instagram Content |' under every chart (4 of 200 pages of a pdf corpus). As a line the
    label stays with its title (Pinned by: test_pdf_highlights.py::TestBarCharts::test_a_figure_label_in_a_box_is_its_caption).
    Known accepted gap: a one-row box of two cells after a table label ('| 표 1 | 사업비 총액 … |') is read as lines, its text kept.
    """
    if len(cells) != 1 or len(cells[0]) != 2:
        return False
    label = (cells[0][0] or "").strip()
    return bool(_CAPTION_LABEL.fullmatch(label)) and bool((cells[0][1] or "").strip())


@_dropping_page_indexes
def _real_tables(page: object) -> list[tuple[tuple[float, float, float, float], list[list[str]], list[tuple],
                                             tuple[list[str], list[str]] | None]]:
    """Filter layout boxes out of the ruled tables: page frames (a box that covers the page but has hardly any cells), outer boxes
    that enclose another table, and boxes whose content is a single column.

    The characters of a filtered-out box are read as body flow. Returns (bbox, cell matrix, list of cell boxes, the two
    rows of a two-level header merged into the top row of the matrix as they were found, or None).
    """
    if _too_many_rules(page):
        budget = _INFLATE_BUDGET.get()
        if budget is not None:
            budget.unruled_pages.add(page.page_number)
        return []
    page_area = max(float(page.width) * float(page.height), 1.0)
    try:
        stated = _clip_tables(page)
    except Exception:  # noqa: BLE001 — boxes that cannot be put into tables state none; the page is read from its rules
        stated = []

    def left_out(table: object) -> bool:
        """A table found by the rules of the page where its writer clipped the cells of one (`_pdf_clip_tables`): the
        clipped cells are the table, and the rules drawn there show only some of them."""
        return any(_overlapping(table.bbox, other.bbox) for other in stated)

    ruled = page.find_tables(_TABLE_SETTINGS)
    # a table that is left out in the end is not closed first: closing only adds to it
    regular = [table if left_out(table) else _closed_at_open_ends(page, _closed_at_the_bottom(page, table))
               for table in ruled]
    open_sided = [table for table in _open_sided_tables(page)
                  if not any(_is_inner_part(table.bbox, closed.bbox) for closed in regular)]
    found = _with_shaded_rows(page, open_sided + [table for table in regular
                                                  if not any(_encloses(other.bbox, table.bbox) for other in open_sided)])
    # after the shaded rows, which find a shaded table again from the page's own rules
    found = [_closed_above_open_header(page, table) for table in found]
    if stated:
        found = stated + [table for table in found if not left_out(table)]
    candidates = []
    for table in found:
        x0, top, x1, bottom = table.bbox
        is_grid = len(table.rows) > _FRAME_MAX_ROWS and len(table.columns) >= 2
        if (x1 - x0) * (bottom - top) >= page_area * _PAGE_FRAME_RATIO and not is_grid:
            continue
        cells = _cell_rows(table)
        merged = _merge_split_header(cells)
        filled_columns = {index for row in merged for index, cell in enumerate(row) if cell and cell.strip()}
        if len(filled_columns) >= 2 and not _is_caption_box(cells):
            header_rows = len(cells) - len(merged) + 1  # 2 if a two-level header was merged
            _mark_filled_cells(table, cells, header_rows)
            rows = [[cell or "" for cell in row] for row in (*merged[:1], *cells[header_rows:])]
            header = tuple([cell or "" for cell in row] for row in cells[:2]) if header_rows == 2 else None
            candidates.append((table.bbox, rows, list(table.cells), table, header))

    kept, absorbed = [], set()
    for bbox, rows, boxes, table, header in candidates:
        inner = [other for other in candidates if other[0] is not bbox and _encloses(bbox, other[0])]
        if inner:
            rows = _with_inner_tables(table, rows, inner)
            if rows is None:
                continue
            absorbed.update(id(other[0]) for other in inner)
            kept.append((bbox, [(bbox, rows, boxes, None)]))
        elif len(rows) == len(table.rows):
            kept.append((bbox, [(*piece, None) for piece in _split_column_sections(table, bbox, rows)]))
        else:
            kept.append((bbox, [(bbox, rows, boxes, header)]))
    return [piece for bbox, pieces in kept if id(bbox) not in absorbed for piece in pieces]


def _overlapping(first: tuple[float, float, float, float], second: tuple[float, float, float, float]) -> bool:
    return first[0] < second[2] and second[0] < first[2] and first[1] < second[3] and second[1] < first[3]


def _too_many_rules(page: object) -> bool:
    """Whether the page's ruling lines are too many for pdfplumber's table finder (`_MAX_RULE_CROSSINGS`,
    `_MAX_RULE_PAIRS`). The lines are counted after the same snapping and joining the finder does (`TableFinder.get_edges`).

    The finder compares every horizontal line with every vertical one and then, for each crossing, every later crossing
    (`intersections_to_cells`), so its time grows with the square of the crossings: an empty 150 × 150 grid on an A4 page took
    14 seconds and a 300 × 300 one 96 seconds, with every later table search on the page (`_closed_at_the_bottom`,
    `_closed_at_open_ends`, `_open_sided_tables`, the shaded regrids) on top of it. Such a page is read as if its lines were
    not drawn: no ruled table is looked for, and its text goes to the borderless table search and the body flow like that of
    any other page (Pinned by: test_pdf_resource_limits.py::TestTooManyRules).
    """
    finder = TableFinder.__new__(TableFinder)  # only the edge step of the finder, without looking for cells
    finder.page, finder.settings = page, TableSettings.resolve(_TABLE_SETTINGS)
    edges = finder.get_edges()
    horizontals = [edge for edge in edges if edge["orientation"] == "h"]
    verticals = [edge for edge in edges if edge["orientation"] == "v"]
    if len(horizontals) * len(verticals) > _MAX_RULE_PAIRS:
        return True
    if len(horizontals) * len(verticals) <= _MAX_RULE_CROSSINGS:
        return False
    return _rule_crossings(horizontals, verticals, finder.settings.intersection_x_tolerance,
                           finder.settings.intersection_y_tolerance, _MAX_RULE_CROSSINGS) > _MAX_RULE_CROSSINGS


def _rule_crossings(horizontals: list[dict], verticals: list[dict], x_tolerance: float, y_tolerance: float, limit: int) -> int:
    """The number of (horizontal, vertical) line pairs that cross within the tolerances, the same test as pdfplumber's
    `edges_to_intersections`, counted up to just past `limit`. A sweep from top to bottom keeps the x positions of the vertical
    lines that reach the current height in order, so each horizontal line counts its crossings with two bisections."""
    events = sorted([*((edge["top"] - y_tolerance, 0, edge["x0"]) for edge in verticals),
                     *((edge["top"], 1, edge["x0"], edge["x1"]) for edge in horizontals),
                     *((edge["bottom"] + y_tolerance, 2, edge["x0"]) for edge in verticals)], key=lambda event: event[:2])
    active: list[float] = []
    crossings = 0

    for event in events:
        if event[1] == 0:
            insort(active, event[2])
        elif event[1] == 2:
            del active[bisect_left(active, event[2])]
        else:
            crossings += bisect_right(active, event[3] + x_tolerance) - bisect_left(active, event[2] - x_tolerance)
            if crossings > limit:
                break
    return crossings


def _closed_at_the_bottom(page: object, table: object) -> object:
    """If the bottom row has a place that did not become a cell and text inside the table lies in no cell, the table found again
    after adding a horizontal line at the bottom of the table. `table` itself when there is nothing to change.

    A vertically merged cell split at the page end (rowspan in Chrome printing) has no bottom border on that page and did not become
    a cell. Its text ('그룹5 담당: 팀5') fell out as a paragraph outside the table and the first cell of those rows was empty (Pinned by:
    test_pdf_table_columns.py::TestMergedCellOpenAtThePageEnd). When the cell text is longer than the last row and continues beyond
    the bottom of the table (text of the same column continuing within the line spacing), only that column is extended down to the
    end of the text and closed there — closing at the table bottom split the text so that only the first part was in the cell and
    the rest ('담당: 팀10') became a paragraph outside the table. The table is replaced only when exactly one table is found again,
    the row and column counts are the same, and all the text that fell out is now in cells. For the open-topped empty cell on the
    next page see `_carry_open_cells`.
    """
    if not table.rows or all(cell is not None for cell in table.rows[-1].cells):
        return table
    x0, top, x1, bottom = table.bbox
    in_cells = _BoxIndex([cell for cell in table.cells if cell]).holds
    stray = [char for char in page.chars if char["text"].strip() and x0 < (char["x0"] + char["x1"]) / 2 < x1
             and top < (char["top"] + char["bottom"]) / 2 < bottom and not in_cells(char)]
    if not stray:
        return table

    # When the cell text continues beyond the bottom of the table (merged-cell text longer than the last row), only that column is
    # extended down to the end of the continuing lines and closed there
    edges = _column_edges(table)
    middle = (min(char["x0"] for char in stray) + max(char["x1"] for char in stray)) / 2
    index = max(0, min(bisect_right(edges, middle) - 1, len(edges) - 2))
    left, right = edges[index], edges[index + 1]
    end = max(char["bottom"] for char in stray)
    for char in sorted((char for char in page.chars if char["text"].strip() and (char["top"] + char["bottom"]) / 2 >= bottom
                        and left < (char["x0"] + char["x1"]) / 2 < right), key=lambda char: char["top"]):
        if char["top"] - end > (char["bottom"] - char["top"]) * 0.5:
            break
        stray.append(char)
        end = max(end, char["bottom"])
    lowest = max(bottom, end + 1)
    closing = [_rule_line(a, bottom, b, bottom) for a, b in ((x0, left), (right, x1)) if b > a]  # close while leaving that column open
    lines = {"explicit_horizontal_lines": [*closing, _rule_line(left, lowest, right, lowest)],
             "explicit_vertical_lines": [_rule_line(x, bottom, x, lowest) for x in (left, right)] if lowest > bottom else []}

    box = _region_on_page(page, x0, top, x1, lowest)
    if box is None:
        return table
    found = _cropped(page, box).find_tables({**_TABLE_SETTINGS, **lines})
    if (len(found) != 1 or len(found[0].rows) != len(table.rows) or len(found[0].columns) != len(table.columns)
            or not all(map(_BoxIndex([cell for cell in found[0].cells if cell]).holds, stray))):
        return table
    return found[0]


def _closed_at_open_ends(page: object, table: object) -> object:
    """If the vertical rules continue past the bottom (top) of the table at every column boundary and there is text in between, the
    table found again after adding a horizontal line at the end of the continuing vertical lines (a row split at the page end).
    `table` itself when there is nothing to change.

    Chrome printing draws every cell of a row split at the page end without a bottom border, and the remainder on the next page
    without a top border. That row did not become cells, so its first part fell out as a paragraph outside the table, and the
    remainder on the next page also became a paragraph above the table, so the header was not carried to the next page's table
    (Pinned by:
    test_pdf_table_columns.py::TestRowSplitAcrossPages::test_row_whose_cells_are_all_open_at_the_page_end_stays_in_the_table).
    For a vertically merged cell open in one column only see `_closed_at_the_bottom`. The table is replaced only when exactly one
    table is found again, the column count is the same, the rows grow by the number of ends added, and all the text in between is in
    cells. How this row is continued on the next page is in `_carry_table_headers`.
    """
    if not table.rows:
        return table
    x0, top, x1, bottom = table.bbox
    edges = _column_edges(table)
    # The page's vertical rules and characters are sorted once per page and each table takes only those of its own range.
    # Listing every rule and every character of the page again for every table took 31 seconds on a page of 800 small tables
    # (a 51 KB file) (Pinned by: test_audit_pdf_mid.py::TestManyTablesOnOnePage::test_each_table_takes_only_its_own_part_of_the_page)
    positions, verticals = _verticals_by_x(page)
    verticals = verticals[bisect_left(positions, min(edges) - 2):bisect_right(positions, max(edges) + 2)] if edges else []
    lowest = _common_reach(verticals, edges, bottom, 1)
    highest = _common_reach(verticals, edges, top, -1)
    bands = [band for band in ((bottom, lowest), (highest, top)) if band[1] - band[0] >= _CHECKBOX_SIDE[0]]
    if not bands:
        return table
    middles, ordered = _text_by_middle(page)
    stray = [char for low, high in bands for char in ordered[bisect_right(middles, low):bisect_left(middles, high)]
             if x0 < (char["x0"] + char["x1"]) / 2 < x1 and low < (char["top"] + char["bottom"]) / 2 < high]
    if not stray:
        return table

    lines = [_rule_line(x0, y, x1, y) for y in (lowest, highest) if any(y in band for band in bands)]
    box = _region_on_page(page, x0, min(top, highest), x1, max(bottom, lowest))
    if box is None:
        return table
    found = _cropped(page, box).find_tables({**_TABLE_SETTINGS, "explicit_horizontal_lines": lines})
    if (len(found) != 1 or len(found[0].rows) != len(table.rows) + len(lines) or len(found[0].columns) != len(table.columns)
            or not all(map(_BoxIndex([cell for cell in found[0].cells if cell]).holds, stray))):
        return table
    return found[0]


def _closed_above_open_header(page: object, table: object) -> object:
    """If vertical rules at two or more of the table's column boundaries go on above its top to one height and there is text in
    between, the table found again with a top rule at that height and its outer sides closed up to it. `table` itself when there
    is nothing to change.

    A header of two levels drawn without a top border or outer sides ('No.', 'Political party' merged over both levels, and
    'Provisional registration result' over two sub-columns) has rules only between its merged cells above the second level,
    so the table started at the second level and the first fell out as lines above it (3 of 200 pages of a pdf corpus). Every
    boundary is needed for a row split at the page end (`_closed_at_open_ends`); here the merged cells cover some of them.
    The table is replaced only when exactly one table is found again, with the same columns, one row more, and all the text in
    between in its cells (Pinned by: test_pdf_highlights.py::TestHeaderWithoutTopBorder).
    """
    if not table.rows:
        return table
    x0, top, x1, bottom = table.bbox
    edges = _column_edges(table)
    positions, verticals = _verticals_by_x(page)
    reaches = []
    for x in edges:  # only the rules at each boundary, found by their position (a grid of hundreds of columns)
        ends = [edge["top"] for edge in verticals[bisect_left(positions, x - 1.5):bisect_right(positions, x + 1.5)]
                if abs(edge["bottom"] - top) <= 1.5]
        if ends:
            reaches.append(min(ends))
    if len(reaches) < 2 or max(reaches) - min(reaches) > 1.5:
        return table
    highest = min(reaches)
    if top - highest < _CHECKBOX_SIDE[0]:
        return table
    middles, ordered = _text_by_middle(page)
    stray = [char for char in ordered[bisect_right(middles, highest):bisect_left(middles, top)]
             if x0 < (char["x0"] + char["x1"]) / 2 < x1]
    if not stray:
        return table
    box = _region_on_page(page, x0, highest, x1, bottom)
    if box is None:
        return table
    # The outer sides are closed down to the first cell of the outer column: its merged header cell has no side either
    sides = [(x, next((cell[1] for row in table.rows if (cell := row.cells[column]) is not None), top))
             for x, column in ((x0, 0), (x1, -1))]
    # The table's own cells are drawn as rules too: a table grown from cell shading (`_with_shaded_rows`) has sides the page
    # does not draw, and found again from the page alone it lost them
    cells = [cell for cell in table.cells if cell]
    lines = {"explicit_horizontal_lines": [_rule_line(x0, highest, x1, highest),
                                           *(_rule_line(left, y, right, y) for left, upper, right, lower in cells for y in (upper, lower))],
             "explicit_vertical_lines": [*(_rule_line(x, highest, x, end) for x, end in sides),
                                         *(_rule_line(x, upper, x, lower) for left, upper, right, lower in cells for x in (left, right))]}
    found = _cropped(page, box).find_tables({**_TABLE_SETTINGS, **lines})
    if (len(found) != 1 or len(found[0].rows) != len(table.rows) + 1 or len(found[0].columns) != len(table.columns)
            or not all(map(_BoxIndex([cell for cell in found[0].cells if cell]).holds, stray))):
        return table
    return found[0]


def _verticals_by_x(page: object) -> tuple[list[float], list[dict]]:
    """The page's vertical rules longer than 1pt in order of their x position, as (list of x positions, list of rules). Built
    once per page (`_closed_at_open_ends`). A rule whose position is not a number is at no column boundary and is left out."""
    def build() -> tuple[list[float], list[dict]]:
        ordered = sorted((edge for edge in page.vertical_edges if edge["bottom"] - edge["top"] > 1 and edge["x0"] == edge["x0"]),
                         key=lambda edge: edge["x0"])
        return [edge["x0"] for edge in ordered], ordered

    return _page_index(page, "verticals", build)


def _common_reach(verticals: list[dict], edges: list[float], at: float, direction: int) -> float:
    """The end reached by all the vertical rules that start at height `at` at each column boundary (`edges`) and continue downwards
    (`direction` 1) or upwards (-1). `at` if any boundary has no continuing line."""
    reach = math.inf if direction > 0 else -math.inf

    for x in edges:
        ends = [edge["bottom"] if direction > 0 else edge["top"] for edge in verticals if abs(edge["x0"] - x) <= 1.5
                and abs((edge["top"] if direction > 0 else edge["bottom"]) - at) <= 1.5]
        if not ends:
            return at
        reach = min(reach, max(ends)) if direction > 0 else max(reach, min(ends))

    return reach if edges else at


def _split_column_sections(table: object, bbox: tuple[float, float, float, float],
                           rows: list[list[str]]) -> list[tuple[tuple[float, float, float, float], list[list[str]], list[tuple]]]:
    """Split a ruled table (`table`, cell text `rows`) into groups of rows with different column boundaries. It is split only where
    two groups, each keeping the same column boundaries for `_SECTION_MIN_ROWS` rows or more, touch and neither side's boundaries are
    all contained in the other's; in a split table the columns that have no cell in that group (columns covered by merging) are
    removed. Without a split the single table is returned as is (one (bbox, rows, list of cell boxes)).

    pdfplumber read two touching tables (a 6-column '경비 내역' table right below a 4-column '인적 사항' grid) as one grid combining
    the column boundaries of both, so empty columns were inserted and the lower table's header became a data row (Pinned by:
    test_pdf_table_columns.py::TestAbuttingTablesWithDifferentColumns).
    Header rows and total rows that span columns are part of rows with different boundaries and are not split off.

    Nor are two groups split when the difference between them is merged cells of one grid (`_merged_from`, both ways): the
    groups have the same outer edges, and every cell of one that lies across a boundary of the other is either a block
    merged over all rows of its group or made of whole columns — it starts and ends at a boundary of the other group (for the
    lower group: of any row of the table above it, as far as that has been kept together). The cells of two tables drawn
    with different columns lie across each other's boundaries row by row and end in between them. A form with a block of
    two columns merged over rows 1-2 and, right below, a block of two other columns merged over rows 3-5 was split between
    the blocks, and its second half took a data row as its header (Pinned by:
    test_pdf_table_columns.py::TestAbuttingTablesWithDifferentColumns::test_merged_blocks_below_each_other_keep_one_table,
    test_merged_blocks_with_no_row_of_all_columns_keep_one_table, test_merged_blocks_overlapping_in_a_column_keep_one_table,
    test_rows_merged_from_a_column_that_a_block_above_covers_keep_one_table).
    A row elsewhere in the grid that has the boundaries of both groups says nothing: under a grid of four columns and a
    table of six, a month grid of twelve columns had the boundaries of both, and the three were read as one table of twelve
    columns (Pinned by: test_three_abutting_tables_stay_apart_when_the_third_has_the_columns_of_both,
    test_tables_stay_apart_when_the_rows_above_them_have_the_columns_of_both,
    test_table_below_a_block_with_a_boundary_of_its_own_is_another_table, test_narrower_table_below_a_grid_is_another_table).
    Each pair of neighbouring groups is looked at once, so the time grows with the number of rows.
    """
    table_rows = table.rows  # a property that regroups all the cells on every access
    if any(len(row.cells) != len(rows[0]) for row in table_rows):
        return [(bbox, rows, list(table.cells))]
    held = _row_cells(table_rows, table.cells)
    edges = [tuple(sorted({round(edge) for cell in row for edge in (cell[0], cell[2])})) for row in held]
    runs: list[list[int]] = []  # consecutive row numbers with the same boundaries

    for index, edge in enumerate(edges):
        if runs and _same_edges(edges[runs[-1][0]], edge):
            runs[-1].append(index)
        else:
            runs.append([index])

    sections = [list(runs[0])]
    grid = set(edges[0])  # the column boundaries of the rows of the section so far

    for previous, run in zip(runs, runs[1:], strict=False):
        above, below = edges[previous[0]], edges[run[0]]
        apart = (len(previous) >= _SECTION_MIN_ROWS and len(run) >= _SECTION_MIN_ROWS
                 and not _edges_within(above, below) and not _edges_within(below, above)
                 and not (_merged_from([held[index] for index in previous], below, set(below))
                          and _merged_from([held[index] for index in run], above, grid)))
        if apart:
            sections.append(list(run))
            grid = set(below)
        else:
            sections[-1].extend(run)
            grid.update(below)
    if len(sections) == 1:
        return [(bbox, rows, list(table.cells))]

    split = []

    for section in sections:
        cells = [[table_rows[index].cells[column] for column in range(len(rows[0]))] for index in section]
        columns = [column for column in range(len(rows[0])) if any(line[column] is not None for line in cells)]
        boxes = [cell for line in cells for cell in line if cell is not None]
        area = (min(box[0] for box in boxes), min(box[1] for box in boxes), max(box[2] for box in boxes), max(box[3] for box in boxes))
        split.append((area, [[rows[index][column] for column in columns] for index in section], boxes))
    return split


def _row_cells(table_rows: list[object], cells: list[tuple]) -> list[list[tuple]]:
    """The cells of each row of a ruled table: every cell whose vertical extent covers the middle of that row, so a vertically
    merged cell counts in every row it spans, not only in the row it starts in. Their left and right edges are the column
    boundaries of the row.

    With only the cells starting in a row, a merged cell at the outer edge (the right column merged over rows 1-3, the left
    column over rows 3-5) left the outer boundary out of the rows it covered, the rows looked like two groups with different
    boundaries, and the table was split in two, losing the left merged cell ('채소') (Pinned by:
    test_pdf_table_columns.py::TestAbuttingTablesWithDifferentColumns::test_merged_cells_at_the_outer_edges_keep_one_table).

    A row reaches from its top to the top of the next row (the last row to its bottom). The box of the row is not used: it is
    the union of the cells that start in the row, so a cell merged downwards makes it as tall as the merged cell (the pitfall
    recorded in `_cell_rows`), and its middle then lay in a row further down. In a form of two rows of four boxes, a row of two
    boxes beside a remarks box two rows high and a row of three boxes beside it, the row the remarks box starts in was measured
    inside the row of three boxes, the two looked like a group with columns of its own, and the form was split in two after
    its second row (Pinned by:
    test_pdf_table_columns.py::TestAbuttingTablesWithDifferentColumns::test_row_beside_a_taller_box_is_measured_at_its_own_height).
    """
    tops = sorted({row.bbox[1] for row in table_rows})
    middles = []
    for row in table_rows:
        below = bisect_right(tops, row.bbox[1])
        middles.append((row.bbox[1] + (tops[below] if below < len(tops) else row.bbox[3])) / 2)
    order = sorted(range(len(middles)), key=middles.__getitem__)
    ordered = [middles[index] for index in order]
    found: list[list[tuple]] = [[] for _ in table_rows]

    for cell in cells:
        if cell is None:
            continue
        for position in range(bisect_left(ordered, cell[1]), bisect_right(ordered, cell[3])):
            found[order[position]].append(cell)
    return found


def _same_edges(edges: tuple[int, ...], others: tuple[int, ...]) -> bool:
    return len(edges) == len(others) and all(abs(edge - other) <= 1 for edge, other in zip(edges, others, strict=True))


def _edges_within(edges: tuple[int, ...], others: tuple[int, ...]) -> bool:
    """Whether all column boundaries in `edges` are boundaries of `others` (within 1pt)."""
    return all(any(abs(edge - other) <= 1 for other in others) for edge in edges)


def _merged_from(group: list[list[tuple]], others: tuple[int, ...], grid: set[int]) -> bool:
    """Whether a group of rows (the cells of each of its rows, `_row_cells`) can be rows of the same table as the rows with
    the column boundaries `others`, with some cells merged: the two have the same outer edges, and every cell of the group
    that lies across a boundary of `others` either is a merged block — one cell reaching over all rows of the group, or over
    two or more of them — or starts and ends at a boundary of `grid`: it is made of whole columns.
    `_split_column_sections` says what this decides. The positions are compared as rounded numbers: the table finder has
    already snapped the rules of one boundary to one position.

    A block has to reach over more than one row, not over the whole group: two blocks merged in the same columns right
    below each other are rows with the same boundaries, so one group, and neither block reaches over all of it. A table of
    three columns with the first two merged over rows 1-2 and the last two merged over rows 3-4 and again over rows 5-6 was
    split after its second row and lost a column
    (Pinned by: test_pdf_table_columns.py::TestAbuttingTablesWithDifferentColumns::test_blocks_stacked_in_the_same_columns_keep_one_table).
    """
    first = group[0]
    if not first or min(round(cell[0]) for cell in first) != others[0] or max(round(cell[2]) for cell in first) != others[-1]:
        return False
    reach = Counter(cell for row in group for cell in row)  # how many rows of the group each cell reaches over
    enough = min(len(group), 2)
    for cell, rows in reach.items():
        left, right = round(cell[0]), round(cell[2])
        crossed = bisect_left(others, right) > bisect_right(others, left)  # a boundary of `others` inside the cell
        if crossed and rows < enough and not (left in grid and right in grid):
            return False
    return True


def _with_inner_tables(table: object, rows: list[list[str]], inner: list[tuple]) -> list[list[str]] | None:
    """If the ruled table `table` (cell text `rows`) that encloses other ruled tables (`inner` = candidates of `_real_tables`) is a
    form containing tables within a table, the cell text with the inner tables spread out in place of the outer cell; otherwise None
    (the outer table is a layout box and is discarded).

    It counts as a form only when every inner table has an outer cell containing it, the row of that cell has other cell text
    (labels) and all of it is `_NESTED_LABEL_MAX` characters or less (deliberate, conservative — one cell holding inner tables per
    row). When the inner table holds all the text of that cell (lines above and below the table become rows before and after), the
    row is expanded to the inner table's row count and the label is repeated on every row ('학력 | 기간 | 학교명 | …'); otherwise (the
    inner table was found for only some rows and text remains beside it, or several tables in one cell) the outer cell's text is
    kept as is. Either way the inner table is not emitted separately. In a résumé printed by Chrome the outer table was discarded as
    'a box enclosing another table', so '성명' and '홍길동' were scattered into paragraphs and '학력' was separated from its table
    (Pinned by: test_pdf_nested_tables.py::TestNestedTables::test_inner_table_found_apart_keeps_the_outer_label_on_its_rows,
    test_inner_table_found_only_in_part_is_kept_as_the_outer_cell_text, test_unit_line_under_the_inner_table_becomes_a_row_after_it).
    """
    if len(rows) != len(table.rows):
        return None  # a table whose header was merged or whose inner-table cell rows were removed
    holders: dict[int, tuple[int, list[tuple]]] = {}  # row → (column, inner tables in that cell)

    for other in inner:
        place = next(((row, column) for row, line in enumerate(table.rows) for column, cell in enumerate(line.cells)
                      if cell is not None and _encloses(cell, other[0])), None)
        if place is None:
            return None
        column, held = holders.setdefault(place[0], (place[1], []))
        if column != place[1]:
            return None
        held.append(other)

    for row, (column, _) in holders.items():
        labels = [text for index, text in enumerate(rows[row]) if index != column and text.strip()]
        if not labels or any(len(text) > _NESTED_LABEL_MAX for text in labels):
            return None

    # The holder cell of each spread row: (first column, number of columns it covers, rows to put in its place). A column
    # at the end of a holder cell is widened by the inner columns the cell lacks, and every other row gets empty cells
    # there too, so the columns to the right of the holder stay under their own header — padding only the ends of the
    # rows put '비고1' of the rows above under the inner table's second column
    # (Pinned by: test_pdf_nested_tables.py::TestNestedTables::test_columns_right_of_the_inner_table_stay_aligned).
    spreads: dict[int, tuple[int, int, list[list[str]]]] = {}
    for row, cells in enumerate(rows):
        column, held = holders.get(row, (0, []))
        around = _text_around(table.page.chars, table.rows[row].cells[column], held[0]) if len(held) == 1 else None
        if around is None:
            continue
        width = 1 + next((count for count, cell in enumerate(table.rows[row].cells[column + 1:]) if cell is not None),
                         len(cells) - column - 1)
        above, below = around
        spreads[row] = (column, width, [*([[above]] if above else []), *held[0][1], *([[below]] if below else [])])
    extra = [0] * max(len(cells) for cells in rows)  # empty cells added after each column
    for column, width, inner_rows in spreads.values():
        end = min(column + width, len(extra)) - 1
        extra[end] = max(extra[end], max(len(inner_row) for inner_row in inner_rows) - width)

    def widened(cells: list[str], start: int = 0) -> list[str]:  # `cells` are the cells from column `start` on
        return [part for index, cell in enumerate(cells, start) for part in (cell, *[""] * (extra[index] if index < len(extra) else 0))]

    spread: list[list[str]] = []
    for row, cells in enumerate(rows):
        if row not in spreads:
            spread.append(widened(cells))
            continue
        column, width, inner_rows = spreads[row]
        room = width + sum(extra[column:column + width])
        spread.extend([*widened(cells[:column]), *inner_row, *[""] * (room - len(inner_row)), *widened(cells[column + width:], column + width)]
                      for inner_row in inner_rows)

    width = max(len(cells) for cells in spread)
    return [[*cells, *[""] * (width - len(cells))] for cells in spread]


def _text_around(chars: list[dict], cell: tuple, inner: tuple) -> tuple[str, str] | None:
    """Text inside the outer cell `cell` that lies outside the cells of the inner table (`inner` = a candidate of `_real_tables`):
    (text above the table, text below the table). None if there are characters beside the table (the inner table was found only in
    part). A unit line ('(단위: 천원)') or caption becomes a row before or after the inner table."""
    bbox, _, inner_cells = inner[:3]
    outside = [char for char in chars
               if char["text"].strip() and _inside_any(char, [cell]) and not _inside_any(char, inner_cells)]
    if any(bbox[1] <= (char["top"] + char["bottom"]) / 2 <= bbox[3] for char in outside):
        return None
    lines: dict[int, set[float]] = {}  # line → the inner-table columns (left edge) the line's characters lie in

    # A line lying across two or more columns of the inner table is not a unit line or caption but a row that was not found as table
    for char in outside:
        middle = (char["x0"] + char["x1"]) / 2
        columns = lines.setdefault(round(char["top"] / _TABLE_ROW_TOLERANCE), set())
        columns.update(x0 for x0, _, x1, _ in inner_cells if x0 <= middle <= x1)
    if any(len(columns) >= 2 for columns in lines.values()):
        return None

    above = _text_outside([char for char in outside if char["bottom"] < bbox[1] + 1], cell, [])
    below = _text_outside([char for char in outside if char["top"] > bbox[3] - 1], cell, [])
    return above, below
