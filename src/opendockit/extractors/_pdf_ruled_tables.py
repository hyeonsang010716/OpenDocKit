"""Tables drawn with ruling lines or shading: which found tables are real, cells and merged cells, shaded rows without
rules, tables open at the sides, text wrapped inside a cell, and the highlights and glyph outlines that look like rules.
"""

from bisect import bisect_left, bisect_right
import math

from opendockit.extractors._pdf_cell_text import _cell_rows, _text_outside
from opendockit.extractors._pdf_chars import _drawn_text
from opendockit.extractors._pdf_model import (
    _CHECKBOX_SIDE,
    _TABLE_ROW_TOLERANCE,
    _TABLE_SETTINGS,
    _BoxIndex,
    _encloses,
    _inside_any,
    _page_index,
    _Table,
    _text_by_middle,
)
from opendockit.extractors._pdf_open_tables import _merge_split_header, _open_sided_tables
from opendockit.extractors._pdf_shaded_tables import _column_edges, _is_inner_part, _mark_filled_cells, _rule_line, _with_shaded_rows
from opendockit.extractors._pdf_table_filters import _dropping_page_indexes
from opendockit.markdown import render_table


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


def _ruled_table(bbox: tuple[float, float, float, float], rows: list[list[str | None]], boxes: list[tuple], chars: list[dict],
                 figures: list[tuple[float, float, float, float, float]]) -> _Table:
    """Turns one ruled table (a result of `_real_tables`) into a `_Table`. For a cell holding a picture the line heights of the cell
    text are stored as well (`_place_images_in_cells`)."""
    cells = [[cell and _drawn_text(cell) for cell in row] for row in rows]
    boxes = tuple(box for box in boxes if box)
    cell_lines = {}

    for index, box in enumerate(boxes):
        if any(_in_cell(figure[:4], box) for figure in figures):
            tops: list[float] = []

            for top in sorted(char["top"] for char in chars if char["text"].strip() and _inside_any(char, [box])):
                if not tops or top - tops[-1] > _TABLE_ROW_TOLERANCE:
                    tops.append(top)

            cell_lines[index] = tuple(tops)
    return _Table(top=bbox[1], text=render_table(cells), bottom=bbox[3], x0=bbox[0], x1=bbox[2], rows=cells,
                  spans=_column_spans(boxes, len(cells[0])), cells=boxes, bands=_row_bands(boxes, len(cells)),
                  cell_lines=cell_lines)


def _with_open_below(page: object, table: _Table) -> _Table:
    """The table with those of its columns put in `open_below` whose bottom cell has, at its bottom edge, a horizontal ruling line
    over less than 80% of the cell width (modified in place). Measuring at each column's own bottom cell keeps the other columns
    from being seen as open in a table that `_closed_at_the_bottom` closed by extending only the merged-cell column down to the end
    of its text (if every column is open it is a split row, `_carry_open_cells`)."""
    open_columns = []

    for column, (x0, x1) in enumerate(table.spans):
        bottom = max((box[3] for box in table.cells if box[0] < (x0 + x1) / 2 < box[2]), default=table.bottom)
        edges = [edge for edge in page.horizontal_edges if abs(edge["top"] - bottom) <= 1.5]
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


@_dropping_page_indexes
def _real_tables(page: object) -> list[tuple[tuple[float, float, float, float], list[list[str]], list[tuple]]]:
    """Filter layout boxes out of the ruled tables: page frames (a box that covers the page but has hardly any cells), outer boxes
    that enclose another table, and boxes whose content is a single column.

    The characters of a filtered-out box are read as body flow. Returns (bbox, cell matrix, list of cell boxes).
    """
    page_area = max(float(page.width) * float(page.height), 1.0)
    regular = [_closed_at_open_ends(page, _closed_at_the_bottom(page, table)) for table in page.find_tables(_TABLE_SETTINGS)]
    open_sided = [table for table in _open_sided_tables(page)
                  if not any(_is_inner_part(table.bbox, closed.bbox) for closed in regular)]
    found = _with_shaded_rows(page, open_sided + [table for table in regular
                                                  if not any(_encloses(other.bbox, table.bbox) for other in open_sided)])
    candidates = []
    for table in found:
        x0, top, x1, bottom = table.bbox
        is_grid = len(table.rows) > _FRAME_MAX_ROWS and len(table.columns) >= 2
        if (x1 - x0) * (bottom - top) >= page_area * _PAGE_FRAME_RATIO and not is_grid:
            continue
        cells = _cell_rows(table)
        merged = _merge_split_header(cells)
        filled_columns = {index for row in merged for index, cell in enumerate(row) if cell and cell.strip()}
        if len(filled_columns) >= 2:
            header_rows = len(cells) - len(merged) + 1  # 2 if a two-level header was merged
            _mark_filled_cells(table, cells, header_rows)
            rows = [[cell or "" for cell in row] for row in (*merged[:1], *cells[header_rows:])]
            candidates.append((table.bbox, rows, list(table.cells), table))

    kept, absorbed = [], set()
    for bbox, rows, boxes, table in candidates:
        inner = [other for other in candidates if other[0] is not bbox and _encloses(bbox, other[0])]
        if inner:
            rows = _with_inner_tables(table, rows, inner)
            if rows is None:
                continue
            absorbed.update(id(other[0]) for other in inner)
            kept.append((bbox, [(bbox, rows, boxes)]))
        elif len(rows) == len(table.rows):
            kept.append((bbox, _split_column_sections(table, bbox, rows)))
        else:
            kept.append((bbox, [(bbox, rows, boxes)]))
    return [piece for bbox, pieces in kept if id(bbox) not in absorbed for piece in pieces]


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

    page_left, page_top, page_right, page_bottom = page.bbox
    box = (max(x0 - 1, page_left), max(top - 1, page_top), min(x1 + 1, page_right), min(lowest + 1, page_bottom))
    found = page.crop(box).find_tables({**_TABLE_SETTINGS, **lines})
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
    page_left, page_top, page_right, page_bottom = page.bbox
    box = (max(x0 - 1, page_left), max(min(top, highest) - 1, page_top), min(x1 + 1, page_right),
           min(max(bottom, lowest) + 1, page_bottom))
    found = page.crop(box).find_tables({**_TABLE_SETTINGS, "explicit_horizontal_lines": lines})
    if (len(found) != 1 or len(found[0].rows) != len(table.rows) + len(lines) or len(found[0].columns) != len(table.columns)
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
    """
    table_rows = table.rows  # a property that regroups all the cells on every access
    if any(len(row.cells) != len(rows[0]) for row in table_rows):
        return [(bbox, rows, list(table.cells))]
    edges = _row_edges(table_rows, table.cells)
    runs: list[list[int]] = []  # consecutive row numbers with the same boundaries

    for index, edge in enumerate(edges):
        if runs and _same_edges(edges[runs[-1][0]], edge):
            runs[-1].append(index)
        else:
            runs.append([index])

    sections = [list(runs[0])]

    for previous, run in zip(runs, runs[1:], strict=False):
        apart = (len(previous) >= _SECTION_MIN_ROWS and len(run) >= _SECTION_MIN_ROWS
                 and not _edges_within(edges[previous[0]], edges[run[0]])
                 and not _edges_within(edges[run[0]], edges[previous[0]]))
        if apart:
            sections.append(list(run))
        else:
            sections[-1].extend(run)
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


def _row_edges(table_rows: list[object], cells: list[tuple]) -> list[tuple[int, ...]]:
    """The column boundaries (rounded) of each row of a ruled table: the left and right edges of every cell whose vertical extent
    covers the middle of that row, so a vertically merged cell counts in every row it spans, not only in the row it starts in.

    With only the cells starting in a row, a merged cell at the outer edge (the right column merged over rows 1-3, the left
    column over rows 3-5) left the outer boundary out of the rows it covered, the rows looked like two groups with different
    boundaries, and the table was split in two, losing the left merged cell ('채소') (Pinned by:
    test_pdf_table_columns.py::TestAbuttingTablesWithDifferentColumns::test_merged_cells_at_the_outer_edges_keep_one_table).
    """
    middles = [(row.bbox[1] + row.bbox[3]) / 2 for row in table_rows]
    order = sorted(range(len(middles)), key=middles.__getitem__)
    ordered = [middles[index] for index in order]
    found: list[set[float]] = [set() for _ in table_rows]

    for cell in cells:
        if cell is None:
            continue
        for position in range(bisect_left(ordered, cell[1]), bisect_right(ordered, cell[3])):
            found[order[position]].update((cell[0], cell[2]))
    return [tuple(round(edge) for edge in sorted(edges)) for edges in found]


def _same_edges(edges: tuple[int, ...], others: tuple[int, ...]) -> bool:
    return len(edges) == len(others) and all(abs(edge - other) <= 1 for edge, other in zip(edges, others, strict=True))


def _edges_within(edges: tuple[int, ...], others: tuple[int, ...]) -> bool:
    """Whether all column boundaries in `edges` are boundaries of `others` (within 1pt)."""
    return all(any(abs(edge - other) <= 1 for other in others) for edge in edges)


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
    bbox, _, inner_cells, _ = inner
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
