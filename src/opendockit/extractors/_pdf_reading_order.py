"""Reading order where the layout analysis mixes it: two columns read line by line, right-aligned lines between left ones,
a lone centred line pushed behind lower text, sidebars and captions.
"""

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
# Each candidate to move looks at the page's line pairs, so it gets slow on a page of scattered text pieces. A body page usually has
# around 100 lines.
_LONE_LINE_MAX_LINES = 500
# Difference between centres (relative to the page width, `_centred_on`) for a line to count as placed in the middle of the page.
_CENTRED_TOLERANCE_RATIO = 0.03


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
    test_heading_and_paragraph_closing_the_longer_left_column_stay_in_the_left_column).
    """
    rights = [line for line in lines if line.x0 >= left_end]
    if not rights:
        return float("inf")

    right_bottom = max(line.bottom for line in rights)
    lefts = sorted((line for line in lines if line.x1 <= right_start), key=lambda line: line.top)
    for index, line in enumerate(lefts):
        above = lefts[:index]
        if not above or line.top < right_bottom - 1 or line.x1 > left_end - line.height:
            continue
        gaps = [lower.top - upper.bottom for upper, lower in zip(above, above[1:], strict=False) if lower.top > upper.bottom]
        gap = line.top - max(other.bottom for other in above)
        left_bottom = max([other.bottom for other in above]
                          + [bottom for x1, bottom in blocks if x1 <= right_start and bottom <= line.top])
        if (line.x0 <= min(other.x0 for other in above) + _TABLE_ROW_TOLERANCE
                and gap >= max(line.height * 0.5, statistics.median(gaps) * 2 if gaps else 0.0)
                and right_bottom >= left_bottom - line.height and not _fills_column_below(line, lefts[index + 1:], lines, left_end)):
            return line.top
    return float("inf")


def _fills_column_below(line: _Line, lower: list[_Line], lines: list[_Line], left_end: float) -> bool:
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
    return not any(line.top < other.top < lower[0].top and other.x1 > left_end for other in lines)


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
    pairs = sum(1 for previous, line in zip(page.lines, page.lines[1:], strict=False) if _side_by_side(previous, line))
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
    """Whether `lines` contains two lines placed side by side in the same row."""
    lines = sorted(lines, key=lambda line: line.top)
    for index, line in enumerate(lines):
        for other in lines[index + 1:]:
            if other.top >= line.bottom:
                break
            if _side_by_side(line, other):
                return True
    return False


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
    """
    if page.simple:
        return
    lines = page.lines  # Modified in place
    index = 1

    while index < len(lines):
        window = _alternating_window(lines, index) if lines[index].top < lines[index - 1].top else None
        if window is None:
            index += 1
            continue
        start, end = window
        lines[start:end] = sorted(lines[start:end], key=lambda line: line.top)
        index = end


def _alternating_window(lines: list[_Line], index: int) -> tuple[int, int] | None:
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
        start -= 1
    while end < len(lines) and side(lines[end]) is not None:
        end += 1

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
    caption. The extent of a borderless table is its cell text, so a caption sticks out a few pt to the left."""
    if not tables:
        return []

    def is_caption(line: _Line) -> bool:
        return (any(-1 <= top - line.bottom <= line.height * _CAPTION_GAP_RATIO and line.x0 < x1 and x0 < line.x1
                    for top, _, x0, x1 in tables)
                and not any(other.x0 < line.x1 and line.x0 < other.x1 and 0 <= line.top - other.bottom <= line.height
                            for other in lines))

    return [row for row in _visual_rows(lines) if len(row) >= 2 and all(is_caption(line) for line in row)]


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

    def bridges(x0: float, x1: float) -> bool:
        if x0 <= gap[0] and gap[1] <= x1:
            return True
        return x0 < line.x1 and line.x0 < x1 and any(x0 < other_x1 and other_x0 < x1 for _, _, other_x0, other_x1 in far)

    if not alone and not any(bridges(x0, x1) for _, _, x0, x1 in spans):
        return False
    if any(top < other_bottom and other_top < end for top, end, _, _ in east for other_top, other_bottom, _, _ in west):
        return False
    west_lines = [other for other in lines if other.x1 <= gap[0] + _TABLE_ROW_TOLERANCE]
    east_lines = [other for other in lines if other.x0 >= gap[1] - _TABLE_ROW_TOLERANCE]
    return not (_interleaved(east_lines, west_lines) or _interleaved(west_lines, east_lines))


def _interleaved(lines: list[_Line], others: list[_Line]) -> bool:
    """Whether `lines` contains a line with lines of `others` close in height (`_is_beside`) both right above and
    right below it, and those two lines share a left edge (lines of one column) — two text boxes alternating, offset by
    half a line. Lines above and below with different left edges are centred captions."""
    for line in lines:
        middle = line.top + line.bottom
        near = [other for other in others if _is_beside(other, line)]
        above = [other.x0 for other in near if other.top + other.bottom < middle]
        below = [other.x0 for other in near if other.top + other.bottom > middle]
        if any(abs(upper - lower) <= _TABLE_ROW_TOLERANCE for upper in above for lower in below):
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
