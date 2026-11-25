"""The text of a run of characters: characters laid left to right with the spaces their gaps stand for, letter-spaced words
closed up, and superscripts and subscripts marked."""

from bisect import bisect_left, bisect_right
from collections.abc import Callable
import math
from operator import itemgetter
import re
import statistics
import unicodedata

from opendockit.extractors._inline import SUBSCRIPT, SUPERSCRIPT, mark_script, resolve_scripts
from opendockit.extractors._pdf_bidi import _RIGHT_TO_LEFT, _is_mark, _logical, _visual_units
from opendockit.extractors._pdf_model import _SPACED_CHAR
from opendockit.extractors._pdf_pdfminer import _JOINERS, _LAPARAMS


# Gap within a line (relative to the character size, `_split_merged_cells`) taken as a spot where pdfminer (char_margin 2.0) merged
# neighbouring cells into one piece inside a borderless table. Ordinary word gaps are 0.25–0.5×. Between the cells of a narrow
# column (32pt) the cell padding is 6pt + 6pt, which was 1.2× for 10pt characters.
_CELL_GAP_RATIO = 0.8


# A spot where digit and digit are this far apart without a space character is also a cell boundary candidate (`fine_parts`). The
# 4px + 4px cell padding of a Chrome table is 0.5× a 12pt character, so a right-aligned amount and the next column's percentage
# became the single cell '1,250 65%' (Pinned by: test_pdf_table_columns.py::TestWideWrappedFirstColumn).
# Digits within one number touch each other, and between words there is a space character.
_NUMBER_GAP_RATIO = 0.4
_NUMBER_EDGE = re.compile(r"[\d%‰)]")  # numeric character ending a cell (before the gap)
_NUMBER_START = re.compile(r"[\d(△▲▼+\-−–]")  # numeric character starting a cell (after the gap)


# Upper bound on the gap between characters (relative to the character size) for a line to count as written with widened character
# spacing (Tc), and the difference (pt) within which two gaps count as the same spacing. Ordinary word gaps are 0.25–0.5×, so
# character spacing widened beyond that cannot be told from word gaps (`_letter_spacings`).
_LETTER_SPACING_MAX = 0.5
_SPACING_TOLERANCE = 0.3


# Text set on a grid (`_fixed_pitches`): the middles of the characters of a line, its own space characters among them,
# stand a whole number of one pitch apart, to within this share of the pitch; the pitch is between these shares of the
# character size (a printer's ten or twelve to the inch is 0.6 to 0.8 of the sizes it is printed in; from the size on,
# letters are the cells of a row, and East Asian text set solid stands a size apart); and a line shows it with this many
# letters, this many of its characters in neighbouring cells. A page names at most this many grids
_PITCH_TOLERANCE = 0.1
_PITCH_MIN_RATIO = 0.3
_PITCH_MAX_RATIO = 0.8
_PITCH_LINE_CHARS = 8
_PITCH_NEIGHBOURS = 4
_PITCH_GRIDS = 8
# A space character belongs to a line when it is written at the height of the line's letters, to within this (pt): the
# characters of one row of text start from one baseline, and a row of blanks laid over the line starts from its own
_SAME_ROW = 0.1


# Superscript/subscript estimation (_scripted_text): maximum size of a script relative to the line's usual character size, and the
# minimum distance raised or lowered. Typesetting convention is a size of 0.6–0.7×, a rise of 0.33–0.4 and a drop of 0.15–0.25.
_SCRIPT_SIZE_RATIO = 0.8
_SUPERSCRIPT_RISE = 0.2
_SUBSCRIPT_DROP = 0.1
# The sup/sub of browser (Chrome) printing are 5/6 the size (0.833×) with the baseline shifted by 0.37 / 0.3 em. A character larger
# than 0.8× is a script only when it is at most this size ratio and its baseline is shifted up or down by at least
# _SCRIPT_FAR_SHIFT (relative to the usual character size) — slightly smaller emphasised text raised a little stays as it is
# (Pinned by: test_pdf.py::TestSuperscriptByPosition::test_browser_scripts_five_sixths_the_size_and_shifted_far_are_marked,
# test_slightly_smaller_text_shifted_only_a_little_is_not_marked).
_SCRIPT_FAR_SIZE_RATIO = 0.85
_SCRIPT_FAR_SHIFT = 0.25


def _drawn_text(text: str) -> str:
    """Text made from characters drawn on the page. A character pdfminer delivers as U+00AD (soft hyphen) is replaced by '-'.

    The '–' of a non-embedded Korean CID font (UniKS-UCS2-H) comes out as Adobe-Korea1's hyphen character (mapped to U+00AD), and
    the result clean-up (`markdown._INVISIBLE`) deleted it as an invisible character so that the numbers ran together
    ('2025–2026' → '20252026', Pinned by: test_pdf.py::test_dash_that_arrives_as_a_soft_hyphen_is_kept_as_a_hyphen). Every
    character in a PDF is drawn, so it is a visible hyphen (a hyphen at a line end is visible on screen too). Known limitation: ₩,
    €, £, ¥, the middle dot (·) and ► of the same font are missing from pdfminer's UniKS-UCS2-H mapping table and are dropped
    entirely (pdfium reads them) — not done because it would mean patching pdfminer's CMap globally.
    """
    return text.replace("\xad", "-")


def _text_from_chars(chars: list[dict]) -> str:
    """Build text by joining the characters of a line left to right. Whitespace characters and wide gaps (same criterion as pdfminer's
    word_margin) are the word spacing. A line with Hebrew or Arabic letters is read in their direction (`_pdf_bidi._logical`)."""
    ordered = sorted(chars, key=lambda char: char["x0"])
    if any(_RIGHT_TO_LEFT.search(char["text"]) for char in ordered):
        # A joiner is drawn where the letter before it ended, which is where the next one starts: it stands between them
        ordered = sorted(chars, key=lambda char: char["x0"] - (_JOINER_REACH if char["text"] in _JOINERS else 0.0))
        return " ".join(_logical(_visual_units(ordered, _LAPARAMS["word_margin"])).split())
    if any(_is_mark(char["text"]) for char in ordered):
        ordered = sorted(chars, key=_place_in_its_word)
    parts: list[str] = []
    previous = None
    for char in ordered:
        if not char["text"].strip():
            parts.append(" ")
            previous = None
            continue
        if _is_mark(char["text"]):
            parts.append(char["text"])  # it stands over its letter: no gap to measure, and none measured from it
            continue
        if previous is not None and char["x0"] - previous["x1"] > _LAPARAMS["word_margin"] * max(char["width"], char["height"]):
            parts.append(" ")
        parts.append(char["text"])
        previous = char
    return " ".join("".join(parts).split())


_JOINER_REACH = 0.05  # pt a zero-width joiner is put to the left of a letter that starts where it stands
# A mark is put before a letter that starts within this share of its height to its left (`_place_in_its_word`).
_MARK_REACH_RATIO = 0.1


def _place_in_its_word(char: dict) -> float:
    """Where a character stands in its line from left to right: its left edge, and for a mark set on a letter (a vowel
    sign or tone mark of Thai, an accent) a little left of it.

    A font draws such a mark from the place where its letter ended, without moving on, so the mark and the letter after
    it start at the same place, give or take what kerning adds. By the left edge alone the mark came after that letter
    and stood on it: 'ดวงจนัทร์' for 'ดวงจันทร์' in the cells of a table, whose text is read from left to right and not in
    the order of drawing (Pinned by: test_pdf_actual_text.py::TestMarksSetOnALetter)."""
    return char["x0"] - (char["height"] * _MARK_REACH_RATIO if _is_mark(char["text"]) else 0.0)


# A character whose text matrix leans its upright strokes by more than this (c / d, about 6 degrees) is slanted: synthetic italic
# (`_glyph_gaps`).
_SLANT_RATIO = 0.1


def _is_slanted(char: dict) -> bool:
    """Whether the character is written horizontally with a slanted text matrix (synthetic italic)."""
    a, b, c, d = (char.get("matrix") or (1.0, 0.0, 0.0, 1.0))[:4]
    return abs(b) <= _SLANT_RATIO * abs(a) and abs(c) > _SLANT_RATIO * abs(d)


def _glyph_gaps(chars: list[dict]) -> tuple[list[dict], list[float]]:
    """The line's characters laid left to right, and the gaps between two neighbouring characters that have no space character
    between them.

    A gap next to a slanted character is not one: synthetic italic (an oblique text matrix, how LibreOffice and Word slant a
    font that has no italic, such as any Korean font) widens the box of each character by the slant, so the gaps of a word in
    it are negative, and one such word kept a letter-spaced line from being closed up ('매 출 을 보 고 서') (Pinned by:
    test_pdf_character_spacing.py::TestWideCharacterSpacing::test_a_word_in_synthetic_italic_does_not_keep_its_line_spaced_out).
    Known accepted gap: a line all in synthetic italic has no gaps and is not closed up."""
    ordered = sorted(chars, key=lambda char: char["x0"])
    gaps = [right["x0"] - left["x1"] for left, right in zip(ordered, ordered[1:], strict=False)
            if left["text"].strip() and right["text"].strip() and not (_is_slanted(left) or _is_slanted(right))]
    return ordered, gaps


def _letter_spacings(lines: list[list[dict]]) -> list[float]:
    """The gap values on the page that look like widened character spacing (Tc). The narrowest gap of each line in which every gap
    between characters exceeds pdfminer's word-gap criterion (`word_margin` × character size), the gaps close to the narrowest one
    (`_SPACING_TOLERANCE`) number three or more and 60% or more, and that gap is below `_LETTER_SPACING_MAX` times the character
    size. A short cell with a single gap ('3%') cannot be decided on its own, so it is judged with these values."""
    found: list[float] = []

    for chars in lines:
        _, gaps = _glyph_gaps(chars)
        size = max((char["size"] for char in chars), default=0.0)
        if len(gaps) < 3 or size <= 0:
            continue
        narrowest = min(gaps)
        near = sum(1 for gap in gaps if gap - narrowest <= _SPACING_TOLERANCE)
        if (_LAPARAMS["word_margin"] * size < narrowest < _LETTER_SPACING_MAX * size and near >= 3
                and near >= len(gaps) * 0.6 and not any(abs(narrowest - known) <= _SPACING_TOLERANCE for known in found)):
            found.append(narrowest)
    return found


_X0 = itemgetter("x0")


def _written_at(char: dict) -> float | None:
    """The height a character is written at: where its text matrix puts the baseline. None when the character has none
    (one put back by the reader). The bottom of its box will not do: it lies under the baseline by the descent of the font,
    so the spaces of a line set in another font than its letters stood up to 0.7pt off them."""
    matrix = char.get("matrix")
    return matrix[5] if matrix is not None and len(matrix) >= 6 else None


# The characters a line set on a grid may hold beside those of ASCII: the letters of Latin-1 and of Latin Extended-A and -B
_ONE_CELL_END = "\u0250"


def _is_of_one_cell(text: str) -> bool:
    """Whether the text is one character a report of fixed pitch sets in one cell: a character of ASCII, or a Latin letter
    below U+0250. An East Asian font draws a syllable, a circled number, a Greek or Cyrillic letter, a piece of a ruled
    line two cells wide, and the signs of Latin-1 too (the multiplication sign, the section sign, plus-minus, the degree
    sign, the middle dot); what the standard calls their width does not say so for all of them. A glyph without a
    character is written as several ('(cid:21)') and is none."""
    return len(text) == 1 and (text < "\x80" or text < _ONE_CELL_END and unicodedata.category(text)[0] == "L")


def _is_upright(char: dict) -> bool:
    """Whether the character is written straight - not turned, not slanted, not upside down: only then is the height of
    its text matrix the height of its row, and the order of the places the order of reading. A text layer written over a
    scan stands a degree or two off, and from one character to the next its height differs by more than rows laid over
    each other do; text turned by half a circle reads from right to left."""
    matrix = char.get("matrix")
    return matrix is None or len(matrix) < 6 or (matrix[1] == 0 and matrix[2] == 0 and matrix[0] > 0 and matrix[3] > 0)


def _pitch_steps(chars: list[dict]) -> tuple[list[dict], list[float]]:
    """The characters of a line from left to right - its letters, and the space characters written at their height
    (`_SAME_ROW`) - and the distance from the middle of each to the middle of the next. A space character of another row,
    laid over the line, is none of the line's. Nothing for a line with a character outside the Latin letters, digits and
    marks (`_is_of_one_cell`): on the grid of an East Asian report a syllable, a circled number or a piece of a ruled line
    takes two cells, and the middles of two of them side by side are two cells apart with no
    cell empty between them; and nothing for a line with a character that is not written straight (`_is_upright`)."""
    if not all(_is_of_one_cell(char["text"]) and _is_upright(char) for char in chars):
        return [], []
    heights = [_written_at(char) for char in chars]
    if heights and heights.count(heights[0]) == len(heights):
        ordered = sorted(chars, key=_X0)  # one height, or none known: the usual line
    else:
        letters = [height for char, height in zip(chars, heights, strict=True) if char["text"].strip() and height is not None]
        if not letters:
            return [], []
        height = statistics.median(letters)
        ordered = sorted((char for char, at in zip(chars, heights, strict=True)
                          if char["text"].strip() or at is None or abs(at - height) <= _SAME_ROW), key=_X0)
    middles = [(char["x0"] + char["x1"]) / 2 for char in ordered]
    return ordered, [right - left for left, right in zip(middles, middles[1:], strict=False)]


def _cells_apart(step: float, pitch: float) -> int:
    """How many cells of a grid of `pitch` two characters `step` apart are from each other; 0 when they do not stand on it."""
    if not (pitch > 0 and math.isfinite(step)):
        return 0
    cells = round(step / pitch)
    return cells if cells >= 1 and abs(step - cells * pitch) <= pitch * _PITCH_TOLERANCE else 0


def _fixed_pitches(lines: list[list[dict]]) -> list[tuple[float, float]]:
    """The (pitch, character size) of the lines of the page that are set on a grid, `_PITCH_GRIDS` at most:
    `_PITCH_LINE_CHARS` letters or more, the middle of each character a whole number of pitches from the one before, the
    pitch (the median of the distances near the smallest, `_PITCH_NEIGHBOURS` of them or more) between `_PITCH_MIN_RATIO`
    and `_PITCH_MAX_RATIO` of the character size, and two letters in neighbouring cells so far apart somewhere that a
    space would be read between them. A short piece on such a page ('20', an amount) is judged with these."""
    found: list[tuple[float, float]] = []
    for chars in lines:
        if len(chars) < _PITCH_LINE_CHARS:
            continue
        # The lines of every page come here, and hardly one stands on a grid: what turns a line away is asked in the order
        # of its cost, the median last
        ordered, steps = _pitch_steps(chars)
        smallest = min(steps, default=0.0)
        if not smallest > 0:
            continue  # characters drawn over each other, or a place that is no number: no grid
        neighbours = [step for step in steps if step <= smallest * (1 + 2 * _PITCH_TOLERANCE)]
        if len(neighbours) < _PITCH_NEIGHBOURS or sum(1 for char in ordered if char["text"].strip()) < _PITCH_LINE_CHARS:
            continue
        size = max(char["size"] for char in ordered)
        pitch = statistics.median(neighbours)
        if not (size > 0 and size * _PITCH_MIN_RATIO <= pitch <= size * _PITCH_MAX_RATIO):
            continue
        if any(abs(pitch - known) <= known * _PITCH_TOLERANCE and known_size == size for known, known_size in found):
            continue
        apart = [_cells_apart(step, pitch) for step in steps]
        if all(apart) and any(cells == 1 and left["text"].strip() and right["text"].strip()
                              and right["x0"] - left["x1"] > _LAPARAMS["word_margin"] * max(right["width"], right["height"])
                              for cells, left, right in zip(apart, ordered, ordered[1:], strict=False)):
            found.append((pitch, size))
            if len(found) == _PITCH_GRIDS:
                break
    return found


def _fixed_pitch_text(chars: list[dict], pitches: list[tuple[float, float]]) -> str | None:
    """The text of a line set on one of the page's grids (`pitches`, from `_fixed_pitches`): a space where the line has a
    space character or leaves a cell empty, and nowhere else. None for any other line.

    Deliberate: a report made for a printer of fixed pitch and drawn in a proportional font puts the middle of every
    character, the spaces of the line among them, on a grid of 0.6 of the character size. The letters are narrower than
    their cells, so the gap between two of them passes the criterion for a word gap, and the statement of a bank lays a row
    of space characters over each line, written 0.6pt higher and on a grid of its own, whose spaces fall between the
    letters: a space was read after every letter - 'A c c o u n t No', '8 , 9 2 3 . 0 7'. The line writes its spaces
    itself, each in a cell; a gap inside a cell is none. Digits written with a space between each two ('1 2 3 4 5 6 7 8')
    stand on a grid as well, of half the distance, with a space character in every other cell: they keep their spaces,
    and no two of them are neighbours, so such a line names no grid. Text of a fixed-pitch font fills its cells and reads
    as before.
    Known accepted gap: a line with a character outside the Latin letters, digits and marks is not asked
    (`_pitch_steps`), so a report of that kind in Korean, Greek or Russian reads as before, and neither is a line that is
    turned or slanted; nor is text a character size apart or more - East Asian text set
    solid, where the quarter of a size a word processor leaves beside a digit puts a single digit on the grid by chance
    and no number of two digits; a row of letters that are the cells of a table
    (Pinned by: test_pdf_character_spacing.py::TestTextSetOnAGrid, ::TestGridOfALine)."""
    if not pitches:
        return None
    ordered, steps = _pitch_steps(chars)
    if sum(1 for char in ordered if char["text"].strip()) < 2:
        return None
    size = max(char["size"] for char in ordered)
    for pitch, pitch_size in pitches:
        if pitch_size != size:
            continue
        apart = [_cells_apart(step, pitch) for step in steps]
        if all(apart):
            parts = [ordered[0]["text"]]
            for cells, char in zip(apart, ordered[1:], strict=True):
                parts.append((" " if cells > 1 else "") + (char["text"] if char["text"].strip() else " "))
            return " ".join("".join(parts).split())
    return None


def _letter_spaced_text(chars: list[dict], spacings: list[float]) -> str | None:
    """For a line written with widened character spacing (a line whose narrowest gap equals one of the page's spacing values
    `spacings`), the text rejoined with spaces only at gaps judged after subtracting the character spacing.

    Deliberate: when the character spacing exceeds 10% of the character size pdfminer puts a space after every character, giving
    '총 사 업 비 는 1 , 2 3 4'. Only space characters and gaps wider than the character spacing + the word-gap criterion remain as
    word spacing. Pieces drawn one character at a time, as in a letter-spaced heading (`_is_letter_spaced`), are separate lines and
    do not come here. Known limitation: text in ruled-table cells (pdfplumber extraction) is not fixed
    (Pinned by: test_pdf_character_spacing.py::TestWideCharacterSpacing)."""
    ordered, gaps = _glyph_gaps(chars)
    size = max((char["size"] for char in chars), default=0.0)
    if not gaps or size <= 0 or min(gaps) <= _LAPARAMS["word_margin"] * size:
        return None
    spacing = next((known for known in spacings if abs(min(gaps) - known) <= _SPACING_TOLERANCE), None)
    if spacing is None:
        return None

    parts: list[str] = []
    previous = None

    for char in ordered:
        if not char["text"].strip():
            parts.append(" ")
            previous = None
            continue
        if previous is not None and char["x0"] - previous["x1"] > spacing + _LAPARAMS["word_margin"] * size:
            parts.append(" ")
        parts.append(char["text"])
        previous = char
    return " ".join("".join(parts).split())


def _gap_parts(chars: list[dict], text: str, size: float, unspaced: float | None = None) -> tuple[tuple[float, float, str], ...]:
    """Pieces ((x0, x1, text)) of the line's characters (`chars`) split where they are `_CELL_GAP_RATIO` times the character size or
    more apart. With `unspaced`, also split where there is no space character yet the gap is that multiple or more (Chrome table cell
    padding 0.75x — between words there is a space character. Between a number and a number from `_NUMBER_GAP_RATIO` times). An
    empty tuple when there is no such gap or when the joined split text differs from the line text (`text`) (script marks,
    overlapping copies, lines in a different order).

    The space characters are looked up by binary search on their sorted x0 (`_SpaceStarts`). Scanning them all for every gap and
    every piece took 36 seconds for one 659-byte line of 16,000 letter-spaced words (Pinned by:
    test_audit_pdf_text.py::TestLongLetterSpacedLine)."""
    glyphs = sorted((char for char in chars if char["text"].strip()), key=lambda char: char["x0"])
    if len(glyphs) < 2 or size <= 0:
        return ()
    spaces = _SpaceStarts([char for char in chars if not char["text"].strip()])

    groups = [[glyphs[0]]]
    for previous, char in zip(glyphs, glyphs[1:], strict=False):
        gap = char["x0"] - previous["x1"]
        wide = unspaced is not None and (gap >= size * unspaced or (
            gap >= size * _NUMBER_GAP_RATIO and _NUMBER_EDGE.match(previous["text"]) and _NUMBER_START.match(char["text"])))
        if gap >= size * _CELL_GAP_RATIO or (wide and not spaces.any_between(previous["x0"], char["x1"])):
            groups.append([])
        groups[-1].append(char)
    if len(groups) < 2:
        return ()
    parts = tuple((group[0]["x0"], max(char["x1"] for char in group),
                   _drawn_text(_text_from_chars([*group, *spaces.between(group[0]["x0"], group[-1]["x1"])])))
                  for group in groups)
    if " ".join(part[2] for part in parts) == text:
        return parts
    # the pieces of a line read from right to left follow each other that way
    return parts if _RIGHT_TO_LEFT.search(text) and " ".join(part[2] for part in reversed(parts)) == text else ()


class _SpaceStarts:
    """The space characters of a line indexed by x0, answering 'which start strictly between a and b' (in their original order,
    as a scan gives them). With an x0 that is not a finite number the list is scanned as before — such a value has no place in the
    order."""

    def __init__(self, spaces: list[dict]) -> None:
        self._spaces = spaces
        self._sorted = all(math.isfinite(space["x0"]) for space in spaces)
        if self._sorted:
            self._order = sorted(range(len(spaces)), key=lambda index: spaces[index]["x0"])
            self._starts = [spaces[index]["x0"] for index in self._order]

    def between(self, low: float, high: float) -> list[dict]:
        if not self._sorted:
            return [space for space in self._spaces if low < space["x0"] < high]
        if not low < high:
            return []
        found = sorted(self._order[bisect_right(self._starts, low):bisect_left(self._starts, high)])
        return [self._spaces[index] for index in found]

    def any_between(self, low: float, high: float) -> bool:
        if not self._sorted:
            return any(low < space["x0"] < high for space in self._spaces)
        index = bisect_right(self._starts, low)
        return index < len(self._starts) and self._starts[index] < high


def _scripted_text(chars: list[dict], line_text: str) -> str | None:
    """Text with the superscripts and subscripts in the line marked. None when there are no scripts or it is not certain (the line
    text is used as is).

    A PDF carries no information about scripts, so they are inferred from position: characters smaller than the line's ordinary
    characters (`_SCRIPT_SIZE_RATIO` or less) whose baseline is off by `_SUPERSCRIPT_RISE` or more upwards or `_SUBSCRIPT_DROP` or
    more downwards (relative to the ordinary character size). Slightly less small characters (`_SCRIPT_FAR_SIZE_RATIO` or less,
    browser printing) only when off by `_SCRIPT_FAR_SHIFT` or more. Small characters on the same baseline (units, annotations) are
    not scripts. The line text is not rebuilt; marks are only inserted into the text pdfminer built: this is done only when the
    characters laid left to right equal the characters of the line text, order included. If they differ (characters of a
    neighbouring line mixed in, or a line where overlapping accents come in a different order) the line is left alone — keeping
    character order and spacing unchanged takes precedence over marking scripts (Pinned by: test_pdf.py::TestSuperscriptByPosition).
    """
    positional = _positional_scripts(chars)
    if positional is None:
        return None
    marked = [(letter, mark) for char, mark in positional for letter in char["text"] if not letter.isspace()]
    # Spans containing Hangul or Hanja are not marked. When a designed document aligns small text or units next to a heading to the
    # top, it is positionally identical to a superscript and gave '12,345^{백만원}' (Pinned by:
    # test_pdf.py::TestSuperscriptByPosition::test_small_raised_korean_words_are_not_marked).
    start = 0
    while start < len(marked):
        end = start + 1
        while end < len(marked) and marked[end][1] == marked[start][1]:
            end += 1
        if marked[start][1] and any(_SPACED_CHAR.match(letter) for letter, _ in marked[start:end]):
            marked[start:end] = [(letter, None) for letter, _ in marked[start:end]]
        start = end
    if not any(mark for _, mark in marked) or [letter for letter, _ in marked] != [letter for letter in line_text if not letter.isspace()]:
        return None

    marks = iter(mark for _, mark in marked)
    pieces = []
    for letter in line_text:
        mark = None if letter.isspace() else next(marks)
        pieces.append(mark_script(letter, mark) if mark else letter)
    return resolve_scripts("".join(pieces))


def _positional_scripts(chars: list[dict]) -> list[tuple[dict, str | None]] | None:
    """The visible characters of a line left to right, each with the script mark its size and position give it (`SUPERSCRIPT`,
    `SUBSCRIPT` or None), by the rule of `_scripted_text`. None when the line gives no basis for scripts: fewer than three
    characters, a line of uniform size, or a line mostly of small characters."""
    visible = [char for char in chars if char["text"].strip() and char.get("size")]
    if len(visible) < 3:
        return None
    largest = max(char["size"] for char in visible)
    if min(char["size"] for char in visible) > largest * _SCRIPT_FAR_SIZE_RATIO:
        return None  # a line of uniform size (most lines)

    body_size = statistics.median(char["size"] for char in visible)
    body = [char for char in visible if char["size"] > body_size * _SCRIPT_SIZE_RATIO]
    if len(body) * 2 < len(visible):
        return None

    # pdfminer's character box extends below the baseline by the font's descent. It differs per font but is around 0.2 times the size.
    def baseline(char: dict) -> float:
        return char["bottom"] - 0.2 * char["size"]

    body_baseline = statistics.median(baseline(char) for char in body)
    marked: list[tuple[dict, str | None]] = []  # (character, script mark), left to right
    for char in sorted(visible, key=lambda char: char["x0"]):
        mark = None
        rise = body_baseline - baseline(char)  # how far it is raised (pt)
        if char["size"] <= body_size * _SCRIPT_SIZE_RATIO:
            mark = SUPERSCRIPT if rise >= body_size * _SUPERSCRIPT_RISE else SUBSCRIPT if rise <= -body_size * _SUBSCRIPT_DROP else None
        elif char["size"] <= body_size * _SCRIPT_FAR_SIZE_RATIO and abs(rise) >= body_size * _SCRIPT_FAR_SHIFT:
            mark = SUPERSCRIPT if rise > 0 else SUBSCRIPT
        marked.append((char, mark))
    return marked


# A raised group in a line text as `format_script` writes it (`^1`, `^{12}`, `^{1)}`, `^{2,3}`).
_RAISED_GROUP = re.compile(r"\^(?:\{([^{}]*)\}|(\S))")


def _raised_digits_unshown(chars: list[dict], text: str) -> bool:
    """Whether the line has more raised digits by position (`_positional_scripts`) than its text `text` shows inside raised
    groups — a raised number that `_scripted_text` left as plain text (a span with Hangul, a line whose characters are in another
    order, a script `format_script` does not mark). Footnotes are not linked on a page with one: that number could be the
    mark of a note while an exponent with the same number takes the note (`_pdf_footnotes`). A digit clearly smaller than the
    largest character of the line (`_SCRIPT_SIZE_RATIO`) that is not shown raised counts too, whether or not it is raised far
    enough to be a superscript: a mark set small and raised less than `_SUPERSCRIPT_RISE` came out as plain text ('5%2') while
    'm^2' after it took note 2, and a line that gives no basis for the position (two characters, or mostly small ones: 'A' and
    a raised '12') marks no scripts at all (Pinned by: test_pdf_footnotes.py::TestLeftAsIs::test_raised_number_left_as_plain_text,
    test_small_mark_raised_too_little, test_two_character_line_with_a_small_digit,
    test_short_line_of_mostly_small_digits; an empty raised group typed in the text,
    '^{}': test_empty_raised_group_typed_in_a_line_of_mixed_sizes_converts)."""
    visible = [char for char in chars if char["text"].strip() and char.get("size")]
    if len(visible) < 2:
        return False
    small = max(char["size"] for char in visible) * _SCRIPT_SIZE_RATIO
    positional = _positional_scripts(chars)
    if positional is None:
        return any(char["text"].isdigit() and char["size"] <= small for char in visible)
    raised = sum(1 for char, mark in positional if mark == SUPERSCRIPT or (mark is None and char["size"] <= small)
                 for letter in char["text"] if letter.isdigit())
    shown = sum(1 for match in _RAISED_GROUP.finditer(text) for letter in _group_text(match) if letter.isdigit())
    return raised > shown


def _group_text(match: re.Match[str]) -> str:
    """The text of a raised group matched by `_RAISED_GROUP` (empty for '^{}')."""
    return match.group(1) if match.group(1) is not None else match.group(2)


def _small_digits(chars: list[dict]) -> bool:
    """Whether a digit among `chars` (the characters of the table cells, or of the sideways text, of a page) is clearly smaller
    than the others (`_SCRIPT_SIZE_RATIO` of their median size). Scripts are not marked there, so a raised footnote mark is glued
    on as a plain digit ('1,2002)'); footnotes are not linked on such a page (`_pdf_footnotes`)."""
    sizes = [char["size"] for char in chars if char["text"].strip() and char.get("size")]
    if not sizes:
        return False
    limit = statistics.median(sizes) * _SCRIPT_SIZE_RATIO
    return any(char["text"].isdigit() and char.get("size") and char["size"] <= limit for char in chars)


def _is_hangul(text: str) -> bool:
    return len(text) == 1 and "가" <= text <= "힣"


# A drop cap (`_drop_cap`), relative to the size of the text beside it: how large the initial is at least and at most, how far
# the first letter of the text may be from the initial, how far the top of the initial may be from the top of the capitals of
# the first line, how far below the first line the initial stands at least, and how far its baseline may be from the baseline
# of the line it rests on.
_DROP_CAP_MIN_RATIO = 2.0
_DROP_CAP_MAX_RATIO = 6.0
_DROP_CAP_GAP_RATIO = 1.0
_DROP_CAP_TOP_RATIO = 0.5
_DROP_CAP_DROP_RATIO = 0.75
_DROP_CAP_FOOT_RATIO = 0.25
_CAP_HEIGHT = 0.7  # height of a capital letter relative to the font size (0.66 Times, 0.72 Helvetica)
_DESCENT = 0.2  # the baseline of a character is this far above the bottom of its box, relative to the font size

# The start of an English word: at most one of the consonant groups a word can begin with, then a vowel. 'fter', 'lthough',
# 'nd', 'ndeed', 'nstead' and 's' (After, Although, And, Indeed, Instead, As) cannot be words (`_initial_is_a_word`).
_WORD_START = re.compile(r"(?:bl|br|chr?|cl|cr|dr|dw|fl|fr|gh|gl|gr|kn|ph|pl|pr|ps|qu|rh|scr?|sch|shr?|sk|sl|sm|sn|spl?|spr|squ|str?|sw|thr?|tr|tw"
                         r"|wh|wr|[bcdfghjklmnpqrstvwz])?[aeiouy]")
# Words written with 'a', not 'an', although they begin with a vowel letter ('A unique', 'A useful', 'A European', 'A one-off').
_A_BEFORE_VOWEL = ("uni", "una", "use", "usu", "uti", "eu", "one", "once")
# Common words beginning with 'A' and 'I' whose rest could start a word of its own ('A bout', 'I ron'), without the initial: a
# closed list, not a dictionary. A rarer word of this kind (Alice, Adam, Italy) is read as 'A lice', as before the drop cap rule.
_REST_OF_A_WORD = {
    "A": frozenset("ble bout bove cross dult dults frica frican ge gain gainst merica merican mericans mid mong nimal nimals "
                   "nother ny nybody nyone nything pril re rea reas round sked verage".split()),
    "I": frozenset("ce dea deas dentity mage mages magine nitially ron ronically sland slands tem tems".split()),
}


def _initial_is_a_word(letter: str, rest: str) -> bool:
    """Whether a drop cap is a word of its own ('A' + 'long time ago', 'I' + 'was born') and not the first letter of the word
    after it ('A' + 'fter', 'I' + 'n'), judged from the letters alone: nothing on the page told them apart (`_drop_cap`).

    Only 'A' and 'I' are words. They are when what follows can be a word by itself: it begins as an English word does
    (`_WORD_START`), is not the rest of a common word (`_REST_OF_A_WORD`), and after 'A' does not begin with a vowel letter
    (it would be 'An'; `_A_BEFORE_VOWEL` holds the exceptions). Known limitation, measured on 346 sentence openers set as
    drop caps (188 'A', 139 'I', 19 'O'): 24 are read wrong (6.9%). 13 of 80 words beginning with 'A' are split as they were
    before there was a drop cap rule ('A long the coast' for Along, 'A lone', 'A head', 'A part', 'A side', 'A way', 'A lice',
    'A dam', 'A sking' …), 5 of 108 'A' + word are joined ('Around table', 'Across word', 'Again of ten', 'About of flu',
    'Amid sized'), 3 of 52 words beginning with 'I' are split ('I taly', 'I reland', 'I solated'), none of 87 'I' + word is
    joined, and the vocative 'O' ('O come all ye') is always joined. With the space written as a glyph the 8 joined words are
    right and the 16 split ones remain (4.6%). The rule is English: in a language where another single capital is a word
    ('Y', 'E', 'O') that word is joined to the next, and 'A' and 'I' are judged as if English.
    (Pinned by: test_pdf_reading_order.py::TestDropCaps::test_an_initial_that_is_a_word_keeps_its_space,
    test_an_initial_of_a_word_beginning_with_a_or_i_is_joined, test_how_often_the_letters_alone_misjudge_a_sentence_opener)"""
    word = re.match("[a-z]*", rest).group()
    if letter not in _REST_OF_A_WORD:
        return False
    if letter == "A" and word[:1] in "aeiou":
        return word.startswith(_A_BEFORE_VOWEL)
    return bool(_WORD_START.match(word)) and word not in _REST_OF_A_WORD[letter]


def _drop_cap(chars: list[dict], line_text: str) -> tuple[str, float, float] | None:
    """(text, top, bottom) of a line that opens with a drop cap, the initial joined to the rest of its word and the line as high as
    the text beside the initial. None for any other line.

    A paragraph that opens with a large initial hanging beside its first lines ('T' + 'he quick brown') was read 'T he quick
    brown', and the height of the initial kept the first line apart from the rest of its paragraph (Pinned by:
    test_pdf_reading_order.py::TestDropCaps::test_a_drop_cap_is_joined_to_its_word_and_paragraph). A drop cap is a capital
    letter followed no further than `_DROP_CAP_GAP_RATIO` away by a lower-case letter, the top of its capital within `_DROP_CAP_TOP_RATIO` of
    the capitals of the first line, standing at least `_DROP_CAP_DROP_RATIO` below the baseline of that line on the baseline
    (within `_DROP_CAP_FOOT_RATIO`) of a character of a later line beside it, and `_DROP_CAP_MIN_RATIO` to
    `_DROP_CAP_MAX_RATIO` times the size of the text. The baselines alone do not tell a drop cap from an option or step letter:
    a letter 1.8 times the text on the second line of text set solid, or 6.5 to 8 times beside four lines, stands the same way,
    and 'D' + 'press and hold the button' was read 'Dpress and hold the button' — 21 of 2,000 such labels that had kept their
    space under the size bounds alone (Pinned by: test_a_letter_under_twice_or_over_six_times_the_text_is_no_drop_cap). A cap
    over two lines of text set solid is about 2.4 times the text, one over three lines of text set 1.4 apart about 5 times. A large letter that
    is a label keeps its space as before: 'Q' beside one line, 'B' centred beside three lines or standing in the margin, a
    letter on the baseline of the first line, a capital or a digit after it (Pinned by:
    test_a_large_label_that_is_no_drop_cap_keeps_its_space). Deliberate: a letter set like a drop cap by chance of the sizes (a
    label as high as the block beside it, resting on one of its baselines) is joined, a drop cap set loosely (resting
    between two lines, as a floated first letter of a web page does) is not, and neither is a cap over four or more lines
    that is more than six times the text (four lines about 1.2 or more apart).

    The initial may be a word of its own ('A long time ago', 'I was born'): joining gave 'Along time ago' and 'Iwas born'. The
    page decides first: a space glyph drawn between the initial and the text (Word, InDesign and LaTeX write the space of 'A
    long' into the content stream, after the large letter or before the small text) keeps the space, for any letter. Without
    one the letters decide (`_initial_is_a_word`). The line gets the height of the text beside the initial either way.
    """
    # A character without a finite position or size is on no line (Pinned by:
    # test_pdf_reading_order.py::TestDropCaps::test_characters_without_a_finite_position_are_no_line_beside_an_initial).
    solid = sorted((char for char in chars if char["text"].strip() and char.get("size")
                    and all(math.isfinite(char[key]) for key in ("x0", "x1", "top", "bottom", "size"))), key=lambda char: char["x0"])
    if len(solid) < 3:
        return None
    initial, others = solid[0], solid[1:]
    # The characters at the level of a line reach as low as the initial, so they hold the lines beside it below as well: the
    # line itself is the top row of them.
    first_top = min(char["top"] for char in others)
    rest = [char for char in others if char["top"] < first_top + char["size"] / 2]
    if not rest:
        return None
    letter, size = initial["text"], max(char["size"] for char in rest)
    if not (len(letter) == 1 and letter.isalpha() and letter.isupper() and rest[0]["text"][:1].islower()):
        return None
    if rest[0]["x0"] - initial["x1"] > _DROP_CAP_GAP_RATIO * size:
        return None
    if not _DROP_CAP_MIN_RATIO * size <= initial["size"] <= _DROP_CAP_MAX_RATIO * size:
        return None
    top, bottom = first_top, max(char["bottom"] for char in rest)
    foot, first_foot = initial["bottom"] - _DESCENT * initial["size"], bottom - _DESCENT * size  # baselines
    if abs((foot - _CAP_HEIGHT * initial["size"]) - (first_foot - _CAP_HEIGHT * size)) > _DROP_CAP_TOP_RATIO * size:
        return None
    if foot < first_foot + _DROP_CAP_DROP_RATIO * size or not any(
            abs(char["bottom"] - _DESCENT * char["size"] - foot) <= _DROP_CAP_FOOT_RATIO * size for char in others):
        return None
    if not line_text.startswith(letter + " ") or not line_text[2:3] == rest[0]["text"][:1]:
        return None
    spaced = any(not char["text"].strip() and initial["x0"] <= char["x0"] <= rest[0]["x0"]
                 and char["top"] < first_top + char.get("size", 0.0) / 2 for char in chars)
    if spaced or _initial_is_a_word(letter, line_text[2:]):
        return line_text, top, bottom
    return letter + line_text[2:], top, bottom


# Lone capitals looked at per page as initials drawn after their lines (`_joined_late_initials`). A page has a few drop caps; a
# crafted page of many single capitals would otherwise compare each with every line.
_MAX_LATE_INITIALS = 16


def _joined_late_initials(prepared: list[tuple[dict, int | None, str, list[dict]]],
                          chars_of: Callable[[dict], list[dict]]) -> list[tuple[dict, int | None, str, list[dict]]]:
    """`prepared` (layout line, XObject number, text, characters around it) with each layout line of a lone capital that opens
    the line beside it as a drop cap (`_drop_cap`) made one line with it, as pdfminer makes them when the initial is drawn
    first; `chars_of` gives the characters around a line.

    LibreOffice writes a drop cap after the text of its paragraph, so pdfminer kept the initial a line of its own: it came out
    as a paragraph of its own and its word lost its first letter ('O' / 'nce upon a time') (Pinned by:
    test_pdf_reading_order.py::TestDropCaps::test_a_drop_cap_drawn_after_its_lines_is_joined_to_its_word). The line beside is
    the highest one that starts right of the capital's left edge and no further than its height past it, with its middle
    within the capital's height, on the same page or XObject; the two are joined only when `_drop_cap` takes the joined line
    for a drop cap, so a large letter that is a label, or a line of another paragraph, stays apart as before. Deliberate: past
    `_MAX_LATE_INITIALS` lone capitals on a page, the rest are left as they are."""
    initials = [index for index, (_, _, text, _) in enumerate(prepared)
                if len(text) == 1 and text.isalpha() and text.isupper()][:_MAX_LATE_INITIALS]
    replaced: dict[int, tuple[dict, int | None, str, list[dict]] | None] = {}
    for index in initials:
        # A capital already joined as the line beside another initial is not an initial itself: joined again, the line it
        # was joined to was dropped with the first initial (Pinned by: test_pdf_reading_order.py::TestDropCaps::
        # test_a_lone_capital_joined_beside_an_initial_is_not_joined_again)
        if index in replaced:
            continue
        initial, owner, letter, _ = prepared[index]
        reach = initial["bottom"] - initial["top"]
        beside = [other for other, (line, line_owner, _, _) in enumerate(prepared)
                  if other != index and other not in replaced and line_owner == owner
                  and initial["top"] <= (line["top"] + line["bottom"]) / 2 <= initial["bottom"]
                  and initial["x0"] < line["x0"] <= initial["x1"] + reach]
        if not beside:
            continue
        first = min(beside, key=lambda other: (prepared[other][0]["top"], prepared[other][0]["x0"]))
        line, _, text, _ = prepared[first]
        joined = {**line, "x0": initial["x0"], "top": min(line["top"], initial["top"]),
                  "bottom": max(line["bottom"], initial["bottom"]), "text": f"{letter} {text}"}
        inside = chars_of(joined)
        if _drop_cap(inside, joined["text"]) is not None:
            replaced[first] = (joined, owner, joined["text"], inside)
            replaced[index] = None
    if not replaced:
        return prepared
    return [replaced.get(index, entry) for index, entry in enumerate(prepared) if replaced.get(index, entry) is not None]
