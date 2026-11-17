"""Footnotes of a PDF page: the marks in the body linked to the notes under the footnote rule, only where the page allows a
single reading.

A PDF has no footnotes, only small raised numbers and small lines at the bottom of the page. Left as they are, the mark reads like
an exponent ('135^1%' — 135 to the power of 1, or a footnote?) and nothing says which note it points to. Linking them is a guess
unless the page proves it, and a wrong note attached to a sentence misleads more than an unlinked one. So a page is linked only
when every piece of evidence agrees, and otherwise it is left exactly as it was (`_link_footnotes`):

1. A footnote rule: a short horizontal rule (`_RULE_MIN_LENGTH`, at most `_RULE_MAX_SHARE` of the page width, not inside a
   table) with the notes right under it, starting at its left end. Word, LibreOffice and LaTeX draw one; the US Supreme Court
   types one as a line of em dashes (`_TYPED_RULE`).
2. The notes: every line under the rule down to the first gap, in a smaller size than the body (`_NOTE_SIZE_RATIO`), with no
   other line between them and no further note after the gap. A note starts with its number raised (`^1`, `^{1)}`); after the
   first note no line starts with another kind of mark (`*`, `†`, a raised letter), no line that does not start a note follows
   a line that stops short of the right end of the text (`_NOTE_END_SIZES`), and no note line holds a raised number after a
   space (`_INNER_NOTE`).
3. The numbers of the notes on the page run on without a gap (3, 4, 5), and start at 1 or higher than the last number of the
   previous page that had notes.
4. The marks: raised numbers in the body (`^1`, `^{12}`, `^{1)}`). Each note number is in exactly one of them, and they come
   in the order of the notes. A raised number that no note has ('m^2' on a page with note 1 only) stays as it is; a second
   raised number with a note number ('m^2' with note 2), wherever it stands, leaves the whole page unlinked — reading order
   does not place a mark in a floated caption.
5. Nothing else on the page could be a mark the body does not show: every digit raised by its size and position, or clearly
   smaller than the largest character of its line, is shown raised in the line text, and no digit in the table cells or the
   sideways text is clearly smaller than the text around it (scripts are not marked there) (`_Page.unshown_marks`); no note
   number is in another raised group ('^{2,3}', '^{1-3}'), in a raised group of a table, in a raised number at the start of a
   line, in a small line holding only a number, or right after two letters ('Sales2'), and the previous page left no mark with
   a note number unlinked, raised or hidden, and is known (not read the simple way, no raised digits it does not show) — its
   note may be on this page; no '^' character is drawn on the page (a typed '^1' cannot be told apart from a raised 1); and no
   character has no Unicode mapping ('(cid:21)', `_UNMAPPED`). A
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
# A line of the notes that stops more than this many character sizes short of the right end of the text (the notes and the
# `_TEXT_END_LINES` body lines right above the rule) ends its note, so a line right under it that does not start a note is not a
# wrapped line of that note but a line of its own (a footer, a 'Note:' line) and the block is not taken: linked, it would be part
# of the note. Wrapped lines of a note run to the right end, short of it by at most one word of ragged-right text (Pinned by:
# test_pdf_footnotes.py::TestLeftAsIs::test_line_of_its_own_right_under_the_notes,
# TestLinked::test_wrapped_line_that_starts_with_a_number_stays_with_its_note).
_NOTE_END_SIZES = 10.0
_TEXT_END_LINES = 5
# The median size of the notes is at most this share of the median size of the body lines (Word 10pt under 11pt is 0.91). A
# numbered list under a short rule is set in the body size (Pinned by: test_pdf_footnotes.py::TestLeftAsIs::test_notes_as_large_as_the_body).
_NOTE_SIZE_RATIO = 0.95
# A page with more candidate rules than this is not looked at (a form of underlined blanks). Each candidate looks at the lines of
# the page once (Pinned by: test_pdf_footnotes.py::TestLeftAsIs::test_page_of_many_short_rules).
_MAX_CANDIDATE_RULES = 50
# A footnote rule typed as text: a line of nothing but em dashes. The slip opinions of the US Supreme Court set their notes under
# '——————' in the note size, with no drawn rule; without it none of the 189 pages with notes in four opinions (Dobbs, SFFA,
# Loper Bright, Trump v. United States) was linked, with it 194 of 210 were. It is measured as a drawn rule (length, left end, not
# in a table) and every other condition stays; the line itself stays in the text. Only the em dash is taken: it is the one seen
# in documents, and a line of hyphens or underscores is also a blank to fill in or a divider (Pinned by:
# test_pdf_footnotes.py::TestLinked::test_rule_typed_as_a_line_of_dashes, TestLeftAsIs::test_line_of_hyphens_is_not_a_footnote_rule,
# test_notes_under_dashes_as_large_as_the_body).
_TYPED_RULE = re.compile(r"—{2,}")

# The number that starts a note: raised (`^1`, `^{12}`, `^{1)}`) and followed by the text. A plain number is not taken: a wrapped
# line of a note can start with one ('2 percent of the floor area'), and every note linked in the measured documents had its
# number raised (Pinned by: test_pdf_footnotes.py::TestLinked::test_wrapped_line_that_starts_with_a_number_stays_with_its_note,
# TestLeftAsIs::test_note_numbers_that_are_not_raised).
_RAISED_NUMBER = re.compile(r"\^(?:(\d)|\{(\d{1,3})[.)]?\})\s*")
# A line of the notes that starts with another kind of mark: a symbol note or a raised letter (an affiliation). After the first
# note it would be read as the wrapped line of the note above it (Pinned by:
# test_pdf_footnotes.py::TestLeftAsIs::test_symbol_note_after_a_numbered_note).
_OTHER_NOTE = re.compile(r"[*∗†‡§¶‖※#]|\^")
# A raised number after a space inside a line of the notes: the number of a second note on the same line ('^1 One. ^2 Two.'). Its
# text would be read as part of the note before it, and its mark left unlinked; such notes leave the page unlinked. An exponent is
# set right after its base ('m^2') (Pinned by: test_pdf_footnotes.py::TestLeftAsIs::test_two_notes_on_one_line).
_INNER_NOTE = re.compile(r"(?<=\s)\^(?:\d|\{\d)")
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
# A character with no Unicode mapping in its font, as pdfminer writes it ('(cid:21)'). It could be the mark of a note that the
# text does not show (a subset font without a ToUnicode map), so a page with one is not linked (Pinned by:
# test_pdf_footnotes.py::TestLeftAsIs::test_mark_in_a_font_without_a_unicode_mapping).
_UNMAPPED = re.compile(r"\(cid:\d+\)")


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
    a note without its mark: test_note_without_a_mark_in_the_body; one raised number per note number (`_only_reading`); the
    labels: TestLinked::test_numbering_that_starts_again_is_written_with_the_page.
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
        unknown = (page.text_untrusted or page.literal_carets or page.unshown_marks
                   or any(_UNMAPPED.search(text) for text in [line.text for line in page.lines] + [table.text for table in page.tables]
                          + page.sideways))
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
    typed = [line for line in page.lines if _TYPED_RULE.fullmatch(line.text.strip())]
    drawn = page.rules + [((line.top + line.bottom) / 2, line.x0, line.x1) for line in typed]
    # Counted before the tables are looked at: a page of thousands of short rules and hundreds of tables is not compared pair by pair
    rules = [rule for rule in drawn if _RULE_MIN_LENGTH <= rule[2] - rule[1] <= page.width * _RULE_MAX_SHARE]
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
    taken.update(id(line) for line in typed)  # a rule, not body text
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
    # The right end of the text in this column: the lines of the notes and the body lines right above the rule that start at its
    # left end
    above = sorted((line for line in lines if line.bottom <= height + 0.5 and line.size > 0
                    and left - _RULE_LEFT_SLACK <= line.x0 <= left + _NOTE_INDENT_SIZES * line.size),
                   key=lambda line: line.bottom, reverse=True)[:_TEXT_END_LINES]
    block: list[_Line] = []
    for line in column:
        previous = block[-1].bottom if block else height
        if line.top - previous > (_NOTE_GAP_LINES if block else _RULE_GAP_LINES) * line.height:
            break
        block.append(line)
    right = max(line.x1 for line in [*block, *above]) if block else 0.0
    # A line after one that stops short of the right end is not a wrapped line of that note (a footer, a 'Note:' line); it would
    # be read as part of the note
    if any(not _note_start(line.text) and earlier.x1 < right - _NOTE_END_SIZES * earlier.size
           for earlier, line in zip(block, block[1:], strict=False)):
        return None
    starts = {index: start for index, line in enumerate(block) if (start := _note_start(line.text))}
    # A note further down, after a gap, means the block is not all of the notes
    if not starts or any(_note_start(line.text) for line in column[len(block):]):
        return None
    first = min(starts)
    if any(index > first and index not in starts and _OTHER_NOTE.match(line.text) for index, line in enumerate(block)):
        return None
    # A raised number after a space inside a line of the notes is the number of another note set on the same line
    if any(_INNER_NOTE.search(line.text, 1) for line in block):
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
    """The marks that take the note `numbers`: the raised numbers of the body with a note number, when there is exactly one of
    each and they come in the order of the notes. None otherwise.

    Deliberate: a second raised number with a note number leaves the page unlinked wherever it stands, even where reading order
    rules it out as the mark ('m^2' after the mark of note 3). Reading order does not place every mark: LaTeX numbers a mark in
    a figure caption in the order of the source and floats the figure to the top or bottom of the page, so the real mark of note
    2 was read before mark 1 or after mark 3 and 'm^2' between them took the note (Pinned by:
    test_pdf_footnotes.py::TestLeftAsIs::test_mark_in_a_floated_caption, test_more_than_one_way_to_choose_the_marks,
    test_exponent_with_a_note_number_after_the_marks).
    """
    wanted = set(numbers)
    chosen = [mark for mark in marks if mark.number in wanted]
    return chosen if [mark.number for mark in chosen] == numbers else None
