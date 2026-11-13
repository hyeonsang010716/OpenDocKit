"""Tables whose outline is not closed: tables open at the sides, rules drawn in pieces joined, and a header split by the
rules merged back.
"""

from bisect import bisect_left, bisect_right, insort
from collections.abc import Callable

from opendockit.extractors._pdf_model import (
    _MIN_FIGURE_POINTS,
    _TABLE_SETTINGS,
    _box_area,
    _encloses,
    _is_white,
)


# Gap and overlap (pt) allowed between the end of one segment and the start of the next when joining horizontal line segments into
# one line (_joined_rules). Lines drawn per cell have the same coordinate (gap 0) or overlap by the line width.
_RULE_JOIN_GAP = 0.1
_RULE_JOIN_OVERLAP = 1.0
# Vertical gap (pt) allowed when taking vertical line segments drawn broken at each row as one line (_chained_verticals). Real
# generators break them by 0.25pt
_VERTICAL_JOIN_GAP = 1.5


def _open_sided_tables(page: object) -> list[object]:
    """Find tables without left and right borders again at their full width.

    A shape common in Korean reports: horizontal lines are drawn across the full table width and vertical lines only between
    the inner columns. Finding cells from the rules made only the columns between vertical lines a table, and the first and
    last columns scattered outside it. When there are three or more horizontal lines of the same width with vertical lines
    inside them but no vertical line at the ends of the horizontal lines, only that region is cropped and the table is
    searched for with both ends of the horizontal lines given as vertical borders
    (pinned by: test_quality.py::test_pdf_table_without_outer_borders_keeps_its_first_and_last_columns).

    When the first (or last) column is vertically merged there are two kinds of horizontal-line width (the full width, and
    the width that stops short of the merged cell), so a region also arises from the narrow width. The widest region is
    searched first, and a region inside a region where a table was found that shares only one end with it is part of the same
    table and is skipped. Searching it separately made the whole table that encloses the partial table get discarded as a
    layout box in `_real_tables`, and the category column went outside the table (the horizontal counterpart of
    `_outermost_spans`. Pinned by:
    test_pdf.py::TestRuledTables::test_open_sided_table_with_vertically_merged_first_column_is_kept_whole).
    Other overlapping regions (sharing no end, or sharing both ends) are searched separately as before and filtered by
    `_real_tables` — the lines of a chart drawing became a large table and repeated the tick labels on every row (public
    paper PDF).
    """
    # Pages with many vector shapes (graphs in a paper) have tens of thousands of lines. Rescanning all of them for every line
    # takes seconds per page (600 million comparisons on a 27-page paper), so vertical lines are sorted by x and horizontal
    # lines by height and only the needed range is examined.
    edges = [edge for edge in page.edges if not _is_invisible_edge(edge)]
    verticals = _chained_verticals([edge for edge in edges if edge["orientation"] != "h"])
    positions = [edge["x0"] for edge in verticals]

    def verticals_between(low: float, high: float) -> list[dict]:
        return verticals[bisect_left(positions, low):bisect_right(positions, high)]

    def at_column_border(x: float, top: float) -> bool:
        return any(edge["top"] - 1 <= top <= edge["bottom"] + 1 for edge in verticals_between(x - 1, x + 1))

    horizontals: dict[tuple[int, int], list[float]] = {}
    for left, right, top in _joined_rules([edge for edge in edges if edge["orientation"] == "h"], at_column_border):
        horizontals.setdefault((round(left), round(right)), []).append(top)

    page_left, page_top, page_right, page_bottom = page.bbox
    areas = []  # (region to crop, left end, right end)
    for (left, right), rule_tops in horizontals.items():
        if len(rule_tops) < 3 or right - left < _MIN_FIGURE_POINTS:
            continue
        rule_tops.sort()
        sides = [verticals_between(side - 2, side + 2) for side in (left, right)]
        # The vertical range of the inner vertical lines is the table's height. When several tables of the same width are
        # stacked, each range is examined separately.
        spans = {(round(edge["top"]), round(edge["bottom"])) for edge in verticals_between(left + 2, right - 2)
                 if left + 2 < edge["x0"] < right - 2}
        for top, bottom in _outermost_spans(spans):
            rules = bisect_right(rule_tops, bottom + 2) - bisect_left(rule_tops, top - 2)
            if rules < 3:
                continue
            closed = [side for side in sides
                      if any(edge["top"] <= top + 2 and edge["bottom"] >= bottom - 2 for edge in side)]
            if len(closed) == 2:
                continue
            box = (max(left - 1, page_left), max(top - 1, page_top), min(right + 1, page_right), min(bottom + 1, page_bottom))
            if box[0] >= box[2] or box[1] >= box[3]:
                continue  # lines drawn off the page
            areas.append((box, left, right))

    # region number → the tables found in that region. The widest region is searched first, but they are returned in region order
    found: dict[int, list[object]] = {}
    for index in sorted(range(len(areas)), key=lambda index: -_box_area(areas[index][0])):
        box, left, right = areas[index]
        outers = [areas[other][0] for other, tables in found.items() if tables]
        if any(_encloses(outer, box) and (abs(outer[0] - box[0]) <= 2) != (abs(outer[2] - box[2]) <= 2) for outer in outers):
            continue
        found[index] = page.crop(box).find_tables({**_TABLE_SETTINGS, "explicit_vertical_lines": [left, right]})

    # Horizontal lines whose ends are off by about 1pt fall into two width groups, so the same table is found in two regions.
    # Keeping both made `_real_tables` see each as 'a box enclosing another table' and discard both, and a two-column
    # definition table ('총배출량 | 순배출량', a design-tool guidebook) scattered line by line (pinned by:
    # test_pdf.py::TestRuledTables::test_open_sided_table_found_from_two_rule_widths_is_kept_once)
    tables: list[object] = []
    for table in (table for index in sorted(found) for table in found[index]):
        if not any(all(abs(a - b) <= 2 for a, b in zip(table.bbox, kept.bbox, strict=True)) for kept in tables):
            tables.append(table)
    return tables


def _is_invisible_edge(edge: dict) -> bool:
    """Whether this is a side of a rectangle that is only filled white and has no border. It is invisible on white paper and so
    is not a table's outer border.

    In an open-sided table with a white filled rectangle under every header cell, the outer sides of
    those rectangles looked like vertical lines closing the left and right on the header row, so the full-width horizontal
    lines were not joined and the first column (vertically merged '가격 기반') and the last column ('적용순위') scattered
    outside the table
    (pinned by: test_pdf.py::TestRuledTables::test_open_sided_table_with_white_filled_header_cells_keeps_its_outer_columns).
    Known accepted gap: the sides of white cells laid over a filled background are dropped even though they are visible —
    only `_open_sided_tables` uses this test, and ruled-table finding uses those sides as before.
    """
    return (edge.get("object_type") == "rect_edge" and not edge.get("stroke") and bool(edge.get("fill"))
            and _is_white(edge.get("non_stroking_color")))


def _chained_verticals(verticals: list[dict]) -> list[dict]:
    """The vertical line pieces at the same x joined into one line where they overlap or are broken by no more than
    `_VERTICAL_JOIN_GAP` (in x order).

    A generator that draws the vertical lines separately per cell breaks them by 0.25pt at each row boundary. Measuring the
    vertical range per piece made the table height look like a single row, no range held three or more horizontal lines, and
    the open-sided table was not found (pinned by: test_pdf.py::TestRuledTables::
    test_open_sided_table_whose_column_rules_are_drawn_row_by_row_keeps_its_outer_columns). Keeping pieces drawn on top of
    each other at the same place (a header cell border and a column separator) apart produced overlapping candidate regions
    and a legal-register table scattered into per-cell paragraphs. Known accepted gap: the lifelines of a sequence diagram
    drawn twice like a shadow also become one line, so a drawing that used to cancel itself out as two overlapping
    candidates remains as a small fake table.
    """
    chained: list[dict] = []

    for edge in sorted(verticals, key=lambda edge: (round(edge["x0"], 1), edge["top"])):
        last = chained[-1] if chained else None
        if (last is not None and round(last["x0"], 1) == round(edge["x0"], 1)
                and edge["top"] - last["bottom"] <= _VERTICAL_JOIN_GAP):
            last["bottom"] = max(last["bottom"], edge["bottom"])
        else:
            chained.append({"x0": edge["x0"], "top": edge["top"], "bottom": edge["bottom"]})

    chained.sort(key=lambda edge: edge["x0"])
    return chained


def _joined_rules(edges: list[dict], at_column_border: Callable[[float, float], bool]) -> list[tuple[float, float, float]]:
    """(x0, x1, top) of the horizontal lines. The horizontal lines of an open-sided table drawn broken per cell are returned
    with their pieces joined into one line.

    Some generators draw a table's horizontal lines broken per cell. Grouping widths piece by piece left an open-sided table
    with not a single full-width horizontal line, and the first and last columns stayed outside the table (pinned by:
    test_pdf.py::TestRuledTables::
    test_open_sided_table_whose_rules_are_drawn_cell_by_cell_keeps_its_outer_columns). The joining conditions are kept narrow.
    - At the same height the end of the previous piece and the start of the next piece touch (a gap of at most
      `_RULE_JOIN_GAP`, an overlap of at most `_RULE_JOIN_OVERLAP`), and there is a vertical line (a column boundary) at that
      place. Joining dotted lines (0.4pt pieces, 0.45pt gaps) turned dotted cell boundaries into full-width horizontal lines
      and split the table
      (pinned by: test_quality.py::test_pdf_table_with_dotted_cell_borders_keeps_rows_and_repeats_merged_cell).
    - There is no vertical line at either end of the joined line (the sides are open). When there is a vertical line at an
      end the pieces are left as they are — the borders of chart panels placed side by side were joined into one table and
      even the figure captions ended up in cells (public paper PDF).
    """
    rules: list[list[dict]] = []  # joined pieces (left to right)
    # (height, end x) → (end x, order, line) of the lines that ended there, in end-x order. When looking for a touching line,
    # only the range of end x that can be joined is examined — scanning the list from the start took 457 seconds for 45,000
    # lines stacked at the same place (a 2KB PDF) (pinned by: test_pdf_resource_limits.py::TestStackedRules).
    ends: dict[tuple[float, int], list[tuple[float, int, list[dict]]]] = {}
    for order, edge in enumerate(sorted(edges, key=lambda edge: edge["x0"])):
        level, start = round(edge["top"], 1), round(edge["x0"])
        rule = None
        for x in (start, start + 1, start - 1):
            ended = ends.get((level, x), [])
            at = bisect_left(ended, (edge["x0"] - _RULE_JOIN_GAP,))
            if at < len(ended) and ended[at][0] <= edge["x0"] + _RULE_JOIN_OVERLAP and ended[at][0] < edge["x1"]:
                if at_column_border(edge["x0"], edge["top"]):
                    rule = ended.pop(at)[2]
                break
        if rule is None:
            rule = [edge]
            rules.append(rule)
        else:
            rule.append(edge)
        insort(ends.setdefault((level, round(edge["x1"])), []), (edge["x1"], order, rule))

    joined = []
    for rule in rules:
        first, last = rule[0], rule[-1]
        if len(rule) > 1 and not at_column_border(first["x0"], first["top"]) and not at_column_border(last["x1"], last["top"]):
            joined.append((first["x0"], last["x1"], first["top"]))
        else:
            joined.extend((edge["x0"], edge["x1"], edge["top"]) for edge in rule)
    return joined


def _outermost_spans(spans: set[tuple[int, int]]) -> list[tuple[int, int]]:
    """Drop vertical ranges that lie inside another range. A contained range is part of the same table (the '상반기|하반기' line
    of a two-level header exists only on the lower row).

    Searching it separately produced a lower fragment table, the whole table enclosing the fragment was discarded as a layout
    box in `_real_tables`, and '구분' and '2025년' scattered outside the table
    (pinned by: test_quality.py::test_pdf_open_sided_table_with_two_level_header_is_kept_whole).
    """
    outermost: list[tuple[int, int]] = []
    for span in sorted(spans, key=lambda span: span[0] - span[1]):  # longest range first
        if not any(other[1] - other[0] > span[1] - span[0] and other[0] - 2 <= span[0] and span[1] <= other[1] + 2
                   for other in outermost):
            outermost.append(span)
    return outermost


def _merge_split_header(rows: list[list[str | None]]) -> list[list[str | None]]:
    """Merge a two-level header ('1월·2월' under '문의') into one row, '문의 1월 | 문의 2월'.

    pdfplumber gives None for cells that disappeared in a merge and "" for truly empty cells. The rows are treated as a
    header only when the first row has a horizontal merge (None), the second row below it has text, and both rows have two
    or more filled cells (a single-cell title row is handled separately by render_table).
    When an unmerged column has text of its own in the second row, that is data, not a sub-header. The first data row of a
    one-level header ('연락처' spanning two columns) was attached to the header and became '이름 홍길동'
    (pinned by: test_quality.py::test_pdf_one_level_header_spanning_two_columns_keeps_the_first_data_row).
    When the second row has only one cell under a merged cell (`_undivided_span`), it is the next row, not a sub-header.
    """
    if len(rows) < 3:
        return rows
    first, second = rows[0], rows[1]
    spanned = [index for index, cell in enumerate(first) if cell is None and index > 0]
    # the merged cells and the cell where the merge starts (a horizontal merge is a run of None)
    covered = {*spanned, *(index - 1 for index in spanned)}
    if (not spanned or sum(1 for cell in first if cell) < 2 or sum(1 for cell in second if cell) < 2
            or not any(index < len(second) and second[index] for index in spanned)
            or any(second[index] and second[index] != first[index]
                   for index in range(min(len(first), len(second))) if index not in covered)
            or _undivided_span(first, second)):
        return rows

    filled, last = [], ""
    for cell in first:
        last = cell if cell is not None else last
        filled.append(last or "")
    merged = [" ".join(dict.fromkeys(part.strip() for part in (filled[index], second[index] or "") if part and part.strip()))
              for index in range(min(len(first), len(second)))]
    return [merged, *rows[2:]]


def _undivided_span(first: list[str | None], second: list[str | None]) -> bool:
    """Whether, among the horizontally merged cells of the first row, there is one under which the second row has only a
    single (undivided) cell whose text differs from the merged cell.

    A two-level header ('1월·2월' under '문의') divides into two or more cells under the merged cell. In a form grid, the
    vertical lines of the item rows below make the '등록번호' cell look like a merge spanning two grid columns, and the
    '상호' cell below it spans the same two columns — merging it as a sub-header gave
    '등록번호 상호 | 123-45-67890 (주)라마바' (pinned by: test_pdf_form_grids.py::TestFormGridHeader).
    """
    width = min(len(first), len(second))
    for start in range(width):
        end = start + 1
        while end < width and first[end] is None:
            end += 1
        if first[start] is None or end - start < 2:
            continue
        below = [cell for cell in second[start:end] if cell and cell.strip() and cell != first[start]]
        if len(below) == 1:
            return True
    return False
