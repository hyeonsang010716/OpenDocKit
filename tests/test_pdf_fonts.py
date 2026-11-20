"""PDF characters whose font information is incomplete or whose font is a symbol font: fonts without /Widths, Wingdings checkboxes."""

from collections.abc import Callable
import re

import pytest

from opendockit import ConvertOptions, DocumentTooLargeError, convert
from tests.test_pdf import _raw_pdf


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


def _old_style_figures(content: bytes, base: bytes = b"BookSerif") -> tuple[bytes, bytes]:
    """A page drawing `content` in a font whose codes 1-10 are the old-style figures 1-9 and 0 (glyphs 'one.os' …; code 11 is
    the ligature 'ﬁ', which pdfium reads as 'f' and 'i'), and the
    same page with a ToUnicode map giving those codes their digits. pdfminer reads the first: its map gives U+FFFD for the
    figures, as a book font's does. pdfium reads the glyphs of such an embedded font right; here it is made to read the
    second (`_pdfium_reads`), so that it reads a real character, and the right one, at every place. The font has /Widths,
    which both readers use — unless `base` is one of the standard 14 fonts, whose widths pdfminer takes from its own
    metrics (and gives the figures, names it does not know, none)."""
    widths = b"/FirstChar 1 /LastChar 126 /Widths [" + b" ".join([b"556"] * 10 + [b"500"] * 21 + [b"278"] + [b"556"] * 94) + b"]"

    def document(figures: bytes) -> bytes:
        cmap = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CIDSystemInfo << /Registry (Adobe) /Ordering"
                b" (UCS) /Supplement 0 >> def /CMapName /Adobe-Identity-UCS def /CMapType 2 def 1 begincodespacerange <00> <FF>"
                b" endcodespacerange 11 beginbfchar %s <0B> <FB01> endbfchar 1 beginbfrange <20> <7E> <0020> endbfrange endcmap"
                b" CMapName currentdict /CMap defineresource pop end end") % figures
        page = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 << /Type"
                b" /Font /Subtype /Type1 /BaseFont /%s /ToUnicode 5 0 R %s /Encoding << /Type /Encoding /BaseEncoding"
                b" /WinAnsiEncoding /Differences [1 %s] >> >> >> >> >>") % (base, widths, _OLD_STYLE)
        return _raw_pdf({1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>", 3: page,
                         4: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
                         5: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(cmap), cmap)})

    return (document(b" ".join(b"<%02X> <FFFD>" % code for code in range(1, 11))),
            document(b" ".join(b"<%02X> <%04X>" % (code, ord(str(code % 10))) for code in range(1, 11))))


def _pdfium_reads(monkeypatch: pytest.MonkeyPatch, truth: bytes) -> None:
    """pdfium reads the page of `truth` (`_old_style_figures`) in place of the page converted."""
    import pypdfium2 as pdfium

    from opendockit.extractors import _pdf_glyph_repair

    original, document = _pdf_glyph_repair._read_glyphs, pdfium.PdfDocument(truth)
    monkeypatch.setattr(_pdf_glyph_repair, "_read_glyphs", lambda _, index: original(document, index))


def _converted(monkeypatch: pytest.MonkeyPatch, content: bytes, base: bytes = b"BookSerif") -> str:
    data, truth = _old_style_figures(content, base)
    _pdfium_reads(monkeypatch, truth)
    return convert(data, "a.pdf").text.replace("<!-- Page 1 -->", "").strip()


def _repaired(content: bytes, base: bytes = b"BookSerif",
              change: Callable[[list], list] | None = None) -> list[tuple[str, str]]:
    """(the true character, the character after the repair) for each character of the page of `_old_style_figures`, pdfium
    reading the second page of that function — its characters passed through `change` first, if given."""
    from io import BytesIO

    import pdfplumber
    import pypdfium2 as pdfium

    from opendockit.extractors._pdf_glyph_repair import _pdfium_glyphs, _repair_replacement_chars

    data, truth = _old_style_figures(content, base)
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
        self._pdfium_reads_two(monkeypatch)

        text = convert(_undecoded_two(), "a.pdf").text

        assert "Page 207 of the report" in text
        assert "\ufffd" not in text

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

    @pytest.mark.parametrize(("shape", "base"), [
        *((shape, b"BookSerif") for shape in ("index", "pairs", "labels", "dates", "versions", "page", "figure",
                                              "figure at end", "starting", "year", "run", "table", "turned", "narrow",
                                              "squeezed", "kerned", "ligature", "tight")),
        *((shape, b"Helvetica") for shape in ("index", "labels", "dates", "versions", "page", "figure", "figure at end"))])
    def test_each_undecoded_character_takes_its_own_glyphs_character(self, shape, base):
        """Paired in order along their rows from a character both read alike at the same place, the figures come back even
        where the two readers' advances differ (Helvetica), at the start or end of a line, in a run, and alone in a table
        cell."""
        assert all(want == got for want, got in _repaired(_SHAPES[shape], base))

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

    def test_a_ligature_pdfium_reads_as_two_characters_is_one_glyph(self, monkeypatch):
        """pdfium gives each character of a glyph mapped to two ('fi') the glyph's box; pdfminer one character."""
        from opendockit.extractors import _pdf_glyph_repair

        original = _pdf_glyph_repair._read_glyphs

        def reads_fi(document, index):
            read = []
            for *box, text in original(document, index):
                read += [(*box, "f"), (*box, "i")] if text == "\ufffd" else [(*box, text)]
            return read

        monkeypatch.setattr(_pdf_glyph_repair, "_read_glyphs", reads_fi)

        assert "the office is" in convert(_undecoded_two(text=b"the of\\001ce is"), "a.pdf").text

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

    @pytest.mark.parametrize("matrix", [b"-1 0 0 -1 400 500", b"-1 0 0 1 400 500"])
    def test_the_characters_of_a_line_drawn_right_to_left_are_put_back_in_its_order(self, monkeypatch, matrix):
        """The line text runs in the order of its characters; filled by their place left to right, a line turned 180° or
        mirrored had its repaired digits swapped ('Page 307 of, total 12.')."""
        content = b"BT /F1 12 Tf %s Tm (Page \\00207 of, total \\001\\003.) Tj ET" % matrix

        assert "Page 207 of, total 13." in _converted(monkeypatch, content)

    @pytest.mark.parametrize("media_box", [b"30 40 625 882", b"-30 -40 565 802"])
    def test_a_page_box_away_from_the_origin(self, monkeypatch, media_box):
        self._pdfium_reads_two(monkeypatch)

        assert "Page 207 of the report" in convert(_undecoded_two(media_box=media_box), "a.pdf").text

    @pytest.mark.timing
    def test_a_page_of_thousands_of_undecoded_characters_is_repaired_in_linear_time(self):
        """Each undecoded character looks only at pdfium's characters of its own rows: looking at every character of a strip
        200pt wide, 8,000 undecoded characters took 2.3 seconds, four times as many sixteen times as long."""
        from types import SimpleNamespace

        from opendockit.extractors._pdf_glyph_repair import _repair_replacement_chars
        from tests.timing import fastest

        def page_of(count: int) -> tuple[SimpleNamespace, list]:
            rows = count // 100
            chars = [{"text": "\ufffd", "x0": 20.0 + 5 * column, "x1": 24.0 + 5 * column, "top": 10.0 + 8 * row,
                      "bottom": 16.0 + 8 * row} for row in range(rows) for column in range(100)]
            height = 20.0 + 8 * rows
            glyphs = sorted((char["x0"], char["x1"], height - char["bottom"], height - char["top"], "\ufffd") for char in chars)
            return SimpleNamespace(chars=chars, rotation=0, height=height), glyphs

        def repair(count: int) -> float:
            page, glyphs = page_of(count)
            return fastest(lambda: _repair_replacement_chars(page, lambda: glyphs))

        small, large = repair(2_000), repair(8_000)
        assert large < small * 8

    @staticmethod
    def _repair_time(chars: list[dict], glyphs: list, height: float) -> float:
        from types import SimpleNamespace

        from opendockit.extractors._pdf_glyph_repair import _repair_replacement_chars
        from tests.timing import fastest

        def repair() -> None:
            for char, text in zip(chars, texts, strict=True):
                char["text"] = text
            _repair_replacement_chars(SimpleNamespace(chars=chars, rotation=0, height=height), lambda: glyphs)

        texts = [char["text"] for char in chars]
        return fastest(repair)

    def test_a_glyph_two_characters_pair_with_is_given_to_neither(self):
        """Two rows of pdfminer 3.5pt apart, and one of pdfium between them: both rows pair their U+FFFD with its '2'."""
        from types import SimpleNamespace

        from opendockit.extractors._pdf_glyph_repair import _repair_replacement_chars

        def char(text: str, left: float, middle: float) -> dict:
            return {"text": text, "x0": left, "x1": left + 6, "top": 842 - middle - 6, "bottom": 842 - middle + 6, "size": 12}

        chars = [char("a", 100, 100), char("\ufffd", 106, 100), char("a", 100, 103.5), char("\ufffd", 106, 103.5)]
        glyphs = [(100.0, 106.0, 95.75, 107.75, "a"), (106.0, 112.0, 95.75, 107.75, "2")]
        _repair_replacement_chars(SimpleNamespace(chars=chars, rotation=0, height=842.0), lambda: glyphs)

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

    def test_a_turned_page_is_left_alone(self, monkeypatch):
        self._pdfium_reads_two(monkeypatch)

        text = convert(_undecoded_two(rotate=90), "a.pdf").text

        assert "of the report" in text and "207" not in text
