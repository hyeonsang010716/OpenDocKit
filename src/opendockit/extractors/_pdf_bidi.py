"""Text written from right to left (Hebrew, Arabic and the scripts beside them): a line of a PDF is brought from the order
of its glyphs on the page to the order in which it is read.
"""

from collections.abc import Sequence
from contextvars import ContextVar
import re
import unicodedata

# Hebrew, Arabic, Syriac, Thaana, N'Ko and the other scripts of that block, their presentation forms, and the right-to-left
# scripts beyond the first plane. A line without any of them is never touched.
_RIGHT_TO_LEFT = re.compile("[֐-ࣿיִ-﷿ﹰ-ﻼ\U00010800-\U00010fff\U0001e800-\U0001efff]")
# The forms a font draws a letter in (initial, medial, final, isolated, ligatures): a PDF made by a browser names those,
# and the letters they stand for are what was written.
_PRESENTATION_FORM = re.compile("[יִ-﷿ﹰ-ﻼ]")
# The stroke that stretches a word to the width of its line (tatweel) and the marks that only steer the direction
_STRETCH_AND_MARKS = re.compile("[ـ‎‏‪-‮⁦-⁩]")
_MIRRORED = {"(": ")", ")": "(", "[": "]", "]": "[", "{": "}", "}": "{", "<": ">", ">": "<", "«": "»", "»": "«", "‹": "›", "›": "‹"}
_OPENING = "([{<«‹"

# (whether most letters of the page being read are right-to-left ones, whether its brackets are named as they look):
# what one line cannot say for itself (`_page_reading`). Set for each page by `pdf._read_page`.
_PAGE_READING: ContextVar[tuple[bool, bool]] = ContextVar("pdf_page_reading", default=(False, False))


def _is_mark(text: str) -> bool:
    """Whether a glyph is only marks set on a letter (vowel signs, shadda, points)."""
    return bool(text) and all(unicodedata.category(letter) == "Mn" for letter in text)


def _kind(unit: str) -> str:
    """The class of the bidirectional algorithm a unit acts as: that of its first character, the neutral ones as one."""
    found = unicodedata.bidirectional(unit[0]) if unit else ""
    return found if found in ("L", "R", "AL", "EN", "AN", "ES", "ET", "CS", "NSM") else "ON"


def _levels(units: Sequence[str], rtl: bool) -> list[int]:
    """The embedding level of each unit of a line: the rules W1 to W7, N1, N2, I1 and I2 of the Unicode Bidirectional
    Algorithm (UAX #9) for one line without explicit embeddings, read over the units in the order they stand on the page.
    Read that way the rules give each unit the level it had when the line was laid out, but for a number between text of
    the two directions, which the page shows the same way for two different orders."""
    kinds = [_kind(unit) for unit in units]
    base = "R" if rtl else "L"
    last = base
    for index, kind in enumerate(kinds):  # W1: a mark is what stands before it
        if kind == "NSM":
            kinds[index] = last
        else:
            last = kind
    strong = base
    for index, kind in enumerate(kinds):  # W2: a European number after Arabic letters is an Arabic number
        if kind == "EN" and strong == "AL":
            kinds[index] = "AN"
        elif kind in ("L", "R", "AL"):
            strong = kind
    kinds = ["R" if kind == "AL" else kind for kind in kinds]  # W3
    for index in range(1, len(kinds) - 1):  # W4: one separator between two numbers belongs to them
        before, kind, after = kinds[index - 1], kinds[index], kinds[index + 1]
        if (kind == "ES" and before == after == "EN") or (kind == "CS" and before == after and before in ("EN", "AN")):
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
        after = base if end == len(kinds) else ("L" if kinds[end] == "L" else "R")
        kinds[index:end] = [before if before == after else base] * (end - index)
        index = end
    if rtl:
        return [1 if kind == "R" else 2 for kind in kinds]
    return [0 if kind == "L" else 1 if kind == "R" else 2 for kind in kinds]


def _reordered(units: Sequence[str], rtl: bool) -> list[tuple[str, bool]]:
    """The units of a line in the order of reading, each with whether it runs from right to left: rule L2 undone — every
    stretch at a level or above it is turned round, from the highest level down to the lowest odd one. Turning is its own
    inverse, so the steps that laid the line out lead back from the page to the text."""
    levels = _levels(units, rtl)
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


def _logical(units: Sequence[str]) -> str:
    """The text of a line whose units — one glyph each with the marks set on it, or a space — stand in the order of the
    page from left to right, in the order of reading. A line without a right-to-left character is joined as it stands.

    A PDF holds no text, only glyphs at places. A line of Arabic or Hebrew read from left to right is the text backwards
    ('םלוע םולש' for 'שלום עולם'), with the numbers and Latin words inside it the right way round, so the layout of the
    line is undone (`_reordered`). Three things the line does not say for itself:

    - Its direction as a whole. On a page most of whose letters are right-to-left ones (`_page_reading`) it is right to
      left; elsewhere it is when it has more of those letters than of the others.
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
    if not any(_RIGHT_TO_LEFT.search(unit) for unit in units):
        return "".join(units)
    page_leftwards, page_mirrored = _PAGE_READING.get()
    read = _reordered(units, page_leftwards or _mostly_leftwards(units))
    vote = _bracket_vote(read)
    if vote > 0 or (vote == 0 and page_mirrored):
        read = [(_MIRRORED.get(unit, unit) if leftwards else unit, leftwards) for unit, leftwards in read]
    text = _PRESENTATION_FORM.sub(lambda match: unicodedata.normalize("NFKC", match.group()), "".join(unit for unit, _ in read))
    return _STRETCH_AND_MARKS.sub("", text)


def _mostly_leftwards(units: Sequence[str]) -> bool:
    kinds = [unicodedata.bidirectional(unit[0]) for unit in units if unit]
    return sum(kind in ("R", "AL") for kind in kinds) > sum(kind == "L" for kind in kinds)


def _visual_units(ordered: Sequence[dict], word_gap: float) -> list[str]:
    """The units of a line of characters sorted from left to right: the text of each glyph, a space for a space glyph and
    for a gap wider than `word_gap` times the size of the character after it, and the marks drawn on a letter joined to
    the letter they stand over, after it. A mark has no width of its own to speak of and is placed over its letter, so its
    place in the row from left to right says nothing: it belongs to the letter under its middle."""
    units: list[str] = []
    previous: dict | None = None  # the letter before, when no space stands between
    waiting = ""  # marks that stand over the letter to come
    for char in ordered:
        text = char["text"]
        if not text.strip():
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


def _page_reading(chars: Sequence[dict], word_gap: float) -> tuple[bool, bool]:
    """(whether most letters of a page are right-to-left ones, whether its brackets are named as they look): what
    `_logical` asks the page. The brackets are judged on the rows of characters that share a bottom edge, each read as a
    line: a count over the page, not a reading of it."""
    if not _RIGHT_TO_LEFT.search("".join(char["text"] for char in chars)):
        return False, False
    kinds = [unicodedata.bidirectional(char["text"][0]) for char in chars if char["text"]]
    leftwards = sum(kind in ("R", "AL") for kind in kinds) > sum(kind == "L" for kind in kinds)
    rows: dict[int, list[dict]] = {}
    for char in chars:
        rows.setdefault(round(char["bottom"] / 2), []).append(char)
    votes = 0
    for row in rows.values():
        if any(char["text"] in _MIRRORED for char in row) and any(_RIGHT_TO_LEFT.search(char["text"]) for char in row):
            units = _visual_units(sorted(row, key=lambda char: char["x0"]), word_gap)
            votes += _bracket_vote(_reordered(units, leftwards or _mostly_leftwards(units)))
    return leftwards, votes > 0
