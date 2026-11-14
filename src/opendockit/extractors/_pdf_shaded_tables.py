"""Tables marked by shading: cells filled as a mark, rows told apart by their shade, column fragments merged back, and a
table grown from shaded bands where no rule is drawn.
"""

from bisect import bisect_left, bisect_right
from itertools import pairwise
import math
import statistics

from opendockit.extractors._pdf_model import (
    _FILL_WHITE_MIN,
    _NUMBER_CELL,
    _SHADE_MIN_SIDE,
    _TABLE_ROW_TOLERANCE,
    _TABLE_SETTINGS,
    _encloses,
    _is_shade,
    _page_index,
)


_FILL_MARK = "■"  # ruled table cell marked only by its fill colour (`_mark_filled_cells`). Same character as DOCX's filled box
_FILL_MIN_COVER = 0.6  # share of the cell area the fill rectangle has to cover


_FILL_HUE_MIN = 0.1  # chromatic if the components differ by at least this
_FILL_GREY_MAX = 0.6  # an achromatic fill marks the cell only if all components are this dark or darker


_SHADED_ROW_REACH = 1.5  # maximum height of a band accepted as continuing a shaded table fragment (vs. the fragment's median row height)
_BAND_SLACK_RATIO = 0.3  # share of the character height that may stick out of a band and still count as the band's text (font box slack)
# Text reaching deeper than this share of the character height onto both sides of a row boundary is the text of a vertically merged
# cell (`_regridded_fragments`). Chrome placed the text of a cell merged over an even number of rows 25% across the row boundary.
# This tells it from a character box grazing the neighbouring row (0.06pt).
_MERGED_TEXT_CROSSING = 0.15
_CELL_GAP_MIN_RATIO = 0.5  # characters either side of a cell boundary closer than this (vs. character size) are text crossing it (a word gap)


def _mark_filled_cells(table: object, rows: list[list[str | None]], header_rows: int) -> None:
    """Put `_FILL_MARK` into the empty cells of a ruled table that are marked only by a fill colour (`rows` is the cell text from
    `_cell_rows`, modified in place).

    In a Gantt-chart schedule (the January–February cells of the '요구사항 분석' row filled blue) the cells have no text, so the whole
    schedule disappeared (Pinned by: test_pdf.py::
    TestCellsMarkedOnlyByFill::test_empty_cells_filled_with_a_colour_are_marked). Only cells that satisfy all of the following are
    marked (deliberate, conservative): an empty cell below the header rows (`header_rows`) ("", not a None hidden by merging), a
    fill rectangle in a mark colour (`_is_mark_fill`) covers at least `_FILL_MIN_COVER` of the cell area, the same row has an
    unfilled cell (not a whole-row background, stripes or a whole-table background), the cells below the header in the same column
    are not all filled (not whole-column shading), and somewhere in the table two neighbouring cells of the same row are filled in
    a run (a bar — not scattered cell emphasis). Header-row shading is excluded because header rows are not examined (Pinned by:
    test_shading_that_is_not_a_mark_leaves_empty_cells_empty). Known accepted gap: a column filled in every data row (a schedule
    over the whole period), a schedule that fills a whole row, pale grey bars and tables whose bars are all one cell wide are not
    marked, while pale coloured 'do not fill in' cells in a table that has cells filled in a run are marked. Cells filled with a
    picture (image) are not examined.
    """
    x0, top, x1, bottom = table.bbox
    fills = [rect for rect in table.page.rects
             if rect["x0"] < x1 and rect["x1"] > x0 and rect["top"] < bottom and rect["bottom"] > top  # only rectangles touching the table
             and rect.get("fill") and _is_mark_fill(rect.get("non_stroking_color"))]
    data = table.rows[header_rows:]
    if not fills or len(data) < 2 or len(rows) != len(table.rows):
        return

    covered = [[cell is not None and _fill_cover(cell, near) >= _FILL_MIN_COVER for cell in row.cells]
               for row, near in zip(data, _fills_by_row(data, fills), strict=True)]
    shaded_columns = {column for column in range(len(covered[0])) if all(row[column] for row in covered)}
    marks: set[tuple[int, int]] = set()
    for index, row in enumerate(data):
        if all(covered[index][column] for column, cell in enumerate(row.cells) if cell is not None):
            continue
        marks.update((header_rows + index, column) for column, cell in enumerate(rows[header_rows + index])
                     if cell == "" and covered[index][column] and column not in shaded_columns)

    # Without a single bar of two neighbouring cells filled in a run it is not a schedule (scattered cell emphasis)
    if not any((row, column + 1) in marks for row, column in marks):
        return
    for row, column in marks:
        rows[row][column] = _FILL_MARK


def _is_mark_fill(colour: object) -> bool:
    """Whether a fill colour counts as a cell mark: a chromatic colour (RGB components differ by at least `_FILL_HUE_MIN`, colours
    close to white excluded) or a colour as dark as `_FILL_GREY_MAX` or darker. Only numeric grey, RGB and CMYK colours are
    considered (unknown colour spaces and patterns are False).

    Light grey is not considered (deliberate): forms commonly paint 'do not fill in' cells light grey, and ■ would read as a
    'selected cell' (pinned by: test_pdf.py::TestCellsMarkedOnlyByFill::test_shading_that_is_not_a_mark_leaves_empty_cells_empty,
    the light-grey case).
    """
    if not isinstance(colour, (tuple, list)) or not colour or not all(isinstance(value, (int, float)) for value in colour):
        return False
    if len(colour) == 4:
        cyan, magenta, yellow, black = colour
        colour = tuple((1 - value) * (1 - black) for value in (cyan, magenta, yellow))
    if min(colour) >= _FILL_WHITE_MIN:
        return False
    return max(colour) - min(colour) >= _FILL_HUE_MIN or max(colour) <= _FILL_GREY_MAX


def _fills_by_row(rows: list[object], fills: list[dict]) -> list[list[dict]]:
    """For each table row, the fill rectangles that reach that row's height. Sweeps top to bottom and keeps only the ones that
    reach — looking at every rectangle for every cell was quadratic time on a table with thousands of filled cells
    (pinned by: test_pdf_resource_limits.py::TestManyCellsOnOnePage)."""
    ordered = sorted(fills, key=lambda rect: rect["top"])
    result, active, position = [], [], 0
    for row in sorted(range(len(rows)), key=lambda index: rows[index].bbox[1]):
        _, top, _, bottom = rows[row].bbox
        while position < len(ordered) and ordered[position]["top"] < bottom:
            active.append(ordered[position])
            position += 1
        active = [rect for rect in active if rect["bottom"] > top]
        result.append((row, _by_area(active)))
    return [near for _, near in sorted(result, key=lambda item: item[0])]


def _fill_cover(cell: tuple[float, float, float, float], fills: list[dict]) -> float:
    """The share of the cell box's (x0, top, x1, bottom) area covered by the fill rectangles (overlapping rectangles are summed
    and clamped at 1).

    Once fully covered, the remaining rectangles are not added — 90,000 cells under thousands of rectangles covering the whole
    table summed all of them per cell and took 78 seconds (fuzzing, pinned by:
    test_pdf_resource_limits.py::TestCellsReachingFarOffThePage). The caller passes the largest rectangles first (`_by_area`)."""
    x0, top, x1, bottom = cell
    area = max((x1 - x0) * (bottom - top), 1e-6)
    inside = 0.0

    for rect in fills:
        inside += max(0.0, min(x1, rect["x1"]) - max(x0, rect["x0"])) * max(0.0, min(bottom, rect["bottom"]) - max(top, rect["top"]))
        if inside >= area:
            return 1.0
    return min(inside / area, 1.0)


def _by_area(rects: list[dict]) -> list[dict]:
    """The rectangles ordered largest first (so `_fill_cover` stops early)."""
    return sorted(rects, key=lambda rect: (rect["x1"] - rect["x0"]) * (rect["bottom"] - rect["top"]), reverse=True)


def _with_shaded_rows(page: object, tables: list[object]) -> list[object]:
    """The table list found again after adding, to ruled tables whose cell boundaries came from cell shading (rectangles that
    are filled but have no border), the unshaded rows that continue on the same column boundaries. `tables` as is when
    there is nothing to change.

    pdfplumber also uses the sides of filled rectangles as rules. In a striped table filled cell by cell (Chrome
    `tr:nth-child(even)`, Word 'Light Shading') only the shaded rows became a ruled table and the unshaded rows became
    separate paragraphs, so the row order was shuffled and the numbers scattered. A table that shades only the header cells
    and draws only a bottom border under the data rows (Chrome th background) became a table of the header alone. A boxed
    table whose vertical lines exist only on the header row (Word 'Medium Shading 1') had its unshaded rows merged into one
    cell (pinned by: test_pdf_shaded_tables.py::TestShadedTables). Only for tables with cell shading, the page is split into
    bands by that table's column boundaries and vertical lines are added only to bands whose cell text fits the cell
    boundaries (`_grown_shaded_table`).
    """
    shades = _by_area([rect for rect in page.rects
                       if rect.get("fill") and not rect.get("stroke") and _is_shade(rect.get("non_stroking_color"))
                       and min(rect["x1"] - rect["x0"], rect["bottom"] - rect["top"]) > _SHADE_MIN_SIDE])
    if not shades:
        return tables
    rules: list[tuple[float, float, float]] | None = None  # collected the first time they are needed
    result = list(tables)
    shaded = _ShadingTest(shades)

    for table in sorted(tables, key=lambda table: table.bbox[1]):
        if not any(other is table for other in result):
            continue  # merged into an earlier table
        if not shaded(table):
            continue
        rules = _horizontal_rules(page) if rules is None else rules
        grown = _grown_shaded_table(page, table, result, rules)
        if grown is None:
            continue
        new, replaced = grown
        position = min(index for index, other in enumerate(result) if any(other is old for old in replaced))
        result = [other for other in result if not any(other is old for old in replaced)]
        result.insert(position, new)
    return _with_merged_column_fragments(page, result, shades, shaded)


def _with_merged_column_fragments(page: object, tables: list[object], shades: list[dict],
                                  shaded: "_ShadingTest | None" = None) -> list[object]:
    """The table list found again after joining, among shaded table fragments, those whose column boundaries are a subset of
    another fragment's (a fragment missing its leading column because of a vertically merged cell) with that wider fragment
    into one table. `tables` as is when there is nothing to change.

    A vertically merged cell in a striped table takes the fill of the row it starts on (rowspan under Chrome
    `tr:nth-child(even)`). A fragment starting at a shaded merged cell has 4 columns and a fragment beside an unshaded merged
    cell has 3 columns with the first one missing, so the growing step (`_grown_shaded_table`), which joins only fragments
    with the same column boundaries, could not join the two. The table grown from the 3-column fragment overlapped the
    4-column fragment and rows came out twice, or it could not be grown and the '2팀' and '개발팀' rows fell out of the
    table (pinned by: test_pdf_shaded_tables.py::TestShadedTablesWithMergedCells). Only when the wide fragment (the one with
    the most column boundaries) and a fragment having only some of those boundaries are within `_SHADED_ROW_REACH` row
    heights, the region covering both is split again by the wide fragment's column boundaries (`_regridded_fragments`).
    Fragments with the same column boundaries are left alone (so the in-between bands the growing step rejected are not
    revived).
    """
    shaded = [table for table in tables if (shaded or _ShadingTest(shades))(table)]
    if len(shaded) < 2 and not any(_has_holes(table) for table in shaded):
        return tables
    result = list(tables)
    row_height = _row_pitch(shaded)

    for widest in sorted(shaded, key=lambda table: -len(_column_edges(table))):
        edges = _column_edges(widest)
        if not any(other is widest for other in result) or len(edges) < 3:
            continue
        members = [widest]
        grew = True

        while grew:
            top, bottom = min(member.bbox[1] for member in members), max(member.bbox[3] for member in members)
            near = [other for other in shaded
                    if any(other is kept for kept in result) and not any(other is member for member in members)
                    and _edges_inside(_column_edges(other), edges) and other.bbox[1] - bottom <= row_height * _SHADED_ROW_REACH
                    and top - other.bbox[3] <= row_height * _SHADED_ROW_REACH]
            members += near
            grew = bool(near)
        if not any(len(_column_edges(member)) < len(edges) or _has_holes(member) for member in members):
            continue

        merged = _regridded_fragments(page, members, edges, shades, row_height)
        if merged is None:
            continue
        position = min(index for index, other in enumerate(result) if any(other is member for member in members))
        result = [other for other in result if not any(other is member for member in members)]
        result.insert(position, merged)
    return result


class _ShadingTest:
    """Whether a ruled table has cell shading: a cell covered by the shading rectangles (`shades`, largest first) for at
    least `_FILL_MIN_COVER` of its area (`_with_shaded_rows`, `_with_merged_column_fragments`). The verdict of each table is
    kept, and a table is measured only against the rectangles that overlap its box — the rest add nothing to any cell, and
    adding them for every cell of a table without shading took 10 seconds on a page of a 60 × 60 grid beside 3,000 small
    filled bars (Pinned by: test_pdf_shaded_tables.py::TestShadingOutsideTheTable)."""

    def __init__(self, shades: list[dict]) -> None:
        self._shades = shades
        self._verdicts: dict[int, tuple[object, bool]] = {}  # id of the table → (the table, verdict)

    def __call__(self, table: object) -> bool:
        known = self._verdicts.get(id(table))
        if known is not None and known[0] is table:
            return known[1]
        x0, top, x1, bottom = table.bbox
        # (a rectangle with a coordinate that is not a number fails these comparisons and adds nothing; added for every
        # cell, `min`/`max` in `_fill_cover` took it for one covering the whole cell)
        near = [shade for shade in self._shades
                if shade["x0"] < x1 and shade["x1"] > x0 and shade["top"] < bottom and shade["bottom"] > top]
        verdict = bool(near) and any(cell and _fill_cover(cell, touching) >= _FILL_MIN_COVER
                                     for cell, touching in _touching_each(table.cells, near))
        self._verdicts[id(table)] = (table, verdict)
        return verdict


_TOUCH_BAND = 16.0  # height (pt) of the bands `_touching_each` sorts rectangles into
_TOUCH_MAX_BANDS = 128  # a rectangle or cell taller than this many bands (or not finite) is matched without bands


def _touching_each(cells: list[tuple | None], rects: list[dict]) -> list[tuple[tuple | None, list[dict]]]:
    """Each cell box with the rectangles of `rects` that overlap it, in the order of `rects` — the rectangles `_fill_cover`
    adds anything for (the others add nothing). Rectangles are sorted into height bands once, so a cell looks only at the
    rectangles of its own bands: a table over thousands of small filled bars scanned every bar for every cell."""
    def overlaps(cell: tuple, rect: dict) -> bool:
        return rect["x0"] < cell[2] and rect["x1"] > cell[0] and rect["top"] < cell[3] and rect["bottom"] > cell[1]

    def bands(top: float, bottom: float) -> range | None:
        if not (math.isfinite(top) and math.isfinite(bottom)) or bottom - top > _TOUCH_BAND * _TOUCH_MAX_BANDS:
            return None
        return range(math.floor(top / _TOUCH_BAND), math.floor(bottom / _TOUCH_BAND) + 1)

    banded: dict[int, list[int]] = {}
    unbanded: list[int] = []  # positions of rectangles matched against every cell
    for position, rect in enumerate(rects):
        spread = bands(rect["top"], rect["bottom"])
        if spread is None:
            unbanded.append(position)
        else:
            for band in spread:
                banded.setdefault(band, []).append(position)
    result = []
    for cell in cells:
        if not cell:
            result.append((cell, []))
            continue
        spread = bands(cell[1], cell[3])
        positions = (range(len(rects)) if spread is None
                     else sorted({*unbanded, *(position for band in spread for position in banded.get(band, ()))}))
        result.append((cell, [rects[position] for position in positions if overlaps(cell, rects[position])]))
    return result


def _row_pitch(tables: list[object]) -> float:
    """The row height of the table fragments: the median gap between the top and bottom sides of the cell boxes (sides within
    1pt count as one). Rows of merged cells are tall.

    Using the median of pdfplumber's row heights gave two row heights on a table where a vertically merged cell spans every
    row, so the header band right above also covered the caption above the table and the header was not accepted
    (pinned by: test_pdf_round44.py::TestZebraTableWhoseMergedGroupsStartOnShadedRows)."""
    heights: list[float] = []
    for y in sorted({y for table in tables for cell in table.cells if cell for y in (cell[1], cell[3])}):
        if not heights or y - heights[-1] > 1:
            heights.append(y)
    gaps = [lower - upper for upper, lower in pairwise(heights)]
    return statistics.median(gaps) if gaps else statistics.median(row.bbox[3] - row.bbox[1] for table in tables for row in table.rows)


def _has_holes(table: object) -> bool:
    """Whether the cell boxes of a ruled table fail to cover the whole table box (there are gaps with no cell).

    When every vertically merged group of a striped table starts on a shaded row, the single table found from the sides of
    the filled rectangles alone has gaps from the second column onward on the unshaded rows. There was no separate wide and
    narrow fragment, so nothing was split again and those rows scattered outside the table one number at a time
    (pinned by: test_pdf_round44.py::TestZebraTableWhoseMergedGroupsStartOnShadedRows)."""
    x0, top, x1, bottom = table.bbox
    covered = sum((cell[2] - cell[0]) * (cell[3] - cell[1]) for cell in table.cells if cell)
    return covered < (x1 - x0) * (bottom - top) * 0.98


def _edges_inside(edges: list[float], others: list[float]) -> bool:
    """Whether there are two or more column boundaries in `edges` and each matches some boundary of `others` within 1pt."""
    return len(edges) >= 2 and all(any(abs(edge - other) <= 1 for other in others) for edge in edges)


def _regridded_fragments(page: object, members: list[object], edges: list[float], shades: list[dict],
                         row_height: float) -> object | None:
    """The single table found again over the region covering the table fragments (`members`) using the column boundaries
    `edges`. None when it cannot be found.

    The row boundaries are the fragments' row boundaries plus the top and bottom sides of the filled rectangles inside that
    region. The header band right above (`_is_header_band`) and the closing band right below (`_band_cells` +
    `_ends_the_text`, the last unshaded row) are added by the same criteria as the growing step. Vertical lines are drawn at
    every column boundary over the full height of the region; horizontal lines are drawn, per column, only at row boundaries
    that the column's text does not straddle and that are not inside a filled rectangle covering that column — a merged cell
    ('영업본부' shaded over 3 rows, '기술본부' whose text straddles a row boundary by more than `_MERGED_TEXT_CROSSING`)
    stays one cell and its value is repeated on the rows below (`_cell_rows`).
    If any line of the region crosses a column boundary at word spacing (`_band_chars`), it is not a table.
    """
    left, right = edges[0], edges[-1]
    top, bottom = min(member.bbox[1] for member in members), max(member.bbox[3] for member in members)
    above, below = (top - row_height, top), (bottom, bottom + row_height)
    if _is_header_band(page, above, edges) or (_band_cells(page, above, edges) is not None
                                               and _ends_the_text(page, above, -1, left, right, row_height)):
        top = above[0]  # a year header ('2024') is a number cell, so the header-shape test misses it
    if _band_cells(page, below, edges) is not None and _ends_the_text(page, below, 1, left, right, row_height):
        bottom = below[1]
    if _band_chars(page, (top, bottom), edges) is None:
        return None

    covers = [shade for shade in shades if shade["x0"] >= left - 1 and shade["x1"] <= right + 1
              and shade["top"] >= top - 1 and shade["bottom"] <= bottom + 1]
    heights = sorted({top, bottom, *(y for member in members for row in member.rows for y in (row.bbox[1], row.bbox[3])),
                      *(y for shade in covers for y in (shade["top"], shade["bottom"]))})
    boundaries = [y for index, y in enumerate(heights) if index == 0 or y - heights[index - 1] > _TABLE_ROW_TOLERANCE]
    # Only a character whose top lies above the boundary by less than the tallest character can reach across it (a character
    # with a box that is not finite or not positive never does), so the characters are sorted by top once and each boundary
    # looks only at that slice — scanning every character of the region for every boundary and column took 14 seconds on a
    # striped table of 700 rows (Pinned by: test_pdf_shaded_tables.py::TestLongZebraTableWithMergedGroups)
    chars = sorted((char for char in page.chars if char["text"].strip() and left < (char["x0"] + char["x1"]) / 2 < right
                    and math.isfinite(char["top"]) and math.isfinite(char["bottom"]) and char["bottom"] > char["top"]),
                   key=lambda char: char["top"])
    char_tops = [char["top"] for char in chars]
    tallest = max((char["bottom"] - char["top"] for char in chars), default=0.0)

    def crossed(y: float, x0: float, x1: float) -> bool:  # whether boundary y lies inside a single cell in this column
        near = chars[bisect_left(char_tops, y - tallest - 1):bisect_right(char_tops, y + 1)]
        return (any(x0 < (char["x0"] + char["x1"]) / 2 < x1
                    and min(y - char["top"], char["bottom"] - y) > (char["bottom"] - char["top"]) * _MERGED_TEXT_CROSSING
                    for char in near)
                or any(shade["x0"] <= x0 + 1 and shade["x1"] >= x1 - 1 and shade["top"] + 1 < y < shade["bottom"] - 1
                       for shade in covers))

    verticals = [_rule_line(x, top, x, bottom) for x in edges]
    horizontals = [_rule_line(x0, y, x1, y) for y in boundaries for x0, x1 in zip(edges, edges[1:], strict=False)
                   if y in (top, bottom) or not crossed(y, x0, x1)]

    page_left, page_top, page_right, page_bottom = page.bbox
    box = (max(left - 1, page_left), max(top - 1, page_top), min(right + 1, page_right), min(bottom + 1, page_bottom))
    found = page.crop(box).find_tables({**_TABLE_SETTINGS, "explicit_vertical_lines": verticals,
                                        "explicit_horizontal_lines": horizontals})
    if len(found) != 1 or len(_column_edges(found[0])) != len(edges):
        return None
    return found[0]


def _horizontal_rules(page: object, edges: list[dict] | None = None) -> list[tuple[float, float, float]]:
    """The page's horizontal rules (lines and rectangle sides, or `edges`) as a list of (height, left end, right end), with
    pieces that continue at the same height (within 1pt) joined."""
    rules: list[tuple[float, float, float]] = []
    edges = [edge for edge in page.edges if edge["orientation"] == "h"] if edges is None else edges
    for edge in sorted(edges, key=lambda edge: (edge["top"], edge["x0"])):
        if rules and abs(edge["top"] - rules[-1][0]) <= 1 and edge["x0"] <= rules[-1][2] + 1:
            rules[-1] = (rules[-1][0], rules[-1][1], max(rules[-1][2], edge["x1"]))
        else:
            rules.append((edge["top"], edge["x0"], edge["x1"]))
    return rules


def _thin_rules(page: object) -> list[tuple[float, float, float]]:
    """The page's thin horizontal rules (horizontal lines, and filled rectangles no taller than `_SHADE_MIN_SIDE` — Chrome
    borders) as a joined list (`_horizontal_rules`). The sides of shaded row rectangles are left out — they would make every
    row of a striped table look ruled."""
    thin = [{"top": (line["top"] + line["bottom"]) / 2, "x0": line["x0"], "x1": line["x1"]} for line in page.lines
            if line["bottom"] - line["top"] <= 1]
    thin += [{"top": (rect["top"] + rect["bottom"]) / 2, "x0": rect["x0"], "x1": rect["x1"]} for rect in page.rects
             if rect["bottom"] - rect["top"] <= _SHADE_MIN_SIDE and rect["x1"] - rect["x0"] > _SHADE_MIN_SIDE]
    return _horizontal_rules(page, thin)


def _shade_bands(page: object) -> list[tuple[float, float, float, float]]:
    """(top, bottom, left end, right end) of the page's filled rectangles (no border, not white, both sides longer than
    `_SHADE_MIN_SIDE`)."""
    return [(rect["top"], rect["bottom"], rect["x0"], rect["x1"]) for rect in page.rects
            if rect.get("fill") and not rect.get("stroke") and _is_shade(rect.get("non_stroking_color"))
            and min(rect["x1"] - rect["x0"], rect["bottom"] - rect["top"]) > _SHADE_MIN_SIDE]


def _column_edges(table: object) -> list[float]:
    """The vertical boundaries of a ruled table's cell boxes (left to right, boundaries within 1pt count as one)."""
    edges: list[float] = []
    for x in sorted({x for cell in table.cells if cell for x in (cell[0], cell[2])}):
        if not edges or x - edges[-1] > 1:
            edges.append(x)
    return edges


def _grown_shaded_table(page: object, table: object, tables: list[object],
                        rules: list[tuple[float, float, float]]) -> tuple[object, list[object]] | None:
    """The table found again after growing a ruled table with cell shading (`table`) by bands on the same column boundaries,
    and the tables it replaces. None when there is nothing to change.

    Upward and downward, a band runs up to (1) another table fragment with the same column boundaries (the next shaded row of
    the stripes), (2) a horizontal rule covering the table width (a row's bottom border), or, when neither exists, (3) one
    band as tall as the fragment's row height (the last unshaded row); it continues only while each band's cell text fits the
    column boundaries (`_band_cells`). Rows with merged cells inside the table get the same test. Such bands and rows get
    vertical lines only when, in two or more columns, the cell text matches the same column's text on another row at the left
    end, right end or centre (`_aligned_cells`) — a body paragraph or first-column note under a shaded header, or a centred
    merged title row, does not become cells. As an exception, when the topmost closed band has non-numeric text in every
    column (`_is_header_band`) it is accepted as the header — the left-aligned text of an unshaded header did not match the
    right-aligned number columns and the whole growing step was given up (pinned by: test_pdf_shaded_tables.py::
    TestShadedTables::test_unshaded_header_over_banded_rows_with_right_aligned_numbers_stays_one_table). Vertical lines are
    drawn in each band exactly at the cell boundaries, only that region is searched again, and the table is replaced only
    when exactly one table comes out.
    """
    edges = _column_edges(table)
    if len(edges) < 3:
        return None
    left, right = edges[0], edges[-1]
    fragments = [other for other in tables if _same_column_edges(_column_edges(other), edges)]
    # rows spanned by a merged cell are tall, so the last open band also covered the paragraph below the table ('끝.')
    row_height = _row_pitch(fragments)
    reach = row_height * _SHADED_ROW_REACH
    spanning = sorted(y for y, x0, x1 in rules if x0 <= left + 2 and x1 >= right - 2)
    chain, bands = [table], []  # the table fragments joined so far, (top, bottom, whether it is an open band)
    top, bottom = table.bbox[1], table.bbox[3]
    # The top and bottom of every fragment are measured once and the joined fragments are looked up by identity. Measuring
    # every fragment and searching the whole chain for it at every step grew with the cube of the number of shaded rows: 5
    # of the 7 seconds this function took on a striped table of 2,000 rows (a 53 KB file) (Pinned by:
    # test_audit_pdf_mid.py::TestLongStripedTable::test_fragments_are_measured_once)
    ends = [(fragment.bbox[1], fragment.bbox[3], fragment) for fragment in fragments]
    joined = {id(table)}

    # continue the bands downward (+1) and upward (-1)
    for direction in (1, -1):
        edge = bottom if direction > 0 else top
        while True:
            nearby = [(start if direction > 0 else end, fragment) for start, end, fragment in ends if id(fragment) not in joined]
            nearby = [(y, fragment) for y, fragment in nearby if -1 <= (y - edge) * direction <= reach]
            nearby += [(y, None) for y in spanning if 1 < (y - edge) * direction <= reach]
            if not nearby:
                band = (edge, edge + row_height) if direction > 0 else (edge - row_height, edge)
                if _band_cells(page, band, edges) is not None and _ends_the_text(page, band, direction, left, right, row_height):
                    bands.append((*band, True))  # added to the region only when accepted (below)
                elif direction < 0 and _band_cells(page, band, edges) is not None:
                    bands += _unshaded_head(page, band, edges, row_height)
                break
            y, fragment = min(nearby, key=lambda item: (item[0] - edge) * direction)
            if (y - edge) * direction > 1:
                band = (edge, y) if direction > 0 else (y, edge)
                if _band_cells(page, band, edges) is None:
                    break
                bands.append((*band, False))
            if fragment is None:
                edge = y
            else:
                chain.append(fragment)
                joined.add(id(fragment))
                edge = fragment.bbox[3] if direction > 0 else fragment.bbox[1]
        if direction > 0:
            bottom = edge
        else:
            top = edge

    # rows with every cell present are the reference for column alignment; rows with merged cells are, like the bands,
    # candidates to split again
    columns = len(edges) - 1
    references, merged = [], []  # cell text of the reference rows, (band, cell text, whether it is an open band)
    for fragment in chain:
        for row in fragment.rows:
            band = (row.bbox[1], row.bbox[3])
            cells = _band_cells(page, band, edges)
            if len(row.cells) == columns and all(cell is not None for cell in row.cells):
                references.append(cells)
            elif cells is not None:
                merged.append((band, cells, False))
    between = [((band_top, band_bottom), _band_cells(page, (band_top, band_bottom), edges), is_open)
               for band_top, band_bottom, is_open in bands]
    candidates = [*merged, *between]

    def aligned(entry: tuple) -> bool:
        return _aligned_cells(entry[1], [*references, *(other[1] for other in candidates if other is not entry)])

    # when the topmost closed band is header-shaped it is accepted without column alignment — a left-aligned header does not
    # match right-aligned number columns
    heading = next((entry for entry in between if not entry[2] and abs(entry[0][0] - top) <= 1
                    and _is_header_band(page, entry[0], edges)), None)
    if any(not entry[2] and entry is not heading and not aligned(entry) for entry in between):
        return None  # a band between shaded fragments is not a table row
    accepted = [(entry[0], entry[2]) for entry in candidates if entry is heading or aligned(entry)]
    if not accepted:
        return None

    # draw the column-boundary vertical lines only in the accepted bands (plus the outer horizontal lines for open bands) and
    # search only that region again
    top = min([top, *(band[0] for band, is_open in accepted if is_open)])
    bottom = max([bottom, *(band[1] for band, is_open in accepted if is_open)])
    verticals = [_rule_line(x, band_top, x, band_bottom) for (band_top, band_bottom), _ in accepted for x in edges]
    horizontals = [_rule_line(left, y, right, y) for (band_top, band_bottom), is_open in accepted if is_open
                   for y in (band_top, band_bottom)]
    page_left, page_top, page_right, page_bottom = page.bbox
    box = (max(left - 1, page_left), max(top - 1, page_top), min(right + 1, page_right), min(bottom + 1, page_bottom))
    if box[0] >= box[2] or box[1] >= box[3]:
        return None
    found = page.crop(box).find_tables({**_TABLE_SETTINGS, "explicit_vertical_lines": verticals,
                                        "explicit_horizontal_lines": horizontals})
    if len(found) != 1 or len(_column_edges(found[0])) != len(edges):
        return None
    return found[0], chain


def _unshaded_head(page: object, band: tuple[float, float], edges: list[float],
                   row_height: float) -> list[tuple[float, float, bool]]:
    """The band list when the unshaded row (`band`) above the table's topmost shaded fragment and the header band above it
    are accepted together as open bands. Otherwise [].

    Only one open band was accepted above and one below, so in a striped table whose header and first data row are both
    unshaded (Chrome `tbody tr:nth-child(even)`) the header continued above the first row at line spacing and the first row
    was not accepted. The header scattered into '번호 지점 / 금액 / 상태' and the first row became a jumbled paragraph,
    worse than with no shading at all (pinned by: test_pdf_round44.py::TestZebraTableUnderAnUnshadedHeaderAndFirstRow).
    Accepted only when the band above is header-shaped (`_is_header_band`) and the text above it is set apart."""
    head = (band[0] - row_height, band[0])
    if not _is_header_band(page, head, edges) or not _ends_the_text(page, head, -1, edges[0], edges[-1], row_height):
        return []
    return [(*band, True), (*head, True)]


def _ends_the_text(page: object, band: tuple[float, float], direction: int, left: float, right: float, row_height: float) -> bool:
    """Whether, beyond the text of the open band at the end of a table (`band`, outward direction `direction`), the next text
    within the table width is more than half a row height away. Text that continues past the band at the same line spacing
    (a list under a shaded title box) is not the table's last row."""
    chars = [char for char in page.chars if char["text"].strip() and char["x1"] > left and char["x0"] < right]
    inside = [char for char in chars if band[0] < (char["top"] + char["bottom"]) / 2 < band[1]]
    if not inside:
        # The band's text (`_band_chars`) may be characters of no height lying exactly on its edge: no text of its own ends
        # here (Pinned by: test_pdf_shaded_tables.py::test_band_whose_only_text_lies_on_its_edge_does_not_end_the_text)
        return False
    if direction > 0:
        end = max(char["bottom"] for char in inside)
        return all(char["top"] - end >= row_height / 2 for char in chars if char["top"] >= band[1] - 1)
    end = min(char["top"] for char in inside)
    return all(end - char["bottom"] >= row_height / 2 for char in chars if char["bottom"] <= band[0] + 1)


def _same_column_edges(edges: list[float], others: list[float]) -> bool:
    return len(edges) == len(others) and all(abs(edge - other) <= 1 for edge, other in zip(edges, others, strict=True))


def _rule_line(x0: float, top: float, x1: float, bottom: float) -> dict:
    """A line to add to table finding (a line object for pdfplumber `explicit_*_lines`)."""
    return {"object_type": "line", "x0": x0, "x1": x1, "top": top, "bottom": bottom, "width": x1 - x0, "height": bottom - top}


def _band_cells(page: object, band: tuple[float, float], edges: list[float]) -> list[tuple[float, float] | None] | None:
    """(left end, right end) per column of the text in a horizontal band (`band` = top, bottom) split by the column boundaries
    (`edges`). Only when there is text in two or more columns, all the text lies inside the band and the table width, and no
    line crosses a column boundary at word spacing; otherwise None (`_band_chars`)."""
    cells = _band_chars(page, band, edges)
    if cells is None:
        return None
    return [(min(char["x0"] for char in cell), max(char["x1"] for char in cell)) if cell else None for cell in cells]


def _is_header_band(page: object, band: tuple[float, float], edges: list[float]) -> bool:
    """Whether the band (`band`) is shaped like a header row: text in every column and not a single number cell."""
    cells = _band_chars(page, band, edges)
    if cells is None or not all(cells):
        return False
    texts = ("".join(char["text"] for char in sorted(cell, key=lambda char: char["x0"])) for cell in cells)
    return not any(_NUMBER_CELL.match(text) for text in texts)


def _band_chars(page: object, band: tuple[float, float], edges: list[float]) -> list[list[dict]] | None:
    """The characters per column for `_band_cells` (the column containing each character's centre). None unless the same
    conditions hold.

    A character that reaches into the band by less than `_BAND_SLACK_RATIO` of its height belongs to the neighbouring row and
    is not counted — the character boxes of a Word number column touched the band of the row below by 0.06pt, that band was
    rejected as 'text sticking out of the band', and the unshaded rows merged into one cell (pinned by:
    test_pdf_shaded_tables.py::TestShadedTables::test_numbers_that_barely_touch_the_next_row_do_not_merge_its_cells).
    """
    top, bottom = band
    left, right = edges[0], edges[-1]
    tops, ordered, tallest = _chars_by_top(page)
    chars = [char for char in ordered[bisect_left(tops, top - tallest):bisect_left(tops, bottom)]
             if char["x1"] > left and char["x0"] < right
             and min(char["bottom"], bottom) - max(char["top"], top) >= (char["bottom"] - char["top"]) * _BAND_SLACK_RATIO]
    for char in chars:
        slack = (char["bottom"] - char["top"]) * _BAND_SLACK_RATIO
        if (char["top"] < top - slack or char["bottom"] > bottom + slack or char["x0"] < left - 1 or char["x1"] > right + 1):
            return None

    cells: list[list[dict]] = [[] for _ in edges[1:]]  # characters per column (the column containing the centre)

    for char in chars:
        middle = (char["x0"] + char["x1"]) / 2
        cells[max(0, min(bisect_right(edges, middle) - 1, len(cells) - 1))].append(char)

    # when the text of two neighbouring columns on the same line sits at word spacing on either side of a column boundary, the
    # text crosses that boundary
    # (each character looks only at the characters of the next column whose top is within its reach, sorted by top once —
    # comparing every pair took time with the square of the band's characters when the band is a whole table,
    # `_regridded_fragments`)
    for index in range(1, len(cells)):
        others = sorted((other for other in cells[index] if math.isfinite(other["top"])), key=lambda other: other["top"])
        other_tops = [other["top"] for other in others]
        for char in cells[index - 1]:
            size = char.get("size") or (char["bottom"] - char["top"])
            if math.isfinite(size) and math.isfinite(char["top"]):
                reach = abs(size) / 2 + 1
                near = others[bisect_left(other_tops, char["top"] - reach):bisect_right(other_tops, char["top"] + reach)]
            else:
                near = cells[index]
            if any(abs(other["top"] - char["top"]) <= size / 2 and other["x0"] - char["x1"] < size * _CELL_GAP_MIN_RATIO
                   for other in near):
                return None
    if sum(1 for cell in cells if cell) < 2:
        return None
    return cells


def _chars_by_top(page: object) -> tuple[list[float], list[dict], float]:
    """The page's non-blank characters in order of their top edge, as (list of top edges, list of characters, tallest character
    height). Built once per page (`_band_chars`) as one of the page's lists for its tables (`_page_index`), so it is dropped
    with them when the tables have been read and built again for a page read again.

    Scanning every character of the page for every band took 48 seconds on a large page with many shaded bands and tables
    (1500pt square, 50,000 characters, 196 tables) (pinned by:
    test_pdf_shaded_tables.py::test_many_bands_on_a_page_with_many_letters_take_linear_time). Kept on the page object by
    itself, it held the characters of every page read until the end of the document and gave the old characters to a page
    read again (Pinned by: test_pdf_shaded_tables.py::TestCharactersByTop).
    """
    def build() -> tuple[list[float], list[dict], float]:
        ordered = sorted((char for char in page.chars if char["text"].strip()), key=lambda char: char["top"])
        return [char["top"] for char in ordered], ordered, max((char["bottom"] - char["top"] for char in ordered), default=0.0)

    return _page_index(page, "chars_by_top", build)


def _aligned_cells(cells: list[tuple[float, float] | None] | None,
                   others: list[list[tuple[float, float] | None] | None]) -> bool:
    """Whether, in two or more columns, the band's cell text (`cells`) matches the same column's text on the other rows
    (`others`) at the left end, right end or centre."""
    if cells is None:
        return False
    matched = 0

    for column, cell in enumerate(cells):
        if cell is None:
            continue
        if any(other is not None and other[column] is not None
               and min(abs(other[column][0] - cell[0]), abs(other[column][1] - cell[1]),
                       abs(sum(other[column]) - sum(cell)) / 2) <= _TABLE_ROW_TOLERANCE
               for other in others):
            matched += 1
    return matched >= 2


def _is_inner_part(part: tuple[float, ...], closed: tuple[float, ...]) -> bool:
    """Whether `part`, found as an open-sided table, is an inner part of `closed`, a table found from rules alone: same height
    and sharing exactly one of the left and right ends.

    When the first column of a table closed on all sides is vertically merged and some row merges columns 1-2 horizontally
    (a header '구분' spanning two columns, a '합계' (total) row), the vertical line between columns 1 and 2 does not run the
    full height of the table, so the horizontal lines that stop short of the merged cell look like a 'table open on the
    left' to `_open_sided_tables`. If the partial table found that way from column 2 onward is kept, the whole table that
    encloses it is discarded as a layout box in `_real_tables` and the category column came out as lines outside the table
    (pinned by: test_pdf.py::TestRuledTables::
    test_closed_table_with_merged_first_column_under_a_two_column_header_is_kept_whole).
    The condition is narrowed to match how `_open_sided_tables` filters regions against each other (deliberate): tables
    that share no end or share both ends, and tables of a different height (a table placed inside a box), are left as
    before — an outer box enclosing an inner table may be layout.
    """
    same_height = abs(closed[1] - part[1]) <= 2 and abs(closed[3] - part[3]) <= 2
    return same_height and _encloses(closed, part) and (abs(closed[0] - part[0]) <= 2) != (abs(closed[2] - part[2]) <= 2)
