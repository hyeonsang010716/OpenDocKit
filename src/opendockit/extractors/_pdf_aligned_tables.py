"""Tables without ruling lines, found from lines whose cells stand in columns: runs of table-like rows, their columns,
wrapped cells and labels, rows split over lines, and headers carried over a page break.
"""

from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Callable
from itertools import pairwise
import math
import re
import statistics

from opendockit.extractors._pdf_label_values import _label_value_rows, _near
from opendockit.extractors._pdf_model import (
    _BARE_NUMBER,
    _COLUMN_LINES_MIN,
    _FAR_VALUE,
    _HEADER_CELL_MAX_CHARS,
    _LONE_BULLET,
    _MARKER_GAP_RATIO,
    _NUMBER_CELL,
    _ROW_OVERLAP_RATIO,
    _SHADE_MIN_SIDE,
    _SPACED_CHAR,
    _TABLE_CELL_MAX_RATIO,
    _TABLE_ROW_TOLERANCE,
    _in_margin,
    _Line,
    _Page,
    _Table,
    _visual_rows,
)
from opendockit.extractors._pdf_paragraphs import _table_text
from opendockit.extractors._pdf_reading_order import _beside_each_other, _is_letter_spaced
from opendockit.markdown import render_table


# List number placed apart from the item text ('1.', '2)', '(3)', '가.', 'IV.', '①'). A bare number ('1') is only ever taken as a
# footnote number (`_marker_rows`).
_LIST_NUMBER = re.compile(r"^(?:\(?(?:\d{1,3}|[가나다라마바사아자차카타파하]|[A-Za-z]|[IVX]{2,5}|[ivx]{2,5})[.)]"
                          r"|\((?:\d{1,3}|[가나다라마바사아자차카타파하])\)|[①-⑳Ⅰ-Ⅻ])$")


_EQUATION_NUMBER = re.compile(r"^\(\d{1,3}(?:[.\-]\d{1,3})?[a-z]?\)$")  # equation number '(1)', '(2.3)', '(4a)'


_TABLE_MIN_ROWS = 3
_TABLE_MIN_COLUMNS = 3
# Character size ratio (larger / smaller, from after the first data row) up to which a borderless table's row continues the same
# table as the row before. A 12pt heading under a 10pt table (1.2) breaks it; a table whose subtotal row is slightly larger (11/10)
# continues.
_TABLE_SIZE_RATIO = 1.15
# Gap for a line to count as wrapped cell text in a borderless table (_continues_cell): at most this multiple of this table's gap
# between rows, and narrower than the row gap by at least this multiple of the line height. Line spacing inside a cell is clearly
# narrower than row spacing. The staggered lines of a list with tightly packed rows (2pt gap) are not wrapped lines.
_WRAP_GAP_RATIO = 0.6
# The 'narrower by at least this multiple of the line height' side of the condition above. In a Chrome table (4px cell padding) the
# gap between rows is 12.7pt at a 12pt line height and the gap of a wrapped line 6pt, so the difference (6.7pt) fell short of 0.6×
# (7.2pt) and the wrapped line became a row of its own (Pinned by: test_pdf_table_columns.py::TestWideWrappedFirstColumn).
# For a list with a 2pt gap the difference is far below even 0.5×, so it is still filtered out.
_WRAP_GAP_MIN_RATIO = 0.5
# Divider row without values in a borderless table (_is_section_row): the gaps above and below are at most this multiple of this
# table's gap between rows. A caption between tables is set further apart.
_SECTION_ROW_GAP_RATIO = 1.5
# Number of data rows directly above whose column alignment is compared when checking whether a borderless table's row belongs to
# another table (`_out_of_line`).
_OUT_OF_LINE_ROWS = 20


# Year and date cells common in header rows ('2024', '2024년', 'FY2024', '2024.12.31', '2024-12'). When deciding on the header row to
# repeat they are not treated as numeric value cells (`_has_value_text`).
_YEAR_OR_DATE = re.compile(r"^(?:FY\s?)?(?:19|20)\d{2}(?:년도?|[.\-/]\s?\d{1,2}(?:[.\-/]\s?\d{1,2})?\.?)?$")


# Difference between the two vertical centres (relative to the line height, `_with_centred_wrapped_cells`) for a cell to count as
# vertically centred beside a two-line cell. In a Chrome table it was 0.4pt, and a neighbouring text box with different line spacing
# (an invoice's '항목 값' (item, value)) was off by 4pt (Pinned by: test_pdf_label_value_rows.py::TestLeftBlockIsNotAWrappedCell).
_CENTRED_CELL_RATIO = 0.2
# Upper bound on the number of visual rows of a centred row whose cells wrapped to different numbers of lines
# (`_centred_band_row`). Keeps each row of a long table with narrow line gaps from being scanned to the end of the table (beyond
# this it is not such a row).
_CENTRED_BAND_ROWS = 16


# Word of an item name spread by justification across one cell of a borderless table (`_spread_over_one_cell`).
_SPREAD_WORD = re.compile(r"^[가-힣一-鿿]{1,4}$")


# Upper bound on the gap (relative to the line height, `_wrapped_header_line`) between the first line and a wrapped second line in a
# header cell of a borderless table.
_HEADER_WRAP_GAP_RATIO = 0.6


# Minimum gap between two table-like rows (`_aligned_pair`) (multiple of the first row's line height). Table rows are apart by the
# cell padding, while a label wrapped onto two lines inside a box sits as close as the line spacing (0.2–0.8×).
_PAIR_ROW_GAP_RATIO = 1.0


# A line ending in the dot leader of a table of contents or price list (5 or more periods/middle dots, or 2 or more ellipses, in a
# row) and the short value placed separately at its right end (page number, amount). The value starts within `_LEADER_GAP_RATIO` ×
# the character size from the end of the leader and is at most `_LEADER_VALUE_MAX_RATIO` × the character size wide (`_leader_rows`).
# A run of dots is tried only from its first dot (the look-behinds): tried from every dot, each time to the end of the run and back,
# a piece with a long run that does not end the text took time with the square of the run — 18 seconds for 20,000 dots followed by
# a word (Pinned by: test_audit_pdf_tail.py::TestLongDotRunInsideALine). A match from inside a run is also one from its start.
_LEADER_END = re.compile(r"(?:(?<![.·∙・])[.·∙・]{5,}|(?<![…‥])[…‥]{2,})$")  # scattered dots of an OCR text layer ('... .. . .') are not a dot leader
_LEADER_GAP_RATIO = 3.0
_LEADER_VALUE_MAX_RATIO = 10.0
# End of a table-of-contents line whose dot leader and page number are one piece with the item text ('서론 ....... 1') (`_leader_rows`).
_LEADER_TAIL = re.compile(r"(?:(?<![.·∙・])[.·∙・]{5,}|(?<![…‥])[…‥]{2,})\s*\S{1,12}$")


def _join_ruled_header_lines(page: _Page) -> None:
    """Join a header stacked in several lines per cell in the band between two horizontal rules into one line per cell (same
    top coordinate) so that it forms one row.

    In a three-line table with only top and bottom rules (financial statements), the two-line header ('2025년 / (제12기)')
    and the vertically centred one-line cell beside it ('과목') have a different height on every line and were not grouped
    into one row — the header became separate paragraphs and the first data row became the header (pinned by:
    test_pdf_table_columns.py::TestTwoLineHeaderBetweenRules). Joined only when the band is at most 4.5 lines high, every line
    in the band is short, the cells formed by grouping horizontally overlapping lines number three or more (one of them
    multi-line), and the row right under the lower rule has three or more pieces.

    In a two-level header ('2023년' spanning the two columns '남성·여성') the upper cell does not overlap the lower cells
    horizontally, so it was not joined per cell. The upper cell is prefixed to each cell below it
    ('2023년 남성 | 2023년 여성', the same form as `_merge_split_header` for ruled tables, `_grouped_columns`).
    The rule under the header may be drawn stopping short of a vertically merged cell ('구분'), so it is accepted when it
    covers at least half within the width of the upper rule (pinned by:
    test_pdf_table_columns.py::TestGroupedHeaderBetweenRules).
    """
    if page.simple or len(page.rules) < 2:
        return
    max_cell = page.width * _TABLE_CELL_MAX_RATIO
    for (upper, left, right), (lower, lower_left, lower_right) in zip(page.rules, page.rules[1:], strict=False):
        if (lower_left < left - 2 or lower_right > right + 2 or (lower_right - lower_left) * 2 < right - left
                or (abs(right - lower_right) > 2 and abs(left - lower_left) > 2) or lower - upper < _SHADE_MIN_SIDE * 3):
            continue
        inside = [line for line in page.lines if line.top >= upper - 1 and line.bottom <= lower + 1
                  and line.x0 >= left - 2 and line.x1 <= right + 2]
        if (len(inside) < _TABLE_MIN_COLUMNS or any(line.x1 - line.x0 > max_cell for line in inside)
                or lower - upper > statistics.median(line.height for line in inside) * 4.5):
            continue
        grouped = _grouped_columns(inside)
        if grouped is None:
            continue
        grouped_ids = {id(line) for column in grouped for line in column}
        columns = sorted([*_stacked_columns([line for line in inside if id(line) not in grouped_ids]), *grouped],
                         key=lambda column: column[-1].x0)
        if len(columns) < _TABLE_MIN_COLUMNS or (not grouped and all(len(column) == 1 for column in columns)):
            continue
        below = _visual_rows([line for line in page.lines if line.top >= lower - 1
                              and line.top - lower <= inside[0].height * 2.5 and line.x0 >= left - 2 and line.x1 <= right + 2])
        if not below or len(below[0]) < _TABLE_MIN_COLUMNS:
            continue

        top = min(line.top for line in inside)
        joined = [_Line(top=top, bottom=max(line.bottom for line in column), x0=column[-1].x0 if grouped_cell else
                        min(line.x0 for line in column), x1=column[-1].x1 if grouped_cell else max(line.x1 for line in column),
                        text=" ".join(line.text for line in column), size=statistics.median(line.size for line in column))
                  for column in columns for grouped_cell in [any(column is cell for cell in grouped)]]
        taken = {id(line) for line in inside}
        # placed where the first header line was in reading order
        first = next(index for index, line in enumerate(page.lines) if id(line) in taken)
        rest = [line for line in page.lines if id(line) not in taken]
        page.lines = [*rest[:first], *joined, *rest[first:]]


def _grouped_columns(lines: list[_Line]) -> list[list[_Line]] | None:
    """The cells of a two-level header: for each upper cell (`upper`) lying wholly above another line in the band, when two or
    more of the lines below have it as the upper cell whose centre is nearest and the upper cell's centre lies between the
    centres of those lines, one [upper cell, lower line] cell is made per lower line.
    An upper cell with a single line below is a stacked two-line cell and is not handled here (`_stacked_columns`). When an
    upper cell got two or more lower lines but its centre is not between them, it is not a two-level header: None (nothing
    is joined). An empty list when there is no upper cell."""
    def centre(line: _Line) -> float:
        return (line.x0 + line.x1) / 2

    upper = [line for line in lines if any(other.top >= line.bottom - 1 for other in lines)]
    below: dict[int, list[_Line]] = {id(line): [] for line in upper}
    for line in lines:
        above = [other for other in upper if other is not line and other.bottom <= line.top + 1]
        if above and not any(line is other for other in upper):
            below[id(min(above, key=lambda other: abs(centre(other) - centre(line))))].append(line)

    columns: list[list[_Line]] = []
    for line in upper:
        cells = sorted(below[id(line)], key=lambda cell: cell.x0)
        if len(cells) < 2:
            continue
        if not centre(cells[0]) < centre(line) < centre(cells[-1]):
            return None
        columns += [[line, cell] for cell in cells]
    return columns


def _stacked_columns(lines: list[_Line]) -> list[list[_Line]]:
    """The cells formed by grouping lines whose horizontal ranges overlap (left to right, top to bottom inside a cell)."""
    columns: list[list[_Line]] = []
    for line in sorted(lines, key=lambda line: line.x0):
        if columns and line.x0 < max(other.x1 for other in columns[-1]):
            columns[-1].append(line)
        else:
            columns.append([line])
    return [sorted(column, key=lambda line: line.top) for column in columns]


def _extract_aligned_tables(page: _Page) -> None:
    """Find tables that have no rules and only aligned cells, turn them into Markdown tables, and take their lines out of the
    body flow."""
    used: set[int] = set()
    for run in _aligned_table_runs(page):
        header = run[0]
        grid = [[""] * len(header) for _ in run]
        columns = [_row_columns(row, header) for row in run]
        for row_index, row in enumerate(run):
            for line, column in zip(row, columns[row_index], strict=True):
                cell = grid[row_index][column]
                same_row = abs(line.top - row[0].top) <= _TABLE_ROW_TOLERANCE  # words spread over the cell width (_spread_over_one_cell)
                joint = " " if cell and same_row else "<br>"  # a lower line is a wrapped line of the cell text (_continues_cell)
                grid[row_index][column] = f"{cell}{joint}{line.text}" if cell else line.text
        if _wrapped_header_line(run, grid):
            grid[0] = [f"{upper} {lower}".strip() for upper, lower in zip(grid[0], grid[1], strict=True)]
            run, grid, columns = [run[0] + run[1], *run[2:]], [grid[0], *grid[2:]], [columns[0] + columns[1], *columns[2:]]
        cells = [line for row in run for line in row]
        placed = [[line for row, row_columns in zip(run, columns, strict=True)
                   for line, other in zip(row, row_columns, strict=True) if other == column] for column in range(len(header))]
        page.tables.append(_Table(top=header[0].top, text=render_table(grid), bottom=max(line.bottom for line in cells),
                                  x0=min(line.x0 for line in cells), x1=max(line.x1 for line in cells), rows=grid,
                                  spans=tuple((min(line.x0 for line in lines), max(line.x1 for line in lines)) for lines in placed),
                                  lines=run))
        used.update(id(line.source or line) for line in cells)  # for split pieces (_split_merged_cells) the original line is removed
        used.update(id(original) for line in cells for original in line.joined)
    if used:
        page.lines = [line for line in page.lines if id(line) not in used]


def _wrapped_header_line(run: list[list[_Line]], grid: list[list[str]]) -> bool:
    """Whether the second row (`run[1]`) of a borderless table is a line wrapped from the header cells: every column with a
    filled cell also has a filled header cell, neither row has a numeric value cell (`_FAR_VALUE`) while the data rows below
    do, and the gap to the header is at most `_HEADER_WRAP_GAP_RATIO` times the line height and narrower than half the gap
    to the row below.

    The second line of a two-line header ('판매량 / (대)', '전년 동기 / 대비 증감률') became the first data row
    '| | (대) | (백만원) |' and only half of each column name remained (pinned by:
    test_pdf_table_columns.py::TestBorderlessTwoLineHeader). A row at the same pitch as the data rows is not a wrapped line
    (test_row_at_the_same_pitch_as_the_data_rows_stays_a_row in the same class). Known accepted gap: in the two-line header
    of a table with no numeric values the second line stays a row as before.
    """
    if len(run) < 4 or not any(grid[1]):
        return False
    if any(lower and not upper for upper, lower in zip(grid[0], grid[1], strict=True)):
        return False
    if any(_FAR_VALUE.match(cell.replace(" ", "")) for cell in [*grid[0], *grid[1]] if cell):
        return False
    if not any(_FAR_VALUE.match(cell.replace(" ", "")) for row in grid[2:] for cell in row if cell):
        return False
    upper_bottom, lower_top = max(line.bottom for line in run[0]), min(line.top for line in run[1])
    gap, below = lower_top - upper_bottom, min(line.top for line in run[2]) - max(line.bottom for line in run[1])
    height = statistics.median(line.height for line in run[0])
    return gap <= height * _HEADER_WRAP_GAP_RATIO and gap < below / 2


def _row_columns(row: list[_Line], header: list[_Line]) -> list[int]:
    """The column of each piece of a borderless table row (`row`, which may have wrapped lines of cell text joined on).

    Pieces at the same height are decided among themselves with `_column_of`, and a wrapped line (below the first line)
    follows the column of the piece above whose left end matches or which overlaps it horizontally — treating a wrapped line
    as one row with the first-line pieces made the wrapped line in the gap before a centred header ('회의실') look as if
    the column on the right was taken by another piece of the same row, and it was attached to the first-column cell
    ('장소<br>회의실') (pinned by: test_pdf_table_columns.py::
    TestCentredHeadersOverLeftAlignedCells::test_wrapped_line_of_a_cell_in_the_gap_before_a_centred_header_stays_in_the_cell).
    """
    columns: list[int] = []

    for index, line in enumerate(row):
        level = [other for other in row if abs(other.top - line.top) <= _TABLE_ROW_TOLERANCE]
        column = _column_of(line, header, level)
        above = [(other, columns[number]) for number, other in enumerate(row[:index])
                 if other.top < line.top - _TABLE_ROW_TOLERANCE and other.x0 < line.x1 and line.x0 < other.x1]
        if above and column not in {other_column for _, other_column in above}:
            column = min(above, key=lambda pair: abs(pair[0].x0 - line.x0))[1]
        columns.append(column)

    return columns


def _carry_table_headers(pages: list[_Page]) -> None:
    """Put the header row of the previous page's table on top of a table that continues onto the next page without repeating
    its header (modifies in place).

    A Markdown table uses its first row as the header, so from the second page on the first data row became the header and
    the column names were lost (pinned by: test_pdf_table_columns.py::TestTableContinuedOnTheNextPage). A table is taken as
    continued only when all of the following hold (deliberate, conservative): there are no lines or pictures outside the
    margin area (`_in_margin`) either below the bottom table of the page right before or above the top table of this page;
    the columns match (ruled tables: same column count and each column's horizontal range overlaps only the same column of
    the other, `_same_columns`; borderless tables: each line piece overlaps only one column of the previous table,
    `_regrid` — an empty cell in the first data row leaves the column count short); and the first row of this page's table
    differs from the previous table's header. When the first row of this page's table has no numeric value cell and the
    second row does, it is taken as its own header and nothing is put on top (another table with the same column shape,
    pinned by: test_new_table_with_its_own_header_at_the_top_of_the_next_page_keeps_its_header). The previous table must
    have three or more rows and its header must have two or more short cell texts, no numeric value cell, and must not
    appear again in the data rows (`_looks_like_header`) — the frame box filling the page on every slide was caught as a
    ruled table and the previous page's body text was put on top as a header
    (test_page_filling_frames_of_slides_do_not_get_the_previous_slide_as_a_header). From the third page on, the carried-over
    header is carried over again. Year and date cells in a header (`_YEAR_OR_DATE`) are not counted as numeric value cells
    (`_has_value_text`) — '과목 | 주석 | 2024.12.31 | 2023.12.31' of a financial statement was not seen as a header, and
    '성과 지표 | 2023 | 2024' of a different table at the top of the next page was not seen as its own header and became a
    data row under the previous table's header. For a two-column table the header is carried over only when the second cell
    is a numeric value in more than half of the previous table's data rows — the first row ('성명 | 홍길동') of an
    application form filled on every page with a ruled 'label | value' grid was put on top of the next page's table as its
    header (pinned by: test_pdf_table_columns.py::TestHeaderCarriedOverWithYearsAndForms). Known accepted gap: when a table
    whose values are all text starts with its own header, the previous table's header is put on top of it. A two-column
    table whose values are text ('연도 | 주요 내용') does not get its header carried over. When every column of the previous
    table is open below (a row split at the end of the page, `_closed_at_open_ends`), the first row of this page is the rest
    of that row and is not taken as its own header (pinned by:
    TestRowSplitAcrossPages::test_row_whose_cells_are_all_open_at_the_page_end_stays_in_the_table).
    """
    for previous, page in zip(pages, pages[1:], strict=False):
        if page.number != previous.number + 1 or not previous.tables or not page.tables:
            continue
        last = max(previous.tables, key=lambda table: table.bottom)
        first = min(page.tables, key=lambda table: table.top)
        if not (last.rows and first.rows and last.spans):
            continue
        if (any(line.top > last.bottom and not _in_margin(line, previous) for line in previous.lines)
                or any(figure[1] > last.bottom for figure in previous.figures)
                or any(line.bottom < first.top and not _in_margin(line, page) for line in page.lines)
                or any(figure[3] < first.top for figure in page.figures)):
            continue

        if (not first.lines and not last.lines and len(first.spans) == len(last.spans)
                and _same_columns(last.spans, first.spans)):
            # The next-page piece of a row split at the end of the page is a top row with a single filled cell among empty
            # ones, so it was pulled out as a table title and became a paragraph (pinned by:
            # test_pdf_table_columns.py::TestRowSplitAcrossPages). A ruled table gives merged cells as None, so only those
            # are taken as a title.
            first.continued = True
            _carry_open_cells(last, first)
            first.text = _table_text(first)

        # In a borderless table the first row acts as the header, so an empty cell leaves it short of columns; the line pieces
        # are fitted to the previous table's columns again
        if first.lines:
            grid = _regrid(first.lines, last.spans)
        else:
            grid = first.rows if len(first.spans) == len(last.spans) and _same_columns(last.spans, first.spans) else None
        header = last.rows[0]
        if grid is None or len(last.rows) < _TABLE_MIN_ROWS or not _looks_like_header(header, [*last.rows[1:], grid[0]]):
            continue
        # a row split at the end of the previous page: the first row is the rest of it, not a header
        split = len(last.open_below) == len(last.spans)
        if len(grid) > 1 and not split and not _has_value_text(grid[0]) and _has_number_text(grid[1]):
            continue
        if len(header) == 2 and sum(1 for row in last.rows[1:] if _has_number_text(row[1:])) * 2 <= len(last.rows) - 1:
            # a two-column table with no numeric value column may be a 'label | value' form grid — there is no telling whether
            # the first row is a header
            continue
        first.rows = [header, *grid]
        first.text = _table_text(first)
        if first.bands:
            first.bands = ((-math.inf, -math.inf), *first.bands)  # the carried-over header row has no position on the page
        if first.lines:
            first.spans = last.spans  # fitted to the previous table's columns, so the next page is fitted to them too


def _carry_open_cells(last: _Table, first: _Table) -> None:
    """For each column where the bottom cell of the previous page's table (`last`) is open below (`open_below`, a vertically
    merged cell split at the end of the page), fill that cell's text into the rows of the continued table (`first`) where
    the column is empty, starting from the top data row (a repeated header is skipped) (stops at the first cell with text,
    modifies in place).

    Chrome draws the text of a rowspan cell split at the end of the page only on the previous page, so the first cell of the
    next page's rows was empty (pinned by: test_pdf_table_columns.py::
    TestMergedCellOpenAtThePageEnd::test_rows_of_the_cell_continued_on_the_next_page_get_its_text).

    Deliberate: when every column is open it is not a merged cell but a row split at the end of the page
    (`_closed_at_open_ends`) and nothing is filled — filling the previous page's values ('5', '500') into the empty cells of
    the rest of the row on the next page would make the same row come out twice, and the amount could be counted twice
    (pinned by: test_pdf_table_columns.py::
    TestRowSplitAcrossPages::test_row_whose_cells_are_all_open_at_the_page_end_stays_in_the_table).
    """
    if len(last.open_below) == len(last.spans):
        return
    start = 1 if first.rows and last.rows and first.rows[0] == last.rows[0] else 0
    for column in last.open_below:
        value = last.rows[-1][column] if column < len(last.rows[-1]) else ""
        if not value:
            continue
        for row in first.rows[start:]:
            if column >= len(row) or row[column]:
                break
            row[column] = value


def _looks_like_header(row: list[str], rows: list[list[str]]) -> bool:
    """Whether the row looks like a header row: two or more cells have text, all of them at most `_HEADER_CELL_MAX_CHARS`
    characters (line breaks excluded), no cell holds only a numeric value, and no cell text appears again in the same column
    of the other rows (`rows`, the previous table's data rows and the first row of the table to be continued).

    The last condition filters out the same frame on every page (a slide layout caught as a ruled table) — the frame's first
    cell (the section title) continues vertically and appears with the same text in the data rows and in the next page's
    frame. The header cells of a data table do not appear again in its data rows. A header repeated on every page is also
    filtered out here."""
    filled = [cell.strip() for cell in row if cell and cell.strip()]
    return (len(filled) >= 2 and not _has_value_text(row)
            and all(len(cell.replace("<br>", "")) <= _HEADER_CELL_MAX_CHARS for cell in filled)
            and not any(cell and cell.strip() and index < len(other) and (other[index] or "").strip() == cell.strip()
                        for other in rows for index, cell in enumerate(row)))


def _regrid(rows: list[list[_Line]], spans: tuple[tuple[float, float], ...]) -> list[list[str]] | None:
    """The cell text of a borderless table's rows (line pieces) fitted to another table's column ranges (`spans`). Each piece
    must overlap exactly one column (a piece spanning several columns is tried split into its wide-gap pieces,
    `_Line.parts`), and when two pieces land in one cell they are joined with `<br>` only when the second is a wrapped line
    placed below the row's first line. None if anything does not fit."""
    def columns(x0: float, x1: float) -> list[int]:
        return [index for index, span in enumerate(spans) if min(x1, span[1]) - max(x0, span[0]) > 0]

    grid = []
    for row in rows:
        cells = [""] * len(spans)
        for line in row:
            pieces = line.parts if line.parts and len(columns(line.x0, line.x1)) > 1 else ((line.x0, line.x1, line.text),)
            for x0, x1, text in pieces:
                found = columns(x0, x1)
                if len(found) != 1 or (cells[found[0]] and line.top - row[0].top <= _TABLE_ROW_TOLERANCE):
                    return None
                cells[found[0]] = f"{cells[found[0]]}<br>{text}" if cells[found[0]] else text
        grid.append(cells)
    return grid


def _same_columns(spans: tuple[tuple[float, float], ...], others: tuple[tuple[float, float], ...]) -> bool:
    """Whether each column's horizontal range in the two tables overlaps only the column at the same position (by more than 1pt)."""
    def overlaps(span: tuple[float, float], other: tuple[float, float]) -> bool:
        return min(span[1], other[1]) - max(span[0], other[0]) > 1.0

    return all(overlaps(span, others[index])
               and not any(overlaps(span, other) for position, other in enumerate(others) if position != index)
               for index, span in enumerate(spans))


def _has_number_text(row: list[str]) -> bool:
    """Whether a row of cell text has a cell holding only a numeric value (the cell-text counterpart of `_has_number_cell`)."""
    return any(_NUMBER_CELL.match((cell or "").strip()) for cell in row)


def _has_value_text(row: list[str]) -> bool:
    """Whether a row of cell text has a numeric value cell that is not a year or date (`_YEAR_OR_DATE`). Used for the header
    test (`_carry_table_headers`)."""
    return any(_NUMBER_CELL.match(text) and not _YEAR_OR_DATE.match(text) for text in ((cell or "").strip() for cell in row))


def _aligned_table_runs(page: _Page) -> list[list[list[_Line]]]:
    """The groups of rows to treat as borderless tables. A row is the line pieces at the same height (left to right), and the
    first row is the header.

    Tables are first searched for with rows grouped from pieces whose tops match, then only the lines not in a table are
    regrouped by vertical overlap (`staggered` of `_visual_rows`) and searched once more. A table already found is regrouped
    from its own lines alone only when neighbouring rows overlap vertically (half rows) (`_regrouped_half_rows`) — grouping
    by overlap from the start merged staggered figure labels into rows and broke existing tables (public paper PDF).
    """
    if page.simple or len(page.lines) < _TABLE_MIN_ROWS * _TABLE_MIN_COLUMNS:
        return []
    max_cell = page.width * _TABLE_CELL_MAX_RATIO
    rows = _with_ruled_wrapped_cells(_visual_rows(page.lines), page.rules)
    runs = [_regrouped_half_rows(run, max_cell) for run in _table_runs(rows, max_cell, page.bands)]
    in_table = {id(original) for run in runs for row in run for line in row for original in (line, *line.joined)}
    rest = [line for line in page.lines if id(line) not in in_table]
    # The second pass accepts only tables in which staggered pieces were actually grouped. A table that arose merely because
    # removing the table lines made the rows group differently is not accepted.
    # A wrapped line of cell text (_continues_cell) starts below the row's first line and does not count as a staggered piece.
    runs += [run for run in _table_runs(_visual_rows(rest, staggered=True), max_cell)
             if any(abs(line.top - row[0].top) > _TABLE_ROW_TOLERANCE and line.top < row[0].bottom
                    for row in run for line in row)]
    in_table = {id(original) for run in runs for row in run for line in row for original in (line, *line.joined)}
    runs += _split_line_table_runs([line for line in page.lines if id(line) not in in_table], max_cell)

    # on a two-column page, tables that mix table rows of the two columns are dropped and searched for again inside each column
    columns = _table_columns(page)
    if columns is not None:
        runs = [run for run in runs if not _mixes_two_columns(run, *columns)]
        runs += _column_table_runs(page, runs, columns, max_cell)
    rows = _visual_rows(page.lines)
    runs = [_cut_at_closing_rule(run, page.rules) for run in runs]

    # The original line (source) of a split piece is a used line too. When it was left out, the table below took that row
    # again while growing upward and it came out in two tables
    # (pinned by: test_pdf_table_columns.py::TestRowsOfASplitTableAreNotTakenAgain)
    taken = {id(original) for run in runs for row in run for line in row for original in (line, line.source) if original is not None}
    return [_split_merged_cells(run, rows, taken, max_cell, page.rules) for run in runs]


def _cut_at_closing_rule(run: list[list[_Line]], rules: list[tuple[float, float, float]]) -> list[list[_Line]]:
    """The rows of a borderless table (`run`) cut at its bottom border (a table-width horizontal line). `run` as is when there
    is no place to cut.

    The totals block on the right under the bottom border of an invoice's item table ('공급가액 1,002,940') lined up with
    the columns and was absorbed as rows of the item table (pinned by:
    test_pdf_label_value_rows.py::TestTotalsBelowAnItemTable). It is cut only when that is the only table-width horizontal
    line between data rows (the one under the header excluded) and the rows below it all leave the first column empty and
    fill at most half of the columns — a table with a rule on every row, and a totals row with '합계' (total) written in the
    first column, are left as they are.
    """
    left, right = min(line.x0 for row in run for line in row), max(line.x1 for row in run for line in row)
    spanning = [y for y, x0, x1 in rules if x0 <= left + _TABLE_ROW_TOLERANCE and x1 >= right - _TABLE_ROW_TOLERANCE]
    header = run[0]

    def ruled(index: int) -> bool:  # whether there is a table-width horizontal line between run[index - 1] and run[index]
        return any(max(line.bottom for line in run[index - 1]) - 1 <= y <= min(line.top for line in run[index]) + 1 for y in spanning)

    if not spanning:
        return run
    cuts = [index for index in range(2, len(run)) if ruled(index)]
    if len(cuts) != 1 or cuts[0] < _TABLE_MIN_ROWS:
        return run
    tail = run[cuts[0]:]
    columns = [{_column_of(line, header, row) for line in row} for row in tail]
    if all(0 not in used and len(used) <= len(header) // 2 for used in columns):
        return run[:cuts[0]]
    return run


def _split_merged_cells(run: list[list[_Line]], rows: list[list[_Line]], taken: set[int],
                        max_cell: float, rules: list[tuple[float, float, float]]) -> list[list[_Line]]:
    """In a found borderless table (`run`) and the rows right above and below it, split the pieces in which pdfminer merged
    neighbouring cells at the wide gaps inside the line (`_Line.parts`) and search for the table again. Only rows above and
    below that continue at the row pitch and lie within the table's horizontal range are considered. The table is replaced
    only when exactly one table is found again, it holds every line of the original table, the columns do not decrease, and
    every column containing a split piece has cells on two or more rows. Otherwise `run` as is.

    The cells of narrow columns (32pt) are only the cell padding (12pt) apart, so pdfminer, which joins up to twice the
    character width, made '120 118' and '정원 현원 결원 비고' single pieces. The numbers went one column to the left and
    the header row fell off the table (pinned by: test_pdf_table_columns.py::
    TestNarrowColumnGaps). Splitting happens only where a table was already found (deliberate) — body lines whose word
    spacing was widened by justification are not a table and are not split
    (test_justified_prose_with_wide_word_spaces_is_left_alone). Lines containing a piece wider than the maximum table cell
    width, and lines of consecutive single Hangul or Hanja character pieces (letter-spaced '구    분',
    `_letter_spaced_parts`), are not split. Known accepted gap: rows whose cells are as close as ordinary word spacing
    (LibreOffice's 3px cell padding) and so have no wide gap are left as they are.
    """
    def splittable(line: _Line) -> bool:
        return _splittable(line, max_cell)

    # Only rows above and below that continue at the row pitch and lie within the table's horizontal range are considered.
    # A body line reaching into the neighbouring column is not a table header.
    # Rows below are considered too because when a row whose cells were merged differently ('광주 | 5,955 | 10.8% 감소')
    # came in the middle of a table, the table was cut above it and the remaining rows, fewer than the minimum row count,
    # became paragraphs in jumbled order (pinned by: test_pdf_round44.py::TestBorderlessTableWithARowMergedDifferently).
    # Downward, a horizontal line is not crossed — the totals block under the bottom border of an item table is not a table
    # (`_cut_at_closing_rule`). A short piece that starts inside the table is accepted even when it sticks out to the right —
    # in a table whose last column (비고, remarks) rarely has a value, a long value in that column ('2월 미집행') went past
    # the right end of the preceding rows and the table was cut from that row on
    # (pinned by: test_pdf_round46.py::TestRightAlignedHeaderMergedWithANearlyEmptyColumn)
    left = min(line.x0 for row in run for line in row) - _TABLE_ROW_TOLERANCE
    right = max(line.x1 for row in run for line in row) + _TABLE_ROW_TOLERANCE

    def joins(row: list[_Line]) -> bool:
        return not any(id(line) in taken or line.x0 < left or line.x0 > right
                       or (line.x1 > right and line.x1 - line.x0 > max_cell) for line in row)

    def spans(table_row: list[_Line]) -> list[int]:
        # the positions in `rows` that hold the original lines of the table row (for a cell joined from several lines, those lines)
        members = {id(original) for line in table_row for original in (line, *line.joined)}
        return [index for index, row in enumerate(rows) if any(id(line) in members for line in row)]

    above: list[list[_Line]] = []
    start = min(spans(run[0]), default=None)
    if start is not None:
        below = run[0]
        for row in reversed(rows[:start]):
            if not joins(row) or below[0].top - max(line.bottom for line in row) > row[0].height * 2.5:
                break
            above.insert(0, row)
            below = row

    after: list[list[_Line]] = []
    end = max(spans(run[-1]), default=None)
    if end is not None:
        upper = run[-1]
        for row in rows[end + 1:]:
            floor, ceiling = max(line.bottom for line in upper), min(line.top for line in row)
            if (not joins(row) or ceiling - floor > upper[0].height * 2.5
                    or any(floor - 1 <= y <= ceiling + 1 and x0 < right and x1 > left for y, x0, x1 in rules)):
                break
            after.append(row)
            upper = row

    region = [line for row in [*above, *run, *after] for line in row]
    if not any(splittable(line) for line in region):
        return run

    # A cell joined from several lines (`_centred_band_row` etc.) is put back as its original lines. Joined cells differ in
    # height from row to row, so regrouping by top edge would scatter one row line by line. They are joined again by the
    # same rules when the table is searched for again
    pieces: list[_Line] = []
    for line in region:
        if splittable(line):
            pieces += [_Line(top=line.top, bottom=line.bottom, x0=x0, x1=x1, text=text, size=line.size, source=line)
                       for x0, x1, text in line.fine_parts or line.parts]
        else:
            pieces += [original for original in line.joined if not original.joined] or [line]

    found = _table_runs(_visual_rows(pieces), max_cell)
    if len(found) != 1 or len(found[0][0]) < len(run[0]):
        return run
    # Original lines that the re-found table holds inside a cell joined from several lines (`_row_with_lead_lines` etc.) also
    # count as held. Only after the right-aligned header '3월 비고' was split did the lead line of the first row's remark get
    # joined to '예정', so '예정' was counted as missing and the header and first row stayed outside the table (pinned by:
    # test_pdf_round47.py::TestWrappedNoteUnderARightAlignedMergedHeader)
    kept = {id(original) for row in found[0] for line in row for original in (line, *line.joined)}
    in_run = {id(line) for row in run for line in row}
    if not all(id(piece) in kept for piece in pieces if id(piece.source or piece) in in_run):
        return run

    # A column containing a split piece must have cells on other rows too. A column produced by splitting justified text
    # inside a cell exists only on that row
    header = found[0][0]
    used = Counter(column for row in found[0] for column in {_column_of(line, header, row) for line in row})
    if any(used[_column_of(line, header, row)] < 2 for row in found[0] for line in row if line.source is not None):
        return run
    return found[0]


def _splittable(line: _Line, max_cell: float) -> bool:
    """A line that can be treated as neighbouring cells merged by pdfminer and split at its wide gaps (`_Line.parts`, or
    `fine_parts`, which also looks at narrow gaps without a space character, when present): it has gaps, every piece is at
    most the maximum table cell width, and there are no consecutive single Hangul or Hanja character pieces
    (`_letter_spaced_parts`).

    The piece width is examined rather than the whole line width (the same criterion as `_split_line_table_runs`) because
    the header '1분기 2분기 3분기 4분기', merged across narrow cell gaps, exceeded 25% of the page width, could not be split
    and became a paragraph outside the table (pinned by: test_pdf_table_columns.py::
    TestHeaderMergedWiderThanACell).

    `fine_parts` is examined too because the gap between a right-aligned number cell and a left-aligned text cell in a
    Chrome table (cell padding 6px + 6px = 9pt, 0.75 times a 12pt character, no space character) fell short of
    `_CELL_GAP_RATIO` (0.8 times) and '18 2023 신규' became one cell (pinned by:
    test_pdf_table_columns.py::TestNarrowColumnGaps::test_right_aligned_numbers_next_to_a_left_aligned_note_column).
    Splitting only inside an already-found table is unchanged."""
    parts = line.fine_parts or line.parts
    return (bool(parts) and all(x1 - x0 <= max_cell for x0, x1, _ in parts)
            and not _letter_spaced_parts(parts))


def _letter_spaced_parts(parts: list[tuple[float, float, str]]) -> bool:
    """Whether the line pieces contain two or more consecutive single Hangul or Hanja character pieces — that is letter-spaced
    text ('구    분') and is not split into cells.

    Blocking on a single one-character piece made it impossible to split '값 단위' in a Chrome three-line table (pdfminer
    merged the 1.3em between cells into one piece), and the header and first row became paragraphs outside the table
    (pinned by: test_pdf_table_columns.py::TestOneLetterHeaderCell)."""
    return any(len(left) == 1 and len(right) == 1 and _SPACED_CHAR.match(left) and _SPACED_CHAR.match(right)
               for (_, _, left), (_, _, right) in zip(parts, parts[1:], strict=False))


def _split_line_table_runs(lines: list[_Line], max_cell: float) -> list[list[list[_Line]]]:
    """The borderless tables found once more after splitting, among the lines not in a table (`lines`), the lines that merged
    neighbouring cells (`_splittable`).

    In a table whose columns are 1 to 1.6 times the font size apart, pdfminer (char_margin 2.0) merged neighbouring cells
    into one piece, the piece count differed from row to row and the table was not found, and with an empty cell the text
    boxes were grouped per column and the rows were shuffled (pinned by: test_pdf_table_columns.py::
    TestBorderlessTableWithNarrowGaps). A table whose first cell wraps to two lines and whose rows are out of step with the
    other, vertically centred cells is also found here (`_with_centred_wrapped_cells`). The split positions are
    `_Line.fine_parts`, which also looks at narrow gaps without a space character (`parts` when absent), and a merged line
    is split even when it exceeds the maximum table cell width as long as each piece is within that width. Only tables
    containing a split piece or a merged two-line cell are accepted; all the pieces of a split line must be in the same
    table, every column containing a split piece must have cells on two or more rows (the same criterion as
    `_split_merged_cells`), every split piece must have a piece on another row whose left end, right end or centre matches,
    and the pieces of every row must sit at one height (`_aligned_in_another_row`, `_on_one_baseline`) — justified body text
    in short lines has column starts that shift from word to word and does not become a table
    (test_pdf_table_columns.py::TestNarrowColumnGaps::
    test_justified_prose_with_wide_word_spaces_is_left_alone).
    """
    pieces: list[_Line] = []
    for line in lines:
        parts = line.fine_parts or line.parts
        if (parts and all(x1 - x0 <= max_cell for x0, x1, _ in parts)
                and not _letter_spaced_parts(parts)):
            pieces += [_Line(top=line.top, bottom=line.bottom, x0=x0, x1=x1, text=text, size=line.size, source=line)
                       for x0, x1, text in parts]
        else:
            pieces.append(line)
    split = Counter(id(piece.source) for piece in pieces if piece.source is not None)
    found = []

    for run in _table_runs(_with_centred_wrapped_cells(_visual_rows(pieces)), max_cell):
        taken = Counter(id(line.source) for row in run for line in row if line.source is not None)
        if not (taken or any(line.joined for row in run for line in row)) or any(split[source] != count
                                                                                 for source, count in taken.items()):
            continue
        header = run[0]
        used = Counter(column for row in run for column in {_column_of(line, header, row) for line in row})
        if all(used[_column_of(line, header, row)] >= 2 and _aligned_in_another_row(line, row, run)
               for row in run for line in row if line.source is not None) and all(map(_on_one_baseline, run)):
            found.append(run)
    return found


def _on_one_baseline(row: list[_Line]) -> bool:
    """Whether the pieces of a row sit at one height (top coordinates within 1pt). Wrapped lines of cell text (more than half a
    line height below the top line) are excluded.

    Two text boxes with different line spacing (an invoice's company details on the left and 'item value' on the right)
    were grouped into one row while 2 to 4pt out of step on every row and became a three-column table (pinned by:
    test_pdf_label_value_rows.py::TestLeftBlockIsNotAWrappedCell — guarded together with `_CENTRED_CELL_RATIO`. That test
    breaks only when both are missing).
    """
    top = min(line.top for line in row)
    return all(line.top - top <= 1 or line.top - top > line.height / 2 for line in row)


def _with_ruled_wrapped_cells(rows: list[list[_Line]], rules: list[tuple[float, float, float]]) -> list[list[_Line]]:
    """The rows with, between two horizontal rules (the rule under the header and the bottom border of a three-line table),
    first-cell text wrapped over several lines grouped into one row with the value row beside it (`_wrapped_label_row`).
    `rows` as is when there are fewer than two rules.

    In a three-line table whose values are aligned to the vertical centre or the bottom, the lead line of the first cell
    was left on its own above the value row, so the table was cut before that row or '| 값) | 900 | 950 |' became the
    header, and the remaining rows scattered cell by cell after the paragraph below the table. With top alignment too, the
    wrapped line of the last row has no following row below and became a paragraph outside the table (pinned by:
    test_pdf_ruled_wrapped_cells.py::TestWrappedFirstCellsInAThreeRuleTable). Nothing is grouped outside the rules — in a
    table without cell padding the line spacing cannot tell whether a wrapped line belongs to the row above or below, and
    the paragraph right above the table could be attached to a header cell. Outside the rules, `_table_runs` groups them only
    under the header of a found table and only when the row gap and line gap clearly differ (`_row_with_lead_lines`).
    """
    if len(rules) < 2:
        return rows
    gap = _paragraph_line_gap(rows, rules)
    merged: list[list[_Line]] = []
    index = 0

    while index < len(rows):
        found = _wrapped_label_row(rows, index, rules, gap)
        if found is None:
            merged.append(rows[index])
            index += 1
            continue
        row, index = found
        merged.append(row)
    return merged


def _paragraph_line_gap(rows: list[list[_Line]], rules: list[tuple[float, float, float]]) -> float | None:
    """The median line gap where single-piece rows continue with matching left ends outside the horizontal rules (above the
    top rule, below the bottom rule). None when there is none.

    The line gap of a paragraph equals the line gap of cell text wrapped with the same font and line spacing
    (`_wrapped_label_row`)."""
    top, bottom = min(y for y, _, _ in rules), max(y for y, _, _ in rules)
    gaps = sorted(line.top - previous.bottom for previous, line in ((above[0], below[0]) for above, below in pairwise(rows)
                                                                    if len(above) == 1 and len(below) == 1)
                  if (previous.bottom < top and line.bottom < top or previous.top > bottom)
                  and abs(line.x0 - previous.x0) <= _TABLE_ROW_TOLERANCE and 0 <= line.top - previous.bottom <= line.height * 2)
    return gaps[len(gaps) // 2] if gaps else None


def _wrapped_label_row(rows: list[list[_Line]], start: int, rules: list[tuple[float, float, float]],
                       paragraph_gap: float | None = None) -> tuple[list[_Line], int] | None:
    """(row, next position) grouping, from `rows[start]`, the first-cell text lines (two or more single-piece lines with the
    same left end, attached at line spacing) with one value row.

    Attached at line spacing means the line gap is at most half the line height, or at most the line gap of the paragraphs
    outside the rules (`paragraph_gap`, `_paragraph_line_gap`) + 1pt. In a table exported by LibreOffice with a Korean font
    the line gap is 0.8 times the line height (9.7pt for 12pt characters), so the first-cell lines were not joined and the
    table was read column by column (pinned by:
    test_pdf_ruled_wrapped_cells.py::TestWrappedFirstCellsWithWideLineSpacing).

    The value row is either a row whose cell text continues at the same height as one of the first-cell text lines, or a row
    of values only that does not overlap the first-cell text horizontally. They are grouped only when the middle of the
    value row lies within the height of the first-cell text lines, horizontal rules above and below enclose the group at the
    table width (with no rule inside the group), and the gaps to the rows before and after the group are wider than the line
    gap inside the group. Otherwise None.
    Exception: when a values-only row sits between the first-cell text lines and its middle is within `_CENTRED_CELL_RATIO`
    of the line height from the middle of the first-cell text lines (vertical centring — with top or bottom alignment the
    value is at the same height as a first-cell line), the gaps before and after are not examined,
    and if lines were collected up to the first line of the next row, only the leading lines whose middle matches are used —
    in a LibreOffice table whose cells have zero top and bottom padding the gap between rows equals the line gap, so the gap
    could not separate them (pinned by: test_cells_without_vertical_padding in the same class). Known accepted gap: when
    such a table is aligned to the top or bottom there is no telling which row a wrapped line belongs to, and nothing is
    grouped.
    """
    first = rows[start][0]
    reach = max(first.height / 2 + 0.5, -1.0 if paragraph_gap is None else paragraph_gap + 1.0)
    labels: list[_Line] = []
    label_ends: list[int] = []  # per line, the position of the next row after using up to that line
    anchor: list[_Line] | None = None
    anchor_end, beside = start, False  # position after the value row, whether the value is at the same height as a first-cell text line
    end = start

    for row in rows[start:]:
        line = row[0]
        if abs(line.x0 - first.x0) <= _TABLE_ROW_TOLERANCE:
            if labels and not 0 <= line.top - labels[-1].bottom <= reach:
                break
            if len(row) > 1:
                if anchor is not None or any(other.x0 < line.x1 for other in row[1:]):
                    break
                anchor, anchor_end, beside = row[1:], end + 1, True
            labels.append(line)
            label_ends.append(end + 1)
        elif (anchor is None and labels and all(other.x0 > max(label.x1 for label in labels) for other in row)
              and labels[-1].top < line.top):
            anchor, anchor_end = row, end + 1
        else:
            break
        end += 1
    if anchor is None or len(labels) < 2:
        return None

    # vertical centring: the first-cell text lines whose middle matches the middle of the value row (two or more, reaching
    # past the value row)
    centre = (anchor[0].top + anchor[0].bottom) / 2
    centred = next((count for count in range(len(labels), 1, -1) if label_ends[count - 1] >= anchor_end
                    and abs((labels[0].top + labels[count - 1].bottom) / 2 - centre) <= first.height * _CENTRED_CELL_RATIO), None)
    if beside:
        # a value at the same height as a first-cell line looks the same as 'bottom alignment + the first line of the next
        # row', so it is no evidence of centring
        centred = None
    if centred is not None:
        labels, end = labels[:centred], label_ends[centred - 1]
    top, bottom = labels[0].top, labels[-1].bottom
    left, right = first.x0, max(line.x1 for line in (*labels, *anchor))
    spanning = [y for y, x0, x1 in rules if x0 <= left + _TABLE_ROW_TOLERANCE and x1 >= right - _TABLE_ROW_TOLERANCE]
    above = max((y for y in spanning if y < top), default=None)
    below = min((y for y in spanning if y > bottom), default=None)
    if not top <= centre <= bottom or above is None or below is None or any(top < y < bottom for y in spanning):
        return None

    # the wrapped lines belong to this row only when the gaps to the rows before and after the group (inside the rules) are
    # wider than the line gap inside the group
    inside = max(line.top - previous.bottom for previous, line in pairwise(labels))
    margin = inside + max(1.0, inside / 2)
    before = rows[start - 1] if start and centred is None else None
    after = rows[end] if end < len(rows) and centred is None else None
    if before is not None and max(line.bottom for line in before) > above and top - max(line.bottom for line in before) < margin:
        return None
    if after is not None and min(line.top for line in after) < below and min(line.top for line in after) - bottom < margin:
        return None

    cell = _Line(top=top, bottom=bottom, x0=first.x0, x1=max(line.x1 for line in labels),
                 text="<br>".join(line.text for line in labels), size=first.size,
                 joined=tuple(original for line in labels for original in (*line.joined, line)))
    return [cell, *anchor], end


def _with_centred_wrapped_cells(rows: list[list[_Line]]) -> list[list[_Line]]:
    """The rows with each row whose remaining cells sit vertically centred at the height between cell text wrapped to two
    lines (A, B) grouped into one row.

    Chrome centres cells vertically, so in a row whose first cell has two lines the text of the other cells sits between the
    two lines of the first cell. In rows grouped by top coordinate this became three rows, [A] · [other cells] · [B], and the
    table was cut (pinned by: test_pdf_table_columns.py::TestBorderlessTableWithNarrowGaps::
    test_first_cell_wrapped_beside_vertically_centred_cells_stays_in_its_row). Only when two single-piece rows have the same
    left end and are attached at line spacing (at most half the line height, with 0.5pt of slack — in a Chrome table with
    4px cell padding the gap is exactly half the line height and fell out through floating-point error, pinned by:
    test_pdf_table_columns.py::TestWideWrappedFirstColumn), the row between them has two or more pieces that do not overlap
    the two horizontally, and its middle height is within `_CENTRED_CELL_RATIO` of the line height from the middle of the
    two lines, A and B are merged into one piece 'A<br>B' (at the height of the middle row, with the original lines in
    `joined`).
    """
    merged: list[list[_Line]] = []
    index = 0

    while index < len(rows):
        found = _centred_wrapped_row(rows, index)
        if found is not None:
            merged.append(found[0])
            index = found[1]
            continue
        merged.append(rows[index])
        index += 1
    return merged


def _centred_wrapped_row(rows: list[list[_Line]], index: int) -> tuple[list[_Line], int] | None:
    """When the three rows from `rows[index]` are a row whose remaining cells are vertically centred at the height between cell
    text wrapped to two lines (A, B), that single row and the next position; otherwise None (conditions in
    `_with_centred_wrapped_cells`)."""
    if not (index + 2 < len(rows) and len(rows[index]) == 1 and len(rows[index + 2]) == 1 and len(rows[index + 1]) >= 2):
        return None
    first, middle, second = rows[index][0], rows[index + 1], rows[index + 2][0]
    centre = (middle[0].top + middle[0].bottom) / 2
    if not (abs(first.x0 - second.x0) <= _TABLE_ROW_TOLERANCE and 0 <= second.top - first.bottom <= first.height / 2 + 0.5
            and first.top < middle[0].top < second.top
            and abs((first.top + second.bottom) / 2 - centre) <= first.height * _CENTRED_CELL_RATIO
            and not any(line.x0 < max(first.x1, second.x1) and min(first.x0, second.x0) < line.x1 for line in middle)):
        return None
    cell = _Line(top=middle[0].top, bottom=middle[0].bottom, x0=min(first.x0, second.x0), x1=max(first.x1, second.x1),
                 text=f"{first.text}<br>{second.text}", size=first.size, joined=(*first.joined, first, *second.joined, second))
    return sorted([cell, *middle], key=lambda line: line.x0), index + 3


def _centred_band_row(rows: list[list[_Line]], index: int, header: list[_Line]) -> tuple[list[_Line], int] | None:
    """In a borderless table, when the mutually attached lines from `rows[index]` are a single vertically centred row whose
    cells wrap to different numbers of lines, that row with the lines joined per column and the next position; otherwise
    None.

    When several cells wrap to different numbers of lines, as in a formula table, the line heights are out of step from cell
    to cell and the lines are staggered within one line height. The visual rows scattered into cell pieces, the table split
    in two, and the wrapped pieces ('함량', 'eco-cost') went to other columns (pinned by: test_pdf_round46.py::
    TestCentredFormulaRowWithCellsWrappedToDifferentHeights). A row in which only one cell wraps is handled by
    `_centred_wrapped_row`. It is accepted only when: the gaps between lines are at most half the line height or they
    overlap; there is a place where the top edges of two neighbouring lines differ by less than 0.75 times the line height (a
    stagger that the consecutive lines of one cell's text cannot produce); the pieces collected per header column do not
    overlap each other and continue at line spacing; their middles all match the middle of the whole row
    (`_CENTRED_CELL_RATIO`); there are at least `_TABLE_MIN_COLUMNS` columns; and one-line cells and multi-line cells occur
    together — otherwise they are several rows whose heights differ slightly from font to font (pinned by:
    test_pdf.py::TestRepeatedMarginsAndBorderlessTables::
    test_borderless_table_row_is_grouped_when_fonts_put_the_cells_at_different_heights).
    """
    band = [rows[index]]
    end = index + 1
    height = min(line.height for line in rows[index])
    while end < len(rows):
        floor = max(line.bottom for row in band for line in row)
        if (rows[end][0].top - floor > height / 2 + 0.5
                or any(abs(line.height - height) > height * 0.2 for line in rows[end])):
            break
        band.append(rows[end])
        end += 1
        if len(band) > _CENTRED_BAND_ROWS:
            return None
    if len(band) < 2 or not any(lower[0].top - upper[0].top < height * 0.75 for upper, lower in pairwise(band)):
        return None

    # collect the lines of cell text per column. Pieces in the same column of one visual row are one line of that cell
    # ('플라스틱 소재')
    groups: dict[int, list[list[_Line]]] = {}
    for row in band:
        pieces: dict[int, list[_Line]] = {}
        for line in row:
            pieces.setdefault(_column_of(line, header), []).append(line)
        for column, same_line in pieces.items():
            groups.setdefault(column, []).append(same_line)
    counts = {len(lines) for lines in groups.values()}
    if len(groups) < _TABLE_MIN_COLUMNS or len(counts) < 2 or min(counts) != 1:
        return None

    top, bottom = min(line.top for row in band for line in row), max(line.bottom for row in band for line in row)
    cells = []
    for column in sorted(groups):
        lines = sorted((_joined_line(same_line, " ") for same_line in groups[column]), key=lambda line: line.top)
        if any(not 0 <= lower.top - upper.bottom + 1 <= upper.height / 2 + 1.5 for upper, lower in pairwise(lines)):
            return None
        if abs((lines[0].top + lines[-1].bottom - top - bottom) / 2) > height * _CENTRED_CELL_RATIO:
            return None
        cells.append(_joined_line(lines, "<br>"))
    return sorted(cells, key=lambda line: line.x0), end


def _joined_line(lines: list[_Line], separator: str) -> _Line:
    """The line joining the pieces into one cell text (the line itself when there is only one). Pieces of the same line are
    passed left to right and wrapped lines top to bottom. The original lines go in `joined`."""
    if len(lines) == 1:
        return lines[0]
    return _Line(top=min(line.top for line in lines), bottom=max(line.bottom for line in lines), x0=min(line.x0 for line in lines),
                 x1=max(line.x1 for line in lines), text=separator.join(line.text for line in lines), size=lines[0].size,
                 joined=tuple(original for line in lines for original in (*line.joined, line)))


def _row_with_lead_lines(rows: list[list[_Line]], index: int, previous: list[_Line], header: list[_Line],
                         row_gap: float | None) -> tuple[list[_Line], int] | None:
    """In a borderless table, when the single-piece lines from `rows[index]` are the lead lines of one cell's text in the row
    right below (the upper lines of bottom-aligned wrapped cell text left on their own above the value row), the row with
    the lead lines joined onto that cell and the next position; otherwise None.

    With bottom-aligned cells, as when Excel exports to PDF, the lead line of a cell wrapped to two lines is left on its own
    above the value row, so the table split at that row and the first row of the second fragment became a header (when it
    was the first data row, the header fell off into per-cell paragraphs; pinned by: test_pdf_round45.py::
    TestWrappedRowsOfABorderlessTable). Taking lines as lead lines is the reverse view of a wrapped line of the cell text on
    the line above (`_continues_cell`): the lines are clearly closer to each other and to the row below than the gap between
    rows (before the first data row, the gap to the row above), they are that far from the row above, and they overlap the
    cell in the same column of the row below horizontally without reaching into the column on the right (`_wraps_into`).
    After a row with a number cell, when the row below has no number cell it may be a caption above a new table's header and
    is not joined (the same idea as `_is_section_row`).
    """
    lead = [rows[index][0]]
    end = index + 1
    while end < len(rows) and len(rows[end]) == 1 and rows[end][0].top - lead[-1].bottom <= lead[-1].height / 2 + 0.5:
        lead.append(rows[end][0])
        end += 1
    if end >= len(rows) or len(rows[end]) < 2:
        return None
    row = rows[end]
    if _has_number_cell(previous) and not _has_number_cell(row):
        return None

    above = lead[0].top - max(line.bottom for line in previous)
    inner = [lower.top - upper.bottom for upper, lower in pairwise(lead)] + [min(line.top for line in row) - lead[-1].bottom]
    reference = above if row_gap is None else min(row_gap, above)
    height = lead[0].height
    if min(inner) < 0 or max(inner) > reference * _WRAP_GAP_RATIO or reference - max(inner) < height * _WRAP_GAP_MIN_RATIO:
        return None
    if any(abs(line.x0 - lead[0].x0) > _TABLE_ROW_TOLERANCE for line in lead):
        return None

    columns = [_column_of(cell, header, row) for cell in row]
    for line in lead:
        candidates = {_column_of(line, header)} | {
            column for cell, column in zip(row, columns, strict=True) if abs(cell.x0 - line.x0) <= _TABLE_ROW_TOLERANCE}
        if not any(_wraps_into(line, row, columns, header, column) for column in candidates):
            return None

    target = next(cell for cell in row if cell.x0 < lead[0].x1 and lead[0].x0 < cell.x1)
    cell = _Line(top=target.top, bottom=target.bottom, x0=min(target.x0, *(line.x0 for line in lead)),
                 x1=max(target.x1, *(line.x1 for line in lead)), text="<br>".join([*(line.text for line in lead), target.text]),
                 size=target.size, joined=(*(original for line in lead for original in (*line.joined, line)), *target.joined, target))
    return sorted([cell if line is target else line for line in row], key=lambda line: line.x0), end + 1


def _in_the_band_of(lines: list[_Line], row: list[_Line], bands: list[tuple[float, float, float, float]]) -> bool:
    """Whether `lines` (the wrapped lines below the last row) are inside the same shaded band as `row`: there is a filled
    rectangle that covers the top of the row and the bottom of the lines and covers the row's horizontal range.

    In a striped table whose whole rows are shaded the fill tells the extent of a row, yet the wrapped line of the last row
    ('운영') had no following row below and became a paragraph under the table (pinned by:
    test_pdf_round45.py::TestWrappedLastRowOfAStripedTable). In a table without fill it stays a line under the table as
    before (accepted gap)."""
    if not lines or not bands:
        return False
    top, bottom = min(line.top for line in row), max(line.bottom for line in lines)
    left, right = min(line.x0 for line in (*row, *lines)), max(line.x1 for line in (*row, *lines))
    return any(band_top - 1 <= top and bottom <= band_bottom + 1 and band_left - 1 <= left and right <= band_right + 1
               for band_top, band_bottom, band_left, band_right in bands)


def _aligned_in_another_row(line: _Line, row: list[_Line], run: list[list[_Line]]) -> bool:
    """Whether another row has a fragment whose left edge, right edge or centre matches within `_TABLE_ROW_TOLERANCE` (column alignment)."""
    return any(min(abs(other.x0 - line.x0), abs(other.x1 - line.x1), abs(other.x0 + other.x1 - line.x0 - line.x1) / 2)
               <= _TABLE_ROW_TOLERANCE for others in run if others is not row for other in others)


def _column_table_runs(page: _Page, runs: list[list[list[_Line]]], columns: tuple[float, float],
                       max_cell: float) -> list[list[list[_Line]]]:
    """Borderless tables found on a two-column page (`columns` is the left column end and right column start from
    `_table_columns`) by grouping rows only from the lines inside one page column (excluding the lines of the tables
    already found, `runs`).

    When rows are grouped from the lines at the same height across the whole page, a body line of the neighbouring
    column slips into every row of a table that sits in one column, the row fails the 'short fragment' criterion, and
    the table scattered into one paragraph per cell (Pinned by: test_pdf.py::TestBorderlessTableInOneColumn). Inside a
    column the table criteria (`_table_runs`) are unchanged.
    """
    left_end, right_start = columns
    # The original lines of a cell joined from several lines are in a table too. Left in `rest`, the lines of a row whose
    # cells wrap to several lines (`_centred_band_row`) made a second table inside the column and came out twice
    # (Pinned by: test_audit_pdf_tail.py::TestJoinedCellLinesOnATwoColumnPage::
    # test_lines_of_a_joined_row_are_not_a_second_table).
    in_table = {id(original) for run in runs for row in run for line in row for original in (line, *line.joined)}
    rest = [line for line in page.lines if id(line) not in in_table]
    found: list[list[list[_Line]]] = []
    for side in ([line for line in rest if line.x1 <= right_start], [line for line in rest if line.x0 >= left_end]):
        found += [_regrouped_half_rows(run, max_cell) for run in _table_runs(_visual_rows(side), max_cell)]
    return found


def _mixes_two_columns(run: list[list[_Line]], left_end: float, right_start: float) -> bool:
    """Whether a table found across the whole page (`run`) mixes the rows of two tables, one in each column of a
    two-column page: lines exist in both columns with none spanning the gap between them, and some row has lines in
    only one column (the row heights of the two tables are out of step).

    With one table in each column, rows of the two tables at the same height were grouped into one row and mixed like
    '공문 | 71.2 | … | 형식 | 평균 | …', and the header rows, whose columns did not line up, scattered outside the
    table. Such a table is dropped and searched again inside each column (`_column_table_runs`, Pinned by: test_pdf.py::
    TestBorderlessTableInOneColumn::test_tables_in_both_columns_under_a_centred_title_keep_their_own_rows). A
    single-column table that spans both columns has cells in both columns in every row, or a cell spanning the gap, so
    it is left alone (Deliberate — two side-by-side tables whose row heights are also identical cannot be separated).
    """
    def side(line: _Line) -> str:
        if line.x1 <= right_start:
            return "left"
        return "right" if line.x0 >= left_end else "across"

    sides = [{side(line) for line in row} for row in run]
    if any("across" in row for row in sides) or not set().union(*sides) >= {"left", "right"}:
        return False
    return any(len(row) == 1 for row in sides)


def _table_columns(page: _Page) -> tuple[float, float] | None:
    """The (left column end, right column start) of the two page columns that `_column_table_runs` looks at. None if
    the page is not two-column.

    Lines wider than a table cell and at most half the page width are split at the page centre into two columns; the
    lines of each column must lie beside the other (within the height from the top to the bottom of the other column,
    `_beside_each_other`), at least `_COLUMN_LINES_MIN` per column. On a single-column page the last lines of
    paragraphs (left) and the wide '비고' (remarks) cells of a table below (right) were judged to be two columns, and
    the '비고' column broke away from the table (Pinned by: test_pdf.py::TestBorderlessTableInOneColumn::
    test_paragraph_ends_above_a_table_with_a_wide_note_column_are_not_two_columns).

    If the left column end still passes the right column start, lines spanning the page centre (centred title, authors,
    figure caption) are removed and the measurement is repeated. A centred line above the two columns or in the middle
    of the page was counted as a right-column line, the two-column verdict switched off, and the table inside a column
    scattered (Pinned by: test_table_in_a_column_under_a_centred_title_or_caption_is_a_table,
    test_table_in_a_column_below_a_figure_caption_in_the_middle_of_the_page_is_a_table). A page that works with the
    first measurement uses that value — so that the column lines of a two-column page whose gap is off the page centre
    are not removed as spanning lines.
    """
    wide = page.width * _TABLE_CELL_MAX_RATIO
    body = [line for line in page.lines if wide <= line.x1 - line.x0 <= page.width / 2]
    lefts = [line for line in body if line.x0 + line.x1 < page.width]
    rights = [line for line in body if line.x0 + line.x1 >= page.width]
    if min(len(lefts), len(rights)) < _COLUMN_LINES_MIN:
        return None
    lefts, rights = _beside_each_other(lefts, rights), _beside_each_other(rights, lefts)
    if min(len(lefts), len(rights)) < _COLUMN_LINES_MIN:
        return None

    left_end, right_start = max(line.x1 for line in lefts), min(line.x0 for line in rights)
    if left_end < right_start:
        return left_end, right_start

    # Remove the lines spanning the page centre and measure again
    middle = page.width / 2
    lefts = [line for line in lefts if line.x1 <= middle]
    rights = [line for line in rights if line.x0 >= middle]
    if min(len(lefts), len(rights)) < _COLUMN_LINES_MIN:
        return None
    left_end, right_start = max(line.x1 for line in lefts), min(line.x0 for line in rights)
    return (left_end, right_start) if left_end < right_start else None


def _regrouped_half_rows(run: list[list[_Line]], max_cell: float) -> list[list[_Line]]:
    """If two neighbouring rows of a table grouped by top coordinate (`run`) overlap vertically (cells of the same row
    split into halves by a difference in font height), regroup the table's lines by vertical overlap (`staggered` of
    `_visual_rows`) and replace it with the single table found. If the regrouped result is not one table made of all
    of those lines, leave it as is.

    In a table whose number cells and cells starting with a digit ('1차') sat a few pt higher than the Hangul cells,
    '1차·날짜' and '담당 부서·대상' became different rows, yet the half rows also satisfied the table criteria, so the
    second search that regroups by overlap (only lines not in a table) never ran (Pinned by:
    test_pdf.py::TestRepeatedMarginsAndBorderlessTables::test_borderless_table_split_into_half_rows_by_font_heights_is_regrouped).
    """
    def overlapping(upper: list[_Line], lower: list[_Line]) -> bool:
        top, bottom = min(line.top for line in lower), max(line.bottom for line in upper)
        height = min(min(line.height for line in upper), min(line.height for line in lower))
        return bottom - top >= height * _ROW_OVERLAP_RATIO

    if not any(overlapping(upper, lower) for upper, lower in zip(run, run[1:], strict=False)):
        return run

    lines = [line for row in run for line in row]
    regrouped = _table_runs(_visual_rows(lines, staggered=True), max_cell)
    if len(regrouped) != 1 or sum(len(row) for row in regrouped[0]) != len(lines):
        return run
    return regrouped[0]


def _table_runs(rows: list[list[_Line]], max_cell: float,
                bands: list[tuple[float, float, float, float]] = ()) -> list[list[list[_Line]]]:
    """The groups of rows in `rows` (top to bottom) that meet the borderless table criteria.

    A data row below the header may have one wide cell — when that cell fits inside its own column
    (`_wide_cell_in_its_column`). Such a row is not counted towards the minimum number of table rows (same as a section
    row). The minimum must be met, as before, by rows with short cells only (Deliberate, the conservative criterion is
    unchanged). In a Chrome table whose '과제명' (task name) column, 45% of the page width, wrapped onto two lines, the
    table was cut at every wide-cell row and the rows were mixed column by column (Pinned by:
    test_pdf_table_columns.py::TestWideWrappedFirstColumn).

    `bands` are the filled rectangles of the page (`_Page.bands`). A wrapped line below the last row is joined only
    when it lies in the same filled band as that row (`_in_the_band_of`).
    """
    def short_and_separate(row: list[_Line]) -> bool:
        return all(line.x1 - line.x0 <= max_cell for line in row) and _is_separate(row)

    def gap_below(previous: list[_Line], row: list[_Line]) -> float:
        return row[0].top - max(line.bottom for line in previous)

    def accepts(row: list[_Line], previous: list[_Line], header: list[_Line], lead: float = 0.0) -> bool:
        # A row after the first data row with fewer fragments than the minimum number of table columns must match the
        # previous row in font size. The header may be larger than the data, so the first data row is not checked, and
        # neither is a row with all fragments present (an OCR text layer varies in size from row to row, which cut the
        # rows of a price list). `lead` is the height of the leading lines of cell text placed separately above the
        # row (`_row_with_lead_lines`). The gap between rows is measured from above those lines
        columns = [_column_of(line, header, row) for line in row]
        return (len(row) >= 2 and (short_and_separate(row) or _wide_cell_in_its_column(row, header, max_cell))
                and (len(set(columns)) == len(columns) or _spread_over_one_cell(row, columns, header))
                and gap_below(previous, row) - lead <= previous[0].height * 2.5
                and (previous is header or len(row) >= _TABLE_MIN_COLUMNS or _sizes_match(row, previous))
                and (len(run) < 3 or not _out_of_line(row, run, header)))

    runs: list[list[list[_Line]]] = []
    index = 0
    taken = 0  # Position after the rows already grouped into a table. Not crossed when extending upwards
    while index < len(rows):
        header = rows[index]
        if len(header) < _TABLE_MIN_COLUMNS or not short_and_separate(header):
            index += 1
            continue
        run = [header]
        following = index + 1
        row_gap: float | None = None  # Gap between the rows of this table (the smallest). Tells wrapped cell lines apart
        sections = 0  # Number of section rows without values. Not counted towards the minimum number of table rows
        full_wide = 0  # Rows with a wide cell that still fill the minimum number of table columns (also in `sections`)
        full_short = 0  # Data rows that fill the minimum number of table columns with short cells only
        centred_band = False  # Whether there is a centred row whose cells wrapped onto differing line counts (`_centred_band_row`)
        while following < len(rows):
            row, previous = rows[following], run[-1]
            if len(row) == 1 and row_gap is not None:
                # Wrapped lines of cell text (there may be several) are joined to the cell only when more table rows
                # follow below. A short note set right below the last row ('* 잠정치', '출처: 인사팀') was attached to
                # the first cell
                # The bottom of the growing row is carried along and the columns of its cells are worked out from what
                # is already known about each cell (`known`, `_cell_columns`): measuring both again for every wrapped
                # line took time cubic in the number of lines under one row (Pinned by: test_audit_pdf_tail.py::
                # TestManyWrappedLinesUnderOneRow).
                merged, ahead = previous, following
                floor, known = max(line.bottom for line in merged), {}
                while (ahead < len(rows) and len(rows[ahead]) == 1
                       and _continues_cell(rows[ahead][0], merged, header, rows[ahead][0].top - floor, row_gap, known)):
                    floor = max(floor, rows[ahead][0].bottom)
                    merged, ahead = [*merged, *rows[ahead]], ahead + 1
                if ahead > following and ((ahead < len(rows) and accepts(rows[ahead], merged, header))
                                          or _in_the_band_of(merged[len(previous):], previous, bands)):
                    run[-1], following = merged, ahead
                    continue
            band = _centred_band_row(rows, following, header)
            if band is not None and accepts(band[0], previous, header, band[0][0].top - min(line.top for line in row)):
                run.append(band[0])
                centred_band = True
                gap = min(line.top for line in row) - max(line.bottom for line in previous)
                row_gap = gap if row_gap is None else min(row_gap, gap)
                following = band[1]
                continue
            if len(row) == 1:
                # In a row whose cell text wraps onto two lines while the other cells are vertically centred or bottom-aligned,
                # the leading line (and, when centred, the trailing line) stands apart
                wrapped = (_centred_wrapped_row(rows, following)
                           or _row_with_lead_lines(rows, following, previous, header, row_gap))
                if wrapped is not None and accepts(wrapped[0], previous, header, wrapped[0][0].top - row[0].top):
                    run.append(wrapped[0])
                    sections += not short_and_separate(wrapped[0])
                    full_wide += not short_and_separate(wrapped[0]) and len(wrapped[0]) >= _TABLE_MIN_COLUMNS
                    gap = row[0].top - max(line.bottom for line in previous)
                    row_gap = gap if row_gap is None else min(row_gap, gap)
                    following = wrapped[1]
                    continue
            if (len(row) == 1 and following + 1 < len(rows) and accepts(rows[following + 1], row, header)
                    and (_is_section_row(row[0], previous, rows[following + 1], header, row_gap, max_cell)
                         or _is_lone_value_row(row[0], previous, rows[following + 1], header, row_gap, max_cell))):
                run.append(row)
                following += 1
                sections += 1
                continue
            if not accepts(row, previous, header):
                break
            run.append(row)
            sections += not short_and_separate(row)  # A wide-cell row is not counted towards the minimum either
            full_wide += not short_and_separate(row) and len(row) >= _TABLE_MIN_COLUMNS
            full_short += short_and_separate(row) and len(row) >= _TABLE_MIN_COLUMNS
            gap = gap_below(previous, row)
            row_gap = gap if row_gap is None else min(row_gap, gap)
            following += 1
        # A wide-cell row with all cells filled counts towards the minimum only when at least one data row fills all
        # cells with short cells only. In a task table of two or three rows where rows with a wrapped task name and
        # long rows were mixed, there was only one short row, so the table was not found and scattered column by column
        # (owner and due date ended up after the paragraph below the table, Pinned by:
        # test_pdf_round46.py::TestSmallTableWithAWrappedCellAndAWideCell). Not counted when the only short rows have
        # cells missing — the '가. 일 시 …' item lines of an official letter looked like three cells because of the
        # letter-spaced item names and became a table (test_item_lines_of_an_official_letter_stay_lines).
        # Counted only when the table is closed both above and below by a single-fragment line (title, paragraph) or a
        # wide gap — taking the top part, cut at a row whose cells are merged differently, as a table first blocked
        # the path that splits that row and searches the whole table again (`_split_merged_cells`), breaking up a
        # large table (test_top_of_a_table_cut_at_a_row_merged_differently_is_not_taken), and a piece open at the top
        # took the wrapped second line of cell text as its header, scattering the rows above into paragraphs
        # (test_wrapped_rows_of_a_top_aligned_table_are_not_cut_at_a_second_line).
        # A centred staggered row is itself evidence of a table, so the header and that row alone make a table
        # (header + one formula row)
        counted = len(run) - sections
        closed = (following < len(rows) and len(rows[following]) == 1
                  and gap_below(run[-1], rows[following]) <= run[-1][0].height * 2.5
                  and (index == 0 or len(rows[index - 1]) == 1 or gap_below(rows[index - 1], header) > header[0].height * 2.5))
        widened = full_short and closed and counted + full_wide >= _TABLE_MIN_ROWS
        if counted < _TABLE_MIN_ROWS and (counted < 2 or not (widened or centred_band)):
            index += 1
            continue

        above = _table_rows_above(rows[taken:index], run, max_cell)
        corner = None if above or index <= taken else _blank_corner_header(rows[index - 1], run, max_cell)
        runs.append([*above, *([corner] if corner else []), *run])
        index = taken = following
    return runs


def _blank_corner_header(row: list[_Line], run: list[list[_Line]], max_cell: float) -> list[_Line] | None:
    """If `row`, right above a found borderless table (`run`), is a header row whose top-left cell is blank, that row
    (with an empty cell fragment inserted at the front); otherwise None.

    When the header of the first column is left blank, as in a quarterly table, the header has one fragment fewer than
    the data rows and did not fit the table's columns. The header fell apart into one paragraph per cell and the first
    data row became the header (Pinned by: test_pdf_table_columns.py::TestBorderlessTableWithBlankCorner). Accepted
    only: a row with one fragment fewer than the table's first row, all short, starting after the first-column cell
    ends, each fragment matching in turn, from the second cell of the first row, by left edge, right edge or centre
    (`_TABLE_ROW_TOLERANCE`), and attached at row spacing.
    """
    first = run[0]
    if len(row) != len(first) - 1 or len(row) < 2 or not _is_separate(row) or any(line.x1 - line.x0 > max_cell for line in row):
        return None
    if row[0].x0 <= first[0].x1 or first[0].top - max(line.bottom for line in row) > row[0].height * 2.5:
        return None
    if not all(min(abs(cell.x0 - line.x0), abs(cell.x1 - line.x1), abs(cell.x0 + cell.x1 - line.x0 - line.x1) / 2)
               <= _TABLE_ROW_TOLERANCE for line, cell in zip(row, first[1:], strict=True)):
        return None
    corner = first[0]
    return [_Line(top=row[0].top, bottom=row[0].bottom, x0=corner.x0, x1=corner.x0, text="", size=row[0].size), *row]


def _wide_cell_in_its_column(row: list[_Line], header: list[_Line], max_cell: float) -> bool:
    """Whether the fragments of the row do not overlap, exactly one exceeds the maximum table cell width, and that
    fragment fits inside its own header column: it ends before the next header cell starts and starts after the
    previous header cell ends (`_table_runs`). A wide fragment that crosses columns (a body line, a fragment merging
    two cells) is not accepted."""
    wide = [line for line in row if line.x1 - line.x0 > max_cell]
    if len(wide) != 1 or not _is_separate(row):
        return False
    column = _header_column(wide[0], header)
    return ((column + 1 >= len(header) or wide[0].x1 < header[column + 1].x0)
            and (column == 0 or wide[0].x0 > header[column - 1].x1))


def _spread_over_one_cell(row: list[_Line], columns: list[int], header: list[_Line]) -> bool:
    """Whether the several fragments in the row's first column (the item-name column; `columns` is the column of each
    fragment) are the text of one cell spread across the cell width by justification ('운영      경비'): apart from
    the first column there is one fragment per column, the first-column fragments are all short Hangul/Hanja words
    (`_SPREAD_WORD`), and they end before the next header cell starts. The header had one column too few and the
    header row fell out of the table (Pinned by: test_pdf_forms_and_amounts.py::
    TestDistributedLabelsInABorderlessTable). The cell text is joined with spaces between fragments of the same row
    (`_extract_aligned_tables`). Restricting this to the first column and requiring it to end before the next header
    cell is deliberate — a text cell in a column with a blank header stuck to the cell of the neighbouring column and
    the row was mashed together (generated borderless text table).
    """
    first = [line for line, column in zip(row, columns, strict=True) if column == 0]
    if len(first) < 2 or len(header) < 2 or len(set(columns)) != len(columns) - len(first) + 1:
        return False
    return all(_SPREAD_WORD.match(line.text) for line in first) and first[-1].x1 < header[1].x0


def _sizes_match(row: list[_Line], previous: list[_Line]) -> bool:
    """Whether the font sizes of two rows (median of the fragment sizes) are within `_TABLE_SIZE_RATIO`. A row of
    unknown size (0) is treated as matching.

    The two fragments of the left and right section headings (larger type) of a two-column band below a table were
    attached as the last row of the table, giving '| 3. 결과 | | 4. 결론 |'
    (Pinned by: test_pdf.py::TestBorderlessTableEnd::test_headings_of_two_columns_below_a_table_are_not_a_table_row).
    Not applied to rows with at least the minimum number of table columns (`_table_runs`) — the price list of an OCR
    book, whose font size varies from row to row, was cut
    (Pinned by: test_pdf.py::TestBorderlessTableEnd::test_full_rows_of_varying_font_size_stay_in_the_table).
    """
    sizes = [statistics.median(line.size for line in lines) for lines in (row, previous)]
    return min(sizes) <= 0 or max(sizes) <= min(sizes) * _TABLE_SIZE_RATIO


def _continues_cell(line: _Line, previous: list[_Line], header: list[_Line], gap: float, row_gap: float,
                    known: dict | None = None) -> bool:
    """Whether a single-fragment line (`line`) is the wrapped continuation of cell text in the row right above (`previous`).

    When cell text wraps onto two lines, the wrapped line becomes a single-fragment row, the table is cut there, and if
    fewer than 3 rows remain the values scattered one per line ('Ⅷ. 법인세비용차감전순이익(손실)' in an income
    statement). It is treated as a wrapped line only when it sits clearly closer than the gap between rows, both in
    ratio and in size (`_WRAP_GAP_RATIO`, `_WRAP_GAP_MIN_RATIO`), overlaps the cell above in the same column
    horizontally, has the same size, and does not reach the cell of a column to the right. A single-fragment line at
    ordinary row spacing (a note below the table, a category row) ends the table as before. A row of two or more
    fragments is a separate row as before (Pinned by: test_pdf.py::TestBorderlessTableWithWrappedCell).

    For a cell of the row above, the column is the one decided by looking at the fragments of the same row
    (`_column_of`); for the wrapped line, if the column decided from the header positions differs from the cell above,
    the column of a cell above whose left edge matches is also tried — a wrapped line ('회의실') lying in the gap
    before a centred header was counted as the first column and the table was cut
    (Pinned by: test_pdf_table_columns.py::TestCentredHeadersOverLeftAlignedCells::
    test_wrapped_line_of_a_cell_in_the_gap_before_a_centred_header_stays_in_the_cell).

    `known` is what a caller that asks about one growing row again and again keeps between calls (`_cell_columns`).
    """
    if not 0 <= gap <= row_gap * _WRAP_GAP_RATIO or row_gap - gap < line.height * _WRAP_GAP_MIN_RATIO:
        return False

    columns = [_column_of(cell, header, previous) for cell in previous] if known is None else _cell_columns(previous, header, known)
    candidates = {_column_of(line, header)} | {
        column for cell, column in zip(previous, columns, strict=True) if abs(cell.x0 - line.x0) <= _TABLE_ROW_TOLERANCE}
    return any(_wraps_into(line, previous, columns, header, column) for column in sorted(candidates))


def _cell_columns(row: list[_Line], header: list[_Line], known: dict) -> list[int]:
    """The same list as `[_column_of(cell, header, row) for cell in row]` for a row that grows: `known` (empty at first)
    keeps what was worked out in the previous call, and `row` is the row of that call with cells added at the end.

    `_column_of` looks at the other cells of the row for two things only: whether an earlier cell occupies the header
    column of the cell (`taken` in `_gap_column` — true exactly when one of the two cells of that header column that end
    leftmost, the cell itself aside, ends before the cell starts), and whether another cell goes to the same column under
    the gap rule. So the gap rule is asked with a row of just that leftmost cell and the cell itself, again only for the
    header column in which an added cell became one of the two leftmost, and the second question is answered by
    counting. Asking `_column_of` for every cell of the row again for every further wrapped line took tens of seconds
    with several hundred lines under one row (Pinned by: test_audit_pdf_tail.py::TestManyWrappedLinesUnderOneRow; the
    equality with `_column_of` by test_columns_of_a_growing_row_are_those_of_column_of).
    """
    heads: list[int] = known.setdefault("heads", [])  # `_header_column` of each cell
    gaps: list[int] = known.setdefault("gaps", [])  # `_gap_column` of each cell in this row
    leftmost: dict[int, list[_Line]] = known.setdefault("leftmost", {})  # header column → the two cells ending leftmost
    seen = len(heads)
    moved: set[int] = set()  # header columns whose leftmost cells changed: `taken` may have changed for their cells
    for cell in row[seen:]:
        heads.append(_header_column(cell, header))
        gaps.append(heads[-1])
        ends = leftmost.setdefault(heads[-1], [])
        if len(ends) < 2 or cell.x1 < max(other.x1 for other in ends):
            if len(ends) == 2:
                ends.remove(max(ends, key=lambda other: other.x1))
            ends.append(cell)
            moved.add(heads[-1])
    for index in range(0 if moved else seen, len(row)):
        cell, head = row[index], heads[index]
        if index >= seen or head in moved:
            earlier = next((other for other in leftmost[head] if other is not cell and other.x1 <= cell.x0), None)
            gaps[index] = _gap_column(cell, header, [cell] if earlier is None else [earlier, cell], head)
    shared = Counter(gaps)
    return [head if shared[gap] > 1 else gap for head, gap in zip(heads, gaps, strict=True)]


def _wraps_into(line: _Line, previous: list[_Line], columns: list[int], header: list[_Line], column: int) -> bool:
    """Whether `line` overlaps the cell in column `column` of the row above (`previous`, with the column of each
    fragment in `columns`) horizontally, has the same size, and does not reach a column to the right (`_continues_cell`)."""
    later = [cell.x0 for index, cell in enumerate(header) if index > column]
    later += [cell.x0 for cell, other in zip(previous, columns, strict=True) if other > column]
    if later and line.x1 > min(later):
        return False
    return any(other == column and cell.x0 < line.x1 and line.x0 < cell.x1 and abs(cell.size - line.size) <= 0.5
               for cell, other in zip(previous, columns, strict=True))


def _is_section_row(line: _Line, previous: list[_Line], following: list[_Line], header: list[_Line],
                    row_gap: float | None, max_cell: float) -> bool:
    """Whether a single-fragment line (`line`) is a section row without values ('자산', '부채') inside a borderless table.

    The section rows of a balance sheet cut the table, so the header fell apart into one paragraph per cell, and a
    subtotal row with an empty note cell ('Ⅰ. 유동자산') could not be a header candidate, so the amounts scattered to
    the end of the page
    (Pinned by: test_pdf.py::TestBorderlessTableEnd::test_section_rows_without_values_stay_in_the_table).
    It is treated as a section row only when it is short and lies within the first column (ends before the cells of
    the second column start), matches the following row in font size (`_sizes_match` — in an equation document a
    subheading between rows that happened to look like a table was sucked into the table), and the gaps above and
    below are at most `_SECTION_ROW_GAP_RATIO` times the gap between the rows of this table (before the first data
    row, the smaller of the gaps above and below). Whether the following row continues the table is checked by the
    caller, and a section row is not counted towards the minimum number of table rows (in an equation document a
    subheading between two rows that happened to line up made a 3-row table).
    A caption set further apart between tables is not a section row
    (Pinned by: test_short_line_set_apart_between_two_tables_is_not_a_section_row).
    After a row with a numeric value cell, if the following row has no numeric value cell at all, that row is the
    header of a new table, so the line is not accepted as a section row — in a document with tight paragraph spacing a
    caption, note or sentence between two tables sat at exactly the row spacing and the two tables became one
    (Pinned by: test_short_line_at_row_spacing_before_the_header_of_a_next_table_is_not_a_section_row). A table whose
    values are all text cannot use this check.
    """
    later = [cell.x0 for cell in (*header, *previous) if _column_of(cell, header) > 0]
    if (line.x1 - line.x0 > max_cell or _column_of(line, header) != 0 or not later or line.x1 >= min(later)
            or not _sizes_match([line], following)):
        return False
    if _has_number_cell(previous) and not _has_number_cell(following):
        return False
    return _at_row_spacing(line, previous, following, row_gap)


def _is_lone_value_row(line: _Line, previous: list[_Line], following: list[_Line], header: list[_Line],
                       row_gap: float | None, max_cell: float) -> bool:
    """Whether a single-fragment line (`line`) is a row inside a borderless table with only one cell filled, not in the
    first column (a subtotal amount).

    A value in the first column was accepted as a section row (`_is_section_row`), but a single value in the second
    column or later cut the table, so the amount fell out as a paragraph and the first data row of the second piece
    became a header (Pinned by: test_pdf_round45.py::TestSubtotalRowInsideABorderlessTable). Accepted only when it is
    short, matches a cell of the same column in the header or the row above or below by left edge, right edge or
    centre (a cell placed in its own column), matches the following row in font size, and the gaps above and below are
    at row spacing (`_at_row_spacing`). If the row after a number cell has no number cell, it is the header of a new
    table and is not accepted (same as a section row).
    The header is also checked because a subtotal in a column that is empty in every other row (비고, remarks) found no
    cell to align with above or below and cut the table (Pinned by:
    test_pdf_round46.py::TestLoneSubtotalInAnOtherwiseEmptyLastColumn).
    """
    column = _column_of(line, header)
    if line.x1 - line.x0 > max_cell or column == 0 or not _sizes_match([line], following):
        return False
    if _has_number_cell(previous) and not _has_number_cell(following):
        return False
    if not any(_column_of(cell, header, row) == column and _edge_aligned(cell, line)
               for row in (header, previous, following) for cell in row):
        return False
    return _at_row_spacing(line, previous, following, row_gap)


def _at_row_spacing(line: _Line, previous: list[_Line], following: list[_Line], row_gap: float | None) -> bool:
    """Whether the gaps above and below `line` are at most `_SECTION_ROW_GAP_RATIO` times the gap between the table's
    rows (before the first data row, the smaller of the gaps above and below)."""
    gaps = (line.top - max(cell.bottom for cell in previous), min(cell.top for cell in following) - line.bottom)
    reference = min(gaps) if row_gap is None else row_gap
    return min(gaps) >= 0 and max(gaps) <= reference * _SECTION_ROW_GAP_RATIO + 1


def _edge_aligned(line: _Line, other: _Line) -> bool:
    """Whether the left edges, right edges or centres of two fragments match within `_TABLE_ROW_TOLERANCE`."""
    return min(abs(line.x0 - other.x0), abs(line.x1 - other.x1), abs(line.x0 + line.x1 - other.x0 - other.x1) / 2) <= _TABLE_ROW_TOLERANCE


def _out_of_line(row: list[_Line], run: list[list[_Line]], header: list[_Line]) -> bool:
    """Whether `row` is a row of another table that does not fit the columns of the borderless table being built
    (`run`, a header and two or more data rows): it has two or more fragments outside the first column and none of
    them matches any cell of the same column in the data rows by left edge, right edge or centre (`_edge_aligned`).

    Two borderless tables with different column counts, set one row spacing apart, were merged into one table and the
    cells of the second landed in the wrong columns ('| 부서 | 인원 | | 비고 |', Pinned by:
    test_pdf_round45.py::TestBorderlessTablesWithDifferentColumnsNextToEachOther). A centred header does not match
    left-aligned cells, so the header is left out of the reference, and the check runs only when there are two or more
    reference data rows (the first column is not checked because it often holds indented sub-items).
    The reference is the `_OUT_OF_LINE_ROWS` data rows right above — looking at the whole table for every row takes
    quadratic time on a long table."""
    later = [line for line in row if _column_of(line, header, row) > 0]
    if len(later) < 2:
        return False
    placed: dict[int, list[_Line]] = {}
    for other in run[max(1, len(run) - _OUT_OF_LINE_ROWS):]:
        for line in other:
            placed.setdefault(_column_of(line, header, other), []).append(line)
    return not any(_edge_aligned(line, other) for line in later for other in placed.get(_column_of(line, header, row), ()))


def _has_number_cell(row: list[_Line]) -> bool:
    """Whether the row has a cell holding only a numeric value."""
    return any(_NUMBER_CELL.match(line.text.strip()) for line in row)


def _is_separate(row: list[_Line]) -> bool:
    """Whether the fragments of the row do not overlap horizontally."""
    return all(right.x0 >= left.x1 - 1 for left, right in zip(row, row[1:], strict=False))


def _has_one_wide_cell_at_most(row: list[_Line], max_cell: float) -> bool:
    """Whether the fragments can be cells: they do not overlap and at most one exceeds the maximum table cell width
    (one wide cell such as 품명 (item name) or 비고 (remarks))."""
    return _is_separate(row) and sum(1 for line in row if line.x1 - line.x0 > max_cell) <= 1


def _table_rows_above(above: list[list[_Line]], run: list[list[_Line]], max_cell: float) -> list[list[_Line]]:
    """The rows of the same table that continue right above a table found with the conservative criteria (`run`) — the
    tail of `above`. [] if there are none.

    In rows where one cell (품명, item name) is wide the 'short fragment' criterion breaks, so the top part of the table
    scattered column by column and the bottom part, made of short rows only, became the table, turning a data row in
    the middle into the header. The criterion itself is left as is (Deliberate, conservative); only the rows above that
    are attached to a table found with it are accepted: they continue at row spacing, have two or more fragments and at
    most one wide fragment. The topmost row, which becomes the new header, must meet the old criterion unchanged (3 or
    more short fragments), and the fragments of every row below it must fall into different columns of that header
    (Pinned by:
    test_pdf.py::TestBorderlessTableWithWideCells::test_table_found_among_short_rows_starts_at_the_header_above_the_wide_rows).
    """
    found = 0  # Number of rows to accept (from the end of `above`)
    below = run[0]
    for number, row in enumerate(reversed(above), start=1):
        gap = below[0].top - max(line.bottom for line in row)
        if len(row) < 2 or not _has_one_wide_cell_at_most(row, max_cell) or gap > row[0].height * 2.5:
            break
        is_header = len(row) >= _TABLE_MIN_COLUMNS and all(line.x1 - line.x0 <= max_cell for line in row)
        if is_header and all(len({_column_of(line, row) for line in lower}) == len(lower)
                             for lower in [*above[len(above) - number + 1:], *run]):
            found = number
        below = row
    return above[len(above) - found:] if found else []


def _join_row_fragments(page: _Page) -> None:
    """Join fragments that sit at the same height but were split apart by pdfminer into one line of the body flow (not a table).

    - Letter-spaced title ('제 안 서'): every character became a line, giving a run of one-character paragraphs. A row
      of same-size single Hangul/Hanja character fragments at even intervals is joined into one line without spaces.
    - 'label  value' two-column layout: pdfminer grouped the labels and the values separately, so the pairs were split
      like '연락처\n주소\n\n010-…\n서울…'. Joined into one 'label value' line only when rows of two fragments continue
      for 2 or more rows, the left fragment is short (by the table cell criterion), the starts of the two columns (for
      values the ends are accepted too) match row by row, the two columns are close (`_LABEL_GAP_RATIO`; a column of
      right-aligned numeric values may be far — `_right_aligned_numbers`), and the right column does not continue in
      the row right above or below the group.
      Even when the left column of a two-column layout has a few short lines, the right column continues above and
      below, so they are not joined. Three or more fragments use the borderless table criteria unchanged (Deliberate,
      conservative criterion). Pinned by: test_pdf.py::TestFlowJoins.
    - Groups of table-like rows that did not become a table (`_table_like_rows`): the fragments of each row are joined
      left to right into one line.
    - A page number or amount set apart from the end of the dot leader in a table of contents or price list
      (`_leader_rows`): appended to the leader line.
    - An equation and the equation number '(1)' set apart to its right (`_equation_number_rows`): appended to the equation.
    - A list number, bullet or footnote number set apart from the item text (`_marker_rows`): prepended to the first
      line of the item starting at the same height, and placed at the position of the item text.
    """
    if page.simple or len(page.lines) < 2:
        return
    rows = _visual_rows(page.lines)
    joined: list[list[_Line]] = [row for row in rows if _is_letter_spaced(row)]
    joined += _label_value_rows(rows, page.width)
    joined += _equation_number_rows(rows, {id(line) for row in joined for line in row})
    joined += _leader_rows(rows, {id(line) for row in joined for line in row})
    order = {id(line): index for index, line in enumerate(page.lines)}
    # Where to put each joined line (the position of its fragment that came first in the flow). A group of table-like
    # rows was read scattered column by column, so within a group the positions are handed out from the front in
    # top-to-bottom row order.
    places = [min(order[id(line)] for line in row) for row in joined]
    cells: set[int] = set()  # Fragments of table-like rows
    for run in _table_like_rows(rows, page.width, {id(line) for row in joined for line in row}):
        joined += run
        places += sorted(min(order[id(line)] for line in row) for row in run)
        cells.update(id(line) for row in run for line in row)
    marked = _marker_rows(page.lines, {id(line) for row in joined for line in row})
    joined += marked
    # The numbers come bunched together before the item text in the flow. Put them at the position of the item text
    places += [order[id(item)] for _, item in marked]
    markers = {id(marker) for marker, _ in marked}
    if not joined:
        return

    # Line id → the line to put in its place (None removes it)
    replaced: dict[int, _Line | None] = {id(line): None for row in joined for line in row}
    for row, place in zip(joined, places, strict=True):
        separator = "" if _is_letter_spaced(row) else " "
        # The size of a table-like row is the median of its fragments. Following the largest fragment (a big ∑ sign in
        # an equation) turned the row into a heading (public paper PDF).
        sizes = [line.size for line in row]
        size = statistics.median_low(sizes) if id(row[0]) in cells else row[-1].size if id(row[0]) in markers else max(sizes)
        replaced[id(page.lines[place])] = _Line(top=min(line.top for line in row), bottom=max(line.bottom for line in row),
                                                x0=row[0].x0, x1=max(line.x1 for line in row),
                                                text=_row_text(row, separator), size=size)
    page.lines = [replaced.get(id(line), line) for line in page.lines if replaced.get(id(line), line) is not None]


def _equation_number_rows(rows: list[list[_Line]], used: set[int]) -> list[list[_Line]]:
    """The rows of two fragments whose right fragment is an equation number ('(1)', '(2.3)', `_EQUATION_NUMBER`) lying
    more than a line height away from the left fragment (the equation) — excluding lines in `used`.

    A centred equation and the equation number at the right edge are lines beside each other, so an equation read
    after the paragraph below could not be moved up into place (a line with a line beside it is not moved,
    `_read_lone_lines_in_place`) and the numbers were grouped separately and bunched at the end of the page (Pinned
    by: test_pdf_reading_order.py::TestEquationNumbers). Joined into one line, the equation and its number go into
    place together.
    """
    return [row for row in rows if len(row) == 2 and not any(id(line) in used for line in row)
            and _EQUATION_NUMBER.match(row[1].text.strip()) and not _EQUATION_NUMBER.match(row[0].text.strip())
            and row[1].x0 - row[0].x1 > row[0].height]


def _row_text(row: list[_Line], separator: str) -> str:
    """Join the text of a row's fragments. A fragment starting below the middle of the previous one (a wrapped line of
    a value cell, `_with_wrapped_values`) is joined with a line break — line breaks inside a paragraph are not joined
    with a space (docs/decisions/pdf.md)."""
    text = row[0].text

    for previous, line in zip(row, row[1:], strict=False):
        text += ("\n" if line.top > previous.top + previous.height / 2 else separator) + line.text
    return text


def _leader_rows(rows: list[list[_Line]], used: set[int]) -> list[list[_Line]]:
    """Groups made of a line ending in a dot leader (`_LEADER_END`), the short value fragment (page number, amount)
    that ends the row right next to it (within font size × `_LEADER_GAP_RATIO`), and the fragments continuing to the
    left of the leader line within the same distance (item text set apart). Only the same row (`_visual_rows`) and the
    same font size are considered (groups containing a line in `used` are excluded). Without including the item text,
    a table of contents whose leaders are drawn separately was split into two lines, '제1장 서론' and '…… 3'
    (test_dot_leaders_drawn_apart_from_the_titles_stay_on_one_line).

    A one-digit page number in a table of contents is further from the end of the leader than twice the character
    width (pdfminer char_margin), so it was grouped separately, and in the text box flow the numbers alone bunched
    after the items as '…\n\n1\n2\n3', so one could not tell which item each page belonged to. The same happened with
    a right-aligned amount in a price list ('3,000원') (Pinned by:
    test_pdf_toc_and_checkboxes.py::TestDotLeaderPageNumbers). A value close to the leader is already joined by pdfminer.

    A single fragment that includes the page number after the leader ('서론 ……… 1', `_LEADER_TAIL`) is also treated as
    the end fragment, and joining extends to a short (at most the same width) same-size fragment set apart to the left
    within font size × `_LEADER_VALUE_MAX_RATIO` (a chapter/section number column, '제1장', '1.1') — in a table of
    contents whose number column is more than 3 times the font size away from the item text, the numbers alone bunched
    separately (Pinned by: test_pdf_toc_and_checkboxes.py::
    test_section_numbers_set_apart_from_the_titles_of_a_table_of_contents_stay_on_their_lines).
    """
    pairs = []

    for row in rows:
        if len(row) < 2:
            continue
        line, following = row[-2], row[-1]
        size = max(line.size, 1.0)
        if (_LEADER_END.search(line.text) and following.x1 - following.x0 <= size * _LEADER_VALUE_MAX_RATIO
                and abs(line.size - following.size) <= 0.5 and 0 <= following.x0 - line.x1 <= size * _LEADER_GAP_RATIO):
            start = len(row) - 2
        elif _LEADER_TAIL.search(following.text) and not _LEADER_END.search(line.text):
            start, size = len(row) - 1, max(following.size, 1.0)  # Item text, leader and page number in one fragment
        else:
            continue

        # In a table of contents with separately drawn leaders ('제1장 서론' + '……' + '3'), also join the item text
        # continuing closely to the left
        while start > 0 and 0 <= row[start].x0 - row[start - 1].x1 <= size * _LEADER_GAP_RATIO:
            start -= 1

        # A chapter/section number column set further apart ('제1장', '1.1') is also joined if short and the same size
        while (start > 0 and 0 <= row[start].x0 - row[start - 1].x1 <= size * _LEADER_VALUE_MAX_RATIO
               and row[start - 1].x1 - row[start - 1].x0 <= size * _LEADER_VALUE_MAX_RATIO
               and abs(row[start - 1].size - size) <= 0.5 and not _LEADER_TAIL.search(row[start - 1].text)):
            start -= 1

        if start < len(row) - 1 and not any(id(piece) in used for piece in row[start:]):
            pairs.append(row[start:])
    return pairs


def _marker_rows(lines: list[_Line], used: set[int]) -> list[list[_Line]]:
    """The [marker, first line] pairs of a list number or bullet set apart from its item text and the first line of
    that item (excluding lines in `used`).

    A PDF exported by LibreOffice from Word lists (List Number, List Bullet) places the numbers apart from the item
    text, so pdfminer grouped the numbers together, giving '1.\n\n2.\n\n3.\n\n<all the item text>'. The numbers in the
    footnote area behaved the same ('1\n2\n3\n\nK-IFRS …'). The line that starts close to the right of the marker
    (`_LIST_NUMBER`, `_LONE_BULLET`; within `_MARKER_GAP_RATIO` times the line height) at the same height (vertical
    centres differ by at most half the smaller line height) is the first line of that item. A bare number is accepted
    only as a footnote number: the previous or next number sits right above or below at the same left edge
    (`_numbered_in_turn`). If no line starts at the same height as the marker (figure tick labels, a number between
    lines) it is left alone (Pinned by: test_pdf.py::TestListMarkers).
    """
    def is_marker(line: _Line) -> bool:
        return bool(_LIST_NUMBER.match(line.text) or _LONE_BULLET.match(line.text) or _BARE_NUMBER.match(line.text))

    markers = [line for line in lines if id(line) not in used and is_marker(line)]
    texts = sorted((line for line in lines if id(line) not in used and not is_marker(line)) if markers else (),
                   key=lambda line: line.top)
    tops = [line.top for line in texts]
    found: list[list[_Line]] = []
    taken: set[int] = set()

    for marker in markers:
        middle = (marker.top + marker.bottom) / 2
        # Excludes lines more than 10 times the marker height
        near = texts[bisect_left(tops, middle - marker.height * 6):bisect_right(tops, middle + marker.height)]
        item = min((line for line in near if id(line) not in taken
                    and marker.x1 - 1 <= line.x0 <= marker.x1 + marker.height * _MARKER_GAP_RATIO
                    and abs((line.top + line.bottom) / 2 - middle) <= min(line.height, marker.height) / 2),
                   key=lambda line: line.x0, default=None)
        if item is None or (_BARE_NUMBER.match(marker.text) and not _numbered_in_turn(marker, lines)):
            continue
        found.append([marker, item])
        taken.add(id(item))
    return found


def _numbered_in_turn(marker: _Line, lines: list[_Line]) -> bool:
    """Whether a line right above or below the bare number `marker` at the same left edge (within 3 line heights) is,
    or starts with, the previous or next number."""
    number = int(marker.text)
    neighbors = {str(number - 1), str(number + 1)}
    return any(line is not marker and abs(line.x0 - marker.x0) <= _TABLE_ROW_TOLERANCE
               and abs(line.top - marker.top) <= marker.height * 3 and line.text.partition(" ")[0] in neighbors
               for line in lines)


def _table_like_rows(rows: list[list[_Line]], page_width: float, used: set[int]) -> list[list[list[_Line]]]:
    """Groups of rows that did not become a table but are laid out like one: rows of 3 or more fragments continue at
    row spacing for `_TABLE_MIN_ROWS` rows or more. With only two rows the cells must line up column by column
    (`_aligned_pair`).

    A table that missed the borderless table criteria because one cell is wide was grouped by pdfminer into one text
    box per column and scattered column by column like '번호 1 2 3 … 품명 … 수량 …'. It is not made into a table (the
    conservative criterion stays); each row is only joined into one line. Conditions (the same idea as 'label value'
    in `_join_row_fragments`):
    - The fragments of a row do not overlap and at most one is wide (over the maximum table cell width). Side-by-side
      body columns have two or more wide lines.
    - If the row right above or below the group has a wide line with the same left edge as a wide fragment of the
      group, the group is not joined. That is the body of the neighbouring column continuing above and below (an
      equation and equation number in the left column + a body line in the right column).
    - Below the cell rows, two-fragment 'label | value spanning several cells' rows aligned to the left edges of the
      first two cells of the first row may follow (`_spanning_row`; only when the group starts with a cell row, has
      `_TABLE_MIN_ROWS` rows or more in total, and the cell rows have no wide fragment). The '참석자 | (list of names
      spanning three cells)' row in the head of meeting minutes cut the group and the '일시·참석자·작성자' labels all
      fell to the end of the page (Pinned by: test_pdf_label_value_rows.py::TestLabelGridWithASpanningRow).
    Pinned by: test_pdf.py::TestBorderlessTableWithWideCells.
    """
    max_cell = page_width * _TABLE_CELL_MAX_RATIO

    def is_cells(row: list[_Line]) -> bool:
        return (len(row) >= _TABLE_MIN_COLUMNS and _has_one_wide_cell_at_most(row, max_cell)
                and not any(id(line) in used for line in row))

    def joins(run: list[list[_Line]], row: list[_Line]) -> bool:
        if run and not _near(run, row):
            return False
        if is_cells(row):
            return True
        # Label-value rows only below a grid whose cells are all short — with a row holding a wide fragment (a notice
        # box beside a list) the table-like shape is a coincidence
        return (bool(run) and not any(id(line) in used for line in row) and _spanning_row(row, run[0], max_cell)
                and all(line.x1 - line.x0 <= max_cell for other in run if is_cells(other) for line in other))

    found: list[list[list[_Line]]] = []
    index = 0
    while index < len(rows):
        run: list[list[_Line]] = []
        while index + len(run) < len(rows) and joins(run, rows[index + len(run)]):
            run.append(rows[index + len(run)])
        cells = [row for row in run if is_cells(row)]
        neighbors = [rows[position] for position in (index - 1, index + len(run)) if 0 <= position < len(rows)]
        beyond = {id(rows[position]): rows[position + step] for position, step in ((index - 1, -1), (index + len(run), 1))
                  if 0 <= position < len(rows) and 0 <= position + step < len(rows)}
        if (len(cells) >= _TABLE_MIN_ROWS or len(cells) < len(run) >= _TABLE_MIN_ROWS
                or (len(run) == 2 and _aligned_pair(run, neighbors, max_cell, beyond))):
            wide = [line.x0 for row in run for line in row if line.x1 - line.x0 > max_cell]
            continued = any(line.x1 - line.x0 > max_cell and abs(line.x0 - x0) <= _TABLE_ROW_TOLERANCE
                            for row in neighbors if _near(run, row) for line in row for x0 in wide)
            if not continued and not _is_beside_a_list(run, neighbors, max_cell):
                found.append(run)
        index += max(len(run), 1)
    return found


def _spanning_row(row: list[_Line], first: list[_Line], max_cell: float) -> bool:
    """Whether `row` is a two-fragment 'label | value' row whose left edges align with the first two cells of the
    first row (`first`) of a group of table-like rows (`_table_like_rows`). The label is short and at least two
    characters. The value may span into later cells or fit inside the second cell (a row whose later cells are empty)."""
    if len(row) != 2 or not _is_separate(row):
        return False
    label, value = row
    return (label.x1 - label.x0 <= max_cell and len(label.text.replace(" ", "")) >= 2
            and abs(label.x0 - first[0].x0) <= _TABLE_ROW_TOLERANCE and abs(value.x0 - first[1].x0) <= _TABLE_ROW_TOLERANCE)


def _aligned_pair(run: list[list[_Line]], neighbors: list[list[_Line]], max_cell: float,
                  beyond: dict[int, list[_Line]] | None = None) -> bool:
    """Whether two table-like rows (`run`) line up cell by cell: they have the same number of fragments, all short (at
    most the maximum table cell width), each fragment of the second row matches the fragment at the same position in
    the first row by left edge, right edge or centre, the rows are at least `_PAIR_ROW_GAP_RATIO` times the first
    row's line height apart, and the first cell is not a text column continuing above and below — that is, a row right
    above or below (`neighbors`) is a single fragment with the same left edge as the first cell that ends before the
    second cell, and such a row exists on both sides or continues in the same shape beyond that row (`beyond`,
    neighbouring row id → the row beyond it).
    A short one-line paragraph like that on one side only ('아래 문단.') does not block it — a two-row table with cell
    padding scattered column by column and that paragraph slipped in between the first and second columns (Pinned by:
    test_pdf_round45.py::TestTwoRowTableNextToAShortParagraph).

    A borderless 2-row table ('적용일 | 날짜 | 한도' in an official letter, the last two rows of a financial statement
    carried over to the next page) falls short of the minimum number of table-like rows and scattered column by column
    (Pinned by:
    test_pdf.py::TestBorderlessTableWithWideCells::test_two_aligned_rows_of_short_pieces_are_read_row_by_row).
    With only two rows it is hard to tell them from fragments that happen to sit at the same height, so they are
    joined into rows only when the cells line up column by column (short fragments placed out of line stay as they
    are, Pinned by: test_two_rows_of_short_pieces_out_of_line_are_left_as_read). Labels wrapped onto two lines in each
    of several side-by-side boxes are not joined even though the cells line up, because the rows are only a line
    spacing apart — joining split the two lines of each box (Pinned by:
    test_two_line_labels_of_boxes_side_by_side_are_left_as_read).
    Two lines of the left column of a two-column table of contents and the right-column items beside them also line
    up, but the left column continues above and below (Pinned by:
    test_two_rows_beside_a_column_continuing_above_them_are_left_as_read). If the row above or below has several
    fragments (another row of the table) or is a single fragment placed at the second cell or later (wrapped cell
    text), it does not block.
    Known accepted gap: a 2-row table whose rows touch without cell padding (the default padding of a Word table) is
    still read column by column. Not making it a table is the same as for 3 rows or more (conservative criterion).
    """
    def aligned(upper: _Line, lower: _Line) -> bool:
        return min(abs(upper.x0 - lower.x0), abs(upper.x1 - lower.x1),
                   abs(upper.x0 + upper.x1 - lower.x0 - lower.x1) / 2) <= _TABLE_ROW_TOLERANCE

    first, second = run

    def continues_first_cell(row: list[_Line], reference: list[list[_Line]]) -> bool:
        return (_near(reference, row) and len(row) == 1 and abs(row[0].x0 - first[0].x0) <= _TABLE_ROW_TOLERANCE
                and row[0].x1 < first[1].x0)

    gap = min(line.top for line in second) - max(line.bottom for line in first)
    column = [row for row in neighbors if continues_first_cell(row, run)]
    beyond = beyond or {}
    return (len(first) == len(second) and gap >= max(line.height for line in first) * _PAIR_ROW_GAP_RATIO
            and all(line.x1 - line.x0 <= max_cell for line in (*first, *second))
            and all(aligned(upper, lower) for upper, lower in zip(first, second, strict=True))
            and not (len(column) == 2 or any(id(row) in beyond and continues_first_cell(beyond[id(row)], [row])
                                             for row in column)))


def _is_beside_a_list(run: list[list[_Line]], neighbors: list[list[list[_Line]]], max_cell: float) -> bool:
    """Whether a group of table-like rows (`run`) is a box placed beside a list: the rows right above and below the
    group both continue, without a wide fragment, at the same left edges as the narrow fragments of the group (the
    list continues outside the group and only the wide fragments are at the group's height).

    A notice box placed beside a few rows of a '제N조 제목' (Article N, title) list became the third fragment of those
    rows and slipped in line by line between the list rows, like '제2조 정의 이 규정은 2026년…' (Pinned by:
    test_pdf.py::TestBorderlessTableWithWideCells::
    test_side_note_beside_part_of_a_two_column_list_is_not_joined_into_its_rows). Looking at only one of above/below
    would catch a table with a total row ('합계 … 금액') attached below, so both sides are checked. A group that is
    caught is left in pdfminer's flow as before (`_table_like_rows` did not exist).
    """
    narrow = [line.x0 for row in run for line in row if line.x1 - line.x0 <= max_cell]

    def continues_list(row: list[_Line]) -> bool:
        return (len(row) >= 2 and _near(run, row) and all(line.x1 - line.x0 <= max_cell for line in row)
                and all(any(abs(line.x0 - x0) <= _TABLE_ROW_TOLERANCE for x0 in narrow) for line in row))

    return len(neighbors) == 2 and all(continues_list(row) for row in neighbors)


def _column_of(line: _Line, header: list[_Line], row: list[_Line] | tuple[()] = ()) -> int:
    """The column a fragment falls into (`_header_column`). Given the fragments of the same row, `row`, a text fragment
    (one containing letters, with no header matching its left or right edge) that overlaps no header horizontally and
    lies in the gap between two headers is sent to the column of the right header when its centre is closer to it —
    only when no other fragment of the same row falls into that column.

    Deliberate: left-aligned cell text under a narrow header centred over a wide column ('항목', '담당자') has its
    centre left of the header's start, so it went one column too early ('손익 매출액 | |'), mashing the row or
    dropping the header row from the table (Pinned by: test_pdf_table_columns.py::
    TestCentredHeadersOverLeftAlignedCells). A number fragment is a right-aligned value overflowing its header, so it
    is not sent — it is sent only when an earlier fragment of the same row already occupies the left column
    (left-aligned number column: '4,5' in the '주석' (notes) column of a financial statement overlapped the account
    column and the table split in two, Pinned by:
    test_left_aligned_note_numbers_under_a_centred_header_stay_in_its_column). If another fragment of the same row is
    in the right column, this fragment is text spread across the previous cell (justified '운영      경비', Pinned by:
    test_pdf_forms_and_amounts.py::TestDistributedLabelsInABorderlessTable). Known accepted gap: decisions made without
    knowing the row (section rows, accepting rows above) keep the old rule.
    Whether the right column is occupied is judged by the column the other fragments move to under the same gap rule
    (`_gap_column`) — going by header positions alone, '확정' lying in the gap before the '비고' header was counted in
    the '내용' column, so the '내용' cell text stayed in the '구분' column and the header row dropped out of the table
    (Pinned by:
    test_pdf_table_columns.py::TestCentredHeadersOverLeftAlignedCells::test_cells_in_the_gaps_before_every_centred_header).
    """
    column = _header_column(line, header)
    later = _gap_column(line, header, row, column)
    if later == column or any(other is not line and _gap_column(other, header, row) == later for other in row):
        return column
    return later


def _gap_column(line: _Line, header: list[_Line], row: list[_Line] | tuple[()], column: int | None = None) -> int:
    """The column as in `_column_of` but without the check on whether the right column is occupied: if a text fragment
    in the gap between two headers goes to the right header because its centre is closer, that column; otherwise
    `_header_column`. A text fragment goes to the right column regardless of its centre when an earlier fragment of
    the same row occupies the left column — short cell text in a wide column ('관계부처 국장급 12명') has its centre
    closer to the left header and overlapped the first-column text (Pinned by:
    test_pdf_table_columns.py::TestCentredHeadersOverLeftAlignedCells::test_cells_in_the_gaps_before_every_centred_header)."""
    if column is None:
        column = _header_column(line, header)
    if not row or any(abs(cell.x0 - line.x0) <= _TABLE_ROW_TOLERANCE or abs(cell.x1 - line.x1) <= _TABLE_ROW_TOLERANCE
                      for cell in header):
        return column
    later = next((index for index, cell in enumerate(header) if cell.x0 >= line.x1), None)
    if not later or header[later - 1].x1 > line.x0 or later != column + 1:
        return column
    taken = any(other is not line and other.x1 <= line.x0 and _header_column(other, header) == column for other in row)
    words = any(letter.isalpha() for letter in line.text)
    if not words and not taken:
        return column  # A number fragment only when an earlier fragment of the row occupies the left column (left-aligned numbers)
    if words and taken:
        # The left column belongs to the earlier fragment of the same row. Short cell text in a wide column has its
        # centre closer to the left header
        return later

    center = (line.x0 + line.x1) / 2
    left_cell, right_cell = header[later - 1], header[later]
    if (right_cell.x0 + right_cell.x1) / 2 - center >= center - (left_cell.x0 + left_cell.x1) / 2:
        return column
    return later


def _header_column(line: _Line, header: list[_Line]) -> int:
    """The column a fragment falls into: the header whose left edge matches → the header whose right edge matches and
    with no other header overlapped → if exactly one header overlaps horizontally, that header (left-aligned cell text
    spanning only one centred header — Pinned by: test_pdf_table_columns.py::
    TestCentredHeadersOverLeftAlignedCells) → the interval its centre falls into (measured from header starts).

    In a right-aligned table (financial table), when a number is wider than its header its centre crosses into the
    interval of the column to the left, so the value went one column to the left or the header row dropped out of the
    table (Pinned by: test_pdf.py::TestRepeatedMarginsAndBorderlessTables::
    test_borderless_table_with_right_aligned_numbers_wider_than_headers). Where pdfminer merged two columns into one
    fragment ('0.627 ± 0.080 +0.084'), the right edge matches the later column but the fragment also lies over the
    header of the earlier column, so the column is not decided by the right edge (public paper PDF).
    """
    def closest(distance: Callable[[_Line], float]) -> int | None:
        index = min(range(len(header)), key=lambda index: abs(distance(header[index])))
        return index if abs(distance(header[index])) <= _TABLE_ROW_TOLERANCE else None

    left = closest(lambda cell: cell.x0 - line.x0)
    if left is not None:
        return left
    right = closest(lambda cell: cell.x1 - line.x1)
    overlapped = [index for index, cell in enumerate(header) if cell.x0 < line.x1 and line.x0 < cell.x1]
    if right is not None and overlapped in ([], [right]):
        return right
    if len(overlapped) == 1:
        return overlapped[0]

    center = (line.x0 + line.x1) / 2
    column = 0
    for index, cell in enumerate(header):
        if center >= cell.x0 - _TABLE_ROW_TOLERANCE:
            column = index
    return column
