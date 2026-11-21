"""Tables drawn with horizontal rules only: a rule above, a rule under the header row and a rule below, all of one width
and with no vertical line (the tables of papers and textbooks). Run after the borderless tables, on the lines they left.
"""

import re
from statistics import median

from opendockit.extractors._pdf_model import _Line, _original_lines_of, _Page, _Table, _visual_rows
from opendockit.markdown import render_table


# How far (pt) the ends of two rules may differ and still be rules of one table, and how close two rules may lie and still
# be one rule (a rule drawn as a thin rectangle has two sides).
_EXTENT_TOLERANCE = 2.0
_SAME_RULE_GAP = 2.5
# The narrowest gap (pt) between the text of two columns, measured over every row of the table.
_GUTTER_MIN = 3.0
# How far (pt) a line may reach past the ends of the rules and still belong to the table.
_OVERHANG = 3.0
# With rules between the rows, a band holding more visual rows than this is not one row of wrapped cells.
_BAND_MAX_LINES = 4
# A table of one column is taken only from short entries: at most this share of its lines may fill the width
# (`_FULL_WIDTH_RATIO` of it), as the lines of a paragraph between rules do.
_FULL_LINES_MAX = 0.3
_FULL_WIDTH_RATIO = 0.85
# The header of a table with two rules only is the row right above the first: within this many line heights of it.
_HEADER_GAP_RATIO = 1.5
_MIN_BODY_ROWS = 2
# A region taller than this share of the page between two rules is the page's own frame (a rule under the running header
# and one over the footer), not a table.
_REGION_MAX_HEIGHT_RATIO = 0.8
# The caption of a table ('Table 8:', 'TABLE II', '표 3'), looked for within `_CAPTION_REACH` line heights above the first
# rule and under the last.
_TABLE_CAPTION = re.compile(r"^(?:(?i:table|tab\.)|표)\s*[\dIVXA-Z]")
_CAPTION_REACH = 4.0
# A grid with fewer of its cells filled than this is the value labels of a chart between its grid lines.
_FILLED_MIN = 0.5
# A page with more rules than this is a drawing or a dense form; its rules are compared pair by pair here, so it is left.
_RULES_MAX = 400
_PAGE_NUMBER = re.compile(r"^\d{1,4}$")


def _extract_banded_tables(page: _Page) -> None:
    """Make tables of the regions between horizontal rules of one width and take their lines out of the body flow.

    Three rules or more need no other sign: the first band is the header. Two rules are a table only with a caption
    ('Table 6.1: …') right above or below and two columns or more — a pair of rules alone also frames a quotation or
    the page itself. The columns are the stretches no text of any row crosses, so a paragraph between rules gives one
    column of full lines and is left (`_FULL_LINES_MAX`). Left as they are too: the value labels of a chart between its
    grid lines (few cells filled, `_FILLED_MIN`) and a list of contents (a last column of rising page numbers).
    Pinned by: test_pdf_banded_tables.py; judged on the scorecard (`evals/README.md`).
    """
    if page.simple or not 2 <= len(page.rules) <= _RULES_MAX or not page.lines:
        return
    used: set[int] = set()
    for left, right, tops in _rule_groups(page.rules):
        if tops[-1] - tops[0] > page.height * _REGION_MAX_HEIGHT_RATIO:
            continue
        if any(table.top < tops[-1] and table.bottom > tops[0] and table.x0 < right and table.x1 > left for table in page.tables):
            continue
        if _crossed_by_bars(page, left, right, tops):
            continue
        found = _banded_table(page, left, right, tops, used)
        if found is None:
            continue
        table, lines = found
        page.tables.append(table)
        used.update(id(original) for line in lines for original in _original_lines_of(line))
    if used:
        page.lines = [line for line in page.lines if id(line) not in used]


def _crossed_by_bars(page: _Page, left: float, right: float, tops: list[float]) -> bool:
    """Whether a filled rectangle narrower than half the region stands across one of its rules: the bar of a chart over
    its grid lines. The shading of a table lies between its rules or runs their whole width."""
    return any(band_right - band_left < (right - left) / 2 and band_left < right and band_right > left
               and any(top + _SAME_RULE_GAP < rule < bottom - _SAME_RULE_GAP for rule in tops)
               for top, bottom, band_left, band_right in page.bands)


def _rule_groups(rules: list[tuple[float, float, float]]) -> list[tuple[float, float, list[float]]]:
    """(left end, right end, tops from the top down) of each set of two or more rules of one width."""
    groups: list[tuple[float, float, list[float]]] = []
    for top, left, right in sorted(rules, key=lambda rule: (rule[1], rule[2], rule[0])):
        for group_left, group_right, tops in groups:
            if abs(group_left - left) <= _EXTENT_TOLERANCE and abs(group_right - right) <= _EXTENT_TOLERANCE:
                tops.append(top)
                break
        else:
            groups.append((left, right, [top]))
    found = []
    for left, right, tops in groups:
        tops.sort()
        apart = [top for index, top in enumerate(tops) if index == 0 or top - tops[index - 1] > _SAME_RULE_GAP]
        if len(apart) >= 2:
            found.append((left, right, apart))
    return sorted(found, key=lambda group: group[2][0])


def _pieces(line: _Line) -> list[tuple[float, float, str]]:
    return list(line.parts) if line.parts else [(line.x0, line.x1, line.text)]


def _columns(lines: list[_Line]) -> list[tuple[float, float]]:
    """The stretches of the width that hold text, split wherever no line has any for `_GUTTER_MIN` or more."""
    columns: list[list[float]] = []
    for start, end, _ in sorted(piece for line in lines for piece in _pieces(line)):
        if columns and start - columns[-1][1] < _GUTTER_MIN:
            columns[-1][1] = max(columns[-1][1], end)
        else:
            columns.append([start, end])
    return [(start, end) for start, end in columns]


def _cells(row: list[_Line], columns: list[tuple[float, float]]) -> list[str]:
    cells = [""] * len(columns)
    for line in row:
        for start, end, text in _pieces(line):
            middle = (start + end) / 2
            column = next((index for index, (low, high) in enumerate(columns) if low <= middle <= high), None)
            if column is None or not text.strip():
                continue
            cells[column] = f"{cells[column]} {text.strip()}" if cells[column] else text.strip()
    return cells


def _banded_table(page: _Page, left: float, right: float, tops: list[float], used: set[int]) -> tuple[_Table, list[_Line]] | None:
    within = [line for line in page.lines if id(line) not in used and left - _OVERHANG <= (line.x0 + line.x1) / 2 <= right + _OVERHANG]
    inside = [line for line in within if tops[0] < (line.top + line.bottom) / 2 < tops[-1]]
    if not inside or any(line.x0 < left - _OVERHANG or line.x1 > right + _OVERHANG for line in inside):
        return None
    bands = [_visual_rows([line for line in inside if upper < (line.top + line.bottom) / 2 < lower])
             for upper, lower in zip(tops, tops[1:], strict=False)]
    bands = [band for band in bands if band]
    if not bands:
        return None
    if len(tops) >= 3:
        if len(bands) < 2:
            return None
        header, body_bands = bands[0], bands[1:]
    else:
        height = median(line.height for line in inside)
        reach = height * _CAPTION_REACH
        if not any(_TABLE_CAPTION.match(line.text) for line in within
                   if 0 <= tops[0] - line.bottom <= reach or 0 <= line.top - tops[-1] <= reach):
            return None
        header, body_bands = [bands[0][0]], [bands[0][1:]]
        if not body_bands[0]:
            return None
    lines = [line for row in header for line in row] + [line for band in body_bands for row in band for line in row]
    columns = _columns(lines)
    if len(tops) < 3 and len(columns) < 2:
        return None
    # With a rule between every two rows a band is one row, its lines the wrapped lines of its cells; otherwise each
    # visual row is a row, and one that leaves the first column empty continues the cells of the row above.
    ruled_rows = len(body_bands) >= _MIN_BODY_ROWS and all(len(band) <= _BAND_MAX_LINES for band in body_bands)
    grid = [_joined([_cells(row, columns) for row in header], " ")]
    for band in body_bands:
        rows = [_cells(row, columns) for row in band]
        if ruled_rows:
            grid.append(_joined(rows, "<br>"))
            continue
        for cells in rows:
            if len(columns) > 1 and not cells[0] and len(grid) > 1:
                grid[-1] = _joined([grid[-1], cells], "<br>")
            else:
                grid.append(cells)
    if len(grid) - 1 < _MIN_BODY_ROWS and not (len(columns) >= 2 and len(grid) == 2 and len(tops) >= 3):
        return None
    if len(columns) == 1:
        body = [line for band in body_bands for row in band for line in row]
        full = sum(1 for line in body if line.x1 - line.x0 >= (right - left) * _FULL_WIDTH_RATIO)
        if full > len(body) * _FULL_LINES_MAX:
            return None
    else:
        filled = sum(1 for row in grid[1:] for cell in row if cell)
        if filled < (len(grid) - 1) * len(columns) * _FILLED_MIN:
            return None
        pages = [row[-1] for row in grid]
        if all(_PAGE_NUMBER.match(page) for page in pages) and all(int(lower) >= int(upper) for upper, lower in zip(pages, pages[1:], strict=False)):
            return None
    table = _Table(top=min(line.top for line in lines), text=render_table(grid), bottom=max(line.bottom for line in lines),
                   x0=min(line.x0 for line in lines), x1=max(line.x1 for line in lines), rows=grid, spans=tuple(columns))
    return table, lines


def _joined(rows: list[list[str]], joint: str) -> list[str]:
    """The rows as one, the cells of each column joined with `joint`."""
    return [joint.join(cell for cell in column if cell) for column in zip(*rows, strict=True)]
