"""PDF characters whose font information is incomplete or whose font is a symbol font: fonts without /Widths, Wingdings checkboxes."""

import pytest

from opendockit import ConvertOptions, convert
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


def _undecoded_two(rotate: int = 0, media_box: bytes = b"0 0 595 842") -> bytes:
    """'Page 207 of the report' in a font whose ToUnicode map gives U+FFFD for the code of its '2' (code 1, glyph 'two.SP'),
    as a book font with old-style figures does: pdfminer reads 'Page \ufffd07'."""
    cmap = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CIDSystemInfo << /Registry (Adobe) /Ordering (UCS)"
            b" /Supplement 0 >> def /CMapName /Adobe-Identity-UCS def /CMapType 2 def 1 begincodespacerange <00> <FF>"
            b" endcodespacerange 1 beginbfchar <01> <FFFD> endbfchar 1 beginbfrange <20> <7E> <0020> endbfrange endcmap"
            b" CMapName currentdict /CMap defineresource pop end end")
    content = b"BT /F1 12 Tf 72 700 Td (Page \\00107 of the report) Tj ET"
    page = (b"<< /Type /Page /Parent 2 0 R /MediaBox [%s] /Rotate %d /Contents 4 0 R"
            b" /Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica /ToUnicode 5 0 R"
            b" /Encoding << /Type /Encoding /BaseEncoding /WinAnsiEncoding /Differences [1 /two.SP] >> >> >> >> >>" % (media_box, rotate))
    return _raw_pdf({1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>", 3: page,
                     4: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
                     5: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(cmap), cmap)})


class TestUndecodedGlyphs:
    """A character pdfminer reads as U+FFFD takes the character pdfium reads at the same place. pdfium reads the glyph of an
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

    def test_where_pdfium_cannot_read_it_either_the_character_stays(self):
        assert "Page \ufffd07 of the report" in convert(_undecoded_two(), "a.pdf").text

    def test_a_pdfium_failure_leaves_the_character_as_it_was(self, monkeypatch):
        """pdfium failing to read the page's text failed the whole document."""
        import pypdfium2 as pdfium

        def fails(*args):
            raise pdfium.PdfiumError("Failed to load text page.")

        monkeypatch.setattr(pdfium.PdfPage, "get_textpage", fails)

        assert "Page \ufffd07 of the report" in convert(_undecoded_two(), "a.pdf").text

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

    def test_a_turned_page_is_left_alone(self, monkeypatch):
        self._pdfium_reads_two(monkeypatch)

        text = convert(_undecoded_two(rotate=90), "a.pdf").text

        assert "of the report" in text and "207" not in text
