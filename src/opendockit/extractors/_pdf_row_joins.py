"""Pieces of one row drawn apart: fragments of a row joined, equation numbers, leader dots between a title and its page
number, list markers, and pairs of lines that stand side by side.
"""

from bisect import bisect_left, bisect_right
import re
import statistics

from opendockit.extractors._pdf_label_values import _label_value_rows, _near
from opendockit.extractors._pdf_model import (
    _BARE_NUMBER,
    _LONE_BULLET,
    _MARKER_GAP_RATIO,
    _TABLE_CELL_MAX_RATIO,
    _TABLE_MIN_COLUMNS,
    _TABLE_MIN_ROWS,
    _TABLE_ROW_TOLERANCE,
    _Line,
    _Page,
    _visual_rows,
)
from opendockit.extractors._pdf_reading_order import _is_letter_spaced
from opendockit.extractors._pdf_table_rows import _has_one_wide_cell_at_most, _is_separate


# List number placed apart from the item text ('1.', '2)', '(3)', '가.', 'IV.', '①'). A bare number ('1') is only ever taken as a
# footnote number (`_marker_rows`).
_LIST_NUMBER = re.compile(r"^(?:\(?(?:\d{1,3}|[가나다라마바사아자차카타파하]|[A-Za-z]|[IVX]{2,5}|[ivx]{2,5})[.)]"
                          r"|\((?:\d{1,3}|[가나다라마바사아자차카타파하])\)|[①-⑳Ⅰ-Ⅻ])$")


_EQUATION_NUMBER = re.compile(r"^\(\d{1,3}(?:[.\-]\d{1,3})?[a-z]?\)$")  # equation number '(1)', '(2.3)', '(4a)'


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
