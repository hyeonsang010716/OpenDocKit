"""Judgments about one row of a table without rules: the column a piece stands in, whether a line continues a cell or is
a section row, a lone value, out of line with the columns, or separate from the rows above.
"""

from bisect import bisect_left, bisect_right
from collections import Counter
from collections.abc import Callable
import math
import re
import statistics
import threading

from opendockit.extractors._pdf_model import (
    _NUMBER_CELL,
    _SPACED_CHAR,
    _TABLE_MIN_COLUMNS,
    _TABLE_ROW_TOLERANCE,
    _WRAP_GAP_MIN_RATIO,
    _WRAP_GAP_RATIO,
    _Line,
)


# Character size ratio (larger / smaller, from after the first data row) up to which a borderless table's row continues the same
# table as the row before. A 12pt heading under a 10pt table (1.2) breaks it; a table whose subtotal row is slightly larger (11/10)
# continues.
_TABLE_SIZE_RATIO = 1.15


# Divider row without values in a borderless table (_is_section_row): the gaps above and below are at most this multiple of this
# table's gap between rows. A caption between tables is set further apart.
_SECTION_ROW_GAP_RATIO = 1.5
# Number of data rows directly above whose column alignment is compared when checking whether a borderless table's row belongs to
# another table (`_out_of_line`).
_OUT_OF_LINE_ROWS = 20


# Word of an item name spread by justification across one cell of a borderless table (`_spread_over_one_cell`).
_SPREAD_WORD = re.compile(r"^[가-힣一-鿿]{1,4}$")


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
    if _opens_a_new_table(previous, following, max_cell):
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
    if _opens_a_new_table(previous, following, max_cell):
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


def _has_number_cell(row: list[_Line], max_cell: float | None = None) -> bool:
    """Whether the row has a cell holding only a numeric value. Given the maximum table cell width (`max_cell`), the row is
    looked at as the table searched again with merged cells split sees it (`_cell_texts`)."""
    return any(_NUMBER_CELL.match(text.strip()) for text in _cell_texts(row, max_cell))


def _cell_texts(row: list[_Line], max_cell: float | None) -> list[str]:
    """The cell texts of a row: with `max_cell`, a line that merged neighbouring cells (`_splittable`) gives one text per
    piece, as in the table searched again with those lines split (`_found_again`); without it, one text per line."""
    return [text for line in row
            for text in ([piece for _, _, piece in line.fine_parts or line.parts]
                         if max_cell is not None and _splittable(line, max_cell) else [line.text])]


def _opens_a_new_table(previous: list[_Line], following: list[_Line], max_cell: float) -> bool:
    """Whether the row below a single-fragment line (`following`) is the header of a new table, so the line between is not a
    row of this table (`_is_section_row`, `_is_lone_value_row`): after a row with a numeric value cell (`previous`) it has
    none, and it has enough cells to head a table at all (`_TABLE_MIN_COLUMNS`, the same as a header in `_table_runs`).

    Both rows are judged as the table searched again with merged cells split sees them (`_cell_texts`). Judged on the
    merged lines, '29,985 배' held no number, the label-only row '합계' was kept, and the table searched again with the
    cells split ended at that row, so the search failed and every merged cell stayed unsplit ('| 사과 | | 79,619 사과 |').
    And a row of two cells cannot head a table, yet it ended the table above, and the rows below, too few for a table of
    their own, scattered column by column (Pinned by: test_pdf_borderless_split_rows.py::TestLabelOnlyRowAmongMergedCells)."""
    cells = _cell_texts(following, max_cell)
    return (len(cells) >= _TABLE_MIN_COLUMNS and _has_number_cell(previous, max_cell)
            and not any(_NUMBER_CELL.match(text.strip()) for text in cells))


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
    index = _header_index(header, line)
    if index is not None:
        if not row or min(index.nearest(0, line.x0)[1], index.nearest(1, line.x1)[1]) <= _TABLE_ROW_TOLERANCE:
            return column
        later = index.first_starting_at(line.x1)
    else:
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
    index = _header_index(header, line)
    if index is not None:
        return index.column(line)

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


# Headers with more cells than this get a sorted index for the column lookups (`_header_column`, `_gap_column`); narrower
# headers keep the scans, which are as fast there.
_INDEXED_HEADER_CELLS = 16
_INDEX_CACHE = threading.local()  # per thread: id of a header list → (that list, its length and end cells, its `_HeaderIndex`)
_INDEX_CACHE_SIZE = 32


def _header_index(header: list[_Line], line: _Line) -> "_HeaderIndex | None":
    """The sorted index of a wide header (more than `_INDEXED_HEADER_CELLS` cells) for looking up the column of `line`. None
    for a narrow header, or when a coordinate of the header or of `line` is not finite (the scans decide those cases, so
    the result is the same in every case).

    Each lookup scanned every header cell, so on a page whose faint rows of 1,000 one-point letters every 4pt formed a
    borderless 'table' of 1,000 columns the lookups took time with cells × columns (77 seconds for a 12,000-row page)
    (Pinned by: test_pdf_table_columns.py::TestVeryWideBorderlessRows). The index is kept per header list while that list
    keeps its length and end cells (rows are not changed once grouped), at most `_INDEX_CACHE_SIZE` of them, and dropped
    when the page's tables are read (`_drop_header_indexes`)."""
    if len(header) <= _INDEXED_HEADER_CELLS or not (math.isfinite(line.x0) and math.isfinite(line.x1)):
        return None
    cache = getattr(_INDEX_CACHE, "entries", None)
    if cache is None:
        cache = _INDEX_CACHE.entries = {}
    cells = (len(header), id(header[0]), id(header[-1]))  # rows are not changed once grouped; this catches a list grown
    known = cache.get(id(header))
    if known is not None and known[0] is header and known[1] == cells:
        return known[2]
    index = _HeaderIndex.of(header)
    if len(cache) >= _INDEX_CACHE_SIZE:
        cache.clear()
    cache[id(header)] = (header, cells, index)
    return index


def _drop_header_indexes() -> None:
    """Forget the header indexes of this thread (`_header_index`), so they do not hold the lines of a page read."""
    _INDEX_CACHE.entries = {}


class _HeaderIndex:
    """The cells of a header sorted by left and by right edge, for `_header_column` and `_gap_column` with the same results
    as their scans: the first header (in header order) at the smallest distance, the last header whose start (less
    `_TABLE_ROW_TOLERANCE`) is at or before a point, the first header starting at or after a point, and the headers
    overlapping a range."""

    @classmethod
    def of(cls, header: list[_Line]) -> "_HeaderIndex | None":
        if not all(math.isfinite(cell.x0) and math.isfinite(cell.x1) for cell in header):
            return None
        return cls(header)

    def __init__(self, header: list[_Line]) -> None:
        self.size = len(header)
        self.edges = ([cell.x0 for cell in header], [cell.x1 for cell in header])
        self.orders = tuple(sorted(range(self.size), key=lambda position: (edges[position], position)) for edges in self.edges)
        self.sorted_edges = tuple([edges[position] for position in order] for edges, order in zip(self.edges, self.orders, strict=True))
        by_start = self.orders[0]
        # the first header (in header order) among those from each place of the start order on
        self.first_from = [0] * (self.size + 1)
        self.first_from[self.size] = self.size
        for place in range(self.size - 1, -1, -1):
            self.first_from[place] = min(by_start[place], self.first_from[place + 1])
        # the widest right edge among the headers up to each place of the start order
        self.widest_to = []
        for place in range(self.size):
            right = self.edges[1][by_start[place]]
            self.widest_to.append(right if not place or right > self.widest_to[-1] else self.widest_to[-1])
        # starts less the tolerance, sorted, with the last header (in header order) among those up to each place
        shifted = sorted((cell.x0 - _TABLE_ROW_TOLERANCE, position) for position, cell in enumerate(header))
        self.shifted = [value for value, _ in shifted]
        self.last_to = []
        for _, position in shifted:
            self.last_to.append(position if not self.last_to or position > self.last_to[-1] else self.last_to[-1])

    def nearest(self, side: int, x: float) -> tuple[int, float]:
        """(the first header in header order whose left (`side` 0) or right (1) edge is nearest to `x`, its distance) —
        `min(range(len(header)), key=…)` with distances `abs(edge - x)`. The distance does not grow towards `x` from
        either side, so the nearest headers are the ones next to where `x` sorts in, and those at the same distance
        beside them."""
        values, order = self.sorted_edges[side], self.orders[side]
        place = bisect_left(values, x)
        best = min(abs(values[at] - x) for at in (place - 1, place) if 0 <= at < self.size)
        found = self.size
        at = place - 1
        while at >= 0 and abs(values[at] - x) == best:
            found, at = min(found, order[at]), at - 1
        at = place
        while at < self.size and abs(values[at] - x) == best:
            found, at = min(found, order[at]), at + 1
        return found, best

    def first_starting_at(self, x: float) -> int | None:
        """The first header in header order whose left edge is at or after `x` (None if there is none)."""
        found = self.first_from[bisect_left(self.sorted_edges[0], x)]
        return None if found == self.size else found

    def overlapping(self, x0: float, x1: float) -> list[int]:
        """Up to two of the headers that overlap the range (x0, x1) horizontally (two means two or more), in header order."""
        starts, by_start = self.sorted_edges[0], self.orders[0]
        found = []
        at = bisect_left(starts, x1) - 1  # the headers starting before x1
        while at >= 0 and self.widest_to[at] > x0 and len(found) < 2:
            if self.edges[1][by_start[at]] > x0:
                found.append(by_start[at])
            at -= 1
        return sorted(found)

    def column(self, line: _Line) -> int:
        """`_header_column` of `line`."""
        left, distance = self.nearest(0, line.x0)
        if distance <= _TABLE_ROW_TOLERANCE:
            return left
        right, distance = self.nearest(1, line.x1)
        right = right if distance <= _TABLE_ROW_TOLERANCE else None
        overlapped = self.overlapping(line.x0, line.x1)
        if right is not None and overlapped in ([], [right]):
            return right
        if len(overlapped) == 1:
            return overlapped[0]
        count = bisect_right(self.shifted, (line.x0 + line.x1) / 2)
        return self.last_to[count - 1] if count else 0
