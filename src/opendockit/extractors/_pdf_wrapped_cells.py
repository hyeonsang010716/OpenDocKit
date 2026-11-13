"""Cells of a table without rules that wrap over lines: wrapped cells between rules, a label wrapped beside its row,
centred cells spread over a band of rows, and lead lines that belong to the row below.
"""

from itertools import pairwise

from opendockit.extractors._pdf_model import (
    _TABLE_MIN_COLUMNS,
    _TABLE_ROW_TOLERANCE,
    _WRAP_GAP_MIN_RATIO,
    _WRAP_GAP_RATIO,
    _Line,
)
from opendockit.extractors._pdf_table_rows import _column_of, _has_number_cell, _wraps_into


# Difference between the two vertical centres (relative to the line height, `_with_centred_wrapped_cells`) for a cell to count as
# vertically centred beside a two-line cell. In a Chrome table it was 0.4pt, and a neighbouring text box with different line spacing
# (an invoice's '항목 값' (item, value)) was off by 4pt (Pinned by: test_pdf_label_value_rows.py::TestLeftBlockIsNotAWrappedCell).
_CENTRED_CELL_RATIO = 0.2
# Upper bound on the number of visual rows of a centred row whose cells wrapped to different numbers of lines
# (`_centred_band_row`). Keeps each row of a long table with narrow line gaps from being scanned to the end of the table (beyond
# this it is not such a row).
_CENTRED_BAND_ROWS = 16


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
