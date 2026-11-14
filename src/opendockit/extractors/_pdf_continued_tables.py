"""Tables that go on: header lines of a ruled table joined, the header carried to the part of a table on the next page,
cells left open at a page break, and columns regridded so both parts stand in the same columns.
"""

import math
import re
import statistics

from opendockit.extractors._pdf_model import (
    _HEADER_CELL_MAX_CHARS,
    _NUMBER_CELL,
    _SHADE_MIN_SIDE,
    _TABLE_CELL_MAX_RATIO,
    _TABLE_MIN_COLUMNS,
    _TABLE_MIN_ROWS,
    _TABLE_ROW_TOLERANCE,
    _in_margin,
    _Line,
    _Page,
    _Table,
    _visual_rows,
)
from opendockit.extractors._pdf_paragraphs import _table_text


# Year and date cells common in header rows ('2024', '2024년', 'FY2024', '2024.12.31', '2024-12'). When deciding on the header row to
# repeat they are not treated as numeric value cells (`_has_value_text`).
_YEAR_OR_DATE = re.compile(r"^(?:FY\s?)?(?:19|20)\d{2}(?:년도?|[.\-/]\s?\d{1,2}(?:[.\-/]\s?\d{1,2})?\.?)?$")


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
            _carry_open_cells(last, first, _starts_with_own_header(last, first))
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
        if _starts_with_own_header(last, first, grid):
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


def _starts_with_own_header(last: _Table, first: _Table, grid: list[list[str]] | None = None) -> bool:
    """Whether the table at the top of the page (`first`, cell text `grid`, its own rows when not given) starts with a
    header of its own rather than continuing `last`: its first row has no numeric value cell and the second row does, the
    first row is not the header of `last` repeated, and `last` does not end in a row split at the end of the page (every
    column open below — then the first row is the rest of that row).

    Such a table does not get the previous header, nor, in a column whose top cell has a top border, the text of a merged
    cell left open at the end of the previous page (`_carry_open_cells`)."""
    rows = first.rows if grid is None else grid
    split = len(last.open_below) == len(last.spans)
    return (len(rows) > 1 and not split and not _has_value_text(rows[0]) and _has_number_text(rows[1])
            and (grid is not None or rows[0] != last.rows[0]))


def _carry_open_cells(last: _Table, first: _Table, own_header: bool = False) -> None:
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

    A column is skipped when the table starts with a header of its own (`own_header`, `_starts_with_own_header`) and its
    top cell in that column has a top border: the merged cell does not go on there — '그룹2 담당: 팀2' went into the blank
    corner cell of a different table's header (Pinned by: test_pdf_table_columns.py::TestMergedCellOpenAtThePageEnd::
    test_new_table_with_its_own_header_does_not_get_the_open_cell). The header test alone also stopped real continuations,
    whose rest of the cell has no top border, when the first row held a date or a text value ('2024.07.15', '미정') (Pinned
    by: test_rows_continued_without_a_header_get_the_open_cell_whatever_their_values in the same class).
    """
    if len(last.open_below) == len(last.spans):
        return
    start = 1 if first.rows and last.rows and first.rows[0] == last.rows[0] else 0
    for column in last.open_below:
        value = last.rows[-1][column] if column < len(last.rows[-1]) else ""
        if not value or (own_header and _closed_at_the_top(first, column)):
            continue
        for row in first.rows[start:]:
            if column >= len(row) or row[column]:
                break
            row[column] = value


def _closed_at_the_top(table: _Table, column: int) -> bool:
    """Whether a ruled table has a cell starting at its top edge over the middle of `column` (the top cell of that column
    has a top border)."""
    if column >= len(table.spans):
        return False
    middle = sum(table.spans[column]) / 2
    return any(abs(cell[1] - table.top) <= 1.5 and cell[0] < middle < cell[2] for cell in table.cells)


def _looks_like_header(row: list[str], rows: list[list[str]]) -> bool:
    """Whether the row looks like a header row: two or more cells have text, all of them at most `_HEADER_CELL_MAX_CHARS`
    characters (line breaks excluded — `<br>` and a ruled cell's `\n`; Pinned by: test_pdf_table_columns.py::
    TestTableContinuedOnTheNextPage::test_header_cell_on_two_lines_is_measured_without_its_line_break), no cell holds only a
    numeric value, and no cell text appears again in the same column
    of the other rows (`rows`, the previous table's data rows and the first row of the table to be continued).

    The last condition filters out the same frame on every page (a slide layout caught as a ruled table) — the frame's first
    cell (the section title) continues vertically and appears with the same text in the data rows and in the next page's
    frame. The header cells of a data table do not appear again in its data rows. A header repeated on every page is also
    filtered out here."""
    filled = [cell.strip() for cell in row if cell and cell.strip()]
    return (len(filled) >= 2 and not _has_value_text(row)
            and all(len(cell.replace("<br>", "").replace("\n", "")) <= _HEADER_CELL_MAX_CHARS for cell in filled)
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
