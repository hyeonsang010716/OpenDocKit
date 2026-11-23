"""Symbol-font (Symbol, Wingdings) characters → Unicode, with the same per-font tables in DOCX, PPTX and PDF."""

import re


# ---- Symbol fonts ------------------------------------------------------------------------------------------------

# A symbol-font character is a glyph number inside the font (00FE, or F0FE in the private use area). The same number means different
# things per font (Symbol B3 is ≥, Wingdings A8 is □), so where the font is known it is converted straight to Unicode with the
# per-font table (`SYMBOL_GLYPHS`). Used by DOCX w:sym, characters of symbol-font runs (w:rFonts) and characters of PPTX runs
# (a:sym, a:latin typeface). Characters not in the table are left to the shared mapping of `markdown.clean_text` (erased if not
# there either). The shared mapping holds only numbers with one meaning regardless of font, so Wingdings ☒ (78) and □ (6F) were erased
# (Pinned by: test_office.py::TestDocxStruckDeletedAndSymbolRuns, test_office_symbols_fields_notes.py::TestSymbolFontCharactersInText).
SYMBOL_FONTS = ("symbol", "wingdings", "webdings")


def _glyph_run(start: int, glyphs: str) -> dict[int, str]:
    """Glyphs laid out in order from glyph number `start`. A space marks a number with no corresponding Unicode character."""
    return {start + offset: glyph for offset, glyph in enumerate(glyphs) if glyph != " "}


# Symbol is the standard mapping of the Adobe Symbol encoding (bracket pieces and extender pieces are left out). The digits and
# punctuation at 0x20-0x3F are the same as ASCII. Wingdings / Wingdings 2 hold only what carries meaning in documents (checks, boxes,
# bullets, circled digits, arrows, pointing hands). Where the exact Unicode character lies outside the Basic Multilingual Plane, as
# for arrows and checks, a common character of the same meaning is used (the same characters as the existing clean_text mapping).
SYMBOL_GLYPHS: dict[str, dict[int, str]] = {
    "symbol": {
        **{code: chr(code) for code in range(0x20, 0x40)},
        **_glyph_run(0x41, "ΑΒΧΔΕΦΓΗΙϑΚΛΜΝΟΠΘΡΣΤΥςΩΞΨΖ[∴]⊥_"),
        **_glyph_run(0x61, "αβχδεφγηιϕκλμνοπθρστυϖωξψζ{|}∼"),
        **_glyph_run(0xA0, "€ϒ′≤⁄∞ƒ♣♦♥♠↔←↑→↓°±″≥×∝∂•÷≠≡≈…  ↵ℵℑℜ℘⊗⊕∅∩∪⊃⊇⊄⊂⊆∈∉∠∇®©™∏√⋅¬∧∨⇔⇐⇑⇒⇓◊⟨®©™∑"),
        0x22: "∀", 0x24: "∃", 0x27: "∋", 0x2A: "∗", 0x2D: "−", 0x40: "≅", 0xF1: "⟩", 0xF2: "∫",
    },
    "wingdings": {
        **_glyph_run(0x45, "☜☞☝☟"), **_glyph_run(0x81, "①②③④⑤⑥⑦⑧⑨⑩"), **_glyph_run(0x8C, "❶❷❸❹❺❻❼❽❾❿"),
        **_glyph_run(0xDF, "←→↑↓↖↗↙↘"), **_glyph_run(0xEF, "⇦⇨⇧⇩"), **_glyph_run(0xFB, "✗✓☒☑"),
        0x6C: "●", 0x6D: "○", 0x6E: "■", 0x6F: "□", 0x71: "❑", 0x72: "❒", 0x75: "◆", 0x76: "❖", 0x78: "☒", 0x9E: "·", 0x9F: "•",
        0xA0: "▪", 0xA1: "○",  # A0 is the smaller square of the two (A7), the mark of a sub-item in Hangul documents
        0xA7: "▪", 0xA8: "□", 0xAB: "★", 0xD8: "➢", 0xE8: "➔",
    },
    "wingdings 2": {0x4F: "✗", 0x50: "✓", 0x51: "☒", 0x52: "☑", 0x53: "☒", 0x54: "☒", 0xA3: "☐"},
    # Wingdings 3 is arrows and triangles in several weights, each weight in the same order of directions; the weights, the
    # outlines and the bends of an arrow are not told apart. Webdings is pictures: only the few with a meaning in a sentence or
    # a table (a check, a cross, a square, the triangles of a player) are read. Both were checked against the glyphs the fonts
    # draw (Pinned by: test_office_symbols_fields_notes.py::TestSymbolsOfTheOtherSymbolFonts).
    "wingdings 3": {
        **_glyph_run(0x21, "←→↑↓↖↗↙↘⇤⇥"), **_glyph_run(0x31, "↔↕"), **_glyph_run(0x48, "⇇⇉⇈⇊"), **_glyph_run(0x50, "↻↺"),
        **_glyph_run(0x5A, "⇦⇨" * 6), **_glyph_run(0x66, "←→↑↓↖↗↙↘↔↕▲▼△▽◀▶◁▷◣◢◤◥◂▸▴"), **_glyph_run(0x80, "▾▲▼◀▶"),
        **_glyph_run(0x89, "←→↑↓" * 7), **_glyph_run(0xA5, "←→←→←→←"), **_glyph_run(0xB0, "→←→"), **_glyph_run(0xB5, "←→↑↓" * 5),
        **_glyph_run(0xD1, "←→↑↓↖↗↙↘" * 3), **_glyph_run(0xE9, "◂▸▴▾"),
    },
    "webdings": {**_glyph_run(0x33, "◀▶▲▼"), 0x3C: "■", 0x3D: "●", 0x59: "♥", 0x61: "✓", 0x63: "□", 0x67: "■", 0x6E: "●",
                 0x72: "✗", 0x73: "?"},
}
# The fonts whose numbers mean something else than the same numbers in Wingdings. A number their table does not hold is left
# out: handed on to the shared mapping (`markdown._SYMBOL_FONT_GLYPHS`), which holds the Wingdings meaning of each number, the
# down triangle of Wingdings 3 (71) came out as ❑ and the right triangle (75) as ◆.
_OTHER_MEANINGS = frozenset({"wingdings 2", "wingdings 3", "webdings"})
# Characters to convert in the text of a symbol-font run: U+F020-F0FF, font numbers moved into the private use area.
_SYMBOL_PRIVATE_USE = re.compile("[\uf020-\uf0ff]")


def symbol_glyph(font: str, code: int) -> str | None:
    """Glyph number `code` (00xx or F0xx) of symbol font `font` (case-insensitive) → Unicode character. None if not in the table."""
    table = SYMBOL_GLYPHS.get(font.strip().lower())
    if table is None:
        return None
    return table.get(code - 0xF000 if 0xF000 <= code <= 0xF0FF else code)


def has_other_meanings(font: str | None) -> bool:
    """Is `font` (case-insensitive) a symbol font whose numbers do not mean what they mean in Wingdings (`_OTHER_MEANINGS`)?"""
    return bool(font) and font.strip().lower() in _OTHER_MEANINGS


def symbol_font_text(font: str | None, text: str, *, ascii_font: str | None = None, latin_font: str | None = None) -> str:
    """Convert private use area characters (U+F0xx) in the text of a symbol-font run with that font's table. A font not in the
    tables or a character not in the table is left as is (`clean_text` handles it with the shared mapping), but for a font
    whose numbers mean something else than in Wingdings (`_OTHER_MEANINGS`), where such a character is left out.

    `ascii_font` / `latin_font` are the fonts that draw ordinary characters (ASCII, the rest of Latin-1). If that is Symbol, those
    characters are also taken as Adobe Symbol encoding glyph numbers and converted — m, a and ³, shown on screen as μ, α and ≥, came
    out unchanged as '10 mm, a ³ 30' (Pinned by:
    test_office_symbols_fields_notes.py::TestSymbolFontCharactersInText::
    test_plain_letters_in_a_symbol_font_run_in_docx_become_the_symbols_they_show). Ordinary characters in Wingdings are not
    converted (the table holds only some meaningful symbols). Characters outside Latin-1 such as Hangul are drawn with the East
    Asian font, so they are not touched."""
    plain_ascii, plain_latin = (bool(name) and name.strip().lower() == "symbol" for name in (ascii_font, latin_font))
    private = bool(font) and font.strip().lower() in SYMBOL_GLYPHS and _SYMBOL_PRIVATE_USE.search(text) is not None
    if not (private or plain_ascii or plain_latin):
        return text

    plain = SYMBOL_GLYPHS["symbol"]
    other = has_other_meanings(font)

    def glyph(character: str) -> str:
        code = ord(character)
        if 0xF000 <= code <= 0xF0FF:
            return ((symbol_glyph(font, code) or ("" if other else character)) if private else character)
        if (plain_ascii and code < 0x80) or (plain_latin and 0x80 <= code <= 0xFF):
            return plain.get(code, character)
        return character

    return "".join(map(glyph, text))
