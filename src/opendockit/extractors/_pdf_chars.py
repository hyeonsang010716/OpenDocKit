"""The text of a run of characters: characters laid left to right with the spaces their gaps stand for, letter-spaced words
closed up, and superscripts and subscripts marked."""

from bisect import bisect_left, bisect_right
import math
import re
import statistics

from opendockit.extractors._inline import SUBSCRIPT, SUPERSCRIPT, mark_script, resolve_scripts
from opendockit.extractors._pdf_model import _SPACED_CHAR
from opendockit.extractors._pdf_pdfminer import _LAPARAMS


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
    word_margin) are the word spacing."""
    parts: list[str] = []
    previous = None
    for char in sorted(chars, key=lambda char: char["x0"]):
        if not char["text"].strip():
            parts.append(" ")
            previous = None
            continue
        if previous is not None and char["x0"] - previous["x1"] > _LAPARAMS["word_margin"] * max(char["width"], char["height"]):
            parts.append(" ")
        parts.append(char["text"])
        previous = char
    return " ".join("".join(parts).split())


def _glyph_gaps(chars: list[dict]) -> tuple[list[dict], list[float]]:
    """The line's characters laid left to right, and the gaps between two neighbouring characters that have no space character
    between them."""
    ordered = sorted(chars, key=lambda char: char["x0"])
    gaps = [right["x0"] - left["x1"] for left, right in zip(ordered, ordered[1:], strict=False)
            if left["text"].strip() and right["text"].strip()]
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
    return parts if " ".join(part[2] for part in parts) == text else ()


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
    marked: list[tuple[str, str | None]] = []  # (character, script mark), left to right
    for char in sorted(visible, key=lambda char: char["x0"]):
        mark = None
        rise = body_baseline - baseline(char)  # how far it is raised (pt)
        if char["size"] <= body_size * _SCRIPT_SIZE_RATIO:
            mark = SUPERSCRIPT if rise >= body_size * _SUPERSCRIPT_RISE else SUBSCRIPT if rise <= -body_size * _SUBSCRIPT_DROP else None
        elif char["size"] <= body_size * _SCRIPT_FAR_SIZE_RATIO and abs(rise) >= body_size * _SCRIPT_FAR_SHIFT:
            mark = SUPERSCRIPT if rise > 0 else SUBSCRIPT
        marked.extend((letter, mark) for letter in char["text"] if not letter.isspace())
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


def _is_hangul(text: str) -> bool:
    return len(text) == 1 and "가" <= text <= "힣"


# A drop cap (`_drop_cap`): an initial letter this many times the size of the text beside it, at most this far from the first
# letter after it and from the top of the line (relative to the text size).
_DROP_CAP_MIN_RATIO = 2.0
_DROP_CAP_MAX_RATIO = 6.0
_DROP_CAP_GAP_RATIO = 1.0
_DROP_CAP_TOP_RATIO = 1.0


def _drop_cap(chars: list[dict], line_text: str) -> tuple[str, float, float] | None:
    """(text, top, bottom) of a line that opens with a drop cap, the initial joined to the rest of its word and the line as high as
    the text beside the initial. None for any other line.

    A paragraph that opens with a large initial hanging beside its first lines ('T' + 'he quick brown') was read 'T he quick
    brown', and the height of the initial kept the first line apart from the rest of its paragraph (Pinned by:
    test_pdf_reading_order.py::TestDropCaps). Only a capital letter, `_DROP_CAP_MIN_RATIO` to `_DROP_CAP_MAX_RATIO` times the size
    of every other character of the line, followed within `_DROP_CAP_GAP_RATIO` by a lower-case letter, whose top is within
    `_DROP_CAP_TOP_RATIO` of the line's top and which reaches at least one more line below it. A large label beside a line (a
    capital or a digit after it, a letter standing on the line's baseline) keeps its space. Known limitation: a one-letter word
    set as a drop cap ('A' + 'long time ago') is joined to the next word as well.
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
    if not _DROP_CAP_MIN_RATIO * size <= initial["size"] <= _DROP_CAP_MAX_RATIO * size:
        return None
    top, bottom = first_top, max(char["bottom"] for char in rest)
    if (not -_DROP_CAP_GAP_RATIO * size <= rest[0]["x0"] - initial["x1"] <= _DROP_CAP_GAP_RATIO * size
            or abs(initial["top"] - top) > _DROP_CAP_TOP_RATIO * size or initial["bottom"] < bottom + size):
        return None
    if not line_text.startswith(letter + " ") or not line_text[2:3] == rest[0]["text"][:1]:
        return None
    return letter + line_text[2:], top, bottom
