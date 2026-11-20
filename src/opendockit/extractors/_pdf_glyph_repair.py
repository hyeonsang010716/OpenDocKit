"""Characters pdfminer could not decode (U+FFFD) read again from pdfium where pdfium reads the same glyph."""

import bisect
from collections.abc import Callable
import math
import unicodedata

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from opendockit.extractors._pdf_model import _PDFIUM_LOCK


_REPLACEMENT = "�"
# How far apart pdfminer's and pdfium's boxes of one glyph may lie (left edges, vertical centres), as a share of the font
# size, and that share's floor and ceiling (pt). It also bounds a row: characters whose centres lie within it of the row's
# first character
_GLYPH_TOLERANCE, _MIN_TOLERANCE, _MAX_TOLERANCE = 0.25, 0.5, 8.0
# More pdfium glyphs than this at the place of a character (a pile of glyphs on one spot) give it no anchor
_MAX_CANDIDATES = 8

# pdfium's characters of a page: (left, right, bottom, top, text) in PDF user space, in the order pdfium lists them (that of
# the content stream, with the pieces of a line it puts in order). The box is pdfium's loose box (advance width × the font's
# ascent and descent), the one pdfminer gives a character.
_Glyphs = list[tuple[float, float, float, float, str]]


def _pdfium_glyphs(document: pdfium.PdfDocument | None, index: int | None) -> Callable[[], _Glyphs]:
    """A function that reads the characters of the page `index` of `document` with pdfium (once). Empty when there is no
    document."""
    read: list[_Glyphs] = []

    def glyphs() -> _Glyphs:
        if not read:
            read.append(_read_glyphs(document, index) if document is not None and index is not None and index >= 0 else [])
        return read[0]

    return glyphs


def _read_glyphs(document: pdfium.PdfDocument, index: int) -> _Glyphs:
    """pdfium's characters of the page, without those pdfium makes up (spaces and line breaks it infers). A failure of pdfium
    (`PdfiumError`) reaches `_repair_replacement_chars`, which leaves U+FFFD (Pinned by:
    test_pdf_fonts.py::TestUndecodedGlyphs::test_a_pdfium_failure_leaves_the_character_as_it_was)."""
    found: _Glyphs = []
    with _PDFIUM_LOCK:
        page = document[index]
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
        finally:
            page.close()
    return found


def _repair_replacement_chars(page: object, glyphs: Callable[[], _Glyphs] | None) -> None:
    """Gives each U+FFFD character of the pdfplumber `page` the character pdfium reads for the same glyph, when pdfium reads a
    real one. Every such character is marked `undecoded` with its index in `page.chars`, repaired or not (`_repaired_line_text`).

    Deliberate: pdfminer decodes a glyph only through the font's ToUnicode map and encoding, and old-style figures of book
    fonts that have no entry there came out as U+FFFD — the page number '207' read '�07' and 'Figure 11.12' read
    'Figure 11.1�' (8 of 200 pages of a pdf corpus). pdfium also reads the glyph names and the embedded font's own map and
    gets them right. pdfium's glyph is taken only where both readers surely mean the same glyph. The page's characters are
    put into rows by the vertical centres of their boxes, in both readers alike, and each row is read left to right (by left
    edge, then in the readers' own order); spaces are left out. The two rows are walked side by side from an anchor: a decoded
    character pdfium reads the same with its left edge and centre within a quarter of the font size (0.5–8pt), the only one
    there. From an anchor with as many characters before it in both rows (the row's first decoded character, as a rule) the
    characters are paired by their order alone, so the advances of the two readers may differ; from any other anchor each
    decoded character must also lie within the tolerance of its pair. A U+FFFD takes its pair's character only when decoded
    pairs that agree close the run of U+FFFD on both sides — or one side and the end or start of both rows — the run follows
    the order of both readers' characters (`_in_order`), no other U+FFFD takes the same glyph, and pdfium reads a real
    character (not U+FFFD, whitespace, a control or a separator). Two
    decoded characters read differently break the walk and drop the run before them. Taking the glyph under the character
    gave a neighbour's letter where the advances differ ('by \ufffd0' became 'by y0', 'total \ufffd\ufffd.' 'total a.'), and so did
    checking only the text of the neighbours, in a repeating pattern ('Pears 1, 2, 3' became 'Pears 1, \ufffd, 2'). Otherwise
    — pdfium reads U+FFFD or nothing as well (a symbol font of a TeX formula maps its minus to nothing either reader knows),
    or a row has no decoded character to anchor it (a page number of old-style figures alone, a raised footnote figure in a
    row of its own) — the character stays U+FFFD. A page turned by /Rotate is left alone: pdfminer turns its coordinates and
    pdfium does not (Pinned by: test_pdf_fonts.py::TestUndecodedGlyphs)."""
    if glyphs is None or getattr(page, "rotation", 0) % 360:
        return
    chars = page.chars
    broken = [number for number, char in enumerate(chars) if char.get("text") == _REPLACEMENT]
    if not broken:
        return
    for number in broken:
        chars[number]["undecoded"] = number  # its place in the page's characters, the order of the layout line's text
    try:
        repairs = _repairs(chars, broken, glyphs, float(page.height))
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001
        # the repair never fails a document: whatever goes wrong in it leaves U+FFFD (Pinned by:
        # test_pdf_fonts.py::TestUndecodedGlyphs::test_a_failure_inside_the_repair_leaves_the_character_as_it_was)
        return
    for char, text in repairs:
        char["text"] = text


# A character of a row: (vertical centre, left, right, tolerance, rank among the reader's characters that are not spaces, its
# index — into the page's characters for pdfminer, into the glyphs for pdfium — and its text, None where pdfminer read U+FFFD)
_Spot = tuple[float, float, float, float, int, int, str | None]


def _repairs(chars: list[dict], broken: list[int], glyphs: Callable[[], _Glyphs], height: float) -> list[tuple[dict, str]]:
    """(character, its text) for the broken characters (indexes into `chars`) that pdfium reads (`_repair_replacement_chars`).
    Sorting and walking the rows is linear but for the sorts: looking at every glyph of a 16pt band for each broken character,
    a page of 32,000 characters took 9 seconds (Pinned by: test_pdf_fonts.py::TestUndecodedGlyphs::
    test_many_undecoded_characters_in_one_band_are_repaired_in_linear_time)."""
    found = glyphs()
    if not found:
        return []
    broken_set = set(broken)
    mine: list[_Spot] = []
    for number, char in enumerate(chars):
        text = char.get("text") or ""
        if not text or text.isspace():
            continue
        # pdfplumber's x is already in PDF space and its top is measured from the top of the page box, so the centre in
        # PDF space is page height − the centre from the top whatever the MediaBox origin (Pinned by:
        # test_pdf_fonts.py::TestUndecodedGlyphs::test_a_page_box_away_from_the_origin)
        left, right, middle = char["x0"], char["x1"], height - (char["top"] + char["bottom"]) / 2
        size = char.get("size") or char["bottom"] - char["top"]
        if all(math.isfinite(value) for value in (left, right, middle, size)):
            tolerance = min(max(_GLYPH_TOLERANCE * abs(size), _MIN_TOLERANCE), _MAX_TOLERANCE)
            mine.append((middle, left, right, tolerance, len(mine), number,
                         None if number in broken_set else unicodedata.normalize("NFKC", text)))
    groups = _grouped(found)
    theirs: list[_Spot] = []
    for position, (left, right, bottom, top, text) in enumerate(groups):
        if not text.isspace() and all(math.isfinite(value) for value in (left, right, bottom, top)):
            theirs.append(((bottom + top) / 2, left, right, 0.0, len(theirs), position, unicodedata.normalize("NFKC", text)))
    mine.sort()
    theirs.sort()
    middles = [glyph[0] for glyph in theirs]
    claims: dict[int, list[int]] = {}
    start = 0
    while start < len(mine):
        first, tolerance = mine[start][0], mine[start][3]
        end = start
        while end < len(mine) and mine[end][0] - first <= tolerance:
            end += 1
        if any(spot[6] is None for spot in mine[start:end]):
            row = sorted(mine[start:end], key=lambda spot: (spot[1], spot[4]))
            low, high = bisect.bisect_left(middles, first - tolerance), bisect.bisect_right(middles, mine[end - 1][0] + tolerance)
            for number, position in _paired(row, sorted(theirs[low:high], key=lambda spot: (spot[1], spot[4]))):
                claims.setdefault(position, []).append(number)
        start = end
    return [(chars[numbers[0]], groups[position][4]) for position, numbers in claims.items()
            if len(numbers) == 1 and _readable(groups[position][4])]


def _paired(mine: list[_Spot], theirs: list[_Spot]) -> list[tuple[int, int]]:
    """(index of a broken character, index of pdfium's glyph) for the broken characters of a row of pdfminer (`mine`, left
    to right) that the walk pairs with a glyph of pdfium's row (`theirs`, left to right; `_repair_replacement_chars`)."""
    lefts = [glyph[1] for glyph in theirs]
    pairs: list[tuple[int, int]] = []
    pending: list[tuple[_Spot, _Spot]] = []  # broken characters since the last pair that agreed, with their glyphs
    at: int | None = None  # the glyph of pdfium the next character of pdfminer is paired with, None outside a walk
    strict = False
    last_mine = last_theirs = -1  # the last pair that agreed (indexes into the rows)

    def agreed() -> list[tuple[_Spot, _Spot]]:
        return [(mine[last_mine], theirs[last_theirs])] if last_mine >= 0 else []

    def take(run: list[tuple[_Spot, _Spot]], closing: list[tuple[_Spot, _Spot]]) -> None:
        if _in_order([*agreed(), *run, *closing]):
            pairs.extend((char[5], glyph[5]) for char, glyph in run)

    for index, char in enumerate(mine):
        if at is not None and at < len(theirs):
            glyph = theirs[at]
            if char[6] is None:
                pending.append((char, glyph))
                at += 1
                continue
            if glyph[6] == char[6] and (not strict or abs(glyph[1] - char[1]) <= char[3]):
                take(pending, [(char, glyph)])
                pending, last_mine, last_theirs, at = [], index, at, at + 1
                continue
        pending, at = [], None
        if char[6] is None:
            continue
        anchor = _anchor(theirs, lefts, char)
        if anchor is None:
            continue
        in_step = anchor - last_theirs == index - last_mine
        if in_step and all(mine[before][6] is None for before in range(last_mine + 1, index)):
            take([(mine[before], theirs[last_theirs + before - last_mine]) for before in range(last_mine + 1, index)],
                 [(char, theirs[anchor])])
        strict, last_mine, last_theirs, at = not in_step, index, anchor, anchor + 1
    if at == len(theirs):
        take(pending, [])  # the run of U+FFFD ends both rows
    return pairs


def _in_order(run: list[tuple[_Spot, _Spot]]) -> bool:
    """Whether the characters of a run, left to right, follow one another in both readers: each next to the one before in the
    reader's own order of characters (spaces left out; either way, as a line drawn right to left runs backwards), or apart
    from it by a clear gap (another cell of a table row). Where the advances go backwards (a negative character spacing over
    glyphs pdfminer gives no width), left to right is not the order of the glyphs, and pairing by place in the row gave a
    neighbour's figure (Pinned by: test_pdf_fonts.py::TestUndecodedGlyphs::
    test_no_undecoded_character_takes_another_glyphs_character[backwards-Helvetica])."""
    return all(abs(after[4] - before[4]) == 1 or after[1] - before[2] >= char[3]
               for (char, glyph), (next_char, next_glyph) in zip(run, run[1:], strict=False)
               for before, after in ((char, next_char), (glyph, next_glyph)))


def _anchor(theirs: list[_Spot], lefts: list[float], char: _Spot) -> int | None:
    """The only glyph of pdfium's row that reads the text of `char` with its left edge and centre within its tolerance."""
    tolerance = char[3]
    low, high = bisect.bisect_left(lefts, char[1] - tolerance), bisect.bisect_right(lefts, char[1] + tolerance)
    if high - low > _MAX_CANDIDATES:
        return None
    same = [at for at in range(low, high) if theirs[at][6] == char[6] and abs(theirs[at][0] - char[0]) <= tolerance]
    return same[0] if len(same) == 1 else None


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


def _readable(text: str) -> bool:
    return _REPLACEMENT not in text and not text.isspace() and all(unicodedata.category(letter)[0] not in "CZ" for letter in text)


def _repaired_line_text(text: str, chars: list[dict]) -> str:
    """The line text with its U+FFFD characters replaced, in turn, by what the `undecoded` characters of the line now hold —
    when they are as many as the U+FFFD of the text; otherwise the text as it was. They are taken in their order among the
    page's characters, which is that of the layout line's text: taken by their place left to right, a line drawn right to
    left (turned 180° or mirrored) had its digits swapped (Pinned by: test_pdf_fonts.py::TestUndecodedGlyphs::
    test_the_characters_of_a_line_drawn_right_to_left_are_put_back_in_its_order)."""
    count = text.count(_REPLACEMENT)
    if not count:
        return text
    repaired = [char["text"] for char in sorted((char for char in chars if "undecoded" in char), key=lambda char: char["undecoded"])]
    if len(repaired) != count:
        return text
    pieces = text.split(_REPLACEMENT)
    return "".join(piece + (repaired[index] if index < count else "") for index, piece in enumerate(pieces))
