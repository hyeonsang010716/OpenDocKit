"""Text written from right to left (Hebrew, Arabic and the scripts beside them): a line of a PDF is brought from the order
of its glyphs on the page to the order in which it is read.
"""

from collections.abc import Sequence
from contextvars import ContextVar
import re
from typing import NamedTuple
import unicodedata


# Hebrew, Arabic, Syriac, Thaana, N'Ko and the other scripts of that block, and their presentation forms. A line without
# any of them is never touched. The right-to-left scripts beyond the first plane are left as they stand: what a PDF
# holds of them is the chart of a font or the letters of a formula (Arabic mathematical symbols), set from left to right.
_RIGHT_TO_LEFT = re.compile("[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufefc]")
# The forms a font draws a letter in (initial, medial, final, isolated, ligatures): a PDF made by a browser names those,
# and the letters they stand for are what was written.
_PRESENTATION_FORM = re.compile("[\ufb1d-\ufdff\ufe70-\ufefc]")
# The stroke that stretches a word to the width of its line (tatweel) and the marks that only steer the direction
_STRETCH_AND_MARKS = re.compile("[\u0640\u200e\u200f\u202a-\u202e\u2066-\u2069]")
_MIRRORED = {"(": ")", ")": "(", "[": "]", "]": "[", "{": "}", "}": "{", "<": ">", ">": "<", "«": "»", "»": "«", "‹": "›", "›": "‹"}
_OPENING = "([{<«‹"
# The marks a sentence or a clause ends with: the end of the line they stand at is the end of its text (`_end_vote`)
_CLOSING_MARKS = ".:!?\u061f\u060c\u061b\u06d4"

class _Reading(NamedTuple):
    """What a line cannot say for itself and the page it stands on can (`_page_reading`)."""

    leftwards: bool | None = None  # the direction of the lines: True from right to left, False from left to right, None unknown
    mirrored: bool = False  # brackets are named as they look, not as they were written
    by_word: bool = False  # the lines were laid out by Microsoft Word, where a slash joins no numbers (`_levels`)


# Set for each page by `pdf._read_page`
_PAGE_READING: ContextVar[_Reading] = ContextVar("pdf_page_reading", default=_Reading())  # noqa: B039 — a tuple, never changed


def _is_mark(text: str) -> bool:
    """Whether a glyph is only marks set on a letter (vowel signs, shadda, points, accents, tone marks)."""
    return bool(text) and all(unicodedata.category(letter) in ("Mn", "Me") for letter in text)


def _kind(unit: str) -> str:
    """The class of the bidirectional algorithm a unit acts as: that of its first character, the neutral ones as one."""
    found = unicodedata.bidirectional(unit[0]) if unit else ""
    return found if found in ("L", "R", "AL", "EN", "AN", "ES", "ET", "CS", "NSM") else "ON"


def _levels(units: Sequence[str], rtl: bool, by_word: bool = False) -> list[int]:
    """The embedding level of each unit of a line: the rules W1 to W7, N1, N2, I1 and I2 of the Unicode Bidirectional
    Algorithm (UAX #9) for one line without explicit embeddings, read over the units as they stand on the page.

    The rules ask what stands before a number in the text. In a line laid out from right to left that is what stands
    to its right on the page — but for a number after Latin words, which is part of them and reads on from the left
    ('Windows 10'). A number between text of the two directions is shown the same way for two different texts; it is
    read with the Latin words before it, as the rules read it on the page.

    `by_word`: the line was laid out by Microsoft Word, where a slash joins no numbers (`_page_reading`): '70/1' or
    '25/9/2015' outside Latin text are numbers apart, the first to the right ('1/70' on the page), where the algorithm
    keeps them as one number from left to right."""
    kinds = [_kind(unit) for unit in units]
    base = "R" if rtl else "L"
    last = base
    for index, kind in enumerate(kinds):  # W1: a mark is what stands before it
        if kind == "NSM":
            kinds[index] = last
        else:
            last = kind
    if rtl:
        # W2 and W7 for a line from right to left: a number after left-to-right letters is one of them; any other is
        # an Arabic number when the letters before it in the text, to its right on the page, are Arabic ones
        after: list[str] = [base] * len(kinds)  # the strong class to the right of each place
        strong = base
        for index in range(len(kinds) - 1, -1, -1):
            after[index] = strong
            if kinds[index] in ("L", "R", "AL"):
                strong = kinds[index]
        strong = base
        for index, kind in enumerate(kinds):
            if kind == "EN":
                kinds[index] = "L" if strong == "L" else "AN" if after[index] == "AL" else "EN"
            elif kind in ("L", "R", "AL"):
                strong = kind
    else:
        strong = base
        for index, kind in enumerate(kinds):  # W2: a European number after Arabic letters is an Arabic number
            if kind == "EN" and strong == "AL":
                kinds[index] = "AN"
            elif kind in ("L", "R", "AL"):
                strong = kind
    kinds = ["R" if kind == "AL" else kind for kind in kinds]  # W3
    for index in range(1, len(kinds) - 1):  # W4: one separator between two numbers belongs to them
        before, kind, after_kind = kinds[index - 1], kinds[index], kinds[index + 1]
        if kind == "ES" and before == after_kind == "EN":
            kinds[index] = before
        elif kind == "CS" and before == after_kind and before in ("EN", "AN") and not (by_word and units[index] == "/"):
            kinds[index] = before
    for index, kind in enumerate(kinds):  # W5: the signs standing at a European number belong to it
        if kind != "EN":
            continue
        for step in (-1, 1):
            beside = index + step
            while 0 <= beside < len(kinds) and kinds[beside] == "ET":
                kinds[beside] = "EN"
                beside += step
    kinds = ["ON" if kind in ("ES", "ET", "CS") else kind for kind in kinds]  # W6
    if not rtl:
        strong = base
        for index, kind in enumerate(kinds):  # W7: a European number after left-to-right letters is one of them
            if kind == "EN" and strong == "L":
                kinds[index] = "L"
            elif kind in ("L", "R"):
                strong = kind
    index = 0
    while index < len(kinds):  # N1, N2: neutral units take the direction both sides agree on, else that of the line
        if kinds[index] != "ON":
            index += 1
            continue
        end = index
        while end < len(kinds) and kinds[end] == "ON":
            end += 1
        before = base if index == 0 else ("L" if kinds[index - 1] == "L" else "R")
        behind = base if end == len(kinds) else ("L" if kinds[end] == "L" else "R")
        kinds[index:end] = [before if before == behind else base] * (end - index)
        index = end
    if rtl:
        return [1 if kind == "R" else 2 for kind in kinds]
    return [0 if kind == "L" else 1 if kind == "R" else 2 for kind in kinds]


def _reordered(units: Sequence[str], rtl: bool, by_word: bool = False) -> list[tuple[str, bool]]:
    """The units of a line in the order of reading, each with whether it runs from right to left: rule L2 undone — every
    stretch at a level or above it is turned round, from the highest level down to the lowest odd one. Turning is its own
    inverse, so the steps that laid the line out lead back from the page to the text."""
    levels = _levels(units, rtl, by_word)
    order = list(range(len(units)))
    for level in range(max(levels, default=0), 0, -1):
        start = None
        for position in range(len(order) + 1):
            inside = position < len(order) and levels[order[position]] >= level
            if inside and start is None:
                start = position
            elif not inside and start is not None:
                order[start:position] = reversed(order[start:position])
                start = None
    return [(units[index], levels[index] % 2 == 1) for index in order]


def _matched(brackets: Sequence[str]) -> int:
    """How many pairs a row of brackets closes, each opened before it is closed."""
    pairs, open_ones = 0, []
    for bracket in brackets:
        if bracket in _OPENING:
            open_ones.append(bracket)
        elif open_ones and open_ones[-1] == _MIRRORED[bracket]:
            open_ones.pop()
            pairs += 1
    return pairs


def _bracket_vote(read: Sequence[tuple[str, bool]]) -> int:
    """Whether the brackets inside the right-to-left text of a line are named as they look on the page: positive when
    more pairs close after each is turned into its mirror image, negative when more close as they are, 0 when the line
    does not say."""
    brackets = [unit for unit, leftwards in read if leftwards and unit in _MIRRORED]
    return _matched([_MIRRORED[bracket] for bracket in brackets]) - _matched(brackets)


def _logical(units: Sequence[str], *, always: bool = False) -> str:
    """The text of a line whose units — one glyph each with the marks set on it, or a space — stand in the order of the
    page from left to right, in the order of reading. A line without a right-to-left character is joined as it stands.

    A PDF holds no text, only glyphs at places. A line of Arabic or Hebrew read from left to right is the text backwards
    ('םלוע םולש' for 'שלום עולם'), with the numbers and Latin words inside it the right way round, so the layout of the
    line is undone (`_reordered`). Three things the line does not say for itself:

    - Its direction as a whole, which says at which end a full stop stands and in which order Latin words and the
      words around them follow each other. A line that ends in a full stop, a colon or a comma after a right-to-left
      letter shows it (`_end_vote`): the mark stands at the left end of a line laid out from right to left, and at the
      right end when Arabic was typed into a paragraph laid out from left to right. Any other line goes by the lines
      of its page that show it, then by the letters of the page, then by its own (`_page_reading`).
    - Whether a bracket is named as written or as it looks. Word and LibreOffice name the bracket that was written, a
      browser the one that is drawn (the mirror image, inside right-to-left text). The line decides when one of the two
      readings closes more pairs (`_bracket_vote`), the page when the line cannot.
    - The letters. A glyph named by a presentation form (the shape of a letter at the start, in the middle or at the end
      of a word, a ligature) is the letter it draws, and the stroke that stretches a word to the width of its line
      (tatweel) is no part of the word.

    Deliberate: a glyph of several characters (a ligature) is kept as it is named — its characters are in reading order
    already — and a number between text of the two directions follows the left-to-right text before it, as the same
    rules read it on the page.
    Pinned by: test_pdf_right_to_left.py.
    """
    if not always and not any(_RIGHT_TO_LEFT.search(unit) for unit in units):
        return "".join(units)
    page = _PAGE_READING.get()
    read = _reordered(units, always or _runs_leftwards(units, page.leftwards), page.by_word)
    vote = _bracket_vote(read)
    if vote > 0 or (vote == 0 and page.mirrored):
        read = [(_MIRRORED.get(unit, unit) if leftwards else unit, leftwards) for unit, leftwards in read]
    text = _PRESENTATION_FORM.sub(lambda match: unicodedata.normalize("NFKC", match.group()), "".join(unit for unit, _ in read))
    return _STRETCH_AND_MARKS.sub("", text)


def _end_vote(units: Sequence[str]) -> int:
    """What the ends of a line say of its direction: 1 when a closing mark stands at its left end before a right-to-left
    letter (the line runs from right to left and ends there), -1 when one stands at its right end after such a letter
    (right-to-left words in a line running from left to right), 0 when neither."""
    solid = [unit for unit in units if unit.strip()]
    if len(solid) < 2:
        return 0
    if solid[0] in _CLOSING_MARKS and _kind(solid[1]) in ("R", "AL"):
        return 1
    if solid[-1] in _CLOSING_MARKS and _kind(solid[-2]) in ("R", "AL"):
        return -1
    return 0


# The number of a list item as it stands on a page read from right to left, set apart from its item: '.1' for '1.',
# ')1(' for '(1)', and '(1' for '1)' where a bracket is named as it looks
_NUMBER_AS_DRAWN = re.compile(r"[.)]\s?\d{1,3}\(?|\(\s?\d{1,3}")


def _number_set_apart(drawn: str) -> str | None:
    """The number of a list item on a page read from right to left, from its characters in the order of the page
    (`drawn`); None for any other text and on any other page.

    The number stands to the right of its item, apart from it, as a line of its own with no letter in it: '.1'. Only
    this shape is read back. A line of digits and signs is otherwise left as it stands — '-5' or '3/4' in the cell of a
    table is as often set from left to right as not, and reads the same way then."""
    if not _PAGE_READING.get().leftwards or not _NUMBER_AS_DRAWN.fullmatch(drawn):
        return None
    return _logical(list(drawn), always=True)


def _runs_leftwards(units: Sequence[str], page_leftwards: bool | None) -> bool:
    """Whether a line is laid out from right to left: by its ends when they show it (`_end_vote`), else by its page, else
    by its own letters."""
    end = _end_vote(units)
    if end:
        return end > 0
    return _mostly_leftwards(units) if page_leftwards is None else page_leftwards


def _mostly_leftwards(units: Sequence[str]) -> bool:
    kinds = [unicodedata.bidirectional(unit[0]) for unit in units if unit]
    return sum(kind in ("R", "AL") for kind in kinds) > sum(kind == "L" for kind in kinds)


def _visual_units(ordered: Sequence[dict], word_gap: float) -> list[str]:
    """The units of a line of characters sorted from left to right: the text of each glyph, a space for a space glyph and
    for a gap wider than `word_gap` times the size of the character after it (not for a space glyph drawn under the
    letter beside it), and the marks drawn on a letter joined to the letter they stand over, after it. A mark has no
    width of its own to speak of and is placed over its letter, so its place in the row from left to right says
    nothing: it belongs to the letter under its middle."""
    units: list[str] = []
    previous: dict | None = None  # the letter before, when no space stands between
    waiting = ""  # marks that stand over the letter to come
    for index, char in enumerate(ordered):
        text = char["text"]
        if not text.strip():
            # A space glyph that moves nothing — the letter beside it covers the place it is drawn at — is no space: a
            # browser draws the zero-width non-joiner inside a Persian word (U+200C, between 'می' and 'شود') as the space glyph of its font
            middle = (char["x0"] + char["x1"]) / 2
            if any(0 <= beside < len(ordered) and ordered[beside]["text"].strip()
                   and ordered[beside]["x0"] <= char["x0"] + 0.01 and ordered[beside]["x1"] > middle
                   for beside in (index - 1, index + 1)):
                continue
            units.append(" ")
            previous = None
            continue
        if _is_mark(text):
            if previous is not None and (char["x0"] + char["x1"]) / 2 <= previous["x1"]:
                units[-1] += text
            else:
                waiting += text
            continue
        if previous is not None and char["x0"] - previous["x1"] > word_gap * max(char["width"], char["height"]):
            units.append(" ")
        units.append(text + waiting)
        waiting = ""
        previous = char
    if waiting:
        units.append(waiting)
    return units


def _page_reading(chars: Sequence[dict], word_gap: float, made_by_word: bool = False) -> _Reading:
    """What `_logical` asks the page its line stands on.

    The direction is that of the lines whose ends show it (`_end_vote`), of most of the letters when none does or as
    many show one as the other, and unknown on a page whose letters are mostly left-to-right ones and whose lines do not
    say: there each line goes by its own letters. It and the brackets are judged on the rows of characters that share a
    bottom edge, each read as a line: a count over the page, not a reading of it.

    `made_by_word`: Word lays a line out by rules of its own. One shows on the page and is followed (`_levels`): of the
    23 numbers with a slash in the Arabic text of two resolutions of the United Nations ('70/1'), 20 stand on the page
    turned round ('1/70'). Two numbers with a hyphen stand either way, 60 as the algorithm sets them and 73 turned
    round, by how each was typed; those are read by the algorithm."""
    if not _RIGHT_TO_LEFT.search("".join(char["text"] for char in chars)):
        return _Reading()
    kinds = [unicodedata.bidirectional(char["text"][0]) for char in chars if char["text"]]
    mostly = sum(kind in ("R", "AL") for kind in kinds) > sum(kind == "L" for kind in kinds)
    rows: dict[int, list[dict]] = {}
    for char in chars:
        rows.setdefault(round(char["bottom"] / 2), []).append(char)
    lines = [_visual_units(sorted(row, key=lambda char: char["x0"]), word_gap) for row in rows.values()
             if any(_RIGHT_TO_LEFT.search(char["text"]) for char in row)]
    ends = sum(_end_vote(units) for units in lines)
    leftwards = ends > 0 if ends else mostly or None
    brackets = sum(_bracket_vote(_reordered(units, _runs_leftwards(units, leftwards), made_by_word))
                   for units in lines if any(unit in _MIRRORED for unit in units))
    return _Reading(leftwards, brackets > 0, made_by_word)
