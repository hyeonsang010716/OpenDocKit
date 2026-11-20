"""Characters pdfminer could not decode (U+FFFD) read again from pdfium where pdfium reads the same glyph."""

from collections.abc import Callable
import math
import unicodedata

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from opendockit.extractors._pdf_model import _PDFIUM_LOCK


_REPLACEMENT = "�"
# How far apart the left edges and the vertical centres of pdfminer's and pdfium's boxes of one glyph may lie, as a share of
# the font size, and that share's floor and ceiling (pt)
_GLYPH_TOLERANCE, _MIN_TOLERANCE, _MAX_TOLERANCE = 0.25, 0.5, 8.0
# Decoded characters on each side of a glyph that both readers must read alike, and the spaces stepped over to reach them
_CONTEXT, _MAX_SPACES = 2, 8
# Cells (pt) of the index of pdfium's glyphs by left edge and vertical centre: a cell holding more than `_MAX_PER_CELL`
# glyphs (a pile of glyphs on one spot) gives no candidate, nor does a place with more than `_MAX_CANDIDATES` of them
_CELL, _MAX_PER_CELL, _MAX_CANDIDATES = 8.0, 32, 4

# pdfium's characters of a page: (left, right, bottom, top, text) in PDF user space, in pdfium's order (that of the content
# stream). The box is pdfium's loose box (advance width × the font's ascent and descent), the one pdfminer gives a character.
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
    """pdfium's characters of the page, without those pdfium makes up (spaces and line breaks it infers). Empty when pdfium
    fails anywhere on the page: the repair never fails a document, it leaves U+FFFD (Pinned by:
    test_pdf_fonts.py::TestUndecodedGlyphs::test_a_pdfium_failure_leaves_the_character_as_it_was)."""
    found: _Glyphs = []
    with _PDFIUM_LOCK:
        try:
            page = document[index]
        except pdfium.PdfiumError:
            return []
        try:
            textpage = page.get_textpage()
            try:
                box = pdfium_c.FS_RECTF()
                for number in range(textpage.count_chars()):
                    code = pdfium_c.FPDFText_GetUnicode(textpage, number)
                    if (not code or pdfium_c.FPDFText_IsGenerated(textpage, number) == 1
                            or not pdfium_c.FPDFText_GetLooseCharBox(textpage, number, box)):
                        continue
                    found.append((box.left, box.right, box.bottom, box.top, chr(code)))
            finally:
                textpage.close()
        except pdfium.PdfiumError:
            return []
        finally:
            page.close()
    return found


def _repair_replacement_chars(page: object, glyphs: Callable[[], _Glyphs] | None) -> None:
    """Gives each U+FFFD character of the pdfplumber `page` the character pdfium reads for the same glyph, when pdfium reads a
    real one. Every such character is marked `undecoded`, repaired or not (`_repaired_line_text`).

    Deliberate: pdfminer decodes a glyph only through the font's ToUnicode map and encoding, and old-style figures of book
    fonts that have no entry there came out as U+FFFD — the page number '207' read '�07' and 'Figure 11.12' read
    'Figure 11.1�' (8 of 200 pages of a pdf corpus). pdfium also reads the glyph names and the embedded font's own map and
    gets them right. pdfium's glyph is taken only when it is surely the same glyph: its left edge and vertical centre lie
    within a quarter of the font size of pdfminer's, the decoded characters beside it on its line (up to two on each side,
    a U+FFFD standing for any) are the same for both readers with at least one of them there, exactly one glyph qualifies,
    and no other U+FFFD takes it. Taking the glyph under the character's centre gave a neighbour's letter where the two
    readers' advances differ ('by �0' became 'by y0', 'total ��.' became 'total a.'). Otherwise — pdfium reads U+FFFD,
    whitespace, a control character or nothing as well (a symbol font of a TeX formula maps its minus to nothing either
    reader knows), or the place is ambiguous — the character stays U+FFFD. A page turned by /Rotate is left alone: pdfminer
    turns its coordinates and pdfium does not (Pinned by: test_pdf_fonts.py::TestUndecodedGlyphs)."""
    if glyphs is None or getattr(page, "rotation", 0) % 360:
        return
    chars = page.chars
    broken = [number for number, char in enumerate(chars) if char.get("text") == _REPLACEMENT]
    if not broken:
        return
    found = glyphs()
    for number in broken:
        chars[number]["undecoded"] = True
    if not found:
        return
    groups = _grouped(found)
    # pdfium's glyphs by cell of (left edge, vertical centre), so that each broken character looks only at those of its own
    # place: looking at every glyph of a 16pt band for each of them, a page of 32,000 characters took 9 seconds (Pinned by:
    # test_pdf_fonts.py::TestUndecodedGlyphs::test_many_undecoded_characters_in_one_band_are_repaired_in_linear_time)
    cells: dict[tuple[int, int], list[int] | None] = {}
    for position, (left, _, bottom, top, _) in enumerate(groups):
        if all(math.isfinite(value) for value in (left, bottom, top)):
            key = (math.floor(left / _CELL), math.floor((bottom + top) / 2 / _CELL))
            cell = cells.setdefault(key, [])
            if cell is not None:
                cell.append(position)
                if len(cell) > _MAX_PER_CELL:
                    cells[key] = None
    height = float(page.height)
    claims: dict[int, list[dict]] = {}
    for number in broken:
        char = chars[number]
        # pdfplumber's x is already in PDF space and its top is measured from the top of the page box, so the centre in
        # PDF space is page height − the centre from the top whatever the MediaBox origin (Pinned by:
        # test_pdf_fonts.py::TestUndecodedGlyphs::test_a_page_box_away_from_the_origin)
        left, middle = char["x0"], height - (char["top"] + char["bottom"]) / 2
        size = char.get("size") or char["bottom"] - char["top"]
        if not all(math.isfinite(value) for value in (left, middle, size)):
            continue
        tolerance = min(max(_GLYPH_TOLERANCE * abs(size), _MIN_TOLERANCE), _MAX_TOLERANCE)
        near = _near(groups, cells, left, middle, tolerance)
        if near is None:
            continue
        before, after = _pdfminer_context(chars, number, -1), _pdfminer_context(chars, number, 1)
        if not any(before + after):
            continue  # nothing decoded beside it to tell its glyph from a neighbour's
        same = [position for position in near if _readable(groups[position][4])
                and _agrees(before, _pdfium_context(groups, position, -1))
                and _agrees(after, _pdfium_context(groups, position, 1))]
        if len(same) == 1:
            claims.setdefault(same[0], []).append(char)
    for position, takers in claims.items():
        if len(takers) == 1:
            takers[0]["text"] = groups[position][4]


def _grouped(found: _Glyphs) -> _Glyphs:
    """pdfium's characters with those of one glyph joined: pdfium gives each character of a glyph mapped to several (a
    ligature 'ﬁ' mapped to 'fi') the glyph's box, pdfminer gives the glyph one character (Pinned by:
    test_pdf_fonts.py::TestUndecodedGlyphs::test_a_ligature_pdfium_reads_as_two_characters_is_one_glyph). The pieces of a
    glyph are joined once at the end: adding each to the text so far took 18 seconds for 1.6 million characters in one box
    (Pinned by: test_pdf_fonts.py::TestUndecodedGlyphs::test_many_characters_in_one_box_are_joined_in_linear_time)."""
    boxes: list[tuple[float, float, float, float]] = []
    pieces: list[list[str]] = []
    for *box, text in found:
        if boxes and boxes[-1] == tuple(box) and not text.isspace() and not pieces[-1][-1].isspace():
            pieces[-1].append(text)
        else:
            boxes.append(tuple(box))
            pieces.append([text])
    return [(*box, "".join(texts)) for box, texts in zip(boxes, pieces, strict=True)]


def _near(groups: _Glyphs, cells: dict[tuple[int, int], list[int] | None], left: float, middle: float,
          tolerance: float) -> list[int] | None:
    """pdfium's glyphs whose left edge and vertical centre lie within `tolerance` of those given; None when the place is
    ambiguous (a cell too full, or too many glyphs there)."""
    near: list[int] = []
    for column in range(math.floor((left - tolerance) / _CELL), math.floor((left + tolerance) / _CELL) + 1):
        for row in range(math.floor((middle - tolerance) / _CELL), math.floor((middle + tolerance) / _CELL) + 1):
            cell = cells.get((column, row), ())
            if cell is None:
                return None
            for position in cell:
                glyph_left, _, bottom, top, _ = groups[position]
                if abs(glyph_left - left) <= tolerance and abs((bottom + top) / 2 - middle) <= tolerance:
                    near.append(position)
                    if len(near) > _MAX_CANDIDATES:
                        return None
    return near


def _pdfminer_context(chars: list[dict], number: int, step: int) -> list[str]:
    """Up to `_CONTEXT` characters of pdfminer beside the character `number` on its line (before it for step −1, after it
    for 1), nearest first, spaces stepped over; '' for one pdfminer could not decode."""
    found: list[str] = []
    last, spaces, number = chars[number], 0, number + step
    while 0 <= number < len(chars) and len(found) < _CONTEXT:
        char = chars[number]
        text = char.get("text") or ""
        if not _same_line((last["top"], last["bottom"]), (char["top"], char["bottom"])):
            break
        if text.isspace():
            spaces += 1
            if spaces > _MAX_SPACES:
                break
        else:
            found.append("" if text == _REPLACEMENT else unicodedata.normalize("NFKC", text))
            last = char
        number += step
    return found


def _pdfium_context(groups: _Glyphs, position: int, step: int) -> list[str]:
    """`_pdfminer_context` for pdfium's glyph `position`."""
    found: list[str] = []
    last, spaces, position = groups[position], 0, position + step
    while 0 <= position < len(groups) and len(found) < _CONTEXT:
        glyph = groups[position]
        # pdfium's boxes are upside down to pdfminer's (bottom < top), so (−top, −bottom) measures them alike
        if not _same_line((-last[3], -last[2]), (-glyph[3], -glyph[2])):
            break
        if glyph[4].isspace():
            spaces += 1
            if spaces > _MAX_SPACES:
                break
        else:
            found.append(unicodedata.normalize("NFKC", glyph[4]))
            last = glyph
        position += step
    return found


def _same_line(one: tuple[float, float], other: tuple[float, float]) -> bool:
    """Whether two boxes (top, bottom), top < bottom, overlap by at least half the lower one."""
    overlap = min(one[1], other[1]) - max(one[0], other[0])
    return overlap >= 0.5 * min(one[1] - one[0], other[1] - other[0]) and overlap > 0


def _agrees(pdfminer: list[str], pdfium: list[str]) -> bool:
    """Whether pdfium reads the context pdfminer reads ('' standing for any character): at least as far, and alike."""
    return len(pdfium) >= len(pdfminer) and all(not mine or mine == theirs for mine, theirs in zip(pdfminer, pdfium, strict=False))


def _readable(text: str) -> bool:
    return _REPLACEMENT not in text and not text.isspace() and all(unicodedata.category(letter)[0] not in "CZ" for letter in text)


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
