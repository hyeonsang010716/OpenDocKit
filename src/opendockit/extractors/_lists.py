"""Lists shared by DOCX, PPTX, HWP and HWPX: auto-numbering strings (1. 가. ① …) from the same number shapes, the prefix of
a list item, level marks inside a table cell, and the head and bullet level of a Hangul (HWP) paragraph.
"""

from dataclasses import dataclass, field
import re

from opendockit.extractors._notation import OLE_PLACEHOLDER
from opendockit.extractors._package import decimal_int
from opendockit.models import IMAGE_MARKER


# Upper bound for list indent levels (PowerPoint and Word use 0-8)
# (Pinned by: test_office.py::TestPptx::test_absurd_paragraph_level_does_not_build_a_huge_indent).
MAX_LEVEL = 8


# ---- Hand-typed bullets ------------------------------------------------------------------------------------------

# Bullet symbols other than '- ' / '* ' that make a line joined by a line break (DOCX w:br, PPTX a:br) inside a list item paragraph
# count as an item one level down (DOCX `_DocxRenderer._list_item_lines`, PPTX `pptx._item_lines`).
HAND_TYPED_BULLET = re.compile(r"[·•●○◦▪■□◆◇▶►➢✓]")


# ---- List levels inside table cells ------------------------------------------------------------------------------

def cell_level_mark(level: int) -> str:
    """Level mark of a list line inside a table cell: none for level 1, "└ " for level 2, "└─ " for level 3 …. Shared by HWP, HWPX
    and DOCX (also SmartArt in a cell and PPTX speaker-note quote blocks, where leading spaces are erased).

    Leading spaces of a line in a cell are erased when the table is built (`markdown._clean_cell`), so indentation cannot convey the
    level. Doubling the bullet ("-- ") cannot be used for numbered lists ("가.") and also reads as a long dash, so a box-drawing
    character was chosen: it does not clash with paragraph heads, is widely used in tree notation and is read by LLMs as a sub-item.
    It is one token per level, so it adds little noise to search and embeddings
    (Pinned by: test_hwp.py::TestListLevelsInsideTableCells).
    """
    return f"└{'─' * (level - 1)} " if level > 0 else ""


# ---- Hangul (HWP) paragraph heads --------------------------------------------------------------------------------


# Placeholders for non-text objects (pictures, and OLE objects / videos in HWP/HWPX). A paragraph piece holding only these is not
# text that takes a paragraph head.
_OBJECT_PLACEHOLDERS = re.compile(rf"{IMAGE_MARKER.pattern}|{re.escape(OLE_PLACEHOLDER)}|\[Video\]")


def is_placeholder_only(text: str) -> bool:
    """Is the paragraph piece nothing but object placeholders (HWP, HWPX)? In a numbered/outline paragraph where a picture comes
    before a table or text box, the head was attached to the first piece holding only the picture, so '2.' / '#' was left alone and
    the real text lost its number (Pinned by: test_hwp_heads_and_shapes.py::
    TestHeadOfAParagraphThatStartsWithAPicture)."""
    return not _OBJECT_PLACEHOLDERS.sub("", text).strip()


def paragraph_head(mark: str, level: int, *, in_cell: bool, heading: bool) -> str:
    """Paragraph head text for HWP/HWPX. `mark` is a number ('가.'), a bullet ('-') or nothing (''); `level` is the level, from 0.

    Lower-level numbers in the body take the same `list_item_prefix` form as DOCX/PPTX (`  - 가. `) — with only indentation added,
    `clean_text` erased it and a blank line appeared at every item (Pinned by:
    test_hwp_heads_and_shapes.py::TestLowerLevelNumbers). Inside a cell it is `cell_level_mark`; an outline paragraph that becomes a
    heading gets the number only (a bullet is dropped — the same as a numbered heading in DOCX).
    """
    if not mark:
        return ""
    if heading:
        return "" if mark == "-" else f"{mark} "
    if in_cell:
        return f"{cell_level_mark(level)}{mark} "
    return f"{'  ' * level}- " if mark == "-" else list_item_prefix(mark, level)


# Hangul (HWP) paragraph left margins (HWPUNIT, the HWP binary value) that are equal within this much are the same level.
_MARGIN_TOLERANCE = 100


@dataclass(slots=True)
class BulletNesting:
    """Aligns the output level of HWP/HWPX bullet paragraphs by left margin. Hangul reports that make □, ○ and - separate bullet
    definitions, all at level 0, and express the hierarchy by paragraph margin alone became a flat `- ` list, with no telling which
    item something was under.

    Conservatively: one level down only when the level equals that of the immediately preceding unclosed bullet item and the margin
    is larger. With an equal or smaller margin that item is closed (sibling / upper level). A non-bullet paragraph closes only items
    indented more than itself, and with no margin (0) closes them all (the same direction as DOCX `_list_depth` /
    `_close_list_items`). Numbered paragraphs are not touched. Table cells, footnotes and text boxes are counted separately (the
    caller creates a new one. Pinned by: test_hwp_heads_and_shapes.py::TestBulletNestingByMargin).
    """

    _open: list[tuple[int, int, int]] = field(default_factory=list)  # unclosed earlier items: (level, left margin, emitted depth)

    def bullet(self, level: int, margin: int) -> int:
        """Output level of one bullet paragraph."""
        items = self._open
        while items and items[-1][1] >= margin - _MARGIN_TOLERANCE:
            items.pop()
        depth = min(items[-1][2] + 1, 8) if items and items[-1][0] == level else level
        items.append((level, margin, depth))
        return depth

    def other(self, margin: int) -> None:
        """Earlier items closed by a non-bullet paragraph (numbered, heading, body)."""
        if margin <= _MARGIN_TOLERANCE:
            self._open.clear()
        while self._open and self._open[-1][1] > margin + _MARGIN_TOLERANCE:
            self._open.pop()


# ---- Auto-numbering ----------------------------------------------------------------------------------------------

# Hangul (HWP) number shapes. HWP writes the number in this order (bits 5-8 of the paragraph head info attribute), HWPX this name
# (numFormat).
NUMBER_SHAPES = (
    "DIGIT", "CIRCLED_DIGIT", "ROMAN_CAPITAL", "ROMAN_SMALL", "LATIN_CAPITAL", "LATIN_SMALL", "CIRCLED_LATIN_CAPITAL",
    "CIRCLED_LATIN_SMALL", "HANGUL_SYLLABLE", "CIRCLED_HANGUL_SYLLABLE", "HANGUL_JAMO", "CIRCLED_HANGUL_JAMO",
    "HANGUL_PHONETIC", "IDEOGRAPH", "CIRCLED_IDEOGRAPH",
)
_HANGUL = "가나다라마바사아자차카타파하"
_JAMO = "ㄱㄴㄷㄹㅁㅂㅅㅇㅈㅊㅋㅌㅍㅎ"
# Counting numbers (일, 一): digit characters, place characters (십, 백, 천), empty-place character. Numbers after 10 are also written
# as counting words — shapes checked on screen in LibreOffice (십일, 이십, 백일, 백십일, 十一, 二十, 一百, 一百零一, 一百一十一). Korean
# does not write '일' before a place and does not write empty places
# (Pinned by: test_office_round45.py::TestNumbersPastTheTable).
_COUNTING = {"HANGUL_PHONETIC": ("일이삼사오육칠팔구", "십백천", ""), "IDEOGRAPH": ("一二三四五六七八九", "十百千", "零")}
# Word's koreanDigital writes a digit per place (10 → 일영, same as the LibreOffice screen)
_HANGUL_DIGIT_LETTERS = "영일이삼사오육칠팔구"
# Circled characters only as far as Unicode has them (beyond that, written without the circle). The same for parenthesized digits
# and digits with a full stop (Word-only shapes).
_CIRCLED = {
    "CIRCLED_DIGIT": "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳",
    "PARENTHESIZED_DIGIT": "⑴⑵⑶⑷⑸⑹⑺⑻⑼⑽⑾⑿⒀⒁⒂⒃⒄⒅⒆⒇",
    "DIGIT_FULL_STOP": "⒈⒉⒊⒋⒌⒍⒎⒏⒐⒑⒒⒓⒔⒕⒖⒗⒘⒙⒚⒛",
    "CIRCLED_LATIN_CAPITAL": "ⒶⒷⒸⒹⒺⒻⒼⒽⒾⒿⓀⓁⓂⓃⓄⓅⓆⓇⓈⓉⓊⓋⓌⓍⓎⓏ",
    "CIRCLED_LATIN_SMALL": "ⓐⓑⓒⓓⓔⓕⓖⓗⓘⓙⓚⓛⓜⓝⓞⓟⓠⓡⓢⓣⓤⓥⓦⓧⓨⓩ",
    "CIRCLED_HANGUL_SYLLABLE": "㉮㉯㉰㉱㉲㉳㉴㉵㉶㉷㉸㉹㉺㉻",
    "CIRCLED_HANGUL_JAMO": "㉠㉡㉢㉣㉤㉥㉦㉧㉨㉩㉪㉫㉬㉭",
    "CIRCLED_IDEOGRAPH": "㊀㊁㊂㊃㊄㊅㊆㊇㊈㊉",
}
_UNCIRCLED = {"CIRCLED_DIGIT": "DIGIT", "PARENTHESIZED_DIGIT": "DIGIT", "DIGIT_FULL_STOP": "DIGIT",
              "CIRCLED_LATIN_CAPITAL": "LATIN_CAPITAL", "CIRCLED_LATIN_SMALL": "LATIN_SMALL",
              "CIRCLED_HANGUL_SYLLABLE": "HANGUL_SYLLABLE", "CIRCLED_HANGUL_JAMO": "HANGUL_JAMO",
              "CIRCLED_IDEOGRAPH": "IDEOGRAPH"}
# Word number format (w:numFmt) → Hangul (HWP) number shape name. A format not listed here is written in Arabic digits. 01., ⑴, ⒈
# and １ are Word-only shapes that Hangul lacks, so they get names of their own (Pinned by: test_office.py::
# TestOfficeReadingOrderAndListMarks::test_zero_padded_parenthesized_full_stop_and_full_width_numbers).
DOCX_NUMBER_SHAPES = {
    "decimal": "DIGIT", "decimalZero": "ZERO_PADDED_DIGIT", "decimalEnclosedParen": "PARENTHESIZED_DIGIT",
    "decimalEnclosedFullstop": "DIGIT_FULL_STOP", "decimalFullWidth": "FULLWIDTH_DIGIT",
    "decimalEnclosedCircle": "CIRCLED_DIGIT", "decimalEnclosedCircleChinese": "CIRCLED_DIGIT",
    "upperRoman": "ROMAN_CAPITAL", "lowerRoman": "ROMAN_SMALL", "upperLetter": "LATIN_CAPITAL",
    "lowerLetter": "LATIN_SMALL", "ganada": "HANGUL_SYLLABLE", "chosung": "HANGUL_JAMO",
    "koreanDigital": "HANGUL_DIGIT", "koreanCounting": "HANGUL_PHONETIC", "ideographDigital": "IDEOGRAPH",
    "chineseCounting": "IDEOGRAPH", "japaneseCounting": "IDEOGRAPH", "ideographEnclosedCircle": "CIRCLED_IDEOGRAPH",
    "none": "NONE",
}
_FULLWIDTH_DIGITS = str.maketrans("0123456789", "０１２３４５６７８９")
_ROMAN = ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"), (40, "XL"), (10, "X"),
          (9, "IX"), (5, "V"), (4, "IV"), (1, "I"))


def format_number(value: int, shape: str) -> str:
    """Write one number in a number shape (a `NUMBER_SHAPES` name). Unknown shapes and out-of-range values give Arabic digits. "NONE"
    (DOCX numFmt none) is empty text — Word does not write that level's number and shows only the rest of the number text (lvlText)."""
    if shape == "NONE":
        return ""
    if value < 1:
        return str(value)
    if shape in _CIRCLED:
        circled = _CIRCLED[shape]
        return circled[value - 1] if value <= len(circled) else format_number(value, _UNCIRCLED[shape])
    if shape in ("ROMAN_CAPITAL", "ROMAN_SMALL") and value < 4000:
        roman = _roman(value)
        return roman if shape == "ROMAN_CAPITAL" else roman.lower()
    if shape in ("LATIN_CAPITAL", "LATIN_SMALL") and value <= 26 * 3:
        # After A…Z come AA, BB … (both Word and Hangul repeat the same letter). The start number is a file value, so the length is bounded
        letter = chr(ord("A") + (value - 1) % 26) * ((value - 1) // 26 + 1)
        return letter if shape == "LATIN_CAPITAL" else letter.lower()
    if shape in ("HANGUL_SYLLABLE", "HANGUL_JAMO"):
        letters = _HANGUL if shape == "HANGUL_SYLLABLE" else _JAMO
        return letters[(value - 1) % len(letters)]
    if shape in _COUNTING and value < 10_000:
        return _counting_number(value, *_COUNTING[shape])
    if shape == "HANGUL_DIGIT":
        return "".join(_HANGUL_DIGIT_LETTERS[int(digit)] for digit in str(value))
    if shape == "ZERO_PADDED_DIGIT":
        return f"{value:02d}"
    if shape == "FULLWIDTH_DIGIT":
        return str(value).translate(_FULLWIDTH_DIGITS)
    return str(value)


def list_item_prefix(number: str, depth: int) -> str:
    """Head of a numbered list item (DOCX, PPTX): indentation + number. If the number of a lower level (depth > 0) is not of the
    '1.' form, a bullet is added, as in `- a. `.

    `markdown.clean_text` / `join_blocks` recognize only lines starting with `-`, `*` or `1.` as list items, keeping their
    indentation and joining the items. Sub-items like 'a.', '1)', '①', '1.1.' lost their indentation and gained blank lines around
    them, so there was no telling they were sub-items. It is the same form as letter numbers in HTML. The top level keeps the number
    as is — adding a bullet to '제1조' / '가.' paragraphs is noise
    (Pinned by: test_office.py::TestDocxNumbering::test_sub_items_with_numbers_other_than_digit_period_keep_their_level).
    """
    indent = "  " * depth
    return f"{indent}{number} " if depth == 0 or re.fullmatch(r"\d+\.", number) else f"{indent}- {number} "


def _counting_number(value: int, digits: str, units: str, zero: str) -> str:
    """1-9999 as counting words: 'digit + place' from the highest place. If there is an empty-place character (零), one is inserted
    for an empty place between digits. The 1 before a place is never written in Korean, and in Han characters is omitted only at the
    very front of 10-19 (十一, 一百一十一)."""
    parts = []
    gap = False
    for place in range(3, -1, -1):
        digit = value // 10 ** place % 10
        if digit == 0:
            gap = gap or bool(parts)
            continue
        if gap and zero:
            parts.append(zero)
        gap = False
        unit = units[place - 1] if place else ""
        parts.append(unit if digit == 1 and unit and not zero else digits[digit - 1] + unit)
    text = "".join(parts)
    return text[1:] if zero and 10 <= value < 20 else text


def _roman(value: int) -> str:
    parts = []
    for amount, symbol in _ROMAN:
        count, value = divmod(value, amount)
        parts.append(symbol * count)
    return "".join(parts)


@dataclass(frozen=True, slots=True)
class NumberLevel:
    """One level of a numbered list. `<mark><level>` in `template` (HWP/HWPX `^1`, DOCX `%1`) is replaced by that level's number."""

    template: str
    shape: str = "DIGIT"
    start: int = 1
    # Legal-style numbering (DOCX w:isLgl): in this level's number text, the numbers of the upper levels are also written in Arabic
    # digits ('1.1.' under 'I.')
    # (Pinned by: test_office.py::TestOfficeLineBreaksNestedFieldsAndMetadata::test_docx_legal_numbering_shows_every_level_in_arabic_digits).
    legal: bool = False
    # The deepest upper level that makes this level count from 1 again (1-based, DOCX w:lvlRestart — ECMA-376 17.9.11). None restarts
    # whenever any shallower level, the one right above included, appears (the default; HWP/HWPX always use this). 0 never restarts —
    # '제N조' (Article N) continuing across chapters
    # (Pinned by: test_office_revisions_and_numbering.py::TestDocxLevelRestart).
    restart_after: int | None = None


@dataclass(slots=True)
class Numbering:
    """Per-level counters of one numbered list (HWP/HWPX paragraph numbering, DOCX w:num).

    When a level's number appears, deeper levels count from the start again (except levels held back by DOCX w:lvlRestart), and
    shallower levels that have not appeared yet are taken at their start number
    (the "4. → 4.1.1.1." behavior of ParaHead in the public hwpxlib sample. Pinned by: test_hwp.py::TestParagraphNumbering,
    TestHwpxObjects::test_paragraph_numbers_and_bullets_are_kept).
    """

    levels: list[NumberLevel]
    mark: str = "^"
    counters: dict[int, int] = field(default_factory=dict)

    def advance(self, level: int) -> str:
        """Number string of one paragraph at `level` (from 0). A level not in the definition uses the shape of the last level."""
        if not self.levels:
            return ""
        level = min(max(level, 0), len(self.levels) - 1)
        for shallower in range(level):
            self.counters.setdefault(shallower, self._level(shallower).start)
        self.counters[level] = self.counters[level] + 1 if level in self.counters else self._level(level).start
        for deeper in [key for key in self.counters if key > level and self._restarts_after(key, level)]:
            del self.counters[deeper]

        def number(match: re.Match[str]) -> str:
            token = match.group(1)
            # A level number of more than 4,300 digits is a level that does not exist (`decimal_int`), not an error that rejects
            # the document (Pinned by: test_audit_core.py::TestHugeLevelNumberInNumberText).
            target = level if token in ("N", "n") else decimal_int(token, 0) - 1
            if not 0 <= target <= level:
                return ""
            shape = "DIGIT" if self.levels[level].legal else self._level(target).shape
            return format_number(self.counters.get(target, self._level(target).start), shape)

        # `^N` / `^n` (the number of the level itself) is Hangul (HWP) notation only. Read for DOCX too, `%N` made '100%Note %1.'
        # come out as '1001ote 1.' (Pinned by: test_audit_core.py::TestLetterAfterAPercentSignInNumberText).
        token = r"(\d+|[Nn])" if self.mark == "^" else r"(\d+)"
        return re.sub(re.escape(self.mark) + token, number, self.levels[level].template).strip()

    def _level(self, level: int) -> NumberLevel:
        return self.levels[min(level, len(self.levels) - 1)]

    def _restarts_after(self, deeper: int, level: int) -> bool:
        """When a number at `level` (from 0) appears, is the deeper `deeper` level counted again (`NumberLevel.restart_after`)?"""
        restart_after = self._level(deeper).restart_after
        return restart_after is None or level < restart_after
