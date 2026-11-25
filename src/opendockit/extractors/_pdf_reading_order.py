"""Reading order where the layout analysis mixes it: two columns read line by line, right-aligned lines between left ones,
a lone centred line pushed behind lower text, sidebars and captions.
"""

from bisect import bisect_left, bisect_right, insort
from itertools import accumulate
import math
import statistics

from opendockit.extractors._pdf_model import (
    _CAPTION_GAP_RATIO,
    _COLUMN_LINES_MIN,
    _HEADING_MIN_RATIO,
    _SPACED_CHAR,
    _TABLE_CELL_MAX_RATIO,
    _TABLE_ROW_TOLERANCE,
    _Line,
    _Page,
    _visual_rows,
)


# Letter-spaced heading: single-character pieces of the same height and size placed at even intervals (widest gap ≤ narrowest gap ×
# this multiple + 1pt).
_SPACED_GAP_SPREAD = 1.5


# Re-reading side-by-side columns (_read_columns_in_turn): when two lines adjacent in reading order are horizontally apart, both
# are wider than a table cell (_TABLE_CELL_MAX_RATIO) and their height difference is at most this multiple of the line height, it
# is a 'jump to the neighbouring column'. Only pages with at least _COLUMN_MIX_MIN such jumps are fixed. A correctly read two-column
# page jumps once, from the bottom of the left column to the top of the right column, and those two are far apart in height.
_COLUMN_MIX_NEAR = 1.5
_COLUMN_MIX_MIN = 3


# Gap (multiple of the line height, `_fills_column_below`) for a line that fills the column width to count as wrapping onto the
# next line. Even the gap between lines at 200% line spacing exceeds it.
_WRAP_LINE_GAP_RATIO = 1.5


_COLUMN_CONTINUE_RATIO = 3.0
# On a page with more lines than this, lines pushed back in a single-column flow are not moved up (`_read_lone_lines_in_place`).
# Each candidate to move looks at all the page's lines (`_one_flow`, sorted lookups since a page of 480 lines took 7 seconds), so the
# pass is quadratic in the lines and gets slow on a page of scattered text pieces. A body page usually has around 100 lines.
_LONE_LINE_MAX_LINES = 500
# Lines the windows of right-aligned lines (`_alternating_window`) of one page may take in all, counted once per window
# looked at; the turn-backs after that stay as read. A page of 1,400 lines, all one window looked at from every turn-back,
# stays within it.
_ALTERNATING_WORK = 1_000_000
# Difference between centres (relative to the page width, `_centred_on`) for a line to count as placed in the middle of the page.
_CENTRED_TOLERANCE_RATIO = 0.03
# Rows with lines side by side inside one column that make it hold a table (`_has_lines_side_by_side`)
_SIDE_BY_SIDE_ROWS_MIN = 2
# Two pages printed side by side on one sheet (`_two_pages_on_a_sheet`): the empty band at the middle is at least this share of
# the two outer margins together - the inner margins of the two pages - and each half holds at least this many lines
_SHEET_GAP_RATIO = 0.75
_SHEET_HALF_LINES_MIN = 3
# and the sheet has the proportions of a sheet of the A or B series (1 to the square root of 2), whose half is a page of
# that series, within this share: A4 as PDFs round it is within 0.4%, and a slide set to 'A4 paper' (780 by 540) is 2.1% off
_SHEET_PROPORTION = math.sqrt(2)
_SHEET_PROPORTION_TOLERANCE = 0.01


def _read_columns_in_turn(page: _Page) -> None:
    """On a page where the lines of two side-by-side columns were read interleaved line by line, reorder the lines as
    left column, then right column.

    When the gap between lines exceeds `line_margin` (0.5) times the line height, pdfminer makes a text box per line
    and pairs the nearest text boxes first. In the side-by-side text boxes of two-column documents and presentations
    with line spacing of 150% or more, the left and right lines at the same height were paired first and read as
    'left 1, right 1, left 2 …' (130% and 140% give one text box per column and are fine). Raising the fixed
    `line_margin` cannot fix this — 1.0 fixes 160% but leaves 200% as it was. So it is corrected from the positions of
    the lines, independent of line spacing (Pinned by: test_pdf.py::TestWidelySpacedColumns).
    - The trigger is kept narrow (Deliberate): places where two lines adjacent in reading order are apart
      horizontally, both wider than a table cell and close in height occur `_COLUMN_MIX_MIN` times or more, and the
      ends of those left lines are all to the left of the starts of the right lines (one gap between columns). Rows
      with a short fragment (page number in a table of contents, bullet, article number) and three or more columns are
      not touched. Borderless tables and 'label value' rows have already been removed or made into one line.
    - On meeting a line that crosses the gap between the columns (full-width heading or paragraph), the left column
      and right column collected so far are emitted and then that line. The order inside a column and what comes
      before and after a crossing line stay as pdfminer decided.
    - A page where this does not trigger is checked for interleaving by paragraph (`_read_paragraph_columns_in_turn`).
    """
    if page.simple:
        return
    middle = _two_pages_on_a_sheet(page)
    if middle is not None:
        page.lines = [*(line for line in page.lines if line.x1 <= middle), *(line for line in page.lines if line.x0 >= middle)]
        page.two_pages = True
        return
    wide = page.width * _TABLE_CELL_MAX_RATIO
    mixed = [sorted(pair, key=lambda line: line.x0) for pair in zip(page.lines, page.lines[1:], strict=False)
             if _is_beside(*pair) and min(line.x1 - line.x0 for line in pair) >= wide]
    if len(mixed) < _COLUMN_MIX_MIN:
        _read_paragraph_columns_in_turn(page)
        return
    left_end, right_start = max(left.x1 for left, _ in mixed), min(right.x0 for _, right in mixed)
    if left_end >= right_start:
        return

    below_top = _below_columns_read_line_by_line(page, left_end, right_start)
    ordered: list[_Line] = []
    left: list[_Line] = []
    right: list[_Line] = []
    for line in (line for line in page.lines if line.top < below_top):
        in_left, in_right = line.x1 <= right_start, line.x0 >= left_end
        if in_left != in_right:
            (left if in_left else right).append(line)
        else:
            ordered += [*left, *right, line]
            left, right = [], []
    page.lines = [*ordered, *left, *right, *(line for line in page.lines if line.top >= below_top)]


def _two_pages_on_a_sheet(page: _Page) -> float | None:
    """The middle of a sheet that holds two pages side by side, None for any other page.

    A document printed two pages to a sheet (a press release of Hangul, A4 turned on its side with two A5 pages) is two
    pages, not two columns: the left one is read to its end, then the right one. The rules for columns look for text
    below both columns and for headings over them, and where they did not apply the order was the layout analysis's own,
    which pairs the boxes nearest to each other across the gap: a heading of the right page came out in the middle of a
    sentence of the left one (Pinned by: test_pdf_reading_order.py::TestTwoPagesOnASheet).
    Kept narrow (Deliberate): the visible area has the proportions of a sheet of the A or B series on its side (1 to the
    square root of 2, so that its half is a page of the series; a slide and a sheet of Letter are not asked);
    every line, table, picture, rule and filled band stands wholly left or wholly right of its middle; each half holds
    `_SHEET_HALF_LINES_MIN` lines or more; and the band left empty at the middle is centred on it and at least
    `_SHEET_GAP_RATIO` of the two outer margins together - each half has its own margins on both sides, where two
    columns of one page share the page's margins and stand close. A page that is turned (/Rotate) or read without
    places has no visible area to measure in (`_Page.text_view`) and is left to the rules for columns.
    Known accepted gap: a page of that shape which is one page - two columns set far apart with nothing across the
    middle, rows of a list read across it, text under both columns - is read half by half too."""
    if page.text_view is None:
        return None
    x0, top, x1, bottom = page.text_view
    if not (bottom > top and abs((x1 - x0) / (bottom - top) - _SHEET_PROPORTION) <= _SHEET_PROPORTION * _SHEET_PROPORTION_TOLERANCE):
        return None
    middle = (x0 + x1) / 2
    lines = [(line.x0, line.x1) for line in page.lines]
    others = [*((table.x0, table.x1) for table in page.tables), *((figure[0], figure[2]) for figure in page.figures),
              *((left, right) for _, left, right in page.rules), *((left, right) for _, _, left, right in page.bands)]
    lefts = [span for span in lines if span[1] <= middle]
    rights = [span for span in lines if span[0] >= middle]
    if len(lefts) + len(rights) != len(lines) or min(len(lefts), len(rights)) < _SHEET_HALF_LINES_MIN:
        return None
    if any(not (right <= middle or left >= middle) for left, right in others):
        return None
    lefts += [span for span in others if span[1] <= middle]
    rights += [span for span in others if span[0] >= middle]
    left_end, right_start = max(right for _, right in lefts), min(left for left, _ in rights)
    outer = min(left for left, _ in lefts) - x0 + x1 - max(right for _, right in rights)
    if not (outer > 0 and right_start - left_end >= outer * _SHEET_GAP_RATIO
            and abs((left_end + right_start) / 2 - middle) <= (x1 - x0) * _CENTRED_TOLERANCE_RATIO):
        return None
    return middle


def _below_columns_read_line_by_line(page: _Page, left_end: float, right_start: float) -> float:
    """The height at which the text below the two columns reordered by `_read_columns_in_turn` starts. Infinity if
    there is none. The same decision as the paragraph-level path (`_below_both_columns`; for two columns under a pair
    of subheadings with fewer than `_COLUMN_LINES_MIN` wide lines, `_heading_below_headed_columns`).

    A single-column heading ('Ⅴ. 결론') or short paragraph below two columns with line spacing of 160% or more is a
    left line within the column width, so it was attached to the left column and came out before the right column
    (Pinned by: test_pdf.py::TestWidelySpacedColumns::
    test_heading_below_widely_spaced_columns_read_line_by_line_comes_after_the_right_column,
    test_heading_below_short_headed_columns_read_line_by_line_comes_after_the_right_column).
    """
    blocks = [(table.x1, table.bottom) for table in page.tables] + [(figure[2], figure[3]) for figure in page.figures]
    below_top = _below_both_columns(page.lines, left_end, right_start, blocks)
    wide = page.width * _TABLE_CELL_MAX_RATIO
    lefts = [line for line in page.lines if line.x1 <= right_start and wide <= line.x1 - line.x0]
    rights = [line for line in page.lines if line.x0 >= left_end and wide <= line.x1 - line.x0]
    if lefts and rights and max(len(lefts), len(rights)) < _COLUMN_LINES_MIN:
        heading_size = _headed_columns(page.lines, lefts, rights, left_end, right_start)
        if heading_size is not None:
            below_top = min(below_top, _heading_below_headed_columns(page.lines, left_end, right_start, heading_size))
    return below_top


def _read_paragraph_columns_in_turn(page: _Page) -> None:
    """Reorder, column by column, two side-by-side columns that were read interleaved by paragraph (pages where
    `_read_columns_in_turn` did not trigger).

    For two columns with wide paragraph spacing (about one line of space after a paragraph, a blank line between
    paragraphs), pdfminer makes a text box per paragraph and pairs the nearest text boxes first, so the paragraphs of
    the right column (including the rest of a sentence carried over from the end of the left column) slipped in
    between the paragraphs of the left column. The adjacent lines are far apart in height, so the line-level condition
    (`_is_beside`) does not catch it (Pinned by: test_pdf.py::TestWidelySpacedColumns::
    test_two_columns_with_wide_paragraph_spacing_are_read_column_by_column). The conditions are kept narrow (Deliberate):
    - Columns: lines wider than a table cell and at most half the page width number `_COLUMN_LINES_MIN` or more on one
      side and 2 or more on the other, with one gap between them. If a column contains lines side by side (a table of
      3 or more columns), or places where two lines adjacent in reading order sit side by side in the same row occur
      `_COLUMN_MIX_MIN` times or more (row-wise layout — a '조건 | 지표' list), nothing is touched.
    - A group of lines that returns to the left column after right-column lines were read is treated as the same
      column only when it continues right below the left column (gap at most `_COLUMN_CONTINUE_RATIO` times the line
      height). Unless the next group is right-column lines starting above it, its left edge must also match the left
      column — outdented text below the two columns ('FAQ') was pulled up into the left column. If it does not
      continue, the flow is cut there and the left column and right column collected so far are emitted (two columns
      above and below a full-width figure or table stay as they are). However, if the non-continuing left-column group
      starts above the bottom of the right column read before it (beside the right column), only the earlier left
      column is emitted and the right column is placed after that group — in a two-column paper that pdfminer gave in
      right column → left column order, the right column came out first (Pinned by:
      test_pdf.py::TestWidelySpacedColumns::test_right_column_read_before_the_left_column_is_put_after_it). If that
      group continues past the bottom of the right column to a line set far apart, it is split there
      (`_set_apart_below`).
    - Lines below both columns (`_below_both_columns`) are placed after the two columns in flow order.
    Known accepted gap: pages with many short lines interleaved line by line, and pages that return to a paragraph
    with an indented first line, stay as they are.
    """
    columns = _paragraph_columns(page)
    if columns is None:
        return
    left_end, right_start, heading_size = columns
    blocks = [(table.x1, table.bottom) for table in page.tables] + [(figure[2], figure[3]) for figure in page.figures]
    below_top = min(_below_both_columns(page.lines, left_end, right_start, blocks),
                    _heading_below_headed_columns(page.lines, left_end, right_start, heading_size))
    below = [line for line in page.lines if line.top >= below_top]

    # (in the left column? — None if crossing, consecutive lines of the same column in reading order)
    runs: list[tuple[bool | None, list[_Line]]] = []
    for line in page.lines:
        if line.top >= below_top:
            continue
        in_left, in_right = line.x1 <= right_start, line.x0 >= left_end
        side = None if in_left == in_right else in_left
        if runs and side is not None and runs[-1][0] == side:
            runs[-1][1].append(line)
        else:
            runs.append((side, [line]))

    ordered: list[_Line] = []
    left: list[_Line] = []
    right: list[_Line] = []
    index = 0
    while index < len(runs):
        in_left, lines = runs[index]
        index += 1
        if in_left is None:
            ordered += [*left, *right, *lines]
            left, right = [], []
            continue
        following = runs[index] if index < len(runs) else (None, lines)
        interleaved = following[0] is False and following[1][0].top < max(line.bottom for line in lines)
        if right and not _continues_column(left if in_left else right, lines, aligned=not (in_left and interleaved)):
            beside = in_left and lines[0].top < max(line.bottom for line in right)  # Left column beside the right column read before it
            rest = _set_apart_below(lines, max(line.bottom for line in right), blocks, right_start) if beside else []
            if rest:  # The rest, continuing set apart below the right column, is handled separately as the next group
                lines = lines[:len(lines) - len(rest)]
                runs.insert(index, (True, rest))
            ordered += left if beside else [*left, *right]
            left, right = [], right if beside else []
        (left if in_left else right).extend(lines)
    page.lines = [*ordered, *left, *right, *below]


def _set_apart_below(lines: list[_Line], bottom: float, blocks: list[tuple[float, float]], right_start: float) -> list[_Line]:
    """The rest of `lines` (a left-column group, in reading order) that starts below `bottom` far apart from the lines
    before it (more than the gap of `_continues_column`). [] if there is none. A table or figure inside the left column
    that ends in between (`blocks` holds (right edge, bottom)) counts as a preceding line — a left column continuing
    below a table became cut-off text (a table in the middle of a two-column paper).
    """
    reached = lines[0].bottom  # Bottom of the preceding lines (and the tables/figures below them)
    for index, line in enumerate(lines[1:], start=1):
        reached = max([reached, *(end for x1, end in blocks if x1 <= right_start and end <= line.top)])
        if line.top >= bottom and line.top - reached > max(lines[index - 1].height, line.height) * _COLUMN_CONTINUE_RATIO:
            return lines[index:]
        reached = max(reached, line.bottom)
    return []


def _below_both_columns(lines: list[_Line], left_end: float, right_start: float,
                        blocks: list[tuple[float, float]]) -> float:
    """The height (top coordinate) at which the text placed below both columns starts. Infinity if there is none.

    A short line within one column width (a subheading or contact line below the two columns) cannot be told from a
    left-column line, so it was attached as a continuation of the left column and came out before the right column
    ('3. 결론' below a LibreOffice two-column section, Pinned by: test_pdf.py::TestWidelySpacedColumns::
    test_short_line_below_both_columns_stays_after_the_right_column). A line on the left-column side is below both
    columns if it satisfies all of the following:
    - Every line of the right column ends above it, and the right column comes down to the height of the preceding
      line of the left column (the bottoms of the two columns agree). For a left-column paragraph on a page where only
      the left column is long (the last page), the right column does not come down that far.
    - It does not fill the column width, and its left edge is at or to the left of the leftmost edge of the left column.
    - It is at least half a line height, and at least twice the median gap between left-column lines, away from the
      preceding line of the left column (spacing between paragraphs or above a subheading).
    In a presentation with wide line spacing the continuation line of the last item of the left column (an indented
    line in parentheses) was pushed below both columns and separated from its item (Pinned by:
    test_last_line_of_the_left_column_below_the_end_of_the_right_column_stays_in_the_left_column) — it is indented and
    the gap is the ordinary line spacing.
    A table or figure inside the left column (`blocks` holds (right edge, bottom)) counts as a preceding line of the
    left column when measuring the bottoms of the two columns — when the right column ended at the same height as the
    text above a left-column table, the caption below the table and the rest of the left column were pushed below both
    columns (Pinned by: test_left_column_continuing_below_its_table_is_not_taken_for_text_below_both_columns). The gap
    is still measured from text lines.
    If the line right below (with no line reaching towards both columns in between) continues while filling the column
    width, this is the closing text of a page whose left column is longer — the subheading and paragraph at the end of
    the left column were pushed after the right column (Pinned by:
    test_heading_and_paragraph_closing_the_longer_left_column_stay_in_the_left_column). So is a line from which the left
    column flows on without a break further down than the right column is tall (`_FlowingOn.past`): the rest of a ragged
    left column under a short right column was pushed after it (Pinned by: test_pdf_reading_order.py::TestShortRightColumn).
    The lines above each candidate are not gone through again for it: their lowest bottom, leftmost edge and the gaps
    between them (sorted, for the median) are carried along from line to line, the bottoms of the tables and figures are
    looked up by bisection and the lines reaching towards the right column by their tops (`_RightReaching`), with the same
    results as the plain computations, which took time with the square of the lines: 8,000 left lines alternating two
    widths under one right-column line took 1.2 seconds here (Pinned by:
    test_pdf_reading_order.py::TestManyLinesBesideColumns).
    """
    rights = [line for line in lines if line.x0 >= left_end]
    if not rights:
        return float("inf")

    right_bottom = max(line.bottom for line in rights)
    lefts = sorted((line for line in lines if line.x1 <= right_start), key=lambda line: line.top)
    block_bottoms = sorted(bottom for x1, bottom in blocks if x1 <= right_start and bottom == bottom)
    reaching = _RightReaching(lines, left_end)
    flowing = _FlowingOn(lefts, left_end)
    right_height = right_bottom - min(line.top for line in rights)
    gaps: list[float] = []  # sorted gaps between the lines above, where the lower line starts below the upper one
    lowest = leftmost = 0.0  # `max(bottom)` and `min(x0)` of the lines above, folded in the same order as `max` and `min`
    for index, line in enumerate(lefts):
        if index:  # the line right above joins the lines above
            newest = lefts[index - 1]
            if index == 1:
                lowest, leftmost = newest.bottom, newest.x0
            else:
                lowest = newest.bottom if newest.bottom > lowest else lowest
                leftmost = newest.x0 if newest.x0 < leftmost else leftmost
                if newest.top > lefts[index - 2].bottom:
                    insort(gaps, newest.top - lefts[index - 2].bottom)
        if not index or line.top < right_bottom - 1 or line.x1 > left_end - line.height:
            continue
        gap = line.top - lowest
        # `max` keeps a NaN it starts with; otherwise the lowest bottom among the lines and the blocks that end above the line
        reached = bisect_right(block_bottoms, line.top) if line.top == line.top else 0
        left_bottom = (lowest if lowest != lowest or not reached or block_bottoms[reached - 1] <= lowest
                       else block_bottoms[reached - 1])
        if (line.x0 <= leftmost + _TABLE_ROW_TOLERANCE
                and gap >= max(line.height * 0.5, _sorted_median(gaps) * 2 if gaps else 0.0)
                and right_bottom >= left_bottom - line.height
                and not _fills_column_below(line, lefts[index + 1:index + 3], reaching, left_end)
                and not flowing.past(index, right_height, reaching)):
            return line.top
    return float("inf")


def _sorted_median(values: list[float]) -> float:
    """`statistics.median` of a list that is already sorted."""
    middle = len(values) // 2
    return values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2


class _RightReaching:
    """The tops of the lines that reach past the left column's end (`x1 > left_end`), sorted, to tell whether one of them
    starts strictly between two heights — what `_fills_column_below` asks of all the lines. A top that is not a number
    is never between two heights and is left out."""

    def __init__(self, lines: list[_Line], left_end: float) -> None:
        self._tops = sorted(line.top for line in lines if line.x1 > left_end and line.top == line.top)

    def between(self, above: float, below: float) -> bool:
        if above != above or below != below:
            return False
        index = bisect_right(self._tops, above)
        return index < len(self._tops) and self._tops[index] < below


class _FlowingOn:
    """For each line of `lefts` (left-column lines sorted by top), how far the left column flows on from it without a
    break: the run of lines each followed by the next at the same left edge and a gap of at most `_WRAP_LINE_GAP_RATIO`
    times its height, and whether a line of that run (not its last) fills the column width and so wraps onto the next.
    Worked out once from the bottom up, so a candidate of `_below_both_columns` asks it in constant time."""

    def __init__(self, lefts: list[_Line], left_end: float) -> None:
        self._lefts = lefts
        count = len(lefts)
        self._end = list(range(count))  # index of the last line of the run starting at each line
        self._wraps = [False] * count  # a line of that run, before its last, fills the column width
        self._reach = list(range(count))  # index of the last line the column reaches from each line, past subheadings
        for index in range(count - 2, -1, -1):
            line, lower = lefts[index], lefts[index + 1]
            if abs(lower.x0 - line.x0) > _TABLE_ROW_TOLERANCE:
                continue
            gap = lower.top - line.bottom
            if 0 <= gap <= line.height * _WRAP_LINE_GAP_RATIO:
                self._end[index] = self._end[index + 1]
                self._wraps[index] = line.x1 > left_end - line.height or self._wraps[index + 1]
                self._reach[index] = self._reach[index + 1]
            elif 0 <= gap <= max(line.height, lower.height) * _COLUMN_CONTINUE_RATIO and self._wraps[index + 1]:
                self._reach[index] = self._reach[index + 1]

    def past(self, index: int, height: float, reaching: "_RightReaching") -> bool:
        """Whether the left column flows on from line `index` without a break, with a line wrapping at the column width
        in that run and no line reaching towards the right column in between, further down than `height` (that of the
        right column) — the left column visibly goes on, so the line is not text below both columns.

        On the last page of a two-column article whose right column holds only the closing lines at its top, ending
        level with the end of a left-column paragraph, the first line of the next left paragraph (ragged right, short of
        the column width) was taken for text below both columns and the rest of the left column came out after the right
        column (Pinned by: test_pdf_reading_order.py::TestShortRightColumn). Text set out below both columns is a few
        lines or crosses the gutter; a run taller than the right column, at the column's left edge and wrapping at its
        width, is the left column going on.
        How far it goes on is measured past a break no wider than a column continues across (`_COLUMN_CONTINUE_RATIO`
        line heights, the space above a subheading) to a run at the same left edge that wraps at the column width
        again: a subheading two paragraphs down cut the run short of the right column's height, and the rest of the
        left column came out after the right column (Pinned by: test_pdf_reading_order.py::TestShortRightColumn::
        test_left_column_running_on_past_a_subheading_comes_before_it). The paragraph the line opens must still flow
        on and wrap by itself.
        """
        end = self._end[index]
        last = self._lefts[self._reach[index]]
        return (end > index + 1 and self._wraps[index + 1] and last.bottom - self._lefts[index].top > height
                and not reaching.between(self._lefts[index].top, last.top))


def _fills_column_below(line: _Line, lower: list[_Line], reaching: "_RightReaching", left_end: float) -> bool:
    """Whether the left-column line right below `line` (with no other line in between; the first line of `lower`)
    fills the column width and wraps at that width onto the next line (the second line of `lower`, same left edge and
    gap at most `_WRAP_LINE_GAP_RATIO` times the line height) — the left-column text continues.

    Without checking that it wraps onto the next line, a single-column subheading and one-line paragraph below both
    columns were pulled up before the right column because that one line happened to be as long as the column width
    (below a LibreOffice two-column section, Pinned by: test_pdf.py::TestWidelySpacedColumns::
    test_one_line_paragraph_under_a_heading_below_both_columns_comes_after_the_right_column). If the single-column
    paragraph has two or more lines, its first line crosses both columns and is not a left-column line. Known accepted
    gap: if the paragraph under a subheading at the end of a page whose left column is longer has only one line, it is
    treated as text below both columns.
    """
    if len(lower) < 2 or lower[0].x1 <= left_end - lower[0].height:
        return False
    wrap_gap = lower[1].top - lower[0].bottom
    if abs(lower[1].x0 - lower[0].x0) > _TABLE_ROW_TOLERANCE or not 0 <= wrap_gap <= lower[0].height * _WRAP_LINE_GAP_RATIO:
        return False
    return not reaching.between(line.top, lower[0].top)


def _paragraph_columns(page: _Page) -> tuple[float, float, float] | None:
    """The (left column end, right column start, smaller font size of the pair of subheadings on top of short two
    columns — infinity for two columns not found through a subheading pair) of the two columns that
    `_read_paragraph_columns_in_turn` reorders. None if the conditions are not met.

    If the left column end passes the right column start, the measurement is repeated with only the lines within the
    height where the two columns are side by side (`_beside_each_other`). The short last line of a single-column
    summary paragraph above two columns was counted as a left-column line and the column verdict switched off (Pinned
    by: test_pdf.py::TestWidelySpacedColumns::
    test_short_last_line_of_a_summary_above_two_columns_does_not_hide_the_columns). A page that works with the first
    measurement is unchanged.

    Lines centred on the page (`_centred_on`; title, author affiliations) are not counted in either column from the
    start. A title slightly right of centre was counted as a right-column line, the left column re-measured with only
    the side-by-side height ended at the first section, and the sections of the short right column slipped in between
    the left column (section order 1 → 4 → 2 → 3, Pinned by: test_pdf_reading_order.py::TestCentredLinesOverTwoColumns).
    """
    wide = page.width * _TABLE_CELL_MAX_RATIO
    body = [line for line in page.lines if wide <= line.x1 - line.x0 <= page.width / 2 and not _centred_on(line, page.width)]
    lefts = [line for line in body if line.x0 + line.x1 < page.width]
    rights = [line for line in body if line.x0 + line.x1 >= page.width]
    if min(len(lefts), len(rights)) < 2:
        return None
    left_end, right_start = max(line.x1 for line in lefts), min(line.x0 for line in rights)
    if left_end >= right_start:
        lefts, rights = _beside_each_other(lefts, rights), _beside_each_other(rights, lefts)
        if min(len(lefts), len(rights)) < 2:
            return None
        left_end, right_start = max(line.x1 for line in lefts), min(line.x0 for line in rights)
        if left_end >= right_start:
            return None

    # Short two columns with few wide lines are columns only when there is a pair of subheadings on top. Such columns
    # are read column by column even if they were read paired line by line (even if they look like a row-wise layout)
    heading_size = float("inf")
    if max(len(lefts), len(rights)) < _COLUMN_LINES_MIN:
        heading_size = _headed_columns(page.lines, lefts, rights, left_end, right_start)
        if heading_size is None:
            return None
    headed = heading_size < float("inf")
    # Rows read across the gutter, a left line next to a right one (a row-wise '조건 | 지표' list). Pieces of one line within a
    # column (a reference split in two pieces) are no such row (Pinned by: test_pdf_reading_order.py::TestRightColumnReadFirst)
    pairs = sum(1 for previous, line in zip(page.lines, page.lines[1:], strict=False)
                if _side_by_side(previous, line) and _across_gutter(previous, line, left_end, right_start))
    if pairs >= _COLUMN_MIX_MIN and not headed:
        return None
    if (_has_lines_side_by_side([line for line in page.lines if line.x1 <= right_start])
            or _has_lines_side_by_side([line for line in page.lines if line.x0 >= left_end])):
        return None
    return left_end, right_start, heading_size


def _headed_columns(lines: list[_Line], lefts: list[_Line], rights: list[_Line], left_end: float,
                    right_start: float) -> float | None:
    """If two columns with fewer than `_COLUMN_LINES_MIN` wide lines (`lefts`, `rights` are the wide lines of each
    column) are short two columns under a pair of subheadings, the smaller font size of that pair; otherwise None.

    A short two-column section with 4 or fewer wide lines per column ('4.1 국내 | 4.2 해외') misses the column
    verdict, so after the subheadings of the two columns came out side by side, the lines of the two columns came out
    alternating line by line (LibreOffice and reportlab reports, Pinned by: test_pdf.py::TestWidelySpacedColumns::
    test_short_two_columns_under_level_headings_are_read_column_by_column). They are columns only when all of the
    following hold (Deliberate, conservative):
    - On top of the two columns there is a pair of subheadings side by side in the same row, both at least
      `_HEADING_MIN_RATIO` times larger than the body lines of the columns. Body-size subheadings cannot be told from
      the header row of a '조건 | 해당 지표' list and are not considered (Known accepted gap, Pinned by:
      test_short_two_columns_under_body_size_headings_are_left_as_read).
    - The column lines under the subheadings (for the left column, down to above a line crossing both columns) share
      the left edge of the subheading in each column, and the right column lines all start right below the subheading
      (within line height × 3).
    - In each column, lines that fill the column width (right edge within a line height of the longest line of the
      column) make up at least half of that column's lines — it is wrapped text. A list paired row by row has ragged
      line lengths (Pinned by:
      test_rows_of_a_condition_beside_its_indicator_under_a_larger_header_row_are_still_read_row_by_row).

    Instead of a subheading pair, a single subheading on top of the right column is also accepted — when it is at the
    very top of the page (no other line above) and the left column also starts at that height (a left-column
    paragraph carried over from the previous page). In the short two columns on the last page of a two-column paper,
    '참고문헌' (References) of the right column slipped in after the first line of the left column (Pinned by:
    test_pdf_reading_order.py::TestShortColumnsUnderOneHeading). The remaining conditions (left edges of the column
    lines, filling the column width) are the same.
    """
    def fills(column: list[_Line]) -> bool:
        end = max(line.x1 for line in column)
        return sum(1 for line in column if line.x1 >= end - line.height) * 2 >= len(column)

    size = max(line.size for line in (*lefts, *rights))
    for left, right in zip(lines, lines[1:], strict=False):
        if right.x0 < left.x0:
            left, right = right, left
        if not (_side_by_side(left, right) and left.x1 <= right_start and right.x0 >= left_end
                and min(left.size, right.size) >= size * _HEADING_MIN_RATIO):
            continue
        across = [line.top for line in lines if line.top > left.bottom and line.x0 < left_end and line.x1 > right_start]
        under = [line for line in lefts if left.bottom - _TABLE_ROW_TOLERANCE <= line.top < min(across, default=float("inf"))]
        if (len(under) >= 2 and all(line.top >= right.bottom - _TABLE_ROW_TOLERANCE for line in rights)
                and min(line.top for line in rights) - right.bottom <= right.height * _COLUMN_CONTINUE_RATIO
                and all(abs(line.x0 - left.x0) <= _TABLE_ROW_TOLERANCE for line in under)
                and all(abs(line.x0 - right.x0) <= _TABLE_ROW_TOLERANCE for line in rights)
                and fills(under) and fills(rights)):
            return min(left.size, right.size)

    # Top of a page where only the right column starts with a subheading (the left column is a paragraph carried over
    # from the previous page): the subheading is at the very top of the page and the left column also starts at that
    # height. The subheading only needs to be larger than the right-column body (a reference list is set in smaller
    # type than the body)
    heads = [line for line in lines if line.x0 >= left_end and line.size >= max(other.size for other in rights) * _HEADING_MIN_RATIO]
    right = min(heads, key=lambda line: line.top, default=None)
    if (right is not None and len(lefts) >= 2 and all(line.top >= right.top - right.height for line in lines)
            and min(line.top for line in lines if line.x1 <= right_start) <= right.bottom
            and all(line.top >= right.bottom - _TABLE_ROW_TOLERANCE for line in rights)
            and min(line.top for line in rights) - right.bottom <= right.height * _COLUMN_CONTINUE_RATIO
            and all(abs(line.x0 - right.x0) <= _TABLE_ROW_TOLERANCE for line in rights)
            and all(abs(line.x0 - lefts[0].x0) <= _TABLE_ROW_TOLERANCE for line in lefts)
            and fills(lefts) and fills(rights)):
        return right.size
    return None


def _heading_below_headed_columns(lines: list[_Line], left_end: float, right_start: float, heading_size: float) -> float:
    """The height at which a single-column heading below short two columns found through a subheading pair
    (`_headed_columns`) starts. Infinity if there is none.

    Among the left-column-side lines below the end of the right column, a line as large as the subheadings of the two
    columns (within 0.5pt) is the next section, not a continuation of the left column — '5. 결론' below short two
    columns with a longer left column came out before the right column (Pinned by: test_pdf.py::TestWidelySpacedColumns::
    test_heading_below_short_two_columns_with_the_longer_left_column_comes_after_the_right_column). Pages of ordinary
    two columns with a longer left column (the bottom condition of `_below_both_columns`) are not changed — without a
    subheading pair on top of the columns there is no basis for telling whether a line of that size is a subheading
    inside the left column or the next section.
    """
    right_bottom = max((line.bottom for line in lines if line.x0 >= left_end), default=float("inf"))
    return min((line.top for line in lines if line.x1 <= right_start and line.top >= right_bottom
                and line.size >= heading_size - 0.5), default=float("inf"))


def _beside_each_other(lines: list[_Line], others: list[_Line]) -> list[_Line]:
    """The lines of `lines` that lie within the height from the top to the bottom of `others`."""
    top, bottom = min(line.top for line in others), max(line.bottom for line in others)
    return [line for line in lines if line.top >= top - _TABLE_ROW_TOLERANCE and line.bottom <= bottom + _TABLE_ROW_TOLERANCE]


def _has_lines_side_by_side(lines: list[_Line]) -> bool:
    """Whether `lines` contains lines placed side by side in the same row on `_SIDE_BY_SIDE_ROWS_MIN` rows or more (a table
    in the column).

    One row is not enough: a single line of a reference list drawn in two pieces ('human feedback without tears.' and
    'arXiv preprint' on one row) kept a two-column page that pdfminer read right column first from being reordered
    (Pinned by: test_pdf_reading_order.py::TestRightColumnReadFirst)."""
    lines = sorted(lines, key=lambda line: line.top)
    rows: list[_Line] = []  # the first line of each row found (pieces of one row count once)
    for index, line in enumerate(lines):
        if rows and _side_by_side_row(rows[-1], line):
            continue
        for other in lines[index + 1:]:
            if other.top >= line.bottom:
                break
            if _side_by_side(line, other):
                rows.append(line)
                if len(rows) >= _SIDE_BY_SIDE_ROWS_MIN:
                    return True
                break
    return False


def _across_gutter(first: _Line, second: _Line, left_end: float, right_start: float) -> bool:
    """Whether one of two lines lies in the left column and the other in the right one."""
    sides = [(line.x1 <= right_start, line.x0 >= left_end) for line in (first, second)]
    return {side for side in sides if side[0] != side[1]} == {(True, False), (False, True)}


def _side_by_side_row(first: _Line, second: _Line) -> bool:
    """Whether two lines are in the same row (vertical overlap at least half the smaller line height)."""
    return min(first.bottom, second.bottom) - max(first.top, second.top) >= min(first.height, second.height) * 0.5


def _side_by_side(first: _Line, second: _Line) -> bool:
    """Whether two lines are in the same row without overlapping horizontally (vertical overlap at least half the
    smaller line height)."""
    overlap = min(first.bottom, second.bottom) - max(first.top, second.top)
    return overlap >= min(first.height, second.height) * 0.5 and (second.x0 >= first.x1 or first.x0 >= second.x1)


def _continues_column(column: list[_Line], lines: list[_Line], aligned: bool) -> bool:
    """Whether `lines` continue right below the column (`column`). With `aligned`, the left edge must match the column too."""
    if not column:
        return False

    gap = lines[0].top - max(line.bottom for line in column)
    if not 0 <= gap <= max(column[-1].height, lines[0].height) * _COLUMN_CONTINUE_RATIO:
        return False
    return not aligned or abs(min(line.x0 for line in lines) - min(line.x0 for line in column)) <= _TABLE_ROW_TOLERANCE


def _read_alternating_sides_in_turn(page: _Page) -> None:
    """Where right-aligned lines sit in between left lines but were read separately like columns (or jumbled), put the
    lines back in top-to-bottom order.

    In a conversation log (left remarks, right-aligned replies), a right-aligned presenter on the line under an agenda
    item, or a right-aligned '※' note between lines, the meaning lies in the top-to-bottom order, but pdfminer's text
    box flow (`boxes_flow`) saw two columns and read all the left lines before the right lines, or jumbled the two
    (Pinned by: test_pdf.py::TestRightAlignedLinesBetweenLines). The conditions are kept narrow (Deliberate) — two
    side-by-side columns or text boxes are not touched (`_alternating_window`).
    Known accepted gap: places with only one right-aligned line, and places where the right lines all have the same
    width (their left edges match too, so they cannot be told from side-by-side text boxes), stay as they are.
    The windows a page looks at take at most `_ALTERNATING_WORK` lines in all; the turn-backs after that stay as read.
    Every turn-back of a page whose lines alternate between two sides that are no right-aligned group grows its window
    over all of them again, so 20,000 such lines took 20 seconds here, of 21 for their 56 KB page (Pinned by:
    test_pdf_reading_order.py::TestManyLinesBesideColumns).
    """
    if page.simple or page.two_pages:
        return
    lines = page.lines  # Modified in place
    index = 1
    work = [_ALTERNATING_WORK]  # lines the windows may still take (`_alternating_window`)

    while index < len(lines) and work[0] > 0:
        window = _alternating_window(lines, index, work) if lines[index].top < lines[index - 1].top else None
        if window is None:
            index += 1
            continue
        start, end = window
        lines[start:end] = sorted(lines[start:end], key=lambda line: line.top)
        index = end


def _alternating_window(lines: list[_Line], index: int, work: list[int]) -> tuple[int, int] | None:
    """The range of lines [start, end) to put back in top-to-bottom order, containing the place (`index`) where the
    reading order went back up. None if the conditions are not met.

    - Of the two lines at the turn-back, the one whose right edge is further right is the reference (`edge`) of the
      right-aligned group. The lines of the range are only lines whose right edge matches `edge` and whose left edge is
      at least a line height to the right of the reference left line (right group), or lines whose right edge is to
      the left of `edge` (left group). It stops at a page-width line (starting at the left and ending at `edge`). On
      the side before the turn-back it is extended only while the lines continue top to bottom.
    - The right group has 2 or more lines and ragged left edges (differing by a line height or more). Column body text
      has matching left edges.
    - At least one line of the left group lies at a height between the lines of the right group (alternating). If the
      right group is one block only above or below, they are side-by-side text boxes — the table of contents on a
      cover (left-aligned items under '【목차】'), which looked ragged because of a single centred heading, was mixed
      top to bottom and the team name and date on the left slipped in between the contents (Pinned by:
      test_pdf.py::TestRightAlignedLinesBetweenLines::
      test_left_aligned_list_under_a_centred_heading_is_not_taken_for_right_aligned_lines).
    - The left edges of the left group are all to the left of the right group, and no two lines in the range overlap
      vertically. In the body of two side-by-side columns the lines sit in the same row or the gap between lines is
      narrower than the line height, so they overlap somewhere.
    """
    pair = (lines[index - 1], lines[index])
    seed_right = max(pair, key=lambda line: line.x1)
    seed_left = pair[0] if seed_right is pair[1] else pair[1]
    edge, height = seed_right.x1, seed_right.height
    if seed_left.x1 >= edge - _TABLE_ROW_TOLERANCE or seed_left.x0 + height > seed_right.x0:
        return None

    def side(line: _Line) -> bool | None:  # Right group True, left group False, neither None
        if abs(line.x1 - edge) <= _TABLE_ROW_TOLERANCE:
            return True if line.x0 >= seed_left.x0 + height else None
        return False if line.x1 < edge else None

    start, end = index - 1, index + 1

    while start > 0 and lines[start - 1].top < lines[start].top and side(lines[start - 1]) is not None:
        if end - start >= work[0]:  # the page's work is spent (`_ALTERNATING_WORK`)
            work[0] = 0
            return None
        start -= 1
    while end < len(lines) and side(lines[end]) is not None:
        if end - start >= work[0]:
            work[0] = 0
            return None
        end += 1
    work[0] -= end - start

    window = lines[start:end]
    right = [line for line in window if side(line)]
    left = [line for line in window if not side(line)]
    if len(right) < 2 or max(line.x0 for line in right) - min(line.x0 for line in right) < height:
        return None
    first, last = min(line.top for line in right), max(line.top for line in right)
    if not any(first < line.top < last for line in left):
        return None
    if max(line.x0 for line in left) >= min(line.x0 for line in right):
        return None
    ordered = sorted(window, key=lambda line: line.top)
    if any(below.top < above.bottom for above, below in zip(ordered, ordered[1:], strict=False)):
        return None
    return start, end


def _read_lone_lines_in_place(page: _Page) -> None:
    """In a single-column flow, move a line that was read after the lines below it up to the position of its height.

    pdfminer's text box flow (`boxes_flow`) sent a short centred caption after the left-aligned paragraph right below
    it (or after '끝.' below a table), and since a table or figure is inserted before 'the first paragraph starting
    below it', it was pushed along with it (Pinned by: test_pdf_reading_order.py::
    TestCentredCaptionOnOneColumnPage). When the consecutive lines right before a line in reading order all lie below
    that line, it is moved to the front of those lines. To tell this from a place where the flow moved on to another
    column, all of the following are checked (Deliberate, conservative):
    - Neither the line to move nor the lines to skip have another line, table or figure beside them (same row, apart
      horizontally). Lines of two side-by-side columns have a line of the other column beside them.
      A figure beside the line to move is fine — text to the right of a figure was pushed after the subheading and
      table below in the left margin. A caption right below a figure (`_is_figure_caption`) also skips lines to skip
      that have a line beside them (a row of side-by-side cards) (Pinned by: test_pdf_pictures_beside_text.py).
    - The line to move and the lines to skip are one column flow (`_one_flow`).
    Known accepted gap: a page where a group of left-aligned text and a group of tables/captions on the right do not
    overlap and nothing connects them is seen as two columns and left alone. Pages with more than
    `_LONE_LINE_MAX_LINES` lines are also left alone.

    First, a row of lines side by side right above tables (a caption row, `_table_caption_rows`) is gathered left to
    right at the position of the row's first line in reading order, and counts as lines that the line to move may
    skip (even with a line beside them). When two sets of side-by-side captioned tables follow one above the other,
    pdfminer read the left captions and the right captions separately like columns, so a right caption went after the
    subheading of the next section and a table was placed after the wrong caption (Pinned by:
    test_pdf_reading_order.py::TestCaptionsOverSideBySideTables).

    Separately, a line (the page title) that lies alone above every line of the page (no line, table or figure beside
    it), is at least `_HEADING_MIN_RATIO` times the median font size of the page's lines, and spans the page centre or
    starts at the leftmost edge of the page's lines is put first. A centred title of a payslip or two-column document
    was pushed after the left text box and came out after the items, amounts or left column (Pinned by:
    test_pdf_forms_and_amounts.py::TestFarRightAmounts). A subheading at the top spanning only the right column (two
    columns whose left column starts lower) is not moved.
    """
    if page.simple or not 2 <= len(page.lines) <= _LONE_LINE_MAX_LINES:
        return
    lines = page.lines  # Modified in place
    tables = [(table.top, table.bottom, table.x0, table.x1) for table in page.tables]
    figures = [(top, bottom, x0, x1) for x0, top, x1, bottom, _ in page.figures]
    blocks = tables + figures
    crowded = _lines_with_neighbours(lines, blocks)
    beside_text = _lines_with_neighbours(lines, tables)  # A line with only a figure beside it (text right of a figure) may be moved

    # Gather the caption rows above tables
    captions = set()
    for row in _table_caption_rows(lines, tables):
        members = {id(line) for line in row}
        first = next(index for index, line in enumerate(lines) if id(line) in members)
        lines[:] = [line for line in lines if id(line) not in members]
        lines[first:first] = row
        captions |= members

    for index in range(1, len(lines)):
        line = lines[index]
        start = index
        caption = _is_figure_caption(line, figures)

        while (start > 0 and _lies_below(lines[start - 1], line)
               and (caption or id(lines[start - 1]) not in crowded or id(lines[start - 1]) in captions)):
            start -= 1
        if start == index or id(line) in beside_text or (start > 0 and _lies_below(lines[start - 1], line)):
            continue
        if _centred_on(line, page.width) or _one_flow(line, lines[start:index], lines, blocks):
            lines.insert(start, lines.pop(index))

    # Large text alone at the very top of the page (the title) is put first. The condition above does not move it when
    # a line to skip has a line beside it (name and department in one row)
    top = min(lines, key=lambda line: line.top)
    sizes = [line.size for line in lines if line.size]
    spans_middle = top.x0 < page.width / 2 < top.x1 or top.x0 <= min(line.x0 for line in lines) + _TABLE_ROW_TOLERANCE
    if (lines[0] is not top and id(top) not in crowded and spans_middle and sizes
            and top.size >= statistics.median(sizes) * _HEADING_MIN_RATIO
            and all(_lies_below(other, top) for other in lines if other is not top)):
        lines.insert(0, lines.pop(lines.index(top)))


def _read_sidebar_first(page: _Page) -> None:
    """On a page where the left sidebar was read after the whole right body column, put the sidebar first if the page
    title is in the sidebar.

    On a page that sets a left 30% sidebar (name, contact details) beside the body on the right, like a résumé, the
    sidebar lines are narrower than a table cell and do not enter the two-column verdict, and pdfminer read body →
    sidebar, so the name came out at the end of the page (Pinned by: test_pdf_reading_order.py::TestSidebarBesideTheBody).
    Kept narrow (Deliberate): only when the reading order splits cleanly into 'all right-column lines → all left-column
    lines' (if mixed, that is for other rules), the two columns are apart horizontally at side-by-side heights with 3
    or more lines each, and the largest text on the page is in the left column (at least `_HEADING_MIN_RATIO` times
    the largest text of the right column). Pages with tables or figures are not touched because their positions hang
    on the line order. Known accepted gap: a sidebar layout whose page title is in the body column (notes in the left
    margin) is still body → sidebar.
    """
    lines = page.lines
    if page.simple or page.tables or page.figures or len(lines) < 6:
        return
    split = next((index for index in range(1, len(lines)) if lines[index].x0 < lines[index - 1].x0 - _TABLE_ROW_TOLERANCE), None)
    if split is None or split < 3 or len(lines) - split < 3:
        return
    right, left = lines[:split], lines[split:]
    if max(line.x1 for line in left) >= min(line.x0 for line in right):
        return
    if not (min(line.top for line in left) < max(line.bottom for line in right)
            and min(line.top for line in right) < max(line.bottom for line in left)):
        return
    if max(line.size for line in left) >= max(line.size for line in right) * _HEADING_MIN_RATIO:
        page.lines = [*left, *right]


def _table_caption_rows(lines: list[_Line], tables: list[tuple[float, float, float, float]]) -> list[list[_Line]]:
    """The rows of lines placed side by side right above tables (top, bottom, x0, x1), left to right within a row: two
    or more lines at the same height all lie right above some table (gap at most `_CAPTION_GAP_RATIO` times the line
    height), overlapping that table horizontally, with no line continuing right above them (gap at most the line
    height) — the last row of a label-value group above a table ('작성 부서 인재개발팀 / 승인자 박본부장') is not a
    caption. The extent of a borderless table is its cell text, so a caption sticks out a few pt to the left.
    Lines over different tables (captions of side-by-side tables) are a caption row only when each is the last line
    above its table — no line below it in its own column (overlapping it horizontally) before the table starts. The
    first line of a two-line paragraph at the top of the right column, right above a table there, made a row with a
    left-column heading over a table at its height and was pulled into the left column ahead of the rest of it (Pinned
    by: test_pdf_reading_order.py::TestCaptionsOverSideBySideTables::
    test_first_line_of_a_paragraph_above_a_table_is_not_a_caption). Lines over one table may each go on below (the
    first lines of header cells set on several lines), as before."""
    if not tables:
        return []

    def captioned(line: _Line) -> list[tuple[int, bool]]:
        """The tables the line lies right above, each with whether it is the last line above that table."""
        if any(other.x0 < line.x1 and line.x0 < other.x1 and 0 <= line.top - other.bottom <= line.height for other in lines):
            return []
        return [(number, not any(other is not line and other.x0 < line.x1 and line.x0 < other.x1
                                 and line.bottom - 1 <= other.top and other.bottom <= top + 1 for other in lines))
                for number, (top, _, x0, x1) in enumerate(tables)
                if -1 <= top - line.bottom <= line.height * _CAPTION_GAP_RATIO and line.x0 < x1 and x0 < line.x1]

    def is_caption_row(row: list[_Line]) -> bool:
        found = [captioned(line) for line in row]
        if not all(found):
            return False
        if set.intersection(*({number for number, _ in tables_of} for tables_of in found)):
            return True  # all over one table
        return all(any(last for _, last in tables_of) for tables_of in found)

    return [row for row in _visual_rows(lines) if len(row) >= 2 and is_caption_row(row)]


def _is_figure_caption(line: _Line, figures: list[tuple[float, float, float, float]]) -> bool:
    """Whether the line is a caption placed right below a figure (top, bottom, x0, x1) (gap at most the line height),
    within the figure's horizontal extent."""
    return any(-1 <= line.top - bottom <= line.height and x0 - _TABLE_ROW_TOLERANCE <= line.x0 and line.x1 <= x1 + _TABLE_ROW_TOLERANCE
               for _, bottom, x0, x1 in figures)


def _centred_on(line: _Line, width: float) -> bool:
    """Whether the line is centred on the page (its centre within `_CENTRED_TOLERANCE_RATIO` of the page width from
    the page centre, its width at most half the page width).

    A centred caption or title is not a line of either column of a two-column page — it spans the gap between the
    columns. On a page with no line or figure linking a left-aligned subheading and a centred caption (a figure frame
    drawn as a border only) the caption was pushed to the end of the page (Pinned by: test_pdf_reading_order.py::
    TestCentredCaptionOnOneColumnPage::test_centred_lines_with_nothing_linking_them_to_the_left_aligned_text_stay_in_place).
    """
    return line.x1 - line.x0 <= width / 2 and abs((line.x0 + line.x1) / 2 - width / 2) <= width * _CENTRED_TOLERANCE_RATIO


def _lies_below(other: _Line, line: _Line) -> bool:
    """Whether `other` lies below `line` (an overlap of up to 1pt is tolerated)."""
    return other.top >= line.bottom - 1.0


def _lines_with_neighbours(lines: list[_Line], blocks: list[tuple[float, float, float, float]]) -> set[int]:
    """The ids of the lines that have another line or a table/figure (top, bottom, x0, x1) beside them (same row,
    apart horizontally)."""
    ordered = sorted(lines, key=lambda line: line.top)
    crowded = set()

    for index, line in enumerate(ordered):
        for other in ordered[index + 1:]:
            if other.top >= line.bottom:
                break
            if _side_by_side(line, other):
                crowded.update((id(line), id(other)))

    for line in lines:
        if any(top < line.bottom and line.top < bottom and (x0 >= line.x1 or line.x0 >= x1) for top, bottom, x0, x1 in blocks):
            crowded.add(id(line))
    return crowded


def _one_flow(line: _Line, below: list[_Line], lines: list[_Line], blocks: list[tuple[float, float, float, float]]) -> bool:
    """Whether `line` and the lines lying below it (`below`) are one column flow (`_read_lone_lines_in_place`). They
    are if they overlap horizontally.

    If they are apart, all of the following are checked. (1) On the far side from `below`, `line` is alone within the
    height range of `below` (several lines, like a table of contents on the right of a cover, are a text box), or some
    line, table or figure (top, bottom, x0, x1) on the page covers the gap between the two or overlaps both `line` and
    what is across the gap (single-column body text, a centred figure). (2) Nothing on the two sides of the gap
    overlaps vertically (on a two-column page whose right column holds only a figure and caption, the figure is beside
    the left column). (3) The lines of one column on the other side do not alternate right above and below a line on
    one side (`_interleaved`, two side-by-side text boxes offset by half a line).
    """
    left, right = min(other.x0 for other in below), max(other.x1 for other in below)
    if line.x0 < right and left < line.x1:
        return True

    gap = (right, line.x0) if line.x0 >= right else (line.x1, left)
    spans = [(other.top, other.bottom, other.x0, other.x1) for other in lines] + blocks
    west = [span for span in spans if span[3] <= gap[0] + _TABLE_ROW_TOLERANCE]
    east = [span for span in spans if span[2] >= gap[1] - _TABLE_ROW_TOLERANCE]
    far = west if line.x0 >= right else east  # The side of `below`
    beyond = [span for span in spans if (span[2] >= right - _TABLE_ROW_TOLERANCE if line.x0 >= right
                                         else span[3] <= left + _TABLE_ROW_TOLERANCE)]  # Everything on the far side from `below`
    bottom = max(other.bottom for other in below)
    alone = sum(1 for top, end, _, _ in beyond if top < bottom and line.top < end) <= 1

    # Something bridges the gap: it covers the gap, or it overlaps both `line` and something on the far side
    covers = any(x0 <= gap[0] and gap[1] <= x1 for _, _, x0, x1 in spans)
    if not alone and not covers and not _any_overlap([(x0, x1) for _, _, x0, x1 in spans if x0 < line.x1 and line.x0 < x1],
                                                     [(x0, x1) for _, _, x0, x1 in far]):
        return False
    if _any_overlap([(top, end) for top, end, _, _ in east], [(top, end) for top, end, _, _ in west]):
        return False
    west_lines = [other for other in lines if other.x1 <= gap[0] + _TABLE_ROW_TOLERANCE]
    east_lines = [other for other in lines if other.x0 >= gap[1] - _TABLE_ROW_TOLERANCE]
    return not (_interleaved(east_lines, west_lines) or _interleaved(west_lines, east_lines))


def _interleaved(lines: list[_Line], others: list[_Line]) -> bool:
    """Whether `lines` contains a line with lines of `others` close in height (`_is_beside`) both right above and
    right below it, and those two lines share a left edge (lines of one column) — two text boxes alternating, offset by
    half a line. Lines above and below with different left edges are centred captions.

    `others` is sorted by vertical centre once, and each line looks only at those whose centre can be close enough
    (the tallest line bounds `_is_beside`'s reach); the left edges are matched by their nearest neighbours in order.
    Comparing every line with every other line, for each line `_read_lone_lines_in_place` tries to move, made that pass
    cubic in a page's lines: 480 lines took 7 seconds, outside the page time budget; now 0.23 (Pinned by:
    test_pdf_reading_order.py::TestManyLinesLiftedInPlace). With a coordinate that is not a finite number every pair is
    compared as before."""
    if not lines or not others:
        return False
    if not all(math.isfinite(value) for line in (*lines, *others) for value in (line.top, line.bottom, line.x0, line.x1)):
        return any(_interleaved_at(line, [other for other in others if _is_beside(other, line)]) for line in lines)
    by_centre = sorted(others, key=lambda other: other.top + other.bottom)
    centres = [other.top + other.bottom for other in by_centre]
    tallest = max(other.height for other in others)
    for line in lines:
        middle = line.top + line.bottom
        reach = 2 * max(tallest, line.height) * _COLUMN_MIX_NEAR + 1.0  # wider than `_is_beside` allows; it decides
        near = [other for other in by_centre[bisect_left(centres, middle - reach):bisect_right(centres, middle + reach)]
                if _is_beside(other, line)]
        if _interleaved_at(line, near):
            return True
    return False


def _interleaved_at(line: _Line, near: list[_Line]) -> bool:
    """Whether a line of `near` right above `line` and one right below it share a left edge (`_interleaved`)."""
    middle = line.top + line.bottom
    above = [other.x0 for other in near if other.top + other.bottom < middle]
    below = sorted(other.x0 for other in near if other.top + other.bottom > middle)
    for upper in above:
        # The left edge closest to `upper` is one of its two neighbours in order
        index = bisect_left(below, upper)
        if any(abs(upper - lower) <= _TABLE_ROW_TOLERANCE for lower in below[max(index - 1, 0):index + 1]):
            return True
    return False


def _any_overlap(spans: list[tuple[float, float]], others: list[tuple[float, float]]) -> bool:
    """Whether a (low, high) of `spans` and one of `others` overlap (low < other high and other low < high).

    `others` is sorted by low end with the running maximum of high ends, so each span looks up the others starting before its
    high end at once (`_one_flow` compared every pair, for each line `_read_lone_lines_in_place` tries to move). With a value
    that is not a finite number every pair is compared."""
    if not spans or not others:
        return False
    if not all(math.isfinite(value) for pair in (*spans, *others) for value in pair):
        return any(low < other_high and other_low < high for low, high in spans for other_low, other_high in others)
    ordered = sorted(others)
    lows = [low for low, _ in ordered]
    reach = list(accumulate((high for _, high in ordered), max))
    for low, high in spans:
        count = bisect_left(lows, high)  # the others whose low end is below `high`
        if count and reach[count - 1] > low:
            return True
    return False


def _is_beside(previous: _Line, line: _Line) -> bool:
    """Whether two lines do not overlap horizontally and are close in height (vertical centres differ by at most
    `_COLUMN_MIX_NEAR` times the line height)."""
    apart = line.x0 >= previous.x1 or previous.x0 >= line.x1
    distance = abs((previous.top + previous.bottom) - (line.top + line.bottom)) / 2
    return apart and distance <= max(previous.height, line.height) * _COLUMN_MIX_NEAR


def _is_letter_spaced(row: list[_Line]) -> bool:
    """A row of single Hangul/Hanja character fragments of the same size at even intervals (a letter-spaced title)."""
    if len(row) < 2 or not all(len(line.text) == 1 and _SPACED_CHAR.match(line.text) for line in row):
        return False
    if max(line.size for line in row) - min(line.size for line in row) > 0.5:
        return False
    gaps = [right.x0 - left.x1 for left, right in zip(row, row[1:], strict=False)]
    return min(gaps) > 0 and max(gaps) <= min(gaps) * _SPACED_GAP_SPREAD + 1
