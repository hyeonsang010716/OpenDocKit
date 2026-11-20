"""Characters pdfminer could not decode (U+FFFD) read again from pdfium at the same place on the page."""

from collections.abc import Callable
import ctypes
import math
import unicodedata

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from opendockit.extractors._pdf_model import _PDFIUM_LOCK


_REPLACEMENT = "�"
# How far (pt) the centre of pdfminer's character may lie outside the box of pdfium's character.
_GLYPH_TOLERANCE = 1.0
# Height bands (pt) of pdfium's characters, and the tallest character (in bands) put into them; a taller one has no place
_BAND, _MAX_BANDS = 16.0, 64

# pdfium's characters of a page: (left, right, bottom, top, text) in PDF user space, left → right.
_Glyphs = list[tuple[float, float, float, float, str]]


def _pdfium_glyphs(document: pdfium.PdfDocument | None, index: int | None) -> Callable[[], _Glyphs]:
    """A function that reads the characters of the page `index` of `document` with pdfium (once). Empty when there is no
    document or the page cannot be opened."""
    read: list[_Glyphs] = []

    def glyphs() -> _Glyphs:
        if not read:
            read.append(_read_glyphs(document, index) if document is not None and index is not None and index >= 0 else [])
        return read[0]

    return glyphs


def _read_glyphs(document: pdfium.PdfDocument, index: int) -> _Glyphs:
    found: _Glyphs = []
    with _PDFIUM_LOCK:
        try:
            page = document[index]
        except pdfium.PdfiumError:
            return found
        try:
            textpage = page.get_textpage()
            try:
                left, right, bottom, top = (ctypes.c_double() for _ in range(4))
                for number in range(textpage.count_chars()):
                    code = pdfium_c.FPDFText_GetUnicode(textpage, number)
                    if not code or not pdfium_c.FPDFText_GetCharBox(textpage, number, left, right, bottom, top):
                        continue
                    found.append((left.value, right.value, bottom.value, top.value, chr(code)))
            finally:
                textpage.close()
        finally:
            page.close()
    found.sort()
    return found


def _repair_replacement_chars(page: object, glyphs: Callable[[], _Glyphs] | None) -> None:
    """Gives each U+FFFD character of the pdfplumber `page` the character pdfium reads at the same place, when pdfium reads a
    real one there. Every such character is marked `undecoded`, repaired or not (`_repaired_line_text`).

    Deliberate: pdfminer decodes a glyph only through the font's ToUnicode map and encoding, and old-style figures of book
    fonts that have no entry there came out as U+FFFD — the page number '207' read '�07' and 'Figure 11.12' read
    'Figure 11.1�' (8 of 200 pages of a pdf corpus). pdfium also reads the glyph names and the embedded font's own map and
    gets them right. Where pdfium reads U+FFFD, a control character or nothing as well (a symbol font of a TeX formula maps its
    minus to nothing either reader knows), the character is left as it was. A page turned by /Rotate is left alone: pdfminer
    turns its coordinates and pdfium does not (Pinned by: test_pdf_fonts.py::TestUndecodedGlyphs)."""
    if glyphs is None or getattr(page, "rotation", 0) % 360:
        return
    broken = [char for char in page.chars if char.get("text") == _REPLACEMENT]
    if not broken:
        return
    found = glyphs()
    if not found:
        return
    # pdfium's characters by height band, so that each broken character looks only at those of its own rows (a page of
    # thousands of undecoded characters scanned every character of a wide strip for each of them)
    bands: dict[int, list[tuple[float, float, float, float, str]]] = {}
    for glyph in found:
        if all(math.isfinite(value) for value in glyph[:4]) and glyph[3] - glyph[2] <= _BAND * _MAX_BANDS:
            for band in range(math.floor((glyph[2] - _GLYPH_TOLERANCE) / _BAND), math.floor((glyph[3] + _GLYPH_TOLERANCE) / _BAND) + 1):
                bands.setdefault(band, []).append(glyph)
    height = float(page.height)
    for char in broken:
        char["undecoded"] = True
        # pdfplumber's x is already in PDF space and its top is measured from the top of the page box, so the centre in
        # PDF space is (x, page height − top) whatever the MediaBox origin (Pinned by:
        # test_pdf_fonts.py::TestUndecodedGlyphs::test_a_page_box_away_from_the_origin)
        x = (char["x0"] + char["x1"]) / 2
        y = height - (char["top"] + char["bottom"]) / 2
        if not (math.isfinite(x) and math.isfinite(y)):
            continue
        near = [glyph for glyph in bands.get(math.floor(y / _BAND), ())
                if glyph[0] - _GLYPH_TOLERANCE <= x <= glyph[1] + _GLYPH_TOLERANCE
                and glyph[2] - _GLYPH_TOLERANCE <= y <= glyph[3] + _GLYPH_TOLERANCE and _readable(glyph[4])]
        if near:
            # the glyph whose centre is nearest: a narrow raised digit lies within the tolerance of its neighbour's box too
            char["text"] = min(near, key=lambda glyph: abs((glyph[0] + glyph[1]) / 2 - x) + abs((glyph[2] + glyph[3]) / 2 - y))[4]


def _readable(text: str) -> bool:
    return text != _REPLACEMENT and not text.isspace() and unicodedata.category(text)[0] not in "CZ"


def _repaired_line_text(text: str, chars: list[dict]) -> str:
    """The line text with its U+FFFD characters replaced, left to right, by what the `undecoded` characters of the line now
    hold — when they are as many as the U+FFFD of the text; otherwise the text as it was."""
    count = text.count(_REPLACEMENT)
    if not count:
        return text
    repaired = [char["text"] for char in sorted(chars, key=lambda char: char["x0"]) if char.get("undecoded")]
    if len(repaired) != count:
        return text
    pieces = text.split(_REPLACEMENT)
    return "".join(piece + (repaired[index] if index < count else "") for index, piece in enumerate(pieces))
