"""Characters pdfminer could not decode (U+FFFD) read again from pdfium where pdfium reads the same glyph."""

import bisect
from collections.abc import Callable, Iterator
import math
import unicodedata

from pdfminer.layout import LTChar, LTContainer
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from opendockit.extractors._pdf_model import _PDFIUM_LOCK


_REPLACEMENT = "�"
# How far apart the vertical centres of pdfminer's and pdfium's boxes of one glyph may lie, as a share of the font size (the
# two readers take the height of a glyph from different font metrics), and its floor (pt). It also bounds a row: characters
# whose centres lie within it of the row's first character
_ROW_TOLERANCE, _MIN_ROW_TOLERANCE = 0.25, 0.5
# How far apart their left edges may lie, as a share of the font size, and its floor (pt): both readers place a glyph by
# the same text matrix and widths, so the edges agree but for rounding
_EDGE_TOLERANCE, _MIN_EDGE_TOLERANCE = 0.02, 0.05

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
    """Gives the U+FFFD characters of the pdfplumber `page` the characters pdfium reads for the same glyphs, where pdfium
    surely reads the same glyphs.

    Deliberate: pdfminer decodes a glyph only through the font's ToUnicode map and encoding, and old-style figures of book
    fonts that have no entry there came out as U+FFFD — the page number '207' read '�07' and 'Figure 11.12' read
    'Figure 11.1�' (8 of 200 pages of a pdf corpus). pdfium also reads the glyph names and the embedded font's own map and
    gets them right. A wrong character is worse than a U+FFFD, so a row is repaired only when the two readers agree on the
    whole of it (`_agreeing`); otherwise every U+FFFD of the row stays as it is. Each rule short of that took wrong
    characters somewhere: the glyph under the character ('by �0' became 'by y0'), the same neighbours ('Pears 1, �, 2, 3'),
    pairing in order from an anchor ('01' for '10' under negative spacing, 't' for '1' from a line 2.5pt above). On the
    review's truth fuzzer the whole-row rule took no wrong character in 8,000 pages where the others took 775 and 1,118.
    A page turned by /Rotate is left alone, its U+FFFD as they are: pdfminer turns its coordinates and pdfium does not
    (Pinned by: test_pdf_fonts.py::TestUndecodedGlyphs)."""
    if glyphs is None or getattr(page, "rotation", 0) % 360 or "_objects" in vars(page):
        return  # (pdfplumber builds its characters and lines from the layout once: once built, nothing is changed)
    # The repair changes pdfminer's characters themselves, before pdfplumber builds its characters and the text of its lines
    # from them, so each line's text holds the repaired character in its own place. Filling the U+FFFD of a line's text in
    # turn from the characters at the line's level and place gave a raised line a figure of a cell below it ('�04' for '694',
    # Pinned by: test_pdf_fonts.py::TestUndecodedGlyphs::test_each_line_text_holds_its_own_repaired_characters)
    letters = list(_layout_chars(page.layout))
    if not any(letter.get_text() == _REPLACEMENT for letter in letters):
        return
    height = float(page.height)
    left_of_box, top_of_box = page.mediabox[:2]
    # the box pdfplumber gives each character (`Page.process_object`)
    chars = [{"text": letter.get_text(), "x0": letter.x0 + left_of_box, "x1": letter.x1 + left_of_box,
              "top": height - letter.y1 + top_of_box, "bottom": height - letter.y0 + top_of_box, "size": letter.size}
             for letter in letters]
    try:
        repairs = _repairs(chars, glyphs, height)
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001
        # the repair never fails a document: whatever goes wrong in it leaves U+FFFD (Pinned by:
        # test_pdf_fonts.py::TestUndecodedGlyphs::test_a_failure_inside_the_repair_leaves_the_character_as_it_was)
        return
    for number, text in repairs:
        letters[number]._text = text


def _layout_chars(layout: LTContainer) -> Iterator[LTChar]:
    """The characters of a pdfminer layout, in the order pdfplumber lists them."""
    stack = [iter(layout)]
    while stack:
        for item in stack[-1]:
            if isinstance(item, LTChar):
                yield item
            elif isinstance(item, LTContainer):
                stack.append(iter(item))
                break
        else:
            stack.pop()


# A character of a row: (vertical centre, left, right, font size, index — into the page's characters for pdfminer, into the
# glyphs for pdfium — and its text after NFKC, None where pdfminer read U+FFFD)
_Spot = tuple[float, float, float, float, int, str | None]


def _repairs(chars: list[dict], glyphs: Callable[[], _Glyphs], height: float) -> list[tuple[int, str]]:
    """(index into `chars`, its text) for the characters pdfminer read as U+FFFD that pdfium surely reads
    (`_repair_replacement_chars`). `chars` are pdfplumber's characters, or ones with the same keys and boxes.

    The characters of both readers, spaces left out, are put into rows by the vertical centres of their boxes: a row holds
    pdfminer's characters whose centres lie within the row tolerance of its first one, and pdfium's glyphs whose centres lie
    within it of the row's. A row with a U+FFFD and a decoded character is repaired when `_agreeing` pairs it whole, and no
    other row pairs a U+FFFD with the same glyph. Sorting and one pass over each row keep it linear but for the sorts: looking
    at every glyph of a 16pt band for each broken character, a page of 32,000 characters took 9 seconds (Pinned by:
    test_pdf_fonts.py::TestUndecodedGlyphs::test_many_undecoded_characters_in_one_band_are_repaired_in_linear_time)."""
    if not any(char.get("text") == _REPLACEMENT for char in chars):
        return []
    found = glyphs()
    if not found:
        return []
    mine: list[_Spot] = []
    for number, char in enumerate(chars):
        text = char.get("text") or ""
        if not text or text.isspace():
            continue
        # pdfplumber's x is already in PDF space and its top is measured from the top of the page box, so the centre in
        # PDF space is page height − the centre from the top whatever the MediaBox origin (Pinned by:
        # test_pdf_fonts.py::TestUndecodedGlyphs::test_a_page_box_away_from_the_origin)
        left, right, middle = char["x0"], char["x1"], height - (char["top"] + char["bottom"]) / 2
        size = abs(char.get("size") or char["bottom"] - char["top"])
        if all(math.isfinite(value) for value in (left, right, middle, size)):
            mine.append((middle, left, right, size, number,
                         None if text == _REPLACEMENT else unicodedata.normalize("NFKC", text)))
    groups = _grouped(found)
    theirs: list[_Spot] = []
    for position, (left, right, bottom, top, text) in enumerate(groups):
        if not text.isspace() and all(math.isfinite(value) for value in (left, right, bottom, top)):
            theirs.append(((bottom + top) / 2, left, right, 0.0, position, unicodedata.normalize("NFKC", text)))
    mine.sort()
    theirs.sort()
    middles = [glyph[0] for glyph in theirs]
    claims: dict[int, list[int]] = {}
    start = 0
    while start < len(mine):
        first = mine[start][0]
        tolerance = max(_ROW_TOLERANCE * mine[start][3], _MIN_ROW_TOLERANCE)
        end = start
        while end < len(mine) and mine[end][0] - first <= tolerance:
            end += 1
        row = mine[start:end]
        if any(char[5] is None for char in row) and any(char[5] is not None for char in row):
            band = theirs[bisect.bisect_left(middles, first - tolerance):bisect.bisect_right(middles, row[-1][0] + tolerance)]
            for number, position in _agreeing(row, band, tolerance):
                claims.setdefault(position, []).append(number)
        start = end
    return [(numbers[0], groups[position][4]) for position, numbers in claims.items() if len(numbers) == 1]


def _agreeing(mine: list[_Spot], theirs: list[_Spot], tolerance: float) -> list[tuple[int, int]]:
    """(index of pdfminer's U+FFFD, index of pdfium's glyph) for a row that both readers read alike, character for
    character; nothing otherwise. Both rows hold as many characters. Paired in the order of their left edges, each pair's
    left edges lie within the edge tolerance — or, in a row drawn right to left (turned 180° or mirrored), paired in the
    order of their right edges, each pair's right edges do: the edge at the glyph's origin, as pdfium's loose box reaches past
    the advance on the other side as far as the glyph's ink — and each pair's centres within the row `tolerance`. So every
    advance but the row's last agrees too. Each decoded character reads the same in both, and each U+FFFD's glyph is a real
    character (not U+FFFD, a control or a separator). No character of pdfminer has no width (a glyph name pdfminer does not
    know in a standard-14 font: the two readers then advance differently), and no two lie so close that their order could
    differ between the readers (Pinned by: test_pdf_fonts.py::TestUndecodedGlyphs::
    test_pages_where_pairing_short_of_the_whole_row_took_a_wrong_character,
    test_a_row_the_readers_do_not_read_alike_is_left_alone, test_no_random_page_takes_a_wrong_character)."""
    if len(mine) != len(theirs):
        return []
    return _paired_by(mine, theirs, tolerance, 1) or _paired_by(mine, theirs, tolerance, 2)


def _paired_by(mine: list[_Spot], theirs: list[_Spot], tolerance: float, side: int) -> list[tuple[int, int]]:
    """`_agreeing` with the characters paired by their left (`side` 1) or right (2) edges."""
    pairs: list[tuple[int, int]] = []
    previous = -math.inf
    for char, glyph in zip(sorted(mine, key=lambda char: char[side]), sorted(theirs, key=lambda glyph: glyph[side]),
                           strict=True):
        middle, left, right, size, number, text = char
        edge = max(_EDGE_TOLERANCE * size, _MIN_EDGE_TOLERANCE)
        if (right - left <= edge or char[side] - previous <= 2 * edge or abs(glyph[side] - char[side]) > edge
                or abs(glyph[0] - middle) > tolerance):
            return []
        previous = char[side]
        if text is None:
            if not _readable(glyph[5]):
                return []
            pairs.append((number, glyph[4]))
        elif glyph[5] != text:
            return []
    return pairs


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
