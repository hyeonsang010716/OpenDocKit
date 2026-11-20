"""The symbols Hancom Office keeps in private-use characters, as text (HWP and HWPX).

Hangul writes its own symbols (the seal mark `(인)`, boxed numbers, bullets, book brackets) as characters of the supplementary
private use area, plane 15 (U+F0000 + X), which no other font has: kept, they are unreadable code points, and the shared cleaning
removes them (`markdown.clean_text`). HWP 5 writes some of them as the single BMP unit 0xA000 | X instead, and HWPX as U+F0000 | X.
Only symbols whose meaning is known are turned into text; the meaning of a private-use code is not guessed from its neighbours.

The values below were established by the rhwp project (MIT License, https://github.com/edwardkim/rhwp) against the PDFs Hancom
Office prints of real documents and against the HWPX Hangul saves of HWP 5 files (`HANCOM_SYMBOL_BMP_TO_PLANE15`,
`VERIFIED_HANCOM_PUA_DISPLAY`, `pua_overlap_digit` and the text-surface table of rhwp's renderer). Line and box pieces Hangul draws
rules and text diagrams with are left out on purpose: as text they are runs of rule characters, up to 96 in a row in a header line.
"""

import re


_PLANE_15 = 0xF0000

# HWP 5 units 0xA000 | X that Hangul shows as U+F0000 | X (its HWPX save of the same document holds those). Only these values:
# 0xA000-0xABFF is also the Yi, Lisu and Syloti Nagri script, and Hangul keeps U+A813 as it is in an English word set in Malgun Gothic.
_FOLDED = frozenset((
    0xA0E1, 0xA12B,
    0xA289, 0xA28A, 0xA292, 0xA293, 0xA294, 0xA295, 0xA296, 0xA297, 0xA298, 0xA299, 0xA29A, 0xA29B,
    0xA2B1, 0xA2B2, 0xA2B3, 0xA2B4, 0xA2B5, 0xA2B6, 0xA2B7, 0xA2B8, 0xA2B9,
    0xA2BA, 0xA2BB, 0xA2BC, 0xA2BD, 0xA2BE, 0xA2BF, 0xA2C0, 0xA2C3, 0xA2C4, 0xA2C5, 0xA2C6, 0xA2C7, 0xA2C8, 0xA2C9, 0xA2CA,
    0xA2CB, 0xA2CC,
    0xA2FC, 0xA3C5, 0xA807, 0xA80A, 0xA80E, 0xA80F, 0xA810, 0xA81A, 0xA832, 0xA852, 0xA853, 0xA854, 0xA855,
))
_UNFOLD = {unit: chr(_PLANE_15 | (unit & 0xFFF)) for unit in _FOLDED}
_FOLDED_UNIT = re.compile("[" + "".join(map(chr, sorted(_FOLDED))) + "]")

# Plane-15 symbols whose meaning Hancom's PDFs show → the text written for them. Boxed digits 0-7 are written as the enclosed
# digits (the boxed ones have no Unicode character); the rest of the row is not known for sure and stays out, and so does U+F02C5,
# read as a boxed 12 on its own but also the ones piece 2 of a boxed two-digit number (`framed_number`).
_SYMBOLS = {
    0xF003B: "↓", 0xF0090: "✺", 0xF012B: "(인)",
    0xF0288: "⓪", 0xF0289: "①", 0xF028A: "②", 0xF028C: "④", 0xF028D: "⑤", 0xF028E: "⑥", 0xF028F: "⑦", 0xF0290: "⑧", 0xF0291: "⑨",
    0xF02B0: "⓪", 0xF02B1: "①", 0xF02B2: "②", 0xF02B3: "③", 0xF02B4: "④", 0xF02B5: "⑤", 0xF02B6: "⑥", 0xF02B7: "⑦",
    0xF02EC: "◇", 0xF02EF: "·", 0xF02FB: "▸", 0xF02FC: "►", 0xF031C: "■",
    0xF03A0: "↵", 0xF03A7: "⊟", 0xF03A8: "⊞", 0xF03C5: "□", 0xF03DA: "□", 0xF03FF: "□",
    0xF03EF: "한", 0xF03F0: "글", 0xF03F1: "과", 0xF03F2: "컴", 0xF03F3: "퓨", 0xF03F4: "터",
    0xF0854: "『", 0xF0855: "』",
}
_PLANE_15_CHARACTER = re.compile("[\U000f0000-\U000ffffd]")

# The digits of a number set in a frame by composed characters (글자 겹치기): Hangul writes each digit as a private-use piece of its
# place. Two-digit pieces of a circle, a reversed circle, a box and a reversed box: tens 1-9 at base + 1-9, ones 0-9 at
# base + 10-19; three-digit pieces of a circle: hundreds 1-9, tens 0-9, ones 0-9 from U+F0491. Matched against the HWPX save of
# the same document, whose composeText holds the digits (`12`, `123`).
_TWO_DIGIT_BASES = (0xF0288, 0xF029C, 0xF02B9, 0xF02D6)
# The frame glyph HWP writes in front of the composed characters, per border kind of the control (1 circle … 13 thick circulating
# triangle, the order of HWPX circleType); HWPX keeps the kind as an attribute and only the characters in composeText.
FRAME_GLYPHS = {1: "◯", 2: "●", 3: "□", 4: "■", 5: "△", 6: "▲", 7: "☼", 8: "◇", 9: "◆", 10: "▢", 11: "♲", 12: "♺", 13: "♻"}


def unfold(text: str) -> str:
    """HWP 5 text with the Hancom symbols written as 0xA000 | X turned into the plane-15 characters HWPX holds for them."""
    return _FOLDED_UNIT.sub(lambda match: _UNFOLD[ord(match.group())], text) if _FOLDED_UNIT.search(text) else text


def hancom_symbols(text: str) -> str:
    """The text with the known plane-15 Hancom symbols written out (`_SYMBOLS`); other private-use characters are left for the
    cleaning to remove."""
    if not _PLANE_15_CHARACTER.search(text):
        return text
    return text.translate(_SYMBOLS)


def _digit_place(character: str) -> tuple[int, int] | None:
    """(place, digit) of a framed-number piece: place 0 for the highest digit; None when the character is none."""
    code = ord(character)
    for base in _TWO_DIGIT_BASES:
        if base + 1 <= code <= base + 9:
            return 1, code - base
        if base + 10 <= code <= base + 19:
            return 2, code - base - 10
    if 0xF0491 <= code <= 0xF0499:
        return 0, code - 0xF0490
    if 0xF049A <= code <= 0xF04A3:
        return 1, code - 0xF049A
    if 0xF04A4 <= code <= 0xF04AD:
        return 2, code - 0xF04A4
    return None


def framed_number(text: str) -> str | None:
    """The digits of composed characters that are nothing but framed-number pieces (`12` for U+F0289 U+F0294), or None."""
    places = [_digit_place(character) for character in text]
    if not places or None in places:
        return None
    return "".join(str(digit) for _, digit in sorted(places, key=lambda place: place[0]))
