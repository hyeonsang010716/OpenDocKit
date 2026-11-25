"""PDF characters whose font information is incomplete or whose font is a symbol font: fonts without /Widths, Wingdings checkboxes."""

from collections.abc import Callable
from importlib.metadata import version
import random
import re
import struct
import zlib

from pdfminer import pdffont
import pytest

from opendockit import ConvertOptions, DocumentTooLargeError, convert
from tests.test_pdf import _raw_pdf


# pdfium before the one pypdfium2 5 bundles gives a glyph turned or scaled by its text matrix a loose box apart from
# pdfminer's, so such a row is not read alike and keeps its U+FFFD there (no wrong character; the minimum supported
# pypdfium2 is 4.30). The tests of what those rows repair to need the newer one.
_TRANSFORMED_BOXES = pytest.mark.skipif(
    int(version("pypdfium2").split(".")[0]) < 5,
    reason="older pdfium measures turned and scaled glyphs otherwise; their rows stay U+FFFD")


def _page_pdf(content: bytes, fonts: dict[bytes, bytes], extra: dict[int, bytes] | None = None) -> bytes:
    """Build a one-page PDF from a mapping of font resource name → font dictionary."""
    resources = b" ".join(b"/%s %s" % (name, font) for name, font in fonts.items())
    return _raw_pdf({1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                     3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R"
                        b" /Resources << /Font << %s >> >> >>" % resources,
                     4: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content), **(extra or {})})


class TestFontWithoutWidths:
    """A font that has no /Widths and whose standard glyph widths pdfminer does not know either (a non-embedded
    TimesNewRomanPSMT, an embedded font that omits /Widths) has an advance width of 0 for every character, so all the
    characters of a line land on the same x. They were taken for copies redrawn at the same spot and removed, so '2024'
    became '204', 'report' became 'repot', and the spaces between words vanished too."""

    _LINES = ["Quarterly sales report 2024", "Total revenue: 1,234,000 USD", "Prepared by the finance team"]

    def test_text_in_a_font_without_widths_is_read_whole(self):
        content = b"BT /F1 12 Tf 72 700 Td 16 TL " + b" ".join(b"(%s) Tj T*" % line.encode() for line in self._LINES) + b" ET"
        data = _page_pdf(content, {b"F1": b"<< /Type /Font /Subtype /TrueType /BaseFont /TimesNewRomanPSMT"
                                          b" /Encoding /WinAnsiEncoding >>"})

        result = convert(data, "a.pdf", ConvertOptions(page_markers=False))

        assert [line for line in result.text.splitlines() if line] == self._LINES

    def test_font_with_widths_is_still_read_with_its_layout(self):
        """A page in a font whose widths are known (Helvetica) is still read with pdfminer — no simple-extraction warning."""
        content = b"BT /F1 12 Tf 72 700 Td 16 TL " + b" ".join(b"(%s) Tj T*" % line.encode() for line in self._LINES) + b" ET"
        data = _page_pdf(content, {b"F1": b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"})

        result = convert(data, "a.pdf", ConvertOptions(page_markers=False))

        assert [line for line in result.text.splitlines() if line] == self._LINES
        assert not result.warnings


class TestSymbolFontCheckboxes:
    """A PDF exported by Word draws checkboxes with Wingdings 2 (☑ = 52, ☐ = A3) or Wingdings (□ = 6F, ☒ = 78), and its
    ToUnicode maps them into the private use area (U+F052 etc.). Those code points are in no shared mapping, so they were
    removed and which of '동의 비동의' (agree / disagree) had been ticked was lost."""

    _CMAP = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /X def 1 begincodespacerange <00> <FF>"
             b" endcodespacerange 4 beginbfchar <52> <F052> <A3> <F0A3> <6F> <F06F> <78> <F078> endbfchar endcmap"
             b" CMapName currentdict /CMap defineresource pop end end")

    def _symbol_font(self, name: bytes) -> bytes:
        return (b"<< /Type /Font /Subtype /TrueType /BaseFont /%s /FirstChar 32 /LastChar 255 /Widths [%s] /ToUnicode 9 0 R >>"
                % (name, b" ".join([b"800"] * 224)))

    @pytest.mark.parametrize(("font", "agree", "disagree", "marks"), [
        (b"Wingdings2", b"(R)", b"<A3>", ("☑", "☐")), (b"ABCDEF+Wingdings-Regular", b"(o)", b"(x)", ("□", "☒"))],
        ids=["wingdings-2", "wingdings"])
    def test_checkbox_glyphs_of_wingdings_fonts_are_kept(self, font, agree, disagree, marks):
        content = (b"BT /F1 11 Tf 72 720 Td (Consent to collection:) Tj ET BT /W 11 Tf 190 720 Td %s Tj ET"
                   b" BT /F1 11 Tf 203 720 Td (Agree) Tj ET BT /W 11 Tf 240 720 Td %s Tj ET"
                   b" BT /F1 11 Tf 253 720 Td (Disagree) Tj ET" % (agree, disagree))
        cmap = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(self._CMAP), self._CMAP)
        data = _page_pdf(content, {b"F1": b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
                                   b"W": self._symbol_font(font)}, {9: cmap})

        result = convert(data, "a.pdf", ConvertOptions(page_markers=False))

        assert result.text == f"Consent to collection: {marks[0]} Agree {marks[1]} Disagree"


class TestWingdingsGlyphsReadAsLetters:
    """Hangul's PDF sets the marks before the items of a list in Wingdings without a ToUnicode map, so they are read by
    their numbers as Windows-1252: 'm' for the circle (6D), '§' for the small square (A7), 'ž' for the dot (9E). 'm (일 시)
    2026.11.20.' came out of four of sixty press releases."""

    @pytest.mark.parametrize(("code", "mark"), [(b"(m)", "○"), (b"<A7>", "▪"), (b"<9E>", "·"), (b"(l)", "●")],
                             ids=["circle", "square", "dot", "disc"])
    def test_a_letter_of_a_wingdings_font_is_the_symbol_of_its_number(self, code, mark):
        content = b"BT /W 11 Tf 72 720 Td %s Tj ET BT /F1 11 Tf 90 720 Td (Venue of the event) Tj ET" % code
        font = (b"<< /Type /Font /Subtype /TrueType /BaseFont /INPILL+Wingdings-Regular /Encoding /WinAnsiEncoding"
                b" /FirstChar 32 /LastChar 255 /Widths [%s] >>" % b" ".join([b"800"] * 224))
        data = _page_pdf(content, {b"F1": b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", b"W": font})

        text = convert(data, "a.pdf", ConvertOptions(page_markers=False)).text

        assert "Venue of the event" in text and mark in text and "m Venue" not in text

    @pytest.mark.parametrize(("font", "code", "mark"), [(b"ABCDEF+Wingdings3", b"(q)", "▼"), (b"Wingdings3", b"(g)", "→"),
                                                        (b"ABCDEF+Webdings", b"(a)", "✓"), (b"Webdings", b"(r)", "✗")],
                             ids=["down", "arrow", "check", "cross"])
    def test_a_letter_of_wingdings_3_or_webdings_is_the_symbol_of_its_number(self, font, code, mark):
        """Wingdings 3 and Webdings hold no letters either, and their numbers mean something else than in Wingdings: 'q' is
        the down triangle of Wingdings 3 and 'a' the check of Webdings."""
        content = b"BT /W 11 Tf 72 720 Td %s Tj ET BT /F1 11 Tf 90 720 Td (Venue of the event) Tj ET" % code
        symbols = (b"<< /Type /Font /Subtype /TrueType /BaseFont /%s /Encoding /WinAnsiEncoding"
                   b" /FirstChar 32 /LastChar 255 /Widths [%s] >>" % (font, b" ".join([b"800"] * 224)))
        data = _page_pdf(content, {b"F1": b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", b"W": symbols})

        text = convert(data, "a.pdf", ConvertOptions(page_markers=False)).text

        assert text == f"{mark} Venue of the event"

    def test_a_letter_of_another_font_stays_a_letter(self):
        content = b"BT /F1 11 Tf 72 720 Td (m) Tj ET BT /F1 11 Tf 90 720 Td (Venue of the event) Tj ET"
        data = _page_pdf(content, {b"F1": b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"})

        assert convert(data, "a.pdf", ConvertOptions(page_markers=False)).text == "m Venue of the event"


def _type3_digits(descriptor: bool = True) -> bytes:
    """Type3 font drawing the digits 0–9 (the form LibreOffice exports a variable font in): the bottom of FontBBox is -1.05em,
    the Descent of the font descriptor is -0.288em."""
    procs = b" ".join(b"/%s 5 0 R" % name for name in (b"zero", b"one", b"two", b"three", b"four", b"five", b"six", b"seven",
                                                       b"eight", b"nine"))
    names = b"/zero /one /two /three /four /five /six /seven /eight /nine"
    described = (b" /FontDescriptor << /Type /FontDescriptor /FontName /NotoSansKR-Thin /Flags 32 /Ascent 1160 /Descent -288"
                 b" /CapHeight 1810 /ItalicAngle 0 /StemV 80 /FontBBox [-991 -1050 2930 1811] >>") if descriptor else b""
    return (b"<< /Type /Font /Subtype /Type3 /FontBBox [-991 -1050 2930 1811] /FontMatrix [0.001 0 0 0.001 0 0]"
            b" /CharProcs << %s >> /Encoding << /Type /Encoding /Differences [48 %s] >> /FirstChar 48 /LastChar 57"
            b" /Widths [%s] /Resources << >>%s >>" % (procs, names, b" ".join([b"560"] * 10), described))


class TestType3DigitsOnALine:
    """pdfminer takes the bottom of a Type3 glyph box from the bottom of FontBBox. When LibreOffice exports a variable font
    ('Noto Sans KR'), the digits are Type3 and the rest is another font, so the tops of characters on the same baseline were
    0.78em apart and they became different lines ('검 / . / 토', table cell '0<br>배출량')."""

    _FONTS = {b"F1": b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", b"F2": _type3_digits()}
    _GLYPH = {5: b"<< /Length 8 >>\nstream\n560 0 d0\nendstream"}

    def test_type3_digits_in_a_table_cell_stay_on_the_line_of_the_text_beside_them(self):
        grid = b" ".join(b"72 %d m 372 %d l S" % (y, y) for y in (720, 700, 680))
        grid += b" " + b" ".join(b"%d 720 m %d 680 l S" % (x, x) for x in (72, 222, 372))
        content = grid + (b" BT /F1 10.5 Tf 76 706 Td (Year) Tj ET BT /F1 10.5 Tf 226 706 Td (Amount) Tj ET"
                          b" BT /F2 10.5 Tf 76 686 Td (2023) Tj ET BT /F1 10.5 Tf 226 686 Td (Total ) Tj /F2 10.5 Tf (450) Tj ET")

        result = convert(_page_pdf(content, self._FONTS, self._GLYPH), "a.pdf", ConvertOptions(page_markers=False))

        assert result.text == "| Year | Amount |\n| --- | --- |\n| 2023 | Total 450 |"

    def test_type3_digits_in_a_paragraph_stay_on_their_lines(self):
        content = (b"BT /F1 10.5 Tf 14 TL 72 700 Td (In ) Tj /F2 10.5 Tf (2023) Tj /F1 10.5 Tf ( the plant cut emissions by ) Tj"
                   b" /F2 10.5 Tf (12) Tj /F1 10.5 Tf ( percent) Tj T* (and in ) Tj /F2 10.5 Tf (2024) Tj"
                   b" /F1 10.5 Tf ( by another ) Tj /F2 10.5 Tf (8) Tj /F1 10.5 Tf ( percent.) Tj ET")

        result = convert(_page_pdf(content, self._FONTS, self._GLYPH), "a.pdf", ConvertOptions(page_markers=False))

        assert result.text == "In 2023 the plant cut emissions by 12 percent\nand in 2024 by another 8 percent."

    def test_type3_font_without_a_descriptor_is_still_read(self):
        content = b"BT /F2 10.5 Tf 72 700 Td (2023) Tj ET"
        data = _page_pdf(content, {b"F2": _type3_digits(descriptor=False)}, {5: b"<< /Length 8 >>\nstream\n560 0 d0\nendstream"})

        result = convert(data, "a.pdf", ConvertOptions(page_markers=False))

        assert result.text == "2023"


class TestWinAnsiUnusedCodesAreBullets:
    """WinAnsiEncoding leaves some codes above 32 unused (7F, 81, 8D, 8F, 90, 9D), and the specification (Appendix D, note 6)
    draws a bullet for every one of them; reportlab writes its list bullets as 7F. pdfminer has no character for them, so the
    bullets came out as `(cid:127)`, were removed and the page got a 'no Unicode mapping' warning."""

    _CONTENT = b"BT /F1 11 Tf 14 TL 72 700 Td (Intro text here) Tj T* (%s first item) Tj T* (%s second item) Tj ET"
    _CMAP = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /X def 1 begincodespacerange <00> <FF>"
             b" endcodespacerange 1 beginbfchar <7F> <2192> endbfchar endcmap CMapName currentdict /CMap defineresource pop end end")

    def _lines(self, font: bytes, code: int = 0x7F, extra: dict[int, bytes] | None = None) -> tuple[list[str], list[str]]:
        mark = b"\\%03o" % code
        result = convert(_page_pdf(self._CONTENT % (mark, mark), {b"F1": font}, extra), "a.pdf",
                         ConvertOptions(page_markers=False))
        return [line for line in result.text.splitlines() if line], result.warnings

    @pytest.mark.parametrize("code", [0x7F, 0x81, 0x8D, 0x8F, 0x90, 0x9D])
    def test_an_unused_win_ansi_code_is_read_as_a_bullet(self, code):
        lines, warnings = self._lines(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>", code)

        assert lines == ["Intro text here", "• first item", "• second item"]
        assert not warnings

    def test_a_to_unicode_entry_for_the_code_is_kept(self):
        lines, _ = self._lines(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding"
                               b" /ToUnicode 9 0 R >>",
                               extra={9: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(self._CMAP), self._CMAP)})

        assert lines == ["Intro text here", "→ first item", "→ second item"]

    def test_differences_naming_the_code_are_kept(self):
        lines, _ = self._lines(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica"
                               b" /Encoding << /BaseEncoding /WinAnsiEncoding /Differences [127 /arrowright] >> >>")

        assert lines == ["Intro text here", "→ first item", "→ second item"]

    @pytest.mark.parametrize("font", [
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",  # StandardEncoding has no bullet rule
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /MacRomanEncoding >>"])
    def test_other_encodings_get_no_bullet(self, font):
        lines, _ = self._lines(font)

        assert "•" not in "".join(lines)

    @pytest.mark.parametrize(("descriptor", "extra", "bullet"), [
        (b"<< /Type /FontDescriptor /FontName /Custom /Flags 32 >>", {}, True),
        (b"<< /Type /FontDescriptor /FontName /Custom /Flags 4 >>", {}, False),
        (b"<< /Type /FontDescriptor /FontName /Custom /Flags 9 0 R >>", {9: b"4"}, False),  # the flags as an indirect object
        (b"<< /Type /FontDescriptor /FontName /Custom /Flags 4.0 >>", {}, False),  # a whole number written as a real
        (b"<< /Type /FontDescriptor /FontName /Custom /Flags 4.5 >>", {}, True),  # not a number of flags
    ])
    def test_a_symbolic_font_gets_no_bullet(self, descriptor, extra, bullet):
        # A font of its own (not one of the standard 14, whose descriptor pdfminer takes from its own metrics), with widths so
        # that pdfminer's reading of the page is kept.
        widths = b" ".join([b"500"] * 224)
        lines, _ = self._lines(b"<< /Type /Font /Subtype /Type1 /BaseFont /Custom /Encoding /WinAnsiEncoding"
                               b" /FirstChar 32 /LastChar 255 /Widths [%s] /FontDescriptor %s >>" % (widths, descriptor), extra=extra)

        assert ("• first item" in lines) is bullet

    _WIDTHS = b" ".join([b"500"] * 224)
    _OWN = (b"/BaseFont /Custom /FirstChar 32 /LastChar 255 /Widths [" + _WIDTHS + b"] /FontDescriptor << /Type /FontDescriptor "
            b"/FontName /Custom /Flags 32 >>")

    @pytest.mark.parametrize(("font", "bullet"), [
        (b"<< /Type /Font /Subtype /TrueType /Encoding /WinAnsiEncoding " + _OWN + b" >>", True),
        (b"<< /Type /Font /Subtype /TrueType /Encoding << /BaseEncoding /WinAnsiEncoding >> " + _OWN + b" >>", True),
        # The code has a glyph of its own, under a name that is not in the glyph list.
        (b"<< /Type /Font /Subtype /TrueType /Encoding << /BaseEncoding /WinAnsiEncoding /Differences [127 /G7F] >> " + _OWN + b" >>", False),
        # No /Encoding: pdfminer reads a TrueType font as WinAnsi, a viewer does not.
        (b"<< /Type /Font /Subtype /TrueType " + _OWN + b" >>", False),
        (b"<< /Type /Font /Subtype /TrueType /Encoding << /Type /Encoding >> " + _OWN + b" >>", False),
        # A Type 3 font draws its own glyphs, and has none for the code.
        (b"<< /Type /Font /Subtype /Type3 /FontBBox [0 0 1000 1000] /FontMatrix [0.001 0 0 0.001 0 0] /CharProcs << >> "
         b"/Encoding /WinAnsiEncoding /FirstChar 32 /LastChar 255 /Widths [" + _WIDTHS + b"] >>", False)])
    def test_fonts_that_do_not_draw_the_win_ansi_bullet_get_none(self, font, bullet):
        lines, _ = self._lines(font)

        assert ("• first item" in lines) is bullet


_UNDECODED_CMAP = (
    b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CIDSystemInfo << /Registry (Adobe) /Ordering (UCS)"
    b" /Supplement 0 >> def /CMapName /Adobe-Identity-UCS def /CMapType 2 def 1 begincodespacerange <00> <FF>"
    b" endcodespacerange 1 beginbfchar <01> <FFFD> endbfchar 1 beginbfrange <20> <7E> <0020> endbfrange endcmap"
    b" CMapName currentdict /CMap defineresource pop end end")


def _undecoded_two(rotate: int = 0, media_box: bytes = b"0 0 595 842", text: bytes = b"Page \\00107 of the report",
                   font: bytes = b"", state: bytes = b"/F1 12 Tf") -> bytes:
    """'Page 207 of the report' in a font whose ToUnicode map gives U+FFFD for the code of its '2' (code 1, glyph 'two.SP'),
    as a book font with old-style figures does: pdfminer reads 'Page \ufffd07'. `font` adds to the font dictionary, `state`
    sets the text state."""
    content = b"BT %s 72 700 Td (%s) Tj ET" % (state, text)
    page = (b"<< /Type /Page /Parent 2 0 R /MediaBox [%s] /Rotate %d /Contents 4 0 R"
            b" /Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 5 0 R %s"
            b" /Encoding << /Type /Encoding /BaseEncoding /WinAnsiEncoding /Differences [1 /two.SP] >> >> >> >> >>"
            % (media_box, rotate, font))
    return _raw_pdf({1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>", 3: page,
                     4: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
                     5: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(_UNDECODED_CMAP), _UNDECODED_CMAP)})


_OLD_STYLE = b" ".join(b"/%s.os" % name for name in
                      (b"one", b"two", b"three", b"four", b"five", b"six", b"seven", b"eight", b"nine", b"zero", b"fi"))


def _old_style_figures(content: bytes, base: bytes = b"BookSerif", media_box: bytes = b"0 0 595 842", rotate: int = 0,
                       dead: int | None = None) -> tuple[bytes, bytes]:
    """A page drawing `content` in a font whose codes 1-10 are the old-style figures 1-9 and 0 (glyphs 'one.os' …; code 11 is
    the ligature 'ﬁ', which pdfium reads as 'f' and 'i'), and the
    same page with a ToUnicode map giving those codes their digits. pdfminer reads the first: its map gives U+FFFD for the
    figures, as a book font's does. pdfium reads the glyphs of such an embedded font right; here it is made to read the
    second (`_pdfium_reads`), so that it reads a real character, and the right one, at every place. The font has /Widths,
    which both readers use — unless `base` is one of the standard 14 fonts, whose widths pdfminer takes from its own
    metrics (and gives the figures, names it does not know, none). On the second page the figure `dead` maps to nothing
    (pdfium then reads no character for it). /F2 is the standard Times-Roman."""
    widths = b"/FirstChar 1 /LastChar 126 /Widths [" + b" ".join([b"556"] * 10 + [b"500"] * 21 + [b"278"] + [b"556"] * 94) + b"]"

    def document(figures: bytes) -> bytes:
        cmap = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CIDSystemInfo << /Registry (Adobe) /Ordering"
                b" (UCS) /Supplement 0 >> def /CMapName /Adobe-Identity-UCS def /CMapType 2 def 1 begincodespacerange <00> <FF>"
                b" endcodespacerange 11 beginbfchar %s <0B> <FB01> endbfchar 1 beginbfrange <20> <7E> <0020> endbfrange endcmap"
                b" CMapName currentdict /CMap defineresource pop end end") % figures
        page = (b"<< /Type /Page /Parent 2 0 R /MediaBox [%s] /Rotate %d /Contents 4 0 R /Resources << /Font << /F1 << /Type"
                b" /Font /Subtype /Type1 /BaseFont /%s /ToUnicode 5 0 R %s /Encoding << /Type /Encoding /BaseEncoding"
                b" /WinAnsiEncoding /Differences [1 %s] >> >> /F2 << /Type /Font /Subtype /Type1 /BaseFont /Times-Roman"
                b" /Encoding /WinAnsiEncoding >> >> >> >>") % (media_box, rotate, base, widths, _OLD_STYLE)
        return _raw_pdf({1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>", 3: page,
                         4: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
                         5: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(cmap), cmap)})

    return (document(b" ".join(b"<%02X> <FFFD>" % code for code in range(1, 11))),
            document(b" ".join(b"<%02X> <%04X>" % (code, 0 if code == dead else ord(str(code % 10))) for code in range(1, 11))))


def _pdfium_reads(monkeypatch: pytest.MonkeyPatch, truth: bytes) -> None:
    """pdfium reads the page of `truth` (`_old_style_figures`) in place of the page converted."""
    import pypdfium2 as pdfium

    from opendockit.extractors import _pdf_glyph_repair

    original, document = _pdf_glyph_repair._read_glyphs, pdfium.PdfDocument(truth)
    monkeypatch.setattr(_pdf_glyph_repair, "_read_glyphs", lambda _, index: original(document, index))


def _converted(monkeypatch: pytest.MonkeyPatch, content: bytes, base: bytes = b"BookSerif", **page: object) -> str:
    data, truth = _old_style_figures(content, base, **page)
    _pdfium_reads(monkeypatch, truth)
    return convert(data, "a.pdf").text.replace("<!-- Page 1 -->", "").strip()


def _repaired(content: bytes, base: bytes = b"BookSerif", change: Callable[[list], list] | None = None,
              dead: int | None = None) -> list[tuple[str, str]]:
    """(the true character, the character after the repair) for each character of the page of `_old_style_figures`, pdfium
    reading the second page of that function — its characters passed through `change` first, if given."""
    from io import BytesIO

    import pdfplumber
    import pypdfium2 as pdfium

    from opendockit.extractors._pdf_glyph_repair import _pdfium_glyphs, _repair_replacement_chars

    data, truth = _old_style_figures(content, base, dead=dead)
    with pdfplumber.open(BytesIO(truth)) as pdf:
        wanted = [char["text"] for char in pdf.pages[0].chars]
    with pdfplumber.open(BytesIO(data)) as pdf:
        page = pdf.pages[0]
        document = pdfium.PdfDocument(truth)
        try:
            glyphs = _pdfium_glyphs(document, 0)
            _repair_replacement_chars(page, (lambda: change(glyphs())) if change else glyphs)
        finally:
            document.close()
        return [(want, char["text"]) for want, char in zip(wanted, page.chars, strict=True)]


def _misread(old: str, new: str) -> Callable[[list], list]:
    """pdfium reads `new` for each `old`."""
    return lambda glyphs: [(*box, new if text == old else text) for *box, text in glyphs]


def _extra_before(text: str) -> Callable[[list], list]:
    """pdfium also reads `text` just left of its last glyph, as for a glyph pdfminer does not see."""
    return lambda glyphs: [*glyphs[:-1], (glyphs[-1][0] - 0.1, *glyphs[-1][1:4], text), glyphs[-1]]


def _line(text: bytes, state: bytes = b"") -> bytes:
    return b"BT /F1 12 Tf %s 72 700 Td (%s) Tj ET" % (state, text)


# Pages of figures pdfminer reads as U+FFFD (codes \001-\011 are the figures 1-9, \012 is 0), drawn in shapes where taking the
# glyph at the place or between the same letters gave a wrong figure, or might
_INDEX = _line(rb"Pears \001, \002, \003, \004, \005, \006, \007, \010, \011")
_PAIRS = _line(rb"Apples \001\002, \003\004, \005\006, \007\010, \011\012, \001\003")
_LABELS = _line(rb"see a\001 a\002 a\003 a\004 a\005 a\006 a\007")
_DATES = _line(rb"on 1/\001, 1/\002, 1/\003, 1/\004, 1/\005, 1/\006")
_VERSIONS = _line(rb"v\001.0 v\002.0 v\003.0 v\004.0 v\005.0 v\006.0 v\007.0")
_PAGE = _line(rb"Page \00207 of the report")
_FIGURE = _line(rb"See Figure 11.1\002 for more")
_FIGURE_AT_END = _line(rb"See Figure 11.1\002")
_STARTING = _line(rb"\002\012\007 pages in all")
_YEAR = _line(rb"In \001\011\011\010 the firm grew")
_RUN = _line(rb"code \001\002\003\004\005\006 end")
_TABLE = (rb"BT /F1 10 Tf 72 700 Td (Item) Tj 100 0 Td (Qty) Tj 100 0 Td (Price) Tj ET BT /F1 10 Tf 72 680 Td (Pens) Tj 100 0 Td"
          rb" (\002) Tj 100 0 Td (4.50) Tj ET BT /F1 10 Tf 72 660 Td (Ink) Tj 100 0 Td (1\002) Tj 100 0 Td (9.\00250) Tj ET")
_TURNED = rb"BT /F1 12 Tf -1 0 0 -1 400 500 Tm (Page \00207 of, total \001\003.) Tj ET"
_BACKWARDS = _line(rb"Pears \001\001\002\003 1\0011\002\002\001 11\00111", b"-1.5 Tc")
_NARROW = _line(rb"11\00211\00311 1\0041", b"-2 Tc")
_SLANTED = rb"BT /F1 12 Tf 0.866 0.5 -0.5 0.866 72 500 Tm (Page \00207 of, total \001\003.) Tj ET"
_UPRIGHT = rb"BT /F1 12 Tf 0 1 -1 0 300 300 Tm (Page \00207 of, total \001\003.) Tj ET"
_CLOSE_LINES = rb"BT /F1 12 Tf 72 700 Td (aa\002aa) Tj ET BT /F1 12 Tf 72.5 703 Td (aa\005aa) Tj ET"
_RAISED = rb"BT /F1 12 Tf 72 700 Td (x) Tj 6 Ts (\002) Tj 0 Ts (y and z) Tj ET"
_LAYERS = rb"BT /F1 12 Tf 72 700 Td (Page \00207 of) Tj ET BT /F1 12 Tf 72.5 700 Td (Page \00507 of) Tj ET"
_BOLD = rb"BT /F1 12 Tf 72 700 Td (Page \00207 of) Tj ET BT /F1 12 Tf 73 700 Td (Page \00207 of) Tj ET"
_KERNED = rb"BT /F1 12 Tf 72 700 Td [(Page ) -300 (\002) 200 (07 of)] TJ ET"
_SQUEEZED = _line(rb"Page \00207 of, total \001\003.", b"50 Tz")
_ALONE = rb"BT /F1 12 Tf 72 700 Td (Body text line here.) Tj ET BT /F1 10 Tf 290 40 Td (\0020\002) Tj ET"
_LIGATURE = _line(rb"the \013\002 of")
_TIGHT = rb"BT /F1 12 Tf 72 700 Td (Page \00207 of) Tj ET BT /F1 12 Tf 72 690 Td (See Figure 11.1\002 for more) Tj ET"
_BREAK = _line(rb"Pears \001, \002, \003 | \004, \005, \006, \007, \010, \011")
_STRAY = _line(rb"Pears ! \001, \002, \003, \004, \005, \006, \007, \010, \011")
_STRAY_LABEL = _line(rb"see a\001 a\002 ! a\003 a\004 a\005 a\006 a\007 a\010 a\011")
_SHAPES = {"index": _INDEX, "pairs": _PAIRS, "labels": _LABELS, "dates": _DATES, "versions": _VERSIONS, "page": _PAGE,
           "figure": _FIGURE, "figure at end": _FIGURE_AT_END, "starting": _STARTING, "year": _YEAR, "run": _RUN,
           "table": _TABLE, "turned": _TURNED, "backwards": _BACKWARDS, "narrow": _NARROW, "slanted": _SLANTED,
           "upright": _UPRIGHT, "close lines": _CLOSE_LINES, "raised": _RAISED, "layers": _LAYERS, "bold": _BOLD,
           "kerned": _KERNED, "squeezed": _SQUEEZED, "alone": _ALONE, "ligature": _LIGATURE, "tight": _TIGHT,
           "break": _BREAK, "stray": _STRAY, "stray label": _STRAY_LABEL}


def _repair_chars(chars: list[dict], glyphs: list, height: float) -> None:
    """Repairs characters given as pdfplumber's (`_repairs`) with pdfium reading `glyphs`."""
    from opendockit.extractors._pdf_glyph_repair import _repairs

    for number, text in _repairs(chars, lambda: glyphs, height):
        chars[number]["text"] = text


def _wrong_in_text(content: bytes, base: bytes = b"BookSerif", dead: int | None = None) -> list[tuple[str, str]]:
    """(true character, converted character) for each character of the converted text of `_old_style_figures` that the repair
    changed to anything but the truth: the text is converted with pdfium reading nothing (no repair), with pdfium reading the
    second page, and that second page itself; where the three texts do not line up, nothing is compared."""
    import pypdfium2 as pdfium

    from opendockit.extractors import _pdf_glyph_repair

    data, truth = _old_style_figures(content, base, dead=dead)
    original, document = _pdf_glyph_repair._read_glyphs, pdfium.PdfDocument(truth)
    try:
        _pdf_glyph_repair._read_glyphs = lambda _, index: original(document, index)
        got = convert(data, "a.pdf").text
        _pdf_glyph_repair._read_glyphs = lambda _, index: []
        plain = convert(data, "a.pdf").text
    finally:
        _pdf_glyph_repair._read_glyphs = original
        document.close()
    want = convert(truth, "a.pdf").text
    if not len(got) == len(plain) == len(want) or any(p != w and p != "\ufffd" for p, w in zip(plain, want, strict=True)):
        return []
    return [(w, g) for p, w, g in zip(plain, want, got, strict=True) if g not in (p, w)]


def _random_text(chance: random.Random, length: int) -> bytes:
    pieces = []
    for _ in range(length):
        roll = chance.random()
        pieces.append(b"\\%03o" % chance.randint(1, 10) if roll < 0.35 else b"\\013" if roll < 0.38 else b" " if roll < 0.48
                      else bytes([chance.choice(b"0123456789")]) if roll < 0.6 else bytes([chance.choice(b".,:/-")])
                      if roll < 0.7 else bytes([chance.choice(b"abcdefghijklmnopqrstuvwxyz")]))
    return b"".join(pieces)


def _random_page(chance: random.Random) -> bytes:
    """A page of a few runs of text mixing old-style figures, digits, letters and punctuation in both fonts, at baselines
    a little apart, with character and word spacing, scaling, rise, invisible text, slants, TJ kerning, and rows of table
    cells."""
    parts = []
    for _ in range(chance.randint(1, 6)):
        state = b""
        if chance.random() < 0.2:
            state += b"%g Tc " % chance.choice([-2, -1.5, -0.5, 0.5, 1, 3])
        if chance.random() < 0.1:
            state += b"%g Tz " % chance.choice([50, 70, 150])
        if chance.random() < 0.15:
            state += b"%g Ts " % chance.choice([-3, 3, 5, 6])
        if chance.random() < 0.1:
            state += b"3 Tr "
        if chance.random() < 0.08:
            state += b"%g Tw " % chance.choice([-3, 5])
        x = chance.choice([72, 72, 72.5, 73, 100, 150, 200, 300]) + chance.choice([0, 0, 0.3, 1.0, 2.0])
        y = chance.choice([700, 700, 700, 701, 702.5, 697, 712, 690, 650])
        if chance.random() < 0.12:
            place = (b"1 0 %g 1 %g %g Tm" % (chance.choice([0.2, -0.2]), x, y) if chance.random() < 0.5
                     else b"0.999 0.035 -0.035 0.999 %g %g Tm" % (x, y))
        else:
            place = b"%g %g Td" % (x, y)
        if chance.random() < 0.3:
            items = []
            for _ in range(chance.randint(1, 5)):
                items += [b"(%s)" % _random_text(chance, chance.randint(1, 6)),
                          b"%d" % chance.choice([-800, -300, -100, 100, 300, 600, 1200])]
            show = b"[%s] TJ" % b" ".join(items[:-1])
        else:
            show = b"(%s) Tj" % _random_text(chance, chance.randint(1, 20))
        if chance.random() < 0.3:
            show += b" %g %g Td (%s) Tj" % (chance.choice([30, 60, 100, -30]), chance.choice([0, 0, -1, -3, 1.5]),
                                           _random_text(chance, chance.randint(1, 10)))
        font = b"F1" if chance.random() < 0.75 else b"F2"
        parts.append(b"BT /%s %d Tf %s %s %s ET" % (font, chance.choice([6, 8, 10, 12, 12, 12, 14, 24]), state, place, show))
    if chance.random() < 0.5:
        for row in range(chance.randint(2, 8)):
            cells = [b"(%s) Tj" % chance.choice([rb"\001\012", rb"1\012", rb"\001\0120", rb"\002", rb"2.\0015", rb"N/A"])
                     for _ in range(chance.randint(2, 5))]
            parts.append(b"BT /F1 %d Tf %s 72 %g Td %s ET" % (chance.choice([8, 10, 12]), chance.choice([b"", b"-0.3 Tc", b"0 Tc"]),
                                                             700 - row * chance.choice([12, 14, 3, 1.5]), b" 60 0 Td ".join(cells)))
    return b" ".join(parts)


def _random_case(seed: int) -> tuple[bytes, bytes, int | None]:
    chance = random.Random(seed)
    content = _random_page(chance)
    return content, chance.choice([b"BookSerif", b"Helvetica"]), chance.randint(1, 10) if chance.random() < 0.6 else None


class TestUndecodedGlyphs:
    """A character pdfminer reads as U+FFFD takes the character pdfium reads for the same glyph. pdfium reads the glyph of an
    embedded book font through the font program ('207', 'Figure 11.12'); with the non-embedded font of a document built here
    it reads U+FFFD as well, so its reading is stood in for by replacing that one character."""

    @staticmethod
    def _pdfium_reads_two(monkeypatch: pytest.MonkeyPatch) -> None:
        from opendockit.extractors import _pdf_glyph_repair

        original = _pdf_glyph_repair._read_glyphs

        def reads_two(document, index):
            return [(*box, "2" if text == "\ufffd" else text) for *box, text in original(document, index)]

        monkeypatch.setattr(_pdf_glyph_repair, "_read_glyphs", reads_two)

    def test_an_undecoded_character_takes_what_pdfium_reads_there(self, monkeypatch):
        text = _converted(monkeypatch, _PAGE)

        assert "Page 207 of the report" in text
        assert "\ufffd" not in text

    def test_pdfminer_giving_the_glyph_no_width_repairs_nothing(self, monkeypatch):
        """pdfminer gives 'two.SP', a name outside the standard widths of Helvetica, no width; pdfium does: the readers place
        the rest of the row differently, so the row is left as it is."""
        self._pdfium_reads_two(monkeypatch)

        assert "Page \ufffd07 of the report" in convert(_undecoded_two(), "a.pdf").text

    @pytest.mark.parametrize("base", [b"BookSerif", b"Helvetica"])
    @pytest.mark.parametrize("shape", list(_SHAPES))
    def test_no_undecoded_character_takes_another_glyphs_character(self, shape, base):
        """pdfium reads the right figure at every place here; a U+FFFD may stay, but must not take a neighbour's figure. With
        the standard Helvetica pdfminer measures by its own widths and gives the figures none, pdfium uses /Widths: further
        along a line its places drift, and a glyph at the place ('Pears 1, \ufffd, 2, 3') or between the same characters (a
        repeating ', \ufffd,') was a neighbour's."""
        assert [(want, got) for want, got in _repaired(_SHAPES[shape], base) if got not in (want, "\ufffd")] == []

    @pytest.mark.parametrize(("shape", "base", "change"), [
        ("page", b"BookSerif", _misread("2", "\x07")), ("page", b"BookSerif", _misread("g", "q")),
        ("break", b"Helvetica", _misread("|", "!")), ("figure at end", b"BookSerif", _extra_before("x")),
        ("figure at end", b"Helvetica", _extra_before("x")), ("stray", b"Helvetica", _misread("!", "?")),
        ("stray label", b"Helvetica", _misread("!", "?"))])
    def test_where_pdfium_reads_otherwise_no_character_takes_a_wrong_one(self, shape, base, change):
        """pdfium reads a control character for the glyph, another character for a decoded one before it (the walk breaks
        there and starts again further on — where the advances differ, from a place that is not in step, so each character
        must then lie at its pair's place), or a glyph pdfminer does not see before it at the end of the row."""
        assert [(want, got) for want, got in _repaired(_SHAPES[shape], base, change) if got not in (want, "\ufffd")] == []

    @pytest.mark.parametrize("shape", ["index", "pairs", "labels", "dates", "versions", "page", "figure", "figure at end",
                                       "starting", "year", "run", "table", pytest.param("turned", marks=_TRANSFORMED_BOXES),
                                       "narrow", pytest.param("squeezed", marks=_TRANSFORMED_BOXES), "kerned",
                                       "ligature", "tight", "backwards", "break", "stray"])
    def test_each_undecoded_character_takes_its_own_glyphs_character(self, shape):
        """Where both readers read the whole row alike, character for character, the figures come back: at the start or
        end of a line, in a run, alone in a table cell, turned 180°, under negative character spacing."""
        assert all(want == got for want, got in _repaired(_SHAPES[shape]))

    @pytest.mark.parametrize("shape", ["index", "labels", "dates", "versions", "page", "figure", "figure at end"])
    def test_where_pdfminer_gives_the_figures_no_width_they_stay(self, shape):
        """pdfminer measures the standard Helvetica by its own widths and gives the figures, names it does not know, none;
        pdfium uses /Widths: the readers place the row differently, and its figures stay U+FFFD."""
        assert {got for want, got in _repaired(_SHAPES[shape], b"Helvetica") if want != got} == {"\ufffd"}

    @pytest.mark.parametrize("content", [
        rb"BT /F1 8 Tf 200 700 Td (Fig) Tj ET BT /F1 12 Tf 100 700 Td (\004 value) Tj ET BT /F1 12 Tf 50 701 Td (Q) Tj ET",
        rb"BT /F1 8 Tf 300 700 Td (Fig) Tj ET BT /F1 12 Tf 72 700 Td (a     \004     b) Tj ET BT /F1 12 Tf 100 701 Td (Q) Tj ET",
        rb"BT /F1 8 Tf 72 700 Td (Fig) Tj ET BT /F1 12 Tf 100 700 Td (the value is \004) Tj ET BT /F1 12 Tf 400 701 Td (Q) Tj ET"])
    def test_a_glyph_pdfium_reads_as_nothing_leaves_its_row_alone(self, content):
        """pdfium reads no character for the figure 4: its row is one short, and a glyph of a line nearby was taken in its
        place ('the value is Q')."""
        assert [(want, got) for want, got in _repaired(content, dead=4) if got not in (want, "\ufffd")] == []

    @pytest.mark.parametrize(("content", "base", "dead"), [
        # table cells under a small negative spacing in Helvetica (figures without width in pdfminer): a run paired by
        # order went backwards in one reader and forwards in the other ('01' for '10')
        (rb"BT /F1 10 Tf -0.3 Tc 72 694 Td (\001\012) Tj 60 0 Td (N/A) Tj 60 0 Td (\002) Tj 60 0 Td (2.\0015) Tj ET",
         b"Helvetica", 5),
        (rb"BT /F1 8 Tf -0.3 Tc 72 676 Td (\001\012) Tj 60 0 Td (2.\0015) Tj 60 0 Td (\002) Tj 60 0 Td (2.\0015) Tj ET",
         b"Helvetica", None),
        # a larger line 2.5pt above a row of cells: its glyphs fell into pdfium's row and shifted the pairing ('t' for '1')
        (rb"BT /F1 14 Tf 100.3 702.5 Td (t \003 \012 \006) Tj ET BT /F1 10 Tf 72 700 Td (1\012) Tj 60 0 Td (\001\0120) Tj"
         rb" 60 0 Td (2.\0015) Tj ET", b"BookSerif", None),
        (rb"BT /F1 24 Tf 1 Tc 150 Tz 3 Tr 75 700 Td (\010wk) Tj 100 -3 Td (m) Tj ET BT /F1 8 Tf 72 700 Td (\002) Tj 60 0 Td"
         rb" (2.\0015) Tj 60 0 Td (\001\0120) Tj 60 0 Td (2.\0015) Tj ET", b"BookSerif", None),
        # pdfium reads nothing for one figure of the row: the end of both rows closed a run without its glyph ('s', '.', '0')
        (rb"BT /F1 24 Tf 300 712 Td (\010\010y\007.q\005\006,\0033\004z\005\011ss) Tj 60 0 Td (tu2 ) Tj ET", b"BookSerif", 9),
        (rb"BT /F1 10 Tf -0.5 Tc 150 Tz 100.3 712 Td (/1\011.\0123 \005..2v) Tj ET", b"BookSerif", 5),
        (rb"BT /F1 10 Tf 72 686 Td (1\012) Tj 60 0 Td (\001\0120) Tj 60 0 Td (\001\012) Tj 60 0 Td (\001\012) Tj ET",
         b"BookSerif", 1),
        # a line 1pt off a row of cells, starting at the same place: two glyphs a reader could take in either order
        (rb"BT /F1 14 Tf 3 Tr 72 690 Td (\012,s\004 p) Tj ET BT /F1 12 Tf 72 691 Td (\001\012) Tj 60 0 Td (1\012) Tj"
         rb" 60 0 Td (\001\012) Tj 60 0 Td (2.\0015) Tj ET", b"BookSerif", None),
        (rb"BT /F1 8 Tf 72 701 Td (\004 .) Tj ET BT /F1 10 Tf 72 700 Td (\001\012) Tj 60 0 Td (\001\012) Tj 60 0 Td"
         rb" (\001\0120) Tj ET", b"BookSerif", 5)])
    def test_pages_where_pairing_short_of_the_whole_row_took_a_wrong_character(self, content, base, dead):
        """Pages of the review's truth fuzzer, reduced to the runs that took a wrong character."""
        assert [(want, got) for want, got in _repaired(content, base, dead=dead) if got not in (want, "\ufffd")] == []

    @staticmethod
    def _spots(spots: list[tuple[str, float, float]]) -> list[dict]:
        return [{"text": text, "x0": left, "x1": left + 6, "top": 842 - middle - 6, "bottom": 842 - middle + 6, "size": 12}
                for text, left, middle in spots]

    @staticmethod
    def _glyphs(spots: list[tuple[str, float, float]]) -> list[tuple]:
        return [(left, left + 6, middle - 6, middle + 6, text) for text, left, middle in spots]

    @pytest.mark.parametrize(("glyphs", "reason"), [
        ([("a", 100, 100), ("2", 109, 100), ("b", 112, 100)], "pdfium's glyph lies 3pt to the right"),
        ([("a", 100, 100), ("2", 106, 100), ("c", 112, 100)], "pdfium reads another letter beside it"),
        ([("a", 100, 100), ("2", 106, 100), ("b", 112, 100), ("x", 118, 100)], "pdfium reads one more glyph in the row"),
        ([("a", 100, 100), ("2", 106, 100)], "pdfium reads one glyph fewer in the row")])
    def test_a_row_the_readers_do_not_read_alike_is_left_alone(self, glyphs, reason):
        chars = self._spots([("a", 100, 100), ("\ufffd", 106, 100), ("b", 112, 100)])
        _repair_chars(chars, self._glyphs([*glyphs, ("c", 100, 300), ("4", 106, 300)]), 842.0)

        assert [char["text"] for char in chars] == ["a", "\ufffd", "b"], reason

    def test_a_glyph_at_another_height_in_the_row_is_not_taken(self):
        """The row reaches 2.9pt higher (a raised 'b'); pdfium's glyph for the U+FFFD lies 5pt above it."""
        chars = self._spots([("a", 100, 100), ("\ufffd", 106, 100), ("b", 112, 102.9)])
        _repair_chars(chars, self._glyphs([("a", 100, 100), ("2", 106, 105), ("b", 112, 102.9)]), 842.0)

        assert [char["text"] for char in chars] == ["a", "\ufffd", "b"]

    def test_a_row_read_alike_is_repaired_whatever_another_row_is(self):
        """Each row is judged on its own: a row pdfium reads otherwise, 4.5pt away, leaves this one alone."""
        chars = self._spots([("a", 100, 100), ("\ufffd", 106, 100), ("c", 100, 104.5), ("\ufffd", 106, 104.5),
                             ("d", 112, 104.5)])
        _repair_chars(chars, self._glyphs([("a", 100, 100), ("2", 106, 100), ("c", 100, 104.5), ("4", 106, 104.5)]), 842.0)

        assert [char["text"] for char in chars] == ["a", "2", "c", "\ufffd", "d"]

    def test_a_row_of_undecoded_characters_alone_is_left_alone(self):
        """Nothing decoded in the row to show that the readers read it alike (a page number all in old-style figures)."""
        chars = self._spots([("\ufffd", 100, 100), ("\ufffd", 106, 100), ("a", 100, 300)])
        _repair_chars(chars, self._glyphs([("2", 100, 100), ("7", 106, 100), ("a", 100, 300)]), 842.0)

        assert [char["text"] for char in chars] == ["\ufffd", "\ufffd", "a"]

    def test_characters_pdfplumber_has_already_built_are_not_changed(self):
        """pdfplumber builds its characters and lines from the layout once; changing pdfminer's characters after that would
        give the lines rebuilt from them (pieces joined) other text than the characters."""
        from io import BytesIO

        import pdfplumber
        import pypdfium2 as pdfium

        from opendockit.extractors._pdf_glyph_repair import _layout_chars, _pdfium_glyphs, _repair_replacement_chars

        data, truth = _old_style_figures(_PAGE)
        with pdfplumber.open(BytesIO(data)) as pdf:
            page = pdf.pages[0]
            assert "\ufffd" in "".join(char["text"] for char in page.chars)
            document = pdfium.PdfDocument(truth)
            try:
                _repair_replacement_chars(page, _pdfium_glyphs(document, 0))
            finally:
                document.close()
            assert "\ufffd" in "".join(letter.get_text() for letter in _layout_chars(page.layout))

    @pytest.mark.parametrize("case", ["raised", "close"])
    def test_characters_of_rows_close_together_take_no_glyph_of_the_other_row(self, case):
        """A raised 'z' far along a row and an 'a' of the next row 1pt from the row's 'a' (an anchor two glyphs could
        serve), or a row and a raised one interleaved."""
        def char(text: str, left: float, middle: float) -> dict:
            return {"text": text, "x0": left, "x1": left + 6, "top": 842 - middle - 6, "bottom": 842 - middle + 6, "size": 12}

        def glyph(text: str, left: float, middle: float) -> tuple:
            return (left, left + 6, middle - 6, middle + 6, text)

        truth = ([("a", 100, 100), ("2", 106, 100), ("z", 300, 102.9), ("a", 99, 104.5)] if case == "raised" else
                 [("2", 100, 100), ("a", 106, 102.9), ("b", 112, 100), ("a", 106.5, 103.5)])
        chars = [char("\ufffd" if text == "2" else text, left, middle) for text, left, middle in truth]
        _repair_chars(chars, [glyph(*spot) for spot in truth], 842.0)

        assert [got["text"] for got in chars] in ([text for text, *_ in truth],
                                                  [("\ufffd" if text == "2" else text) for text, *_ in truth])

    @pytest.mark.parametrize("seed", range(150))
    def test_no_random_page_takes_a_wrong_character(self, seed):
        """The review's truth fuzzer, reduced: on 5,000 such pages each rule short of a whole row read alike took wrong
        characters (364, 536); the whole-row rule took none."""
        content, base, dead = _random_case(seed)

        assert [(want, got) for want, got in _repaired(content, base, dead=dead) if got not in (want, "\ufffd")] == []

    @pytest.mark.parametrize("seed", range(40))
    def test_no_random_page_converts_to_a_wrong_character(self, seed):
        content, base, dead = _random_case(seed)

        assert _wrong_in_text(content, base, dead) == []

    def test_a_table_cell_of_one_undecoded_figure_takes_it(self, monkeypatch):
        assert "| Pens | 2 | 4.50 |" in _converted(monkeypatch, _TABLE)

    @pytest.mark.parametrize("state", [b"/F1 12 Tf", b"/F1 7 Tf", b"/F1 12 Tf 60 Tz"])
    def test_where_pdfium_cannot_read_it_either_the_character_stays(self, state):
        """Nor does it take a neighbour's letter: pdfminer gives the glyph 'two.SP' (no /Widths, a name outside the standard
        widths) no width and pdfium does, so later on the line pdfminer's U+FFFD lies where pdfium draws the letter before
        it, and took it ('total a.', 'total l.'). (The two U+FFFD of pdfminer lie on one spot, and one of them is removed as
        a copy.)"""
        text = convert(_undecoded_two(text=b"Page \\00107 of the report, total \\001\\001.", state=state), "a.pdf").text

        assert "Page \ufffd07 of the report" in text
        assert re.search("total \ufffd+\\.", text), text

    def test_where_the_readers_advance_differently_no_neighbour_is_taken(self):
        """pdfminer measures the standard Helvetica by its own widths, pdfium by the font's /Widths: further along the line
        the glyph under pdfminer's U+FFFD is a neighbour's ('by \ufffd0' became 'by y0')."""
        widths = b"/FirstChar 1 /LastChar 126 /Widths [" + b" ".join([b"456"] * 126) + b"]"
        data = _undecoded_two(text=b"Revenue grew in quarter \\001 by \\0010 percent", font=widths)

        assert "Revenue grew in quarter \ufffd by \ufffd0 percent" in convert(data, "a.pdf").text

    def test_a_ligature_pdfium_reads_as_two_characters_is_one_glyph(self):
        """pdfium gives each character of a glyph mapped to two ('fi') the glyph's box; pdfminer one character."""
        def reads_fi(glyphs: list) -> list:
            return [piece for *box, text in glyphs for piece in ([(*box, "f"), (*box, "i")] if text == "2" else [(*box, text)])]

        assert "".join(got for _, got in _repaired(_line(rb"the of\002ce is"), change=reads_fi)) == "the office is"

    def test_a_pdfium_failure_leaves_the_character_as_it_was(self, monkeypatch):
        """pdfium failing to read the page's text failed the whole document."""
        import pypdfium2 as pdfium

        def fails(*args):
            raise pdfium.PdfiumError("Failed to load text page.")

        monkeypatch.setattr(pdfium.PdfPage, "get_textpage", fails)

        assert "Page \ufffd07 of the report" in convert(_undecoded_two(), "a.pdf").text

    @pytest.mark.parametrize("error", [ValueError, OSError, KeyError])
    def test_a_failure_inside_the_repair_leaves_the_character_as_it_was(self, monkeypatch, error):
        from opendockit.extractors import _pdf_glyph_repair

        def fails(*args):
            raise error("x")

        monkeypatch.setattr(_pdf_glyph_repair, "_grouped", fails)

        assert "Page \ufffd07 of the report" in convert(_undecoded_two(), "a.pdf").text

    def test_running_out_of_memory_inside_the_repair_is_not_hidden(self, monkeypatch):
        from opendockit.extractors import _pdf_glyph_repair

        def fails(*args):
            raise MemoryError

        monkeypatch.setattr(_pdf_glyph_repair, "_grouped", fails)

        with pytest.raises(DocumentTooLargeError):
            convert(_undecoded_two(), "a.pdf")

    def test_each_line_text_holds_its_own_repaired_characters(self):
        """The U+FFFD of a line's text were filled in turn from the repaired characters at the line's level and place; a
        line raised over the row of cells took a '0' of a cell ('\ufffd04' for '694')."""
        content = (rb"BT /F1 10 Tf 3 Ts 73 690 Td [(\006) 600 (\0114)] TJ -30 0 Td (\005:\011i) Tj ET BT /F1 10 Tf 72 700 Td (\002) Tj"
                   rb" 60 0 Td (\001\0120) Tj 60 0 Td (1\012) Tj 60 0 Td (\002) Tj 60 0 Td (\001\0120) Tj ET BT /F1 8 Tf 72 686 Td"
                   rb" (1\012) Tj 60 0 Td (\001\0120) Tj 60 0 Td (2.\0015) Tj ET")

        assert _wrong_in_text(content) == []

    @_TRANSFORMED_BOXES
    @pytest.mark.parametrize("matrix", [b"-1 0 0 -1 400 500", b"-1 0 0 1 400 500"])
    def test_the_characters_of_a_line_drawn_right_to_left_are_put_back_in_its_order(self, monkeypatch, matrix):
        """The line text runs in the order of its characters; filled by their place left to right, a line turned 180° or
        mirrored had its repaired digits swapped ('Page 307 of, total 12.'). Such a row is paired by the right edges of its
        characters, where their origins are."""
        content = b"BT /F1 12 Tf %s Tm (Page \\00207 of, total \\001\\003.) Tj ET" % matrix

        assert "Page 207 of, total 13." in _converted(monkeypatch, content)

    @pytest.mark.parametrize("media_box", [b"30 40 625 882", b"-30 -40 565 802"])
    def test_a_page_box_away_from_the_origin(self, monkeypatch, media_box):
        content = rb"BT /F1 12 Tf 72 700 Td (Page \00207 of the report) Tj ET"

        assert "Page 207 of the report" in _converted(monkeypatch, content, media_box=media_box)

    @pytest.mark.timing
    def test_a_page_of_thousands_of_undecoded_characters_is_repaired_in_linear_time(self):
        """Each undecoded character looks only at pdfium's characters of its own rows: looking at every character of a strip
        200pt wide, 8,000 undecoded characters took 2.3 seconds, four times as many sixteen times as long."""
        from tests.timing import fastest

        def page_of(count: int) -> tuple[list, list, float]:
            rows = count // 100
            chars = [{"text": "\ufffd", "x0": 20.0 + 5 * column, "x1": 24.0 + 5 * column, "top": 10.0 + 8 * row,
                      "bottom": 16.0 + 8 * row} for row in range(rows) for column in range(100)]
            height = 20.0 + 8 * rows
            glyphs = sorted((char["x0"], char["x1"], height - char["bottom"], height - char["top"], "\ufffd") for char in chars)
            return chars, glyphs, height

        def repair(count: int) -> float:
            chars, glyphs, height = page_of(count)
            return fastest(lambda: _repair_chars(chars, glyphs, height))

        small, large = repair(2_000), repair(8_000)
        assert large < small * 8

    @staticmethod
    def _repair_time(chars: list[dict], glyphs: list, height: float) -> float:
        from tests.timing import fastest

        def repair() -> None:
            for char, text in zip(chars, texts, strict=True):
                char["text"] = text
            _repair_chars(chars, glyphs, height)

        texts = [char["text"] for char in chars]
        return fastest(repair, runs=10)  # a few milliseconds: the fastest of three was passed now and then on a macOS runner

    def test_a_glyph_two_characters_pair_with_is_given_to_neither(self):
        """Two rows of pdfminer 3.5pt apart, and one of pdfium between them: both rows pair their U+FFFD with its '2'."""
        def char(text: str, left: float, middle: float) -> dict:
            return {"text": text, "x0": left, "x1": left + 6, "top": 842 - middle - 6, "bottom": 842 - middle + 6, "size": 12}

        chars = [char("a", 100, 100), char("\ufffd", 106, 100), char("a", 100, 103.5), char("\ufffd", 106, 103.5)]
        glyphs = [(100.0, 106.0, 95.75, 107.75, "a"), (106.0, 112.0, 95.75, 107.75, "2")]
        _repair_chars(chars, glyphs, 842.0)

        assert [char["text"] for char in chars] == ["a", "\ufffd", "a", "\ufffd"]

    @pytest.mark.timing
    def test_a_pile_of_glyphs_on_one_spot_is_passed_over_in_linear_time(self):
        """Each decoded character pdfium reads otherwise looks for an anchor among the glyphs at its place."""
        def time_of(count: int) -> float:
            chars = [{"text": "\ufffd" if number % 2 else "a", "x0": 100.0, "x1": 100.0, "top": 736.0, "bottom": 748.0,
                      "size": 12.0} for number in range(count)]
            glyphs = [(100.0, 106.0, 94.0, 106.0, "b")] * count
            return self._repair_time(chars, glyphs, 842.0)

        small, large = time_of(2_000), time_of(8_000)
        assert large < small * 8

    @pytest.mark.timing
    def test_many_characters_in_one_box_are_joined_in_linear_time(self):
        """pdfium gives every character of one glyph the glyph's box; adding each to the text so far was quadratic."""
        from opendockit.extractors._pdf_glyph_repair import _grouped
        from tests.timing import fastest

        def time_of(count: int) -> float:
            found = [(10.0, 20.0, 30.0, 40.0, "x")] * count
            return fastest(lambda: _grouped(found))

        small, large = time_of(50_000), time_of(200_000)
        assert large < small * 8

    @pytest.mark.timing
    def test_many_undecoded_characters_in_one_band_are_repaired_in_linear_time(self):
        """Sixteen lines 1pt apart, every other character undecoded: each one looked at every glyph of its 16pt band, and
        32,000 characters took 9 seconds instead of 1."""
        def time_of(count: int) -> float:
            per_line, height = count // 16, 842.0
            width = 500.0 / per_line
            chars = [{"text": "�" if column % 2 else "a", "x0": 50 + width * column, "x1": 50 + width * (column + 1),
                      "top": 400.0 + line, "bottom": 401.0 + line, "size": 1.0}
                     for line in range(16) for column in range(per_line)]
            glyphs = [(char["x0"], char["x1"], height - char["bottom"], height - char["top"],
                       "2" if char["text"] == "�" else "a") for char in chars]
            return self._repair_time(chars, glyphs, height)

        small, large = time_of(4_000), time_of(16_000)
        assert large < small * 8

    @pytest.mark.timing
    def test_undecoded_characters_of_tall_glyphs_are_repaired_in_linear_time(self):
        """Glyphs 900pt tall went into every 16pt band they reach, and each character of the row looked at all of them."""
        def time_of(count: int) -> float:
            width, height = 500.0 / count, 1000.0
            chars = [{"text": "�" if column % 2 else "a", "x0": 50 + width * column, "x1": 50 + width * (column + 1),
                      "top": 50.0, "bottom": 950.0, "size": 900.0} for column in range(count)]
            glyphs = [(char["x0"], char["x1"], 50.0, 950.0, "2" if char["text"] == "�" else "a") for char in chars]
            return self._repair_time(chars, glyphs, height)

        small, large = time_of(2_000), time_of(8_000)
        assert large < small * 8

    @pytest.mark.parametrize("rotate", [90, 180, 270])
    def test_a_turned_page_is_left_alone(self, monkeypatch, rotate):
        """pdfminer turns the coordinates of a page turned by /Rotate, pdfium does not: a row of one reader can meet another
        row of the other (two lines turned 180° about the page's centre took each other's figures)."""
        content = (rb"BT /F1 12 Tf 72 300 Td (xa\002ay) Tj ET BT /F1 12 Tf 494.64 290.3 Td (ya\005ax) Tj ET")

        text = _converted(monkeypatch, content, media_box=b"0 0 600 600", rotate=rotate)

        assert "\ufffd" in text and "2" not in text and "5" not in text


def _cff_index(objects: list[bytes]) -> bytes:
    if not objects:
        return b"\x00\x00"
    offsets, at = [1], 1
    for body in objects:
        at += len(body)
        offsets.append(at)
    return struct.pack(">HB", len(objects), 2) + struct.pack(f">{len(offsets)}H", *offsets) + b"".join(objects)


def _cff(names: dict[int, str], *, ranges: bool = False, charset: int = 0, more: dict[int, str] | None = None,
         encoding: int | None = None, cid_keyed: bool = False, charset_at: int | None = None, codes: bytes | None = None,
         padding: int = 0) -> bytes:
    """A CFF font program that draws at each code of `names` the glyph of that name: glyph 1 is the first of them. Its
    encoding lists the codes (format 0) or `ranges` of them (format 1), `more` names further codes for glyphs by name, and
    its charset is written in the format `charset`. `encoding` puts a predefined encoding in its place (0: StandardEncoding),
    `charset_at` a predefined charset (0: ISOAdobe), `codes` the code of each glyph as given, and `padding` that many
    operands without an operator into the dictionary."""
    standard = pdffont.CFFFont.STANDARD_STRINGS
    own = sorted({name for name in [*names.values(), *(more or {}).values()] if name not in standard})

    def string_id(name: str) -> int:
        return standard.index(name) if name in standard else len(standard) + own.index(name)

    ids = [string_id(name) for name in names.values()]
    charset_table = {0: b"\x00" + struct.pack(f">{len(ids)}H", *ids),
                     1: b"\x01" + b"".join(struct.pack(">HB", one, 0) for one in ids),
                     2: b"\x02" + b"".join(struct.pack(">HH", one, 0) for one in ids)}[charset]
    codes = bytes(names) if codes is None else codes
    table = (b"\x01" + bytes((len(codes),)) + b"".join(bytes((code, 0)) for code in codes) if ranges
             else b"\x00" + bytes((len(codes),)) + codes)
    if more:
        table = bytes((table[0] | 0x80,)) + table[1:] + bytes((len(more),)) + b"".join(
            struct.pack(">BH", code, string_id(name)) for code, name in more.items())
    glyphs = _cff_index([b"\x0e"] * (len(codes) + 1))
    front = b"\x01\x00\x04\x02" + _cff_index([b"Any"])
    behind = _cff_index([name.encode() for name in own]) + _cff_index([])

    def top(charset_at: int, encoding_at: int, glyphs_at: int) -> bytes:
        entries = b"".join(b"\x1d" + struct.pack(">i", value) + bytes((operator,))
                           for value, operator in ((charset_at, 15), (encoding_at, 16), (glyphs_at, 17)))
        return _cff_index([(b"\x8b\x8b\x8b\x0c\x1e" if cid_keyed else b"") + b"\x8b" * padding + entries])

    start = len(front) + len(top(0, 0, 0)) + len(behind)
    return (front + top(start if charset_at is None else charset_at, start + len(charset_table) if encoding is None else encoding,
                        start + len(charset_table) + len(table)) + behind + charset_table + table + glyphs)


def _cff_font_text(program: bytes, text: bytes, font: bytes = b"", *, kind: bytes = b"Type1C") -> str:
    """The text of a page drawn in a Type 1 font that embeds `program` as CFF. `font` adds entries to the font dictionary."""
    data = _page_pdf(b"BT /F1 12 Tf 72 700 Td (" + text + b") Tj ET",
                     {b"F1": b"<< /Type /Font /Subtype /Type1 /BaseFont /ABCDEF+Any /FirstChar 0 /LastChar 255 /Widths ["
                             + b"600 " * 256 + b"] /FontDescriptor 5 0 R " + font + b">>"},
                     {5: b"<< /Type /FontDescriptor /FontName /ABCDEF+Any /Flags 4 /FontBBox [0 -200 1000 800] /ItalicAngle 0"
                         b" /Ascent 800 /Descent -200 /CapHeight 700 /StemV 80 /FontFile3 6 0 R >>",
                      6: b"<< /Length %d /Subtype /%s >>\nstream\n%s\nendstream" % (len(program), kind, program)})
    return convert(data, "a.pdf", ConvertOptions(page_markers=False)).text


_FRENCH = {0x27: "quotesingle", 0x50: "P", 0x80: "Euro", 0x96: "endash", 0xB0: "degree", 0xE0: "agrave", 0xE8: "egrave",
           0xE9: "eacute", 0xF4: "ocircumflex"}


class TestEncodingBuiltIntoACffFont:
    """A Type 1 font without /Encoding whose program is embedded as CFF (FontFile3) was read with StandardEncoding, not
    with the encoding the program brings: a bank statement read `ProtØgØ`, `DØpts`, `ClientŁle` and `d’un`, 27 wrong
    letters a page, with no warning."""

    _LINE = rb"Prot\351g\351, cr\350dit \340 d\351p\364ts d'un N\260 5 \226 10 \200"

    @pytest.mark.parametrize("layout", [{}, {"ranges": True}, {"charset": 1}, {"charset": 2}],
                             ids=["codes", "ranges-of-codes", "charset-of-short-ranges", "charset-of-long-ranges"])
    def test_codes_are_read_by_the_encoding_of_the_program(self, layout):
        assert _cff_font_text(_cff(_FRENCH, **layout), self._LINE) == "Protégé, crèdit à dépôts d'un N° 5 – 10 €"

    def test_codes_named_beside_the_glyphs_are_read(self):
        program = _cff({0xE9: "eacute"}, more={0xC9: "eacute", 0xB0: "degree"})

        assert _cff_font_text(program, rb"Prot\351g\311 25\260") == "Protégé 25°"

    def test_code_the_program_does_not_name_reads_as_before(self):
        """Code E8 has no glyph in this program: it is the `Ł` of StandardEncoding, as it was."""
        assert _cff_font_text(_cff({0xE9: "eacute"}), rb"caf\351 \350") == "café Ł"

    def test_glyph_name_that_says_no_character_reads_as_before(self):
        assert _cff_font_text(_cff({0x41: "G38", 0xE9: "eacute"}), rb"A caf\351") == "A café"

    def test_program_that_renames_the_letters_is_not_believed(self):
        """A form whose fonts name the glyph drawn for `A` `five` hides its text from a reader of names: the codes are it."""
        program = _cff({0x41: "five", 0x75: "c", 0x74: "b", 0x6F: "bracketright", 0xE9: "eacute"})

        assert _cff_font_text(program, b"Auto") == "Auto"

    def test_to_unicode_goes_first(self):
        cmap = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /T def /CMapType 2 def\n"
                b"1 begincodespacerange <00> <FF> endcodespacerange\n1 beginbfchar <E9> <00C9> endbfchar\n"
                b"endcmap CMapName currentdict /CMap defineresource pop end end")
        data = _page_pdf(rb"BT /F1 12 Tf 72 700 Td (caf\351 \340) Tj ET",
                         {b"F1": b"<< /Type /Font /Subtype /Type1 /BaseFont /ABCDEF+Any /FirstChar 0 /LastChar 255 /Widths ["
                                 + b"600 " * 256 + b"] /FontDescriptor 5 0 R /ToUnicode 7 0 R >>"},
                         {5: b"<< /Type /FontDescriptor /FontName /ABCDEF+Any /Flags 4 /FontBBox [0 -200 1000 800]"
                             b" /ItalicAngle 0 /Ascent 800 /Descent -200 /CapHeight 700 /StemV 80 /FontFile3 6 0 R >>",
                          6: b"<< /Length %d /Subtype /Type1C >>\nstream\n%s\nendstream" % (len(_cff(_FRENCH)), _cff(_FRENCH)),
                          7: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(cmap), cmap)})

        assert convert(data, "a.pdf", ConvertOptions(page_markers=False)).text == "cafÉ à"

    @pytest.mark.parametrize("font", [b"/Encoding /WinAnsiEncoding ", b"/Encoding /MacRomanEncoding "], ids=["win", "mac"])
    def test_font_with_an_encoding_is_read_by_it(self, font):
        expected = {b"/Encoding /WinAnsiEncoding ": "È", b"/Encoding /MacRomanEncoding ": "»"}[font]

        assert _cff_font_text(_cff({0xC8: "eacute"}), rb"\310", font) == expected

    @pytest.mark.parametrize("program", [
        _cff(_FRENCH, encoding=0), _cff(_FRENCH, cid_keyed=True), _cff(_FRENCH)[:40], b"", b"\x01\x00\x04\x02\xff\xff\x04",
        _cff(_FRENCH).replace(b"\x00\x09'P", b"\x07\x09'P"),
    ], ids=["standard-encoding", "cid-keyed", "cut-short", "empty", "index-past-the-end", "encoding-of-an-unknown-format"])
    def test_program_without_an_encoding_to_read_changes_nothing(self, program):
        assert _cff_font_text(program, rb"caf\351 \350") == "cafØ Ł"

    def test_predefined_charset_names_the_glyphs_by_their_number(self):
        """ISOAdobe: glyph n is the standard string n, so the glyph of `eacute` is the one of that number."""
        glyph = pdffont.CFFFont.STANDARD_STRINGS.index("eacute")

        assert _cff_font_text(_cff({}, charset_at=0, codes=b"\x80" * (glyph - 1) + b"\xe9"), rb"caf\351") == "café"

    @pytest.mark.parametrize("layout", [{"encoding": 1}, {"charset_at": 1}, {"charset_at": 2}],
                             ids=["expert-encoding", "expert-charset", "expert-subset-charset"])
    def test_expert_sets_are_not_read(self, layout):
        assert _cff_font_text(_cff(_FRENCH, **layout), rb"caf\351 \350") == "cafØ Ł"

    @pytest.mark.parametrize("renamed", [{0x41: "five"}, {0x35: "A"}], ids=["letter", "digit"])
    def test_one_renamed_letter_or_digit_discredits_the_whole_program(self, renamed):
        """Its other names are not taken either: `eacute` at E9 stays the `Ø` of StandardEncoding."""
        assert _cff_font_text(_cff({**renamed, 0xE9: "eacute"}), rb"A5 caf\351") == "A5 cafØ"

    def test_true_type_font_is_not_read_by_a_cff_program(self):
        data = _page_pdf(rb"BT /F1 12 Tf 72 700 Td (caf\351) Tj ET",
                         {b"F1": b"<< /Type /Font /Subtype /TrueType /BaseFont /ABCDEF+Any /FirstChar 0 /LastChar 255 /Widths ["
                                 + b"600 " * 256 + b"] /FontDescriptor 5 0 R >>"},
                         {5: b"<< /Type /FontDescriptor /FontName /ABCDEF+Any /Flags 4 /FontBBox [0 -200 1000 800]"
                             b" /ItalicAngle 0 /Ascent 800 /Descent -200 /CapHeight 700 /StemV 80 /FontFile3 6 0 R >>",
                          6: b"<< /Length %d /Subtype /Type1C >>\nstream\n%s\nendstream"
                             % (len(_cff({0xE9: "egrave"})), _cff({0xE9: "egrave"}))})

        assert convert(data, "a.pdf", ConvertOptions(page_markers=False)).text == "café"

    @staticmethod
    def _converted(program: bytes, text: bytes, flate: bool = False, **options: object):
        stream = zlib.compress(program) if flate else program
        data = _page_pdf(b"BT /F1 12 Tf 72 700 Td (" + text + b") Tj ET",
                         {b"F1": b"<< /Type /Font /Subtype /Type1 /BaseFont /ABCDEF+Any /FirstChar 0 /LastChar 255 /Widths ["
                                 + b"600 " * 256 + b"] /FontDescriptor 5 0 R >>"},
                         {5: b"<< /Type /FontDescriptor /FontName /ABCDEF+Any /Flags 4 /FontBBox [0 -200 1000 800]"
                             b" /ItalicAngle 0 /Ascent 800 /Descent -200 /CapHeight 700 /StemV 80 /FontFile3 6 0 R >>",
                          6: b"<< /Length %d /Subtype /Type1C%s >>\nstream\n%s\nendstream"
                             % (len(stream), b" /Filter /FlateDecode" if flate else b"", stream)})
        return convert(data, "a.pdf", ConvertOptions(page_markers=False, **options))

    @pytest.mark.parametrize("name", ["oneoldstyle", "Asmall", "uni0000", "uniFFFD", "uniE000"])
    @pytest.mark.parametrize("code", [0x80, 0xE8], ids=["code-without-a-standard-character", "code-with-one"])
    def test_glyph_name_of_no_text_has_no_character(self, name, code):
        """A name of the private use area, a control or U+FFFD is no text. Taken, it was cleaned away and the page lost the
        warning of a character without a mapping while still losing the character; left to StandardEncoding, code E8 read `Ł`,
        which the font does not draw there."""
        result = self._converted(_cff({code: name, 0xE9: "eacute"}), b"No %c caf\351" % code)

        assert result.text == "No café"
        assert any("no Unicode mapping" in warning for warning in result.warnings)

    def test_another_cut_of_the_standard_character_reads_as_before(self):
        """A font of old-style figures names the glyph of `1` `oneoldstyle`, one of small capitals the glyph at `a` `Asmall`:
        the code is that character, and the names beside it are taken."""
        program = _cff({0x31: "oneoldstyle", 0x61: "Asmall", 0x24: "dollaroldstyle", 0x26: "ampersandsmall",
                        0x2D: "hyphensuperior", 0x3F: "questionsmall", 0xA2: "centoldstyle", 0xE9: "eacute"})
        result = self._converted(program, rb"caf\351 1a $5 & a-b ok? 5\242")

        assert result.text == "café 1a $5 & a-b ok? 5¢"
        assert not result.warnings

    def test_cut_of_another_character_has_no_character(self):
        result = self._converted(_cff({0x32: "oneoldstyle", 0xE9: "eacute"}), rb"caf\351 2")

        assert result.text == "café"
        assert any("no Unicode mapping" in warning for warning in result.warnings)

    @pytest.mark.parametrize("filters", [b"[/ASCII85Decode /FlateDecode]", b"[/ASCIIHexDecode /FlateDecode]", b"/ASCII85Decode",
                                         b"/ASCIIHexDecode", b"[/FlateDecode]", b"/Fl"])
    def test_program_under_ascii_filters_is_read(self, filters):
        import base64
        import binascii

        stream = _cff(_FRENCH)
        for name in reversed(filters.strip(b"[]").split()):
            stream = {b"/FlateDecode": zlib.compress, b"/Fl": zlib.compress,
                      b"/ASCII85Decode": lambda data: base64.a85encode(data) + b"~>",
                      b"/ASCIIHexDecode": lambda data: binascii.hexlify(data) + b">"}[name](stream)
        data = _page_pdf(rb"BT /F1 12 Tf 72 700 Td (caf\351) Tj ET",
                         {b"F1": b"<< /Type /Font /Subtype /Type1 /BaseFont /ABCDEF+Any /FirstChar 0 /LastChar 255 /Widths ["
                                 + b"600 " * 256 + b"] /FontDescriptor 5 0 R >>"},
                         {5: b"<< /Type /FontDescriptor /FontName /ABCDEF+Any /Flags 4 /FontBBox [0 -200 1000 800]"
                             b" /ItalicAngle 0 /Ascent 800 /Descent -200 /CapHeight 700 /StemV 80 /FontFile3 6 0 R >>",
                          6: b"<< /Length %d /Subtype /Type1C /Filter %s >>\nstream\n%s\nendstream" % (len(stream), filters, stream)})

        assert convert(data, "a.pdf", ConvertOptions(page_markers=False)).text == "café"

    def test_program_under_an_ascii_filter_behind_the_flate_one_is_not_read(self):
        """Only a Flate stage that ends the chain has its head inflated: one before another filter would have to be inflated whole."""
        import base64

        stream = zlib.compress(base64.a85encode(_cff(_FRENCH)) + b"~>")
        data = _page_pdf(rb"BT /F1 12 Tf 72 700 Td (caf\351) Tj ET",
                         {b"F1": b"<< /Type /Font /Subtype /Type1 /BaseFont /ABCDEF+Any /FirstChar 0 /LastChar 255 /Widths ["
                                 + b"600 " * 256 + b"] /FontDescriptor 5 0 R >>"},
                         {5: b"<< /Type /FontDescriptor /FontName /ABCDEF+Any /Flags 4 /FontBBox [0 -200 1000 800]"
                             b" /ItalicAngle 0 /Ascent 800 /Descent -200 /CapHeight 700 /StemV 80 /FontFile3 6 0 R >>",
                          6: b"<< /Length %d /Subtype /Type1C /Filter [/FlateDecode /ASCII85Decode] >>\nstream\n%s\nendstream"
                             % (len(stream), stream)})

        assert convert(data, "a.pdf", ConvertOptions(page_markers=False)).text == "cafØ"

    def test_program_that_cannot_be_read_is_tried_once(self, monkeypatch):
        from opendockit.extractors import _pdf_pdfminer

        read = []
        reader = _pdf_pdfminer._cff_glyph_names
        monkeypatch.setattr(_pdf_pdfminer, "_cff_glyph_names", lambda program: read.append(1) or reader(program))
        program = b"\x01\x00\x04\x02\xff\xff\x04"
        font = (b"<< /Type /Font /Subtype /Type1 /BaseFont /ABCDEF+Any /FirstChar 0 /LastChar 255 /Widths [" + b"600 " * 256
                + b"] /FontDescriptor 5 0 R >>")
        form = rb"BT /F1 12 Tf 0 0 Td (caf\351) Tj ET"
        content = b"".join(b"q 1 0 0 1 72 %d cm /X Do Q\n" % (760 - 20 * row) for row in range(30))
        data = _raw_pdf({
            1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /XObject << /X 7 0 R >> >> >>",
            4: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
            5: b"<< /Type /FontDescriptor /FontName /ABCDEF+Any /Flags 4 /FontBBox [0 -200 1000 800] /ItalicAngle 0 /Ascent 800"
               b" /Descent -200 /CapHeight 700 /StemV 80 /FontFile3 6 0 R >>",
            6: b"<< /Length %d /Subtype /Type1C >>\nstream\n%s\nendstream" % (len(program), program),
            7: b"<< /Type /XObject /Subtype /Form /BBox [0 0 100 20] /Resources << /Font << /F1 %s >> >> /Length %d >>\nstream\n%s\n"
               b"endstream" % (font, len(form), form)})

        assert convert(data, "a.pdf", ConvertOptions(page_markers=False)).text.count("cafØ") == 30
        assert len(read) == 1

    def test_program_with_a_predictor_is_not_read(self):
        stream = zlib.compress(_cff(_FRENCH))
        data = _page_pdf(rb"BT /F1 12 Tf 72 700 Td (caf\351) Tj ET",
                         {b"F1": b"<< /Type /Font /Subtype /Type1 /BaseFont /ABCDEF+Any /FirstChar 0 /LastChar 255 /Widths ["
                                 + b"600 " * 256 + b"] /FontDescriptor 5 0 R >>"},
                         {5: b"<< /Type /FontDescriptor /FontName /ABCDEF+Any /Flags 4 /FontBBox [0 -200 1000 800]"
                             b" /ItalicAngle 0 /Ascent 800 /Descent -200 /CapHeight 700 /StemV 80 /FontFile3 6 0 R >>",
                          6: b"<< /Length %d /Subtype /Type1C /Filter /FlateDecode /DecodeParms << /Predictor 12 /Columns 4 >> >>"
                             b"\nstream\n%s\nendstream" % (len(stream), stream)})

        assert convert(data, "a.pdf", ConvertOptions(page_markers=False)).text == "cafØ"

    @pytest.mark.parametrize(("name", "text", "expected"), [("sfthyphen", rb"caf\351 co\255operate", "café co-operate"),
                                                             ("uni0009", rb"caf\351 a\255b", "café a b")],
                             ids=["soft-hyphen", "tab"])
    def test_format_and_space_characters_are_text(self, name, text, expected):
        assert self._converted(_cff({0xAD: name, 0xE9: "eacute"}), text).text == expected

    def test_only_the_head_of_a_program_is_inflated(self, monkeypatch):
        """`get_data` inflates the whole stream and keeps it: a 306 KB file whose font program inflates to 300 MB took
        1.6 GB. Only the head is inflated, and no stream of the size of the program is decoded."""
        from opendockit.extractors import _pdf_pdfminer

        decoded = []
        decode = _pdf_pdfminer._decode_unbounded

        def noting(stream):
            decode(stream)
            decoded.append(len(stream.data or b""))

        monkeypatch.setattr(_pdf_pdfminer, "_decode_unbounded", noting)
        program = _cff(_FRENCH) + bytes(5_000_000)

        assert self._converted(program, rb"caf\351", flate=True).text == "café"
        assert max(decoded, default=0) < 1_000_000

    def test_program_under_another_filter_is_not_read(self):
        packed = zlib.compress(zlib.compress(_cff(_FRENCH)))
        data = _page_pdf(rb"BT /F1 12 Tf 72 700 Td (caf\351) Tj ET",
                         {b"F1": b"<< /Type /Font /Subtype /Type1 /BaseFont /ABCDEF+Any /FirstChar 0 /LastChar 255 /Widths ["
                                 + b"600 " * 256 + b"] /FontDescriptor 5 0 R >>"},
                         {5: b"<< /Type /FontDescriptor /FontName /ABCDEF+Any /Flags 4 /FontBBox [0 -200 1000 800]"
                             b" /ItalicAngle 0 /Ascent 800 /Descent -200 /CapHeight 700 /StemV 80 /FontFile3 6 0 R >>",
                          6: b"<< /Length %d /Subtype /Type1C /Filter [/FlateDecode /FlateDecode] >>\nstream\n%s\nendstream"
                             % (len(packed), packed)})

        assert convert(data, "a.pdf", ConvertOptions(page_markers=False)).text == "cafØ"

    def test_ranges_with_codes_named_beside_them(self):
        program = _cff({0xE9: "eacute", 0xE8: "egrave"}, ranges=True, more={0xC9: "eacute"})

        assert self._converted(program, rb"\351\350\311").text == "éèé"

    def test_dictionary_longer_than_a_font_writes_is_not_walked(self):
        assert _cff_font_text(_cff(_FRENCH, padding=5000), rb"caf\351 \350") == "cafØ Ł"
        assert _cff_font_text(_cff(_FRENCH, padding=500), rb"caf\351 \350") == "café è"

    def test_program_is_read_once_however_often_its_font_is_built(self, monkeypatch):
        """A font written into the resources of a form has no object number, so pdfminer builds it again each time the form
        is drawn: a 12 KB file that drew such a form 200 times with a font program of 4 MB took 66 s for 0.1 s."""
        from opendockit.extractors import _pdf_pdfminer

        read = []
        reader = _pdf_pdfminer._cff_glyph_names
        monkeypatch.setattr(_pdf_pdfminer, "_cff_glyph_names", lambda program: read.append(1) or reader(program))
        program = _cff(_FRENCH)
        font = (b"<< /Type /Font /Subtype /Type1 /BaseFont /ABCDEF+Any /FirstChar 0 /LastChar 255 /Widths [" + b"600 " * 256
                + b"] /FontDescriptor 5 0 R >>")
        form = rb"BT /F1 12 Tf 0 0 Td (caf\351) Tj ET"
        content = b"".join(b"q 1 0 0 1 72 %d cm /X Do Q\n" % (760 - 20 * row) for row in range(30))
        data = _raw_pdf({
            1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /XObject << /X 7 0 R >> >> >>",
            4: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
            5: b"<< /Type /FontDescriptor /FontName /ABCDEF+Any /Flags 4 /FontBBox [0 -200 1000 800] /ItalicAngle 0 /Ascent 800"
               b" /Descent -200 /CapHeight 700 /StemV 80 /FontFile3 6 0 R >>",
            6: b"<< /Length %d /Subtype /Type1C >>\nstream\n%s\nendstream" % (len(program), program),
            7: b"<< /Type /XObject /Subtype /Form /BBox [0 0 100 20] /Resources << /Font << /F1 %s >> >> /Length %d >>\nstream\n%s\n"
               b"endstream" % (font, len(form), form)})

        result = convert(data, "a.pdf", ConvertOptions(page_markers=False))

        assert result.text.count("café") == 30
        assert len(read) == 1

    def test_program_of_another_kind_is_not_read_as_cff(self):
        assert _cff_font_text(_cff(_FRENCH), rb"caf\351 \350", kind=b"OpenType") == "cafØ Ł"
