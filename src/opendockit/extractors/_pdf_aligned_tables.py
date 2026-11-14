"""Tables without ruling lines, found from lines whose cells stand in columns: runs of table-like rows, their columns,
wrapped cells and labels, rows split over lines, and headers carried over a page break.
"""

from bisect import bisect_left, bisect_right
from collections import Counter
from itertools import pairwise
import math
import re
import statistics

from opendockit.extractors._pdf_continued_tables import _YEAR_OR_DATE
from opendockit.extractors._pdf_model import (
    _COLUMN_LINES_MIN,
    _FAR_VALUE,
    _NUMBER_CELL,
    _ROW_OVERLAP_RATIO,
    _SPACED_CHAR,
    _TABLE_CELL_MAX_RATIO,
    _TABLE_MIN_COLUMNS,
    _TABLE_MIN_ROWS,
    _TABLE_ROW_TOLERANCE,
    _Line,
    _Page,
    _Table,
    _visual_rows,
)
from opendockit.extractors._pdf_reading_order import _beside_each_other
from opendockit.extractors._pdf_table_rows import (
    _blank_corner_header,
    _column_of,
    _continues_cell,
    _drop_header_indexes,
    _is_lone_value_row,
    _is_section_row,
    _is_separate,
    _out_of_line,
    _sizes_match,
    _spread_over_one_cell,
    _table_rows_above,
    _wide_cell_in_its_column,
)
from opendockit.extractors._pdf_wrapped_cells import (
    _aligned_in_another_row,
    _centred_band_row,
    _centred_wrapped_row,
    _in_the_band_of,
    _row_with_lead_lines,
    _with_centred_wrapped_cells,
    _with_ruled_wrapped_cells,
)
from opendockit.markdown import render_table


# Upper bound on the gap (relative to the line height, `_wrapped_header_line`) between the first line and a wrapped second line in a
# header cell of a borderless table.
_HEADER_WRAP_GAP_RATIO = 0.6


def _extract_aligned_tables(page: _Page) -> None:
    """Find tables that have no rules and only aligned cells, turn them into Markdown tables, and take their lines out of the
    body flow."""
    try:
        _extract_tables(page)
    finally:
        _drop_header_indexes()


def _extract_tables(page: _Page) -> None:
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
    # The pieces at the height of a top are listed once per distinct top, and the pieces before a piece are looked through
    # only when one of them starts higher — listing both again for every piece of a row of 1,000 pieces took time with the
    # square of the row (Pinned by: test_pdf_table_columns.py::TestVeryWideBorderlessRows)
    levels: dict[float, list[_Line]] = {}
    ordinary = all(math.isfinite(line.top) for line in row)
    highest = math.inf  # the highest top among the pieces before this one

    for index, line in enumerate(row):
        level = levels.get(line.top) if ordinary else None
        if level is None:
            level = [other for other in row if abs(other.top - line.top) <= _TABLE_ROW_TOLERANCE]
            if ordinary:
                levels[line.top] = level
        column = _column_of(line, header, level)
        above = ([(other, columns[number]) for number, other in enumerate(row[:index])
                  if other.top < line.top - _TABLE_ROW_TOLERANCE and other.x0 < line.x1 and line.x0 < other.x1]
                 if not ordinary or highest < line.top - _TABLE_ROW_TOLERANCE else [])
        highest = min(highest, line.top)
        if above and column not in {other_column for _, other_column in above}:
            column = min(above, key=lambda pair: abs(pair[0].x0 - line.x0))[1]
        columns.append(column)

    return columns


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
    # The original lines of a cell joined from several lines are used lines as well.
    taken = {id(original) for run in runs for row in run for line in row for original in (line, line.source, *line.joined)
             if original is not None}
    return _split_runs(runs, rows, taken, max_cell, page.rules)


def _split_runs(runs: list[list[list[_Line]]], rows: list[list[_Line]], taken: set[int], max_cell: float,
                rules: list[tuple[float, float, float]]) -> list[list[list[_Line]]]:
    """`_split_merged_cells` for every borderless table of the page (`runs`, in the same order), top to bottom. The rows a
    table took above or below it count as used for the tables after it, and a table that the rows below a table lead into
    is searched for together with that table (`_split_merged_cells`); it is then left out. Rows between two tables that
    line up with the columns of the lower one are left to it.

    Each table used to grow on its own from the same used lines, so a row that pdfminer merged differently between two
    tables was taken by the table above (downwards) and by the table below (upwards): it came out in both tables, and the
    one table stayed split in two (Pinned by: test_pdf_borderless_split_rows.py::TestRowMergedDifferentlyBetweenTwoTables).
    """
    starts: dict[int, int] = {}  # position in `rows` of the first row of a table → the number of that table
    for number, run in enumerate(runs):
        first = _row_positions(run[0], rows)
        if first:
            starts.setdefault(min(first), number)
    result: list[list[list[_Line]] | None] = list(runs)
    for number in sorted(range(len(runs)), key=lambda number: min(line.top for line in runs[number][0])):
        if result[number] is None:
            continue  # searched for together with a table above it
        found, joined = _split_merged_cells(runs[number], rows, taken, max_cell, rules,
                                            {position: runs[other] for position, other in starts.items()
                                             if other != number and result[other] is not None})
        result[number] = found
        if joined is not None:
            result[next(other for other, run in enumerate(runs) if run is joined)] = None
        taken.update(id(original) for row in found for line in row for original in (line, line.source, *line.joined)
                     if original is not None)
    return [run for run in result if run is not None]


def _row_positions(table_row: list[_Line], rows: list[list[_Line]]) -> list[int]:
    """The positions in `rows` that hold the original lines of a table row (for a cell joined from several lines, those
    lines)."""
    members = {id(original) for line in table_row for original in (line, *line.joined)}
    return [index for index, row in enumerate(rows) if any(id(line) in members for line in row)]


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
                        max_cell: float, rules: list[tuple[float, float, float]],
                        starts: dict[int, list[list[_Line]]] | None = None, *,
                        settle: bool = True) -> tuple[list[list[_Line]], list[list[_Line]] | None]:
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

    Returns (table, the other table it was joined with or None). When the rows below lead into the first row of another
    found table (`starts`: position in `rows` of a table's first row → that table) that starts with a data row (a numeric
    value cell) and every gap on the way is within the row pitch of the two tables, the region down to the end of that
    table, and on below it as far as that table would have grown by itself, is searched for first, and it becomes one
    table only when exactly one table is found that holds every line of both (the same conditions as above). Otherwise
    this table is searched for alone. Rows below that line up with the columns of that other table rather than with this
    table's are left to it (see below); with `settle` off they are left without first checking that it takes them.
    """
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

    def joins(row: list[_Line], left: float = left, right: float = right) -> bool:
        return not any(id(line) in taken or line.x0 < left or line.x0 > right
                       or (line.x1 > right and line.x1 - line.x0 > max_cell) for line in row)

    def ruled(upper: list[_Line], row: list[_Line], left: float = left, right: float = right) -> bool:
        floor, ceiling = max(line.bottom for line in upper), min(line.top for line in row)
        return any(floor - 1 <= y <= ceiling + 1 and x0 < right and x1 > left for y, x0, x1 in rules)

    def spans(table_row: list[_Line]) -> list[int]:
        return _row_positions(table_row, rows)

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
    following: list[list[_Line]] | None = None  # another table the rows below lead into
    end = max(spans(run[-1]), default=None)
    if end is not None:
        upper = run[-1]
        for position in range(end + 1, len(rows)):
            row = rows[position]
            if _row_gap(upper, row) > upper[0].height * 2.5 or ruled(upper, row):
                break
            if not joins(row):
                if after and starts and position in starts:
                    following = starts[position]
                break
            after.append(row)
            upper = row

    # The rows below from the first one that lines up with the columns of the table below rather than with this table's
    # (`_aligned_pieces`) are left to that table when it takes them growing upwards — it is searched for here as it will
    # be when its turn comes (`settle` off: without asking the table below it in turn) — and the two are not joined.
    # This table, searched for first, took the header of the table below that pdfminer grouped as one piece ('구분 금액
    # 수량 비율') and the rows under it as rows of its own, and the table below came out without its column names. When
    # the table below does not take them they stay rows of this table, and when the first of them holds a measured
    # value (a data row, the round-44 shape) joining the two tables is tried first, as before
    # (Pinned by: test_pdf_borderless_split_rows.py::TestRowsBetweenTwoTablesThatBelongToTheTableBelow)
    kept = len(after)
    if following is not None:
        own, other = _column_edges(run), _column_edges(following)
        kept = next((number for number, row in enumerate(after) if _aligned_pieces(row, other) > _aligned_pieces(row, own)),
                    kept)
    data_row = kept < len(after) and any(_measured_value(text) for line in after[kept] for _, _, text in _cell_pieces(line))

    def without_the_rows_of_the_table_below() -> list[list[_Line]] | None:
        # The table may end at one of the rows below only when that row is a whole row. A row of fewer pieces is the second
        # line of a wrapped cell: in the middle of the table it is joined to its row, as the last row of the table it fell
        # out as a paragraph between the two tables (Pinned by: test_pdf_borderless_split_rows.py::
        # TestRowsBetweenTwoTablesThatBelongToTheTableBelow::test_second_line_of_a_wrapped_cell_stays_in_its_row).
        if kept and 2 * sum(1 for line in after[kept - 1] for _ in _cell_pieces(line)) < len(run[0]):
            return None
        found = _found_again([*above, *run, *after[:kept]], {id(line) for row in run for line in row}, len(run[0]),
                             max_cell)
        if found is None:
            # Without the rows left to the table below, the rows this table keeps hold no line to split, and falling back
            # on the rows first found would drop the others. The table then takes all the rows below it, as it did before.
            if above or kept:
                return None
            found = run
        if settle and following is not None:
            grown, _ = _split_merged_cells(following, rows, taken | _original_lines(found), max_cell, rules,
                                           {position: table for position, table in (starts or {}).items()
                                            if table is not following}, settle=False)
            held = _original_lines(grown)
            if not all(id(line) in held for row in after[kept:] for line in row):
                return None
        return found

    if kept < len(after) and not data_row:
        found = without_the_rows_of_the_table_below()
        if found is not None:
            return found, None

    if following is not None:
        last = max(_row_positions(following[-1], rows), default=-1)
        lower = rows[end + 1 + len(after):last + 1]
        members = {id(original) for row in following for line in row for original in (line, line.source, *line.joined)
                   if original is not None}
        # Only the round-44 shape is joined: the other table starts with a row holding a clearly measured value
        # (`_measured_value`: a number with a thousands separator or a decimal point — not a bare integer, bare
        # percentage, year or date, which are just as common in a header of its own: '구분 | 2023 | 2024',
        # '구분 | 10% | 20%') and the rows between continue at the usual (median) row gap of the two tables. When in doubt
        # the tables stay apart: that outcome has every row once, in two tables. A totals row followed by a product table
        # with its own header ('품목 | 단가 | …') was joined into the table above, and so were tables under a year or
        # percentage header; with the widest row gap as the bound, one wide gap inside a table let an equally wide gap
        # between two tables through (Pinned by: test_pdf_borderless_split_rows.py::TestSeparateTableBelowARowInNoTable)
        pitch = [_row_gap(upper, lower_row) for table in (run, following) for upper, lower_row in pairwise(table)]
        chain = [_row_gap(upper, lower_row) for upper, lower_row in pairwise([run[-1], *after, following[0]])]
        if (lower and all(id(line) in members for row in lower for line in row)
                and any(_measured_value(line.text) for line in following[0])
                and pitch and max(chain) <= statistics.median(pitch) + 1.0):
            # The joined table grows downwards past the end of the other table as that table would have on its own
            wide_left = min(left, *(line.x0 - _TABLE_ROW_TOLERANCE for row in following for line in row))
            wide_right = max(right, *(line.x1 + _TABLE_ROW_TOLERANCE for row in following for line in row))
            tail: list[list[_Line]] = []
            upper = lower[-1]
            for row in rows[last + 1:]:
                if (_row_gap(upper, row) > upper[0].height * 2.5 or ruled(upper, row, wide_left, wide_right)
                        or not joins(row, wide_left, wide_right)):
                    break
                tail.append(row)
                upper = row
            originals = {id(line) for row in [*run, *lower] for line in row}
            found = _found_again([*above, *run, *after, *lower, *tail], originals, max(len(run[0]), len(following[0])),
                                 max_cell)
            if found is not None:
                return found, following

    if data_row:
        found = without_the_rows_of_the_table_below()
        if found is not None:
            return found, None

    found = _found_again([*above, *run, *after], {id(line) for row in run for line in row}, len(run[0]), max_cell)
    return (run if found is None else found), None


# How far apart (pt) the ends of two cell pieces may be to count as the same column edge (`_aligned_pieces`)
_COLUMN_EDGE_TOLERANCE = 1.0

# A number with a thousands separator or a decimal point ('5,483', '4.3%', '1.2') — a measured value, not a label (`_measured_value`)
_MEASURED = re.compile(r"\d,\d{3}|\d\.\d")


def _measured_value(text: str) -> bool:
    """Whether a piece of text is a clearly measured value: a numeric value cell (`_NUMBER_CELL`) that is not a year or date
    (`_YEAR_OR_DATE`) and has a thousands separator or a decimal point (`_MEASURED`). A bare integer ('1', '30') or a bare
    percentage ('10%') is not, since a header of its own holds those as often as a data row."""
    text = text.strip()
    return bool(_NUMBER_CELL.match(text)) and not _YEAR_OR_DATE.match(text) and bool(_MEASURED.search(text))


def _original_lines(table: list[list[_Line]]) -> set[int]:
    """The ids of the lines a borderless table holds: its pieces, the lines they were split from (`source`) and the lines
    of its joined cells (`joined`)."""
    return {id(original) for row in table for line in row for original in (line, line.source, *line.joined)
            if original is not None}


def _cell_pieces(line: _Line) -> list[tuple[float, float, str]]:
    """The pieces of a line as (left, right, text): the cells pdfminer merged into it (`fine_parts`, `parts`), or the line."""
    return list(line.fine_parts or line.parts) or [(line.x0, line.x1, line.text)]


def _column_edges(table: list[list[_Line]]) -> tuple[list[float], list[float]]:
    """The left ends and the right ends of the cell pieces of a borderless table, each in ascending order
    (`_aligned_pieces`)."""
    pieces = [piece for row in table for line in row for piece in _cell_pieces(line)]
    return sorted(x0 for x0, _, _ in pieces), sorted(x1 for _, x1, _ in pieces)


def _aligned_pieces(row: list[_Line], edges: tuple[list[float], list[float]]) -> int:
    """How many cell pieces of a row start where two or more pieces of a table start, or end where two or more end (within
    `_COLUMN_EDGE_TOLERANCE`; `edges` from `_column_edges`) — the pieces that sit in a column of that table."""
    lefts, rights = edges

    def shared(values: list[float], x: float) -> bool:
        return bisect_right(values, x + _COLUMN_EDGE_TOLERANCE) - bisect_left(values, x - _COLUMN_EDGE_TOLERANCE) >= 2

    return sum(shared(lefts, x0) or shared(rights, x1) for line in row for x0, x1, _ in _cell_pieces(line))


def _row_gap(upper: list[_Line], lower: list[_Line]) -> float:
    """The gap between the bottom of a row and the top of the row below it."""
    return min(line.top for line in lower) - max(line.bottom for line in upper)


def _found_again(region_rows: list[list[_Line]], in_run: set[int], columns: int,
                 max_cell: float) -> list[list[_Line]] | None:
    """The single borderless table found again from the rows `region_rows` with the merged pieces split
    (`_split_merged_cells`): it has at least `columns` columns, holds every piece of the lines in `in_run`, and every column
    holding a split piece has cells on two or more rows. None otherwise."""
    def splittable(line: _Line) -> bool:
        return _splittable(line, max_cell)

    region = [line for row in region_rows for line in row]
    if not any(splittable(line) for line in region):
        return None

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
    if len(found) != 1 or len(found[0][0]) < columns:
        return None
    # Original lines that the re-found table holds inside a cell joined from several lines (`_row_with_lead_lines` etc.) also
    # count as held. Only after the right-aligned header '3월 비고' was split did the lead line of the first row's remark get
    # joined to '예정', so '예정' was counted as missing and the header and first row stayed outside the table (pinned by:
    # test_pdf_round47.py::TestWrappedNoteUnderARightAlignedMergedHeader)
    kept = {id(original) for row in found[0] for line in row for original in (line, *line.joined)}
    if not all(id(piece) in kept for piece in pieces if id(piece.source or piece) in in_run):
        return None

    # A column containing a split piece must have cells on other rows too. A column produced by splitting justified text
    # inside a cell exists only on that row
    header = found[0][0]
    used = Counter(column for row in found[0] for column in {_column_of(line, header, row) for line in row})
    if any(used[_column_of(line, header, row)] < 2 for row in found[0] for line in row if line.source is not None):
        return None
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
