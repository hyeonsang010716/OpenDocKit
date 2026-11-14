"""Lines that are a label and its value (forms, specification sheets): label and value columns, wrapped values,
right-aligned numbers, and the visual rows of a line.
"""

import math
import re

from opendockit.extractors._pdf_model import _BARE_NUMBER, _FAR_VALUE, _HEADER_CELL_MAX_CHARS, _TABLE_CELL_MAX_RATIO, _TABLE_ROW_TOLERANCE, _Line


# Upper bound on the gap (ratio of the page width) between the label column (the end of the longest label) and the value column
# that are joined as a two-column '항목  값' (item, value) layout. Two columns further apart are two side-by-side text columns
# (Pinned by: test_quality.py::test_pdf_side_by_side_form_xobjects_are_not_interleaved).
_LABEL_GAP_RATIO = 0.2
# Width of a value piece (relative to the maximum table cell width; half the page width). A wider value is accepted only when such
# rows are fewer in the group than the rows with narrower values (`_label_value_rows`).
_VALUE_MAX_RATIO = 2.0
# Upper bound on the gap (relative to the line height) between a wrapped line in the value column (`_wraps_value`) and the value
# line above it. This is the line spacing inside a cell.
_VALUE_WRAP_GAP_RATIO = 1.0
# Lower bound on how much smaller a wrapped value line may be than the value line above (relative to the size of the line above).
# The second line of a schedule (speaker, affiliation) is written smaller at 9/11 (`_wraps_value`). A larger line is not accepted —
# it is the next section's heading.
_VALUE_WRAP_MIN_SIZE_RATIO = 0.75
_WRAP_LABEL_RATIO = 0.6  # upper bound on the label width of a group that accepts wrapped value lines (vs. max table cell width; 15% of page width)
_LABEL_MAX_WORDS = 2  # word count of a label that accepts wrapped value lines even beyond the width bound ('총 연구개발기간')


# Upper bound on the item indentation (relative to the character size, `_pairs_with`) accepted into the same group in a table of
# contents without dot leaders. Up to three levels (20pt each).
_TOC_INDENT_RATIO = 4.0
_LINE_NUMBER_LABEL = re.compile(r"^\s*\d+\s*$")  # label taken as a line number (`_sectioned_values`)


def _label_value_rows(rows: list[list[_Line]], page_width: float) -> list[list[_Line]]:
    """The rows to treat as a 'label  value' two-column layout (conditions in `_join_row_fragments`). Wrapped value
    lines are returned attached to the row above them.

    Where the pairs break off, the rows are checked once more without the lines of another column that ends to the
    left of the label column (`_without_left_block`) — the company information block on the left of an invoice head
    and the '청구번호 INV-…' (invoice number) block on the right have different line spacing and are out of step, so a
    left line became a row in between or mixed into the same row, the pairs broke off, and the group of labels and the
    group of values were split (Pinned by: test_pdf_label_value_rows.py::TestLabelValueRowsBesideAnotherBlock).
    """
    max_label = page_width * _TABLE_CELL_MAX_RATIO
    found: list[list[_Line]] = []
    index = 0
    reach: dict = {}  # what `_centered_label_rows` worked out about the rows below each place (`_may_hold_labels`)

    while index < len(rows):
        centered, taken = _centered_label_rows(rows, index, max_label, reach)
        if centered:
            found.extend(centered)
            index += taken
            continue
        paired = _pairs_with([], rows[index], max_label)
        run = _label_value_run(rows, index, max_label) if paired else []
        if len(run) < 2 and (paired or len(rows[index]) > 2):
            beside = _without_left_block(rows, index, max_label, page_width)
            if beside is not None:
                found.extend(beside)
                taken = {id(line) for row in beside for line in row}
                index = max(position for position, row in enumerate(rows) if any(id(line) in taken for line in row)) + 1
                continue
        if not paired:
            index += 1
            continue
        widths = [row[1].x1 - row[1].x0 for row in run if len(row) == 2]
        narrow = sum(1 for width in widths if width <= max_label * _VALUE_MAX_RATIO)
        if (narrow < 2 or narrow <= len(widths) - narrow) and not _sectioned_values(run, max_label):
            index += 1
            continue

        if _stands_apart(rows, index, run, page_width):
            found.extend(_with_wrapped_values(run))
        index += len(run)
    return found


def _centered_label_rows(rows: list[list[_Line]], index: int, max_label: float,
                         reach: dict | None = None) -> tuple[list[list[_Line]], int]:
    """If, starting at `rows[index]` (one line of the value column), there is a 'label value' group whose labels sit at
    the vertical centre of multi-line value blocks: (rows of a label + its value lines, number of rows consumed).
    Otherwise ([], 0).

    When the labels of a borderless two-column table are vertically centred in their cells (valign=middle), a label
    does not sit in the same row as the first line of its value, so no pairs formed, and either all labels were read
    before all values (Chrome) or one value was placed after the wrong label (LibreOffice) (Pinned by:
    test_pdf_label_value_rows.py::TestLabelsCenteredOnTheirValues). Accepted narrowly: the value lines all share the
    left edge and size; the labels are short lines of the same size to the left of the value column, share a left edge
    and are further apart than the line spacing (two touching lines are one wrapped label); two or more labels each sit
    at the centre of one value block; the gaps between blocks are clearly wider than between the lines inside a block;
    and the rows right above and below do not continue the value column. Margin notes beside evenly spaced body text
    have no gaps between blocks, and with left-column headings of a two-column layout the type is larger or the right
    column continues outside the group. The first row is a single value line, or a pair of a one-line value and its
    label at the same height — when it could not start with a pair, a group whose first item has a one-line value was
    rejected on the grounds that the pair right above continues the value column (Pinned by:
    test_pdf_label_value_rows.py::
    TestLabelsCenteredOnTheirValues::test_value_of_one_line_at_any_place_keeps_each_label_before_its_value).
    """
    first = rows[index]
    if len(first) == 2 and first[0].x1 < first[1].x0 and abs(first[0].top - first[1].top) <= _TABLE_ROW_TOLERANCE:
        first = first[1:]  # 'label value' pair of a one-line value — the label is picked up by the loop below
    elif len(first) != 1:
        return [], 0
    column, size = first[0].x0, first[0].size
    if reach is not None and not _may_hold_labels(rows, index, column, size, max_label, reach):
        return [], 0  # the walk and the first label tests below would find no group
    values: list[_Line] = []
    labels: list[_Line] = []
    end = index

    while end < len(rows):
        row = rows[end]
        if end > index and row[0].top - max(line.bottom for line in rows[end - 1]) > row[0].height * 2.5:
            break
        value = [line for line in row if abs(line.x0 - column) <= _TABLE_ROW_TOLERANCE and abs(line.size - size) <= 0.5]
        label = [line for line in row if line.x1 < column and len(line.text.replace(" ", "")) >= 2 and abs(line.size - size) <= 0.5
                 and _short_label([line], max_label) and not _LINE_NUMBER_LABEL.match(line.text)]
        if len(value) > 1 or len(label) > 1 or len(value) + len(label) != len(row):
            break
        values += value
        labels += label
        end += 1

    if len(labels) < 2 or any(abs(label.x0 - labels[0].x0) > _TABLE_ROW_TOLERANCE for label in labels):
        return [], 0
    if any(lower.top - upper.bottom < upper.height for upper, lower in zip(labels, labels[1:], strict=False)):
        return [], 0  # Label lines touching at line spacing are one label wrapped onto two lines ('정성 / 효과')
    for neighbor, gap in ((rows[index - 1] if index else [], lambda row: first[0].top - max(line.bottom for line in row)),
                          (rows[end] if end < len(rows) else [], lambda row: row[0].top - values[-1].bottom)):
        if neighbor and gap(neighbor) <= first[0].height * 2.5 and any(
                abs(line.x0 - column) <= _TABLE_ROW_TOLERANCE for line in neighbor):
            return [], 0  # The value column continues above or below the group (right column of a two-column layout)

    # Split the value blocks at the widest gap between the centres of two neighbouring labels
    blocks: list[list[_Line]] = []
    gaps: list[float] = []  # Gaps between blocks
    start = 0
    for upper, lower in zip(labels, labels[1:], strict=False):
        middle, below = (upper.top + upper.bottom) / 2, (lower.top + lower.bottom) / 2
        splits = [at for at in range(start + 1, len(values))
                  if values[at - 1].bottom >= middle - _TABLE_ROW_TOLERANCE and values[at].top <= below + _TABLE_ROW_TOLERANCE]
        if not splits:
            return [], 0
        at = max(splits, key=lambda at: values[at].top - values[at - 1].bottom)
        gaps.append(values[at].top - values[at - 1].bottom)
        blocks.append(values[start:at])
        start = at
    blocks.append(values[start:])
    inner = max((lower.top - upper.bottom for block in blocks for upper, lower in zip(block, block[1:], strict=False)),
                default=0.0)
    if min(gaps) < inner + size * 0.2:
        return [], 0
    for label, block in zip(labels, blocks, strict=True):
        middle = (block[0].top + block[-1].bottom) / 2
        if abs((label.top + label.bottom) / 2 - middle) > label.height * 0.6 and abs(label.top - block[0].top) > _TABLE_ROW_TOLERANCE:
            return [], 0
    if all(abs(label.top - block[0].top) <= _TABLE_ROW_TOLERANCE for label, block in zip(labels, blocks, strict=True)):
        return [], 0  # Every label sits on the first line of its value — the ordinary 'label value' group handles it
    return [[label, *block] for label, block in zip(labels, blocks, strict=True)], end - index


def _row_split(row: list[_Line], column: float, size: float, max_label: float) -> tuple[list[_Line], list[_Line]]:
    """The value lines (at the value column `column`, of size `size`) and the label lines of a row, as
    `_centered_label_rows` takes them."""
    value = [line for line in row if abs(line.x0 - column) <= _TABLE_ROW_TOLERANCE and abs(line.size - size) <= 0.5]
    label = [line for line in row if line.x1 < column and len(line.text.replace(" ", "")) >= 2 and abs(line.size - size) <= 0.5
             and _short_label([line], max_label) and not _LINE_NUMBER_LABEL.match(line.text)]
    return value, label


# What the walk of `_centered_label_rows` collects from a place on: (number of labels, first label, smallest and largest
# label left edge, whether every label left edge is finite, whether two consecutive labels touch)
_NO_LABELS = (0, None, math.inf, -math.inf, True, False)


def _may_hold_labels(rows: list[list[_Line]], index: int, column: float, size: float, max_label: float, reach: dict) -> bool:
    """False when the walk of `_centered_label_rows` down from `rows[index]` (value column `column`, size `size`) is sure
    to end in no group: it collects fewer than two labels, labels whose left edges differ by more than
    `_TABLE_ROW_TOLERANCE`, or two labels touching at line spacing — the first tests after the walk, which return no group.
    True otherwise (the walk is then done in full).

    The walk from each row of a long paragraph (one value column) or of a column of short lines went on to its end, so
    the rows were walked again for every row: 15 seconds of own time for a paragraph of 6,000 lines (Pinned by:
    test_pdf_label_value_rows.py::TestLongRunsOfLinesForCentredLabels). What is collected from each place on is kept in
    `reach` per (column, size, place) — the walk reaches a place from the row above the same way whatever row it started
    from — so the rows are walked once per value column."""
    def split(position: int) -> tuple[bool, list[_Line]]:  # whether the walk stops at this row, and its label
        value, label = _row_split(rows[position], column, size, max_label)
        return len(value) > 1 or len(label) > 1 or len(value) + len(label) != len(rows[position]), label

    def joined(label: list[_Line], after: tuple) -> tuple:  # a row's label in front of what follows it
        if not label:
            return after
        line = label[0]
        count, first, low, high, finite, touching = after
        return (count + 1, line, min(low, line.x0), max(high, line.x0), finite and math.isfinite(line.x0),
                touching or (first is not None and first.top - line.bottom < line.height))

    stops, label = split(index)
    if stops:
        return False
    known = reach.setdefault((column, size), {})
    path: list[tuple[int, list[_Line]]] = []
    position, after = index + 1, _NO_LABELS
    while position < len(rows):
        if position in known:
            after = known[position]
            break
        row = rows[position]
        if row[0].top - max(line.bottom for line in rows[position - 1]) > row[0].height * 2.5:
            break
        stops, row_label = split(position)
        if stops:
            break
        path.append((position, row_label))
        position += 1
    for place, row_label in reversed(path):
        after = joined(row_label, after)
        known[place] = after
    count, first, low, high, finite, touching = joined(label, after)
    if count < 2 or touching:
        return False
    return not finite or (high - first.x0 <= _TABLE_ROW_TOLERANCE and first.x0 - low <= _TABLE_ROW_TOLERANCE)


def _without_left_block(rows: list[list[_Line]], index: int, max_label: float, page_width: float) -> list[list[_Line]] | None:
    """The group of 'label value' rows found again from the rows with the lines of another column removed — lines that
    end to the left of the label column of the last two fragments (a label-value pair) of `rows[index]` (wrapped value
    lines attached). None if neither this row nor the row right below has such a line, or no group forms.

    When the first line of the left block is at the same height as the first pair, that row has three fragments and
    could not start as a pair (Pinned by: test_pdf_label_value_rows.py::
    TestLabelValueRowsBesideAnotherBlock::test_pairs_whose_first_rows_line_up_with_the_left_block_stay_paired). In that
    case, if every row of the group has a left line, it is a three-column layout rather than a left block, so the
    result is None (test_rows_of_three_short_columns_are_not_split_into_a_pair_and_a_left_line in the same class).
    """
    if len(rows[index]) < 2:
        return None
    label = rows[index][-2]

    def kept(row: list[_Line]) -> list[_Line]:
        return [line for line in row if line.x1 > label.x0 - _TABLE_ROW_TOLERANCE]

    if len(kept(rows[index])) != 2 or index + 1 >= len(rows):
        return None
    if len(kept(rows[index])) == len(rows[index]) and len(kept(rows[index + 1])) == len(rows[index + 1]):
        return None
    view = [row for row in map(kept, rows) if row]
    start = next(position for position, row in enumerate(view) if row[0] is label)
    if not _pairs_with([], view[start], max_label):
        return None
    run = _label_value_run(view, start, max_label)
    widths = [row[1].x1 - row[1].x0 for row in run if len(row) == 2]
    narrow = sum(1 for width in widths if width <= max_label * _VALUE_MAX_RATIO)
    if narrow < 2 or narrow <= len(widths) - narrow or not _stands_apart(view, start, run, page_width):
        return None
    sides = {id(line): len(row) > len(kept(row)) for row in rows for line in row}  # Line → whether its row has a left line
    if len(rows[index]) > 2 and all(sides[id(row[0])] for row in run):
        return None
    return _with_wrapped_values(run)


def _label_value_run(rows: list[list[_Line]], index: int, max_label: float) -> list[list[_Line]]:
    """The group of 'label value' rows continuing downwards from `rows[index]` (a label-value pair): pairs in the same
    two columns (`_pairs_with`), wrapped value lines (`_wraps_value`), and label rows without a value between pairs
    (`_is_empty_value_row`). A group that has accepted a wrapped value line accepts further pairs only with a short label."""
    def pairs(run: list[list[_Line]], row: list[_Line], wrapped: bool) -> bool:
        return _pairs_with(run, row, max_label) and (_short_label(row, max_label) or not wrapped)

    def wraps(row: list[_Line]) -> bool:  # what `row` adds to `_has_wrapped_value` of the group (judged against the first row)
        return _has_wrapped_value([run[0], row])

    run = [rows[index]]
    # What `_wraps_value` needs of the pairs of the group (whether all labels are short, the first value, the widest value
    # end), carried along as rows are added like `wrapped` below — gathered from the whole group for every line, a group
    # of thousands of wrapped value lines took time with the square of its rows (Pinned by:
    # test_pdf_label_value_rows.py::TestLongRunsOfLinesForCentredLabels)
    summary = (_short_label(run[0], max_label), run[0][1], run[0][1].x1)

    def added(row: list[_Line]) -> None:
        nonlocal summary
        if len(row) == 2:
            short, first, widest = summary
            summary = (short and _short_label(row, max_label), first, row[1].x1 if row[1].x1 > widest else widest)

    # `_has_wrapped_value(run)`, carried along as rows are added. Asked of the whole group again for every pair with a long
    # label, a group of many such pairs took time with the square of its rows, and with the cube when it is then discarded
    # for its wide values and built again from each of its rows (Pinned by: test_audit_pdf_tail.py::
    # TestManyPairsWithLongLabels).
    wrapped = _has_wrapped_value(run)

    while index + len(run) < len(rows):
        row = rows[index + len(run)]
        if pairs(run, row, wrapped) or _wraps_value(run, row, max_label, summary):
            run.append(row)
            added(row)
            wrapped = wrapped or wraps(row)
        elif (index + len(run) + 1 < len(rows) and _is_empty_value_row(run, row, max_label)
              and pairs([run[0], row], rows[index + len(run) + 1], wrapped or wraps(row))):  # (only the first and last rows count)
            wrapped = wrapped or wraps(row) or wraps(rows[index + len(run) + 1])
            following = rows[index + len(run) + 1]
            run += [row, following]
            added(row)
            added(following)
        else:
            break
    return run


def _stands_apart(rows: list[list[_Line]], index: int, run: list[list[_Line]], page_width: float) -> bool:
    """Whether the group of 'label value' rows from `rows[index]` (`run`) stands alone, with no value column continuing
    above or below, and its two columns are close (a column of right-aligned numeric values, or a group where a pair
    follows again after a wrapped value line, may be far apart)."""
    neighbors = [rows[position] for position in (index - 1, index + len(run)) if 0 <= position < len(rows)]
    column = run[0][1]
    if len(run[-1]) == 1 and run[-1][0].x0 > column.x0 - _TABLE_ROW_TOLERANCE:
        # A group ending in a wrapped value line looks at the next line that overlaps the value column horizontally. In
        # a two-column layout a right-column line, where the left-column paragraph has ended, comes in as a wrapped
        # value line, and the right column continues below it too (left-column subheading rows are skipped).
        right = max(row[1].x1 for row in run if len(row) == 2)
        below = [row for row in rows[index + len(run):] if any(line.x0 < right and column.x0 < line.x1 for line in row)]
        neighbors += below[:1]
    if any(_near(run, row) and any(abs(line.x0 - column.x0) <= _TABLE_ROW_TOLERANCE for line in row) for row in neighbors):
        return False

    # If a 'label value' row follows again after a line where only the value wrapped, it is a table with row structure
    # (in two side-by-side text boxes the left line is never empty). Joined even when the columns are far apart —
    # LibreOffice splits the column widths of a borderless two-column table in half, leaving label and value 30% of
    # the page width apart.
    wrapped = any(len(row) == 1 and len(later) == 2 and abs(later[1].x0 - row[0].x0) <= _TABLE_ROW_TOLERANCE
                  for position, row in enumerate(run) for later in run[position + 1:position + 2])
    return (column.x0 - max(row[0].x1 for row in run if len(row) == 2) <= page_width * _LABEL_GAP_RATIO
            or _right_aligned_numbers(run) or wrapped)


def _wraps_value(run: list[list[_Line]], row: list[_Line], max_label: float,
                 pairs: tuple[bool, _Line, float] | None = None) -> bool:
    """Whether `row` is a line wrapped from the last value of a group of 'label value' rows (`run`): a single fragment
    shares the left edge of the value column, is attached to the value line right above at the line spacing inside a
    cell (`_VALUE_WRAP_GAP_RATIO`), has the same size, and does not reach further right than the group's value column.
    The labels of the group must all be short (`_WRAP_LABEL_RATIO`) — in two-column text made of short lines, a
    right-column line where the left-column paragraph had ended came in as a wrapped value line and the two columns
    were joined row by row (generated two-column document).

    A line smaller than the value line above (down to `_VALUE_WRAP_MIN_SIZE_RATIO`) is also accepted, and such a line
    may reach further right than the value column — in a programme schedule the small print '한국제조혁신협회 회장
    김혁신' under '개회사' was not accepted as a wrapped line, so all the times were read before all the sessions
    (Pinned by: test_pdf_label_value_rows.py::TestShortLabelsWithLongerValues::
    test_program_schedule_with_a_smaller_second_line_keeps_each_time_with_its_session). Right-column lines of two
    columns have the same size.

    In a borderless two-column table from LibreOffice or Word, the next line of a value wrapped at the cell width sat
    alone in the value column and cut the group, and the cut group was discarded because of the value-column fragment
    in the neighbouring row, so the group of labels and the group of values were split wholesale (Pinned by:
    test_pdf.py::TestFlowJoins::test_wrapped_value_lines_stay_under_their_label). A wrapped line is accepted only
    inside a group, so a right column continuing above and below (two-column layout) is still filtered out by the
    neighbouring-row condition outside the group (`_stands_apart`).
    """
    if len(row) != 1:
        return False
    line, above = row[0], run[-1][-1]
    if pairs is None:
        values = [pair[1] for pair in run if len(pair) == 2]
        pairs = (all(_short_label(pair, max_label) for pair in run if len(pair) == 2), values[0],
                 max(value.x1 for value in values))
    short, first, widest = pairs
    return (short
            and abs(line.x0 - first.x0) <= _TABLE_ROW_TOLERANCE
            and above.size * _VALUE_WRAP_MIN_SIZE_RATIO - 0.5 <= line.size <= above.size + 0.5
            and line.top - above.bottom <= above.height * _VALUE_WRAP_GAP_RATIO and line.top > above.top + above.height / 2
            and (line.x1 <= widest + line.size * 2 or line.size < above.size - 0.5))


def _short_label(row: list[_Line], max_label: float) -> bool:
    """Whether the label of a 'label value' row is short enough to accept a wrapped value line (`_WRAP_LABEL_RATIO`).
    A wider label with two words or fewer (`_LABEL_MAX_WORDS`) is accepted as well.

    '정부지원연구개발비' and '총 연구개발기간' in an R&D form exceed 15% of the page width, so when the value wrapped,
    the group of labels and the group of values were split wholesale (Pinned by:
    test_pdf_label_value_rows.py::TestLongLabelsWithWrappedValues). The left-column lines of two columns of short
    lines, which the 15% cap used to block, are body text of several words (Pinned by: test_pdf.py::TestFlowJoins::
    test_two_columns_of_short_lines_are_not_joined_through_a_line_only_on_the_right). Known accepted gap: two columns
    made of wide body lines of two words or fewer cannot be told apart.
    """
    label = row[0]
    return label.x1 - label.x0 <= max_label * _WRAP_LABEL_RATIO or len(label.text.split()) <= _LABEL_MAX_WORDS


def _has_wrapped_value(run: list[list[_Line]]) -> bool:
    """Whether a group of 'label value' rows has a wrapped value line (a fragment alone in the value column)."""
    return any(len(row) == 1 and row[0].x0 > run[0][1].x0 - _TABLE_ROW_TOLERANCE for row in run)


def _sectioned_values(run: list[list[_Line]], max_label: float) -> bool:
    """Whether the shape counts as a 'label value' group even with many wide values: there are two or more labels, all
    short (`_short_label`) and not digits only, and a pair follows again right after a line where the value wrapped —
    the label column is empty for the several lines of a value and starts again at the next item.

    In a history (date | multi-line content) or side headings (section title | body paragraph), half or more of the
    rows have a value over half the page width, so the group was discarded and all labels were read before all values
    (Pinned by: test_pdf_label_value_rows.py::TestShortLabelsWithLongerValues). A document with line numbers has
    digit-only labels and is discarded as before (test_line_numbers_beside_wide_lines_are_not_label_value_rows in the
    same class). In two columns where the lines of a narrow side column sit beside the body lines line by line, no pair
    follows again after a wrapped line.
    """
    pairs = [row for row in run if len(row) == 2]
    if len(pairs) < 2 or any(not _short_label(row, max_label) or _LINE_NUMBER_LABEL.match(row[0].text) for row in pairs):
        return False
    return any(len(row) == 1 and len(later) == 2 for row, later in zip(run, run[1:], strict=False))


def _with_wrapped_values(run: list[list[_Line]]) -> list[list[_Line]]:
    """Attach wrapped value lines (single-fragment rows) to the 'label value' row right above. Label rows without a
    value (`_is_empty_value_row`) are left as they are."""
    rows: list[list[_Line]] = []

    for row in run:
        if len(row) == 1 and rows and len(rows[-1]) >= 2 and row[0].x0 >= rows[-1][1].x0 - _TABLE_ROW_TOLERANCE:
            rows[-1] = [*rows[-1], row[0]]
        else:
            rows.append(row)
    return rows


def _right_aligned_numbers(run: list[list[_Line]]) -> bool:
    """Whether the values of a group of 'label value' rows are all numeric values (`_FAR_VALUE`: amounts, ratios,
    dates, page numbers; units such as '원' allowed) with matching right edges.

    Right-aligned amounts in a payslip, receipt or invoice are more than 20% of the page width (`_LABEL_GAP_RATIO`)
    away from the items, so the items bunched together and the amounts bunched together
    (Pinned by: test_pdf_forms_and_amounts.py::TestFarRightAmounts). The right column of two side-by-side forms or text
    boxes is not numeric values only, so it does not qualify (Deliberate — the distance condition is left as is and
    only this shape is additionally accepted, test_quality.py::
    test_pdf_side_by_side_form_xobjects_are_not_interleaved).

    The first row may be a non-numeric header ('항목 | 금액(원)') — when there are two or more numeric values below
    with the same right edge. Because of the header the whole amount column fell after the next section (Pinned by:
    test_pdf_forms_and_amounts.py::TestFarRightAmounts::
    test_amount_column_with_a_header_word_stays_beside_its_items).
    """
    values = [row[1] for row in run if len(row) == 2]
    header = (len(run[0]) == 2 and len(values) >= 3 and len(run[0][1].text) <= _HEADER_CELL_MAX_CHARS
              and not _FAR_VALUE.match(run[0][1].text.replace(" ", "")))
    numbers = values[1:] if header else values
    return all(abs(value.x1 - values[0].x1) <= _TABLE_ROW_TOLERANCE for value in values) and all(
        _FAR_VALUE.match(value.text.replace(" ", "")) for value in numbers)


def _pairs_with(run: list[list[_Line]], row: list[_Line], max_label: float) -> bool:
    """Whether `row` is a label-value pair and fits the same two columns right below the last row of `run` (if `run`
    is empty, only whether it is a pair).

    The label is at least two characters. An identifier printed vertically in the left margin of a paper ('arXiv:…')
    or a drop cap came out as one-character lines and was joined with the body line beside it (public paper PDF). The
    value width is not checked here but over the whole group (`_label_value_rows`) — when one long value cut the
    group, both cut halves were discarded because of the value-column fragment in the neighbouring row (Pinned by:
    test_pdf.py::TestFlowJoins::
    test_label_value_rows_with_one_value_longer_than_half_the_page_keep_the_pairs).
    """
    if len(row) != 2:
        return False
    label, value = row
    if label.x1 - label.x0 > max_label or len(label.text.replace(" ", "")) < 2 or value.x0 < label.x1:
        return False
    if not run:
        return True

    first_label, first_value = run[0]
    # Table of contents without leaders: items indented per level also belong to the same group if they have a
    # right-aligned page number. All the items were read first and then the page numbers came out bunched together
    # (Pinned by: test_pdf_toc_and_checkboxes.py::TestTableOfContentsWithoutLeaders).
    indented = (bool(_BARE_NUMBER.match(value.text)) and bool(_BARE_NUMBER.match(first_value.text))
                and abs(value.x1 - first_value.x1) <= _TABLE_ROW_TOLERANCE
                and abs(label.x0 - first_label.x0) <= max(label.size, first_label.size) * _TOC_INDENT_RATIO)
    return (_near(run, row) and (abs(label.x0 - first_label.x0) <= _TABLE_ROW_TOLERANCE or indented)
            and (abs(value.x0 - first_value.x0) <= _TABLE_ROW_TOLERANCE
                 or abs(value.x1 - first_value.x1) <= _TABLE_ROW_TOLERANCE))


def _is_empty_value_row(run: list[list[_Line]], row: list[_Line], max_label: float) -> bool:
    """Whether `row` is a label row without a value right below a group of 'label value' rows (`run`): a single
    fragment shares the left edge of the label column, is at least two characters, and ends before the value column
    starts. The caller adds it to the group only when the next row is a pair again.

    The valueless '(경유)' row in the head of an official letter, '수신 / (경유) / 제목', cut the group, the pairs fell
    short of 2 rows, and the labels and values came out grouped separately
    (Pinned by: test_pdf.py::TestFlowJoins::test_label_row_with_an_empty_value_between_label_value_rows_keeps_the_pairs).
    """
    label, value = run[0]
    line = row[0]
    return (len(row) == 1 and _near(run, row) and abs(line.x0 - label.x0) <= _TABLE_ROW_TOLERANCE
            and line.x1 - line.x0 <= max_label and len(line.text.replace(" ", "")) >= 2 and line.x1 < value.x0)


def _near(run: list[list[_Line]], row: list[_Line]) -> bool:
    """Whether `row` is the row right above or below `run` (by the row spacing criterion of borderless tables)."""
    top, bottom = min(line.top for line in run[0]), max(line.bottom for line in run[-1])
    height = run[0][0].height
    return row[0].top - bottom <= height * 2.5 and top - max(line.bottom for line in row) <= height * 2.5
