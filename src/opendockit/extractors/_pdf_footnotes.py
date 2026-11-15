"""Footnotes of a PDF page: the marks in the body linked to the notes under the footnote rule, only where the page allows a
single reading.

A PDF has no footnotes, only small raised numbers and small lines at the bottom of the page. Left as they are, the mark reads like
an exponent ('135^1%' — 135 to the power of 1, or a footnote?) and nothing says which note it points to. Linking them is a guess
unless the page proves it, and a wrong note attached to a sentence misleads more than an unlinked one. So a page is linked only
when every piece of evidence agrees, and otherwise it is left exactly as it was (`_link_footnotes`):

1. A footnote rule: a short horizontal rule (`_RULE_MIN_LENGTH`, at most `_RULE_MAX_SHARE` of the page width, not inside a
   table) with the notes right under it, starting at its left end. Word, LibreOffice and LaTeX draw one.
2. The notes: every line under the rule down to the first gap, in a smaller size than the body (`_NOTE_SIZE_RATIO`), with no
   other line between them and no further note after the gap. A note starts with its number raised (`^1`, `^{1)}`); after the
   first note no line starts with another kind of mark (`*`, `†`, a raised letter).
3. The numbers of the notes on the page run on without a gap (3, 4, 5), and start at 1 or higher than the last number of the
   previous page that had notes.
4. The marks: raised numbers in the body (`^1`, `^{12}`, `^{1)}`). Each note number is given to one mark, in reading order, and
   there is exactly one way to do that. A raised number that no note has ('m^2' after the last mark) stays as it is; a second
   way of choosing ('m^2' between the marks 1 and 3 when note 2 exists) leaves the whole page unlinked.
5. Nothing else on the page could be a mark the body does not show: every digit raised by its size and position is shown raised
   in the line text, and no digit in the table cells or the sideways text is clearly smaller than the text around it (scripts
   are not marked there) (`_Page.unshown_marks`); no note number is in another raised group ('^{2,3}', '^{1-3}'), in a
   raised group of a table, in a raised number at the start of a line, in a small line holding only a number, or right after
   two letters ('Sales2'), and the previous page left no mark with a note number unlinked, raised or hidden, and is known
   (not read the simple way, no raised digits it does not show) — its note may be on this page; and no '^' character is
   drawn on the page (a typed '^1' cannot be told apart from a raised 1). A
   mark raised without being made smaller is not seen as raised (Word, LibreOffice and LaTeX make it smaller).

A linked page writes the mark as `[^n]` and the note as `[^n]: text` in its place at the bottom of the page — the page stays one
section with its notes, and nothing is moved or removed. The label is the printed number; a number that is linked on more than
one page (numbering that starts again in each chapter) is written with its page (`[^p12-1]`) so that the labels of the whole
document stay unique.
"""

from collections import Counter
from dataclasses import dataclass
import re
import statistics

from opendockit.extractors._pdf_chars import _RAISED_GROUP, _group_text
from opendockit.extractors._pdf_model import _Line, _Page


# A footnote rule is at least this long (pt) and at most this share of the page width. Word draws 144pt (a quarter of an A4
# page), LibreOffice a quarter of the text width, LaTeX 0.4 of the column by default — all under 0.35 of the page. A rule across
# the text (0.7 of the page and more) is a section divider or the border of a box. The page, not the text, is the measure: a
# page with a few short lines has a narrow text (Pinned by: test_pdf_footnotes.py::TestLeftAsIs::test_short_dash_is_not_a_footnote_rule,
# test_rule_across_the_whole_text_is_not_a_footnote_rule; a rule that is the border of a table:
# test_border_of_a_table_is_not_a_footnote_rule).
_RULE_MIN_LENGTH = 20.0
_RULE_MAX_SHARE = 0.5
# The first line of the notes starts at most this many line heights under the rule, and every next line at most this many line
# heights under the line above (the notes of Word and LibreOffice are set with no space between them; a note style with 6pt after
# is 0.6 of a 10pt line) (Pinned by: test_pdf_footnotes.py::TestLeftAsIs::test_notes_far_below_the_rule,
# test_further_note_after_a_gap).
_RULE_GAP_LINES = 3.0
_NOTE_GAP_LINES = 1.5
# A line of the notes starts at most `_RULE_LEFT_SLACK` pt left of the rule and at most this many character sizes right of its
# left end (LaTeX indents the first line by 1em; a hanging indent puts the wrapped lines under the text after the number). A line further right
# belongs to another column (Pinned by: test_pdf_footnotes.py::TestLinked::test_notes_under_the_rule_of_each_column,
# TestLeftAsIs::test_other_line_between_the_notes).
_RULE_LEFT_SLACK = 2.0
_NOTE_INDENT_SIZES = 4.0
# The median size of the notes is at most this share of the median size of the body lines (Word 10pt under 11pt is 0.91). A
# numbered list under a short rule is set in the body size (Pinned by: test_pdf_footnotes.py::TestLeftAsIs::test_notes_as_large_as_the_body).
_NOTE_SIZE_RATIO = 0.95
# A page with more candidate rules than this is not looked at (a form of underlined blanks). Each candidate looks at the lines of
# the page once (Pinned by: test_pdf_footnotes.py::TestLeftAsIs::test_page_of_many_short_rules).
_MAX_CANDIDATE_RULES = 50

# The number that starts a note: raised (`^1`, `^{12}`, `^{1)}`) and followed by the text. A plain number is not taken: a wrapped
# line of a note can start with one ('2 percent of the floor area'), and every note linked in the measured documents had its
# number raised (Pinned by: test_pdf_footnotes.py::TestLinked::test_wrapped_line_that_starts_with_a_number_stays_with_its_note,
# TestLeftAsIs::test_note_numbers_that_are_not_raised).
_RAISED_NUMBER = re.compile(r"\^(?:(\d)|\{(\d{1,3})[.)]?\})\s*")
# A line of the notes that starts with another kind of mark: a symbol note or a raised letter (an affiliation). After the first
# note it would be read as the wrapped line of the note above it (Pinned by:
# test_pdf_footnotes.py::TestLeftAsIs::test_symbol_note_after_a_numbered_note).
_OTHER_NOTE = re.compile(r"[*∗†‡§¶‖※#]|\^")
# A number written right after two letters ('Sales2', '매출액1)'): a mark that was not recognized as raised, or text that looks
# like one. A note number found this way outside the notes leaves the page unlinked (Pinned by:
# test_pdf_footnotes.py::TestLeftAsIs::test_mark_that_was_not_recognized_as_raised; the other hidden marks:
# test_mark_in_a_table_cell_with_an_exponent_of_the_same_number, test_combined_mark, test_raised_number_at_the_start_of_a_line,
# test_mark_drawn_apart_as_a_small_number).
_GLUED_NUMBER = re.compile(r"(?<=[^\W\d_]{2})(\d{1,3})(?!\d)")
# A mark in the body: a raised number, as `_scripted_text` writes it (`^1`, `^{12}`, `^{1)}`). One at the start of a line is the
# number of a note (no line breaks before a mark) and leaves the page unlinked (`_hidden_marks`).
_MARK = re.compile(r"\^(?:(\d)|\{(\d{1,3})\)?\})")
# The numbers in a raised group other than a single number, ranges included ('^{2,3}', '^{1-3}' — 1, 2 and 3 — and '^{-3}'); a
# range takes at most `_RANGE_MAX` numbers (Pinned by: test_pdf_footnotes.py::TestLeftAsIs::test_combined_mark).
_NUMBER_RANGE = re.compile(r"(\d{1,3})(?:\s*[-–]\s*(\d{1,3}))?")
_RANGE_MAX = 50
# A line holding only a number ('2', '2)') at most `_LONE_NUMBER_RATIO` of the body size: a mark drawn apart from the text it
# belongs to (Pinned by: test_pdf_footnotes.py::TestLeftAsIs::test_mark_drawn_apart_as_a_small_number).
_LONE_NUMBER = re.compile(r"\(?(\d{1,3})[).]?")
_LONE_NUMBER_RATIO = 0.85


@dataclass(slots=True)
class _Notes:
    """The notes under one footnote rule: their lines top → bottom, and the lines that start a note (index into `lines` →
    (number, text after the number)). Lines before the first note carry the end of a note from the previous page."""

    lines: list[_Line]
    starts: dict[int, tuple[int, str]]


@dataclass(slots=True)
class _Mark:
    line: _Line
    start: int
    end: int
    number: int


def _link_footnotes(pages: list[_Page]) -> None:
    """Write the marks and notes of every page that proves its footnotes as `[^n]` and `[^n]: text` (see the module docstring).
    The other pages are not touched.

    Pinned by: test_pdf_footnotes.py — the numbers running on: TestLeftAsIs::test_note_numbers_with_a_gap,
    test_numbers_that_do_not_follow_the_previous_page, TestLinked::test_numbering_that_jumps_ahead; a '^' drawn on the page:
    TestLeftAsIs::test_caret_typed_in_the_text; a mark left on the previous page: test_mark_left_unlinked_on_the_previous_page;
    a note without its mark: test_note_without_a_mark_in_the_body; only one
    way of choosing the marks: test_more_than_one_way_to_choose_the_marks, TestLinked::test_exponent_that_cannot_be_the_mark_in_
    reading_order_stays; the labels: TestLinked::test_numbering_that_starts_again_is_written_with_the_page.
    """
    linked: list[tuple[_Page, list[_Notes], list[_Mark]]] = []
    previous = 0  # the last note number of the previous page that had notes
    # The numbers of the marks the previous page left unlinked: its raised marks and the marks it may hide (`_hidden_marks`); None
    # when that is not known (a page read the simple way, or one with raised digits it does not show). LaTeX can set a note on the
    # page after its mark; that note must not be given to an exponent with the same number on its own page.
    left_over: set[int] | None = set()
    for page in pages:
        before, left_over = left_over, None
        if page.simple:
            continue
        areas = _note_areas(page)
        in_areas = {id(line) for area in areas for line in area.lines}
        candidates = _body_marks(page, in_areas)
        hidden = _hidden_marks(page, in_areas)
        unknown = page.text_untrusted or page.literal_carets or page.unshown_marks
        left_over = None if unknown else {mark.number for mark in candidates} | hidden
        if not areas or before is None or unknown:
            continue
        numbers = [number for area in areas for number, _ in area.starts.values()]
        if any(later != earlier + 1 for earlier, later in zip(numbers, numbers[1:], strict=False)):
            continue
        follows = numbers[0] == 1 or numbers[0] > previous
        previous = numbers[-1]
        if not follows:
            continue
        if (hidden | before) & set(numbers):
            continue
        marks = _only_reading(numbers, candidates)
        if marks is not None:
            linked.append((page, areas, marks))
            left_over -= {mark.number for mark in marks} - hidden

    seen = Counter(number for _, areas, _ in linked for area in areas for number, _ in area.starts.values())
    for page, areas, marks in linked:
        labels = {mark.number: str(mark.number) if seen[mark.number] == 1 else f"p{page.number}-{mark.number}" for mark in marks}
        for mark in reversed(marks):  # right to left, so the positions of the marks before it on the same line stay
            mark.line.text = f"{mark.line.text[:mark.start]}[^{labels[mark.number]}]{mark.line.text[mark.end:]}"
        for area in areas:
            for index, line in enumerate(area.lines):
                line.footnote = True
                if index in area.starts:
                    number, text = area.starts[index]
                    line.text = f"[^{labels[number]}]: {text}"
                    line.footnote_start = True


def _note_areas(page: _Page) -> list[_Notes]:
    """The notes under the page's footnote rules, in reading order. Empty when there are none, or when their size does not set
    them apart from the body."""
    spread = [line for line in page.lines if line.size > 0]
    if not spread:
        return []
    # Counted before the tables are looked at: a page of thousands of short rules and hundreds of tables is not compared pair by pair
    rules = [rule for rule in page.rules if _RULE_MIN_LENGTH <= rule[2] - rule[1] <= page.width * _RULE_MAX_SHARE]
    if len(rules) > _MAX_CANDIDATE_RULES:
        return []
    rules = [rule for rule in rules
             if not any(table.top - 2 <= rule[0] <= table.bottom + 2 and rule[1] < table.x1 and table.x0 < rule[2]
                        for table in page.tables)]
    areas: list[_Notes] = []
    taken: set[int] = set()
    for rule in rules:
        area = _notes_under(rule, page.lines)
        if area is None or any(id(line) in taken for line in area.lines):
            continue
        taken.update(id(line) for line in area.lines)
        areas.append(area)
    if not areas:
        return []
    body = [line.size for line in spread if id(line) not in taken]
    if not body:
        return []
    body_size = statistics.median(body)
    if any(statistics.median(line.size for line in area.lines) > body_size * _NOTE_SIZE_RATIO for area in areas):
        return []
    order = {id(line): index for index, line in enumerate(page.lines)}
    return sorted(areas, key=lambda area: order[id(area.lines[0])])


def _notes_under(rule: tuple[float, float, float], lines: list[_Line]) -> _Notes | None:
    """The notes right under `rule` (height, left end, right end), or None when what is under it is not a block of notes."""
    height, left, _ = rule
    column = sorted((line for line in lines
                     if line.top >= height - 0.5 and line.size > 0
                     and left - _RULE_LEFT_SLACK <= line.x0 <= left + _NOTE_INDENT_SIZES * line.size),
                    key=lambda line: line.top)
    block: list[_Line] = []
    for line in column:
        above = block[-1].bottom if block else height
        if line.top - above > (_NOTE_GAP_LINES if block else _RULE_GAP_LINES) * line.height:
            break
        block.append(line)
    starts = {index: start for index, line in enumerate(block) if (start := _note_start(line.text))}
    # A note further down, after a gap, means the block is not all of the notes
    if not starts or any(_note_start(line.text) for line in column[len(block):]):
        return None
    first = min(starts)
    if any(index > first and index not in starts and _OTHER_NOTE.match(line.text) for index, line in enumerate(block)):
        return None
    # A line of another kind between the rule and the last note (a centred line, text indented further) means this is not a
    # block of notes
    members = {id(line) for line in block}
    right = max(line.x1 for line in block)
    bottom = block[-1].bottom
    if any(id(line) not in members and line.top >= height - 0.5 and line.top < bottom and line.x0 < right and left < line.x1
           for line in lines):
        return None
    return _Notes(block, starts)


def _note_start(text: str) -> tuple[int, str] | None:
    """(number, text after it) when `text` starts a note, otherwise None."""
    match = _RAISED_NUMBER.match(text)
    if match is None or int(match.group(1) or match.group(2)) == 0:
        return None
    rest = text[match.end():].strip()
    return (int(match.group(1) or match.group(2)), rest) if rest else None


def _hidden_marks(page: _Page, in_areas: set[int]) -> set[int]:
    """Numbers outside the notes that could be a mark the body does not show as one: in a raised group of the body other than a
    single number ('^{2,3}', '^{1-3}') or at the start of a line, in any raised group of a table, in a small line holding only a
    number, or written right after two letters (`_GLUED_NUMBER`) in the body lines, table cells and sideways text (Pinned by:
    test_pdf_footnotes.py::TestLeftAsIs::test_raised_mark_in_a_borderless_table)."""
    lines = [line for line in page.lines if id(line) not in in_areas]
    numbers = {int(match.group(1)) for text in [line.text for line in lines] + [table.text for table in page.tables] + page.sideways
               for match in _GLUED_NUMBER.finditer(text)}
    # Raised groups in the body that are not the mark of one note, and every raised group in a table (the lines of a borderless
    # table keep their scripts, and a mark in a table is not linked)
    groups = [match for line in lines for match in _RAISED_GROUP.finditer(line.text)
              if match.start() == 0 or not _MARK.fullmatch(match.group(0))]
    groups += [match for table in page.tables for match in _RAISED_GROUP.finditer(table.text)]
    for match in groups:
        for number in _NUMBER_RANGE.finditer(_group_text(match)):
            low = int(number.group(1))
            high = int(number.group(2)) if number.group(2) else low
            numbers.update(range(low, min(high, low + _RANGE_MAX) + 1))
    sizes = [line.size for line in lines if line.size > 0]
    if sizes:
        small = statistics.median(sizes) * _LONE_NUMBER_RATIO
        numbers.update(int(match.group(1)) for line in lines
                       if line.size and line.size <= small and (match := _LONE_NUMBER.fullmatch(line.text.strip())))
    return numbers


def _body_marks(page: _Page, in_areas: set[int]) -> list[_Mark]:
    """The raised numbers of the lines outside the notes, in reading order (lines in page order, left to right on a line)."""
    return [_Mark(line, match.start(), match.end(), int(match.group(1) or match.group(2)))
            for line in page.lines if id(line) not in in_areas
            for match in _MARK.finditer(line.text)]


def _only_reading(numbers: list[int], marks: list[_Mark]) -> list[_Mark] | None:
    """The marks that take the note `numbers` in order (one mark per number, each later in reading order than the one before),
    when there is exactly one way to choose them. None when there is no way or more than one.

    Ways are counted per prefix of `numbers` (capped at 2): a mark with the number at position i extends every way of choosing
    the numbers before it. When there is exactly one way, the earliest choice is that way.
    """
    position = {number: index for index, number in enumerate(numbers)}
    ways = [1] + [0] * len(numbers)
    for mark in marks:
        index = position.get(mark.number)
        if index is not None:
            ways[index + 1] = min(ways[index + 1] + ways[index], 2)
    if ways[-1] != 1:
        return None
    chosen: list[_Mark] = []
    for mark in marks:
        if len(chosen) < len(numbers) and mark.number == numbers[len(chosen)]:
            chosen.append(mark)
    return chosen
