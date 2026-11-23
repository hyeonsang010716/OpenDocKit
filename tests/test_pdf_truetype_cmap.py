"""The Unicode map of an embedded TrueType font, read from the font's own `cmap` table: the last source of a CID font that
names its glyphs by number and has no ToUnicode. pdfminer read the commonest subtable wrongly, so more than a third of the
glyphs of an ordinary font came out as another character and nothing said so."""

from io import BytesIO
import struct
import zlib

from pdfminer import pdffont

from opendockit import convert
from opendockit.extractors import _pdf_pdfminer


def _format_4(segments: list[tuple[int, int, int | list[int]]]) -> bytes:
    """A format 4 subtable. A segment is (first code, last code, delta) or (first code, last code, the glyph of each code[,
    delta]): the second kind reads its glyphs through an offset, as fonts do for codes whose glyphs are not in order."""
    segments = [*segments, (0xFFFF, 0xFFFF, 1)]
    count = len(segments)
    glyph_arrays = b""
    deltas, offsets = [], []
    for index, (_first, _last, glyphs, *delta) in enumerate(segments):
        if isinstance(glyphs, int):
            deltas.append(glyphs)
            offsets.append(0)
        else:
            deltas.append(delta[0] if delta else 0)
            offsets.append(2 * (count - index) + len(glyph_arrays))   # counted from the place of this offset
            glyph_arrays += struct.pack(f">{len(glyphs)}H", *glyphs)
    body = (struct.pack(">HHHH", 2 * count, 0, 0, 0)
            + struct.pack(f">{count}H", *(segment[1] for segment in segments)) + b"\x00\x00"
            + struct.pack(f">{count}H", *(segment[0] for segment in segments))
            + struct.pack(f">{count}h", *deltas) + struct.pack(f">{count}H", *offsets) + glyph_arrays)
    return struct.pack(">HHH", 4, 6 + len(body), 0) + body


def _format_12(groups: list[tuple[int, int, int]], *, claimed: int | None = None) -> bytes:
    body = b"".join(struct.pack(">III", *group) for group in groups)
    return struct.pack(">HHIII", 12, 0, 16 + len(body), 0, len(groups) if claimed is None else claimed) + body


def _font(*subtables: tuple[int, int, bytes]) -> bytes:
    """A font file that holds nothing but a `cmap` table with the subtables (platform, encoding, bytes)."""
    records, bodies, at = b"", b"", 4 + 8 * len(subtables)
    for platform, encoding, body in subtables:
        records += struct.pack(">HHL", platform, encoding, at + len(bodies))
        bodies += body
    cmap = struct.pack(">HH", 0, len(subtables)) + records + bodies
    return struct.pack(">LHHHH", 0x00010000, 1, 0, 0, 0) + struct.pack(">4sLLL", b"cmap", 0, 28, len(cmap)) + cmap


def _pdf(font: bytes, lines: list[list[int]]) -> bytes:
    """A page whose lines are drawn by glyph number in an Identity-H font that embeds `font` and has no ToUnicode."""
    content = b"".join(b"BT /F1 12 Tf 72 %d Td <%s> Tj ET\n" % (760 - 20 * row, b"".join(b"%04X" % glyph for glyph in line))
                       for row, line in enumerate(lines))
    packed = zlib.compress(font)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type0 /BaseFont /ABCDEF+Any /Encoding /Identity-H /DescendantFonts [6 0 R] >>",
        b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /ABCDEF+Any /CIDToGIDMap /Identity /DW 600 "
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> /FontDescriptor 7 0 R >>",
        b"<< /Type /FontDescriptor /FontName /ABCDEF+Any /Flags 4 /FontBBox [0 -200 1000 800] /ItalicAngle 0 /Ascent 800 "
        b"/Descent -200 /CapHeight 700 /StemV 80 /FontFile2 8 0 R >>",
        b"<< /Length %d /Length1 %d /Filter /FlateDecode >>\nstream\n%s\nendstream" % (len(packed), len(font), packed),
    ]
    out, offsets = bytearray(b"%PDF-1.7\n"), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, start)
    return bytes(out)


# ASCII in glyph order from glyph 3 (a segment with a delta), then two segments whose glyphs are not in the order of their
# codes, each read through its own offset: the Latin-1 signs and three Greek capitals.
_SIGNS = {0xA3: 140, 0xB0: 131, 0xB1: 150, 0xE9: 120, 0xD7: 133}
_GREEK = {0x394: 201, 0x3A9: 180, 0x3A0: 222}


def _segments() -> list[tuple[int, int, int | list[int]]]:
    return [(0x20, 0x7E, 3 - 0x20),
            (0xA0, 0xFF, [_SIGNS.get(code, 300 + code) for code in range(0xA0, 0x100)]),
            (0x391, 0x3A9, [_GREEK.get(code, 500 + code) for code in range(0x391, 0x3AA)])]


def _glyphs(text: str) -> list[int]:
    return [3 + ord(char) - 0x20 if ord(char) < 0x7F else {**_SIGNS, **_GREEK}[ord(char)] for char in text]


def _text(font: bytes, *lines: str) -> str:
    result = convert(_pdf(font, [_glyphs(line) for line in lines]), "a.pdf")
    return "\n".join(line for line in result.text.splitlines() if line and not line.startswith("<!--"))


class TestSegmentsReadThroughAnOffset:
    def test_every_segment_is_read_from_its_own_offset(self):
        """pdfminer counted the offset of every segment from the start of the offset array, so only the first segment with
        an offset was read right: '25\u00b0C \u00b1 2, \u00a3 10' came out as '25\u00b1C\u00a1u00b2\u00a12,\u00a1price\u00a1u00a4\u00a110' from Arial."""
        font = _font((3, 1, _format_4(_segments())))

        text = _text(font, "Store at 25\u00b0C \u00b1 2, price \u00a3 10", "Caf\u00e9 3 \u00d7 4", "\u0394x and \u03a9 and \u03a0")

        assert text == "Store at 25\u00b0C \u00b1 2, price \u00a3 10\nCaf\u00e9 3 \u00d7 4\n\u0394x and \u03a9 and \u03a0"

    def test_glyph_of_zero_in_a_segment_is_no_glyph(self):
        """A code whose entry in the glyph array is 0 has no glyph, whatever the delta of its segment: with the delta added
        it named a glyph of the font, and that glyph was read as the code."""
        segments = [(0x30, 0x32, [0, 70, 0], 0x41), (0x41, 0x43, 0)]   # '0' and '2' have no glyph; 'A' is glyph 0x41

        unicode_map = pdffont.TrueTypeFont("Any", BytesIO(_font((3, 1, _format_4(segments))))).create_unicode_map()

        assert unicode_map.get_unichr(0x41) == "A"
        assert unicode_map.get_unichr(70 + 0x41) == "1"

    def test_glyph_shared_by_two_characters_is_the_lower_one(self):
        """Fonts give the hyphen and the soft hyphen, the space and the no-break space one glyph. pdfminer kept the last,
        and cleaning removes a soft hyphen: 'well-known' read 'wellknown'."""
        segments = [(0x20, 0x7E, 3 - 0x20), (0xA0, 0xAD, [3 if code == 0xA0 else 3 + 0x2D - 0x20 if code == 0xAD else 400 + code
                                                         for code in range(0xA0, 0xAE)])]
        font = _font((3, 1, _format_4(segments)))

        assert _text(font, "a well-known rule") == "a well-known rule"


class TestSubtablesOfOtherFormats:
    def test_subtable_of_an_unknown_format_is_passed_over(self):
        """Most current fonts hold a table of variation sequences (format 14) under the Unicode platform. pdfminer raised on
        a format it does not know, and the font was left without a map."""
        variation_sequences = struct.pack(">HII", 14, 10, 0)
        font = _font((0, 5, variation_sequences), (3, 1, _format_4(_segments())))

        assert _text(font, "Caf\u00e9 \u00d7 2") == "Caf\u00e9 \u00d7 2"

    def test_characters_past_the_basic_plane_are_read(self):
        font = _font((3, 1, _format_4(_segments())), (3, 10, _format_12([(0x20, 0x7E, 3), (0x1F600, 0x1F601, 900)])))

        result = convert(_pdf(font, [[*_glyphs("ok "), 901]]), "a.pdf")

        assert "ok \U0001f601" in result.text

    def test_subtable_that_ends_early_keeps_what_was_read(self):
        whole = _format_4(_segments())
        font = _font((3, 1, _format_12([(0x20, 0x7E, 3)])), (3, 1, whole[:len(whole) - 40]))

        assert _text(font, "plain text") == "plain text"

    def test_font_without_a_unicode_subtable_has_no_map(self):
        font = pdffont.TrueTypeFont("Any", BytesIO(_font((1, 0, _format_4(_segments())))))

        try:
            font.create_unicode_map()
        except pdffont.TrueTypeFont.CMapNotFound:
            return
        raise AssertionError("a Macintosh subtable is not a Unicode map")


class TestRangesNamingMoreThanAFontHolds:
    """A subtable names ranges of codes, so a few bytes name billions: 2,000 groups of four billion codes each (24 KB) were
    walked one code at a time. One table may name `_CMAP_CODES_MAX` code points and the tables of a document
    `_DOCUMENT_CMAP_CODES`."""

    def test_one_table_is_read_up_to_its_allowance(self):
        groups = [(0x20, 0x7E, 3)] + [(0x30000, 0xFFFFFFFF, 60000)] * 2000
        font = pdffont.TrueTypeFont("Any", BytesIO(_font((3, 10, _format_12(groups)))))
        budget = _pdf_pdfminer._InflateBudget(remaining=1 << 30)
        token = _pdf_pdfminer._INFLATE_BUDGET.set(budget)
        try:
            unicode_map = font.create_unicode_map()
        finally:
            _pdf_pdfminer._INFLATE_BUDGET.reset(token)

        assert unicode_map.get_unichr(3 + ord("a") - 0x20) == "a"
        assert budget.cmap_codes == _pdf_pdfminer._DOCUMENT_CMAP_CODES - _pdf_pdfminer._CMAP_CODES_MAX
        assert len(unicode_map.cid2unichr) <= 0xFFFF

    def test_groups_the_table_does_not_hold_are_not_walked(self):
        font = pdffont.TrueTypeFont("Any", BytesIO(_font((3, 10, _format_12([(0x20, 0x7E, 3)], claimed=0xFFFFFFFF)))))

        assert font.create_unicode_map().get_unichr(3) == " "

    def test_document_allowance_is_shared_by_its_fonts(self):
        font = _font((3, 10, _format_12([(0x20, 0x7E, 3), (0x30000, 0x10FFFF, 200)])))
        budget = _pdf_pdfminer._InflateBudget(remaining=1 << 30)
        budget.cmap_codes = 0x7F - 0x20 + 10
        token = _pdf_pdfminer._INFLATE_BUDGET.set(budget)
        try:
            first = pdffont.TrueTypeFont("Any", BytesIO(font)).create_unicode_map()
            try:
                pdffont.TrueTypeFont("Any", BytesIO(font)).create_unicode_map()
                second_has_a_map = True
            except pdffont.TrueTypeFont.CMapNotFound:
                second_has_a_map = False
        finally:
            _pdf_pdfminer._INFLATE_BUDGET.reset(token)

        assert first.get_unichr(3 + ord("z") - 0x20) == "z"
        assert budget.cmap_codes == 0
        assert not second_has_a_map
