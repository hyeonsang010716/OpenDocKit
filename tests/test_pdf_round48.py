"""PDF defects from review round 48: text in a CID font that uses an embedded CMap stream as its encoding disappeared
without a warning, a PowerPoint PDF that splits every word into two pieces got spaces inside words, and a document with few
lines per page was read to the end even past the character limit."""

from io import BytesIO

import pytest

from opendockit import ConvertOptions, convert
from opendockit.extractors import pdf as pdf_module
from tests.test_quality import _draw_words, _pdf


def _stream_cmap_pdf(lines: list[str], latin: str = "Quiz OX", codes_are_cids: bool = True, notdef: bool = False) -> bytes:
    """One-page PDF whose Type0 font has an embedded CMap stream as /Encoding (`/WP-Encod-0`, code = CID), as WeasyPrint
    writes. The Latin line `latin` is drawn in an ordinary font (Helvetica) so the page also has characters pdfminer can
    read. With `codes_are_cids` False the character codes are numbered by character order (from 0x0100) so they differ
    from the CIDs (the Unicode value of the character) — ToUnicode maps the codes, as the specification says. With `notdef`
    every code is sent to CID 0, like a fallback font that has no Hangul glyphs."""
    chars = sorted({ord(char) for line in lines for char in line})
    code_of = {char: char if codes_are_cids else 0x0100 + index for index, char in enumerate(chars)}
    cidchar = "\n".join(f"<{code_of[char]:04x}> {0 if notdef else char}" for char in chars)
    cmap = ("/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n/CMapName /WP-Encod-0 def\n/CMapType 1 def\n"
            f"1 begincodespacerange\n<0000> <ffff>\nendcodespacerange\n{len(chars)} begincidchar\n{cidchar}\nendcidchar\n"
            "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n").encode()
    bfchar = "\n".join(f"<{code_of[char]:04x}> <{char:04x}>" for char in chars)
    to_unicode = ("/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n/CMapName /Adobe-Identity-UCS def\n"
                  f"/CMapType 2 def\n1 begincodespacerange\n<0000> <ffff>\nendcodespacerange\n{len(chars)} beginbfchar\n"
                  f"{bfchar}\nendbfchar\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n").encode()
    shown = " ".join("<" + "".join(f"{code_of[ord(char)]:04x}" for char in line) + "> Tj T*" for line in lines)
    content = f"BT /F1 12 Tf 14 TL 72 760 Td {shown} ET BT /F2 12 Tf 72 800 Td ({latin}) Tj ET".encode()
    identity = b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >>"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R /F2 10 0 R >> >> "
        b"/Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /Font /Subtype /Type0 /BaseFont /Noto-Sans-CJK-KR /Encoding 7 0 R /DescendantFonts [6 0 R] "
        b"/ToUnicode 8 0 R >>",
        b"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /Noto-Sans-CJK-KR " + identity + b" /DW 1000 /FontDescriptor 9 0 R >>",
        b"<< /Type /CMap /CMapName /WP-Encod-0 " + identity + b" /Length %d >>\nstream\n%s\nendstream" % (len(cmap), cmap),
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(to_unicode), to_unicode),
        b"<< /Type /FontDescriptor /FontName /Noto-Sans-CJK-KR /Flags 4 /FontBBox [0 -120 1000 880] /ItalicAngle 0 "
        b"/Ascent 880 /Descent -120 /CapHeight 700 /StemV 80 >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    out = bytearray(b"%PDF-1.7\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    start = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, start)
    return bytes(out)


_KOREAN_LINES = ["다음 중 옳은 것을 고르시오", "핵 협상의 쟁점은 무엇인가"]


class TestEmbeddedEncodingCMap:
    """With an OTF (CFF) Korean font, WeasyPrint puts an embedded CMap stream in /Encoding instead of a name. pdfminer looks
    CMaps up by name only and used an empty CMap, so not a single character of that font came out. Because the same page
    had characters in a Latin font, the pdfium fallback for pages with zero characters was not taken either (a real exam
    paper PDF: 359 of 4,476 characters, no warning)."""

    def test_text_in_a_font_with_an_embedded_encoding_cmap_is_read(self):
        data = _stream_cmap_pdf(_KOREAN_LINES)

        result = convert(data, "a.pdf")

        assert "다음 중 옳은 것을 고르시오" in result.text
        assert "핵 협상의 쟁점은 무엇인가" in result.text
        assert "Quiz OX" in result.text
        assert result.warnings == [], "pdfminer reads it directly, so it is not switched to simple extraction"

    def test_text_is_read_when_character_codes_differ_from_cids(self):
        """ToUnicode maps character codes, but pdfminer looks it up by CID. When the embedded table gives CIDs that differ from
        the codes, every character was removed as `(cid:n)` (a real textbook PDF: 17,810 → 5,189 characters)."""
        data = _stream_cmap_pdf(_KOREAN_LINES, codes_are_cids=False)

        result = convert(data, "a.pdf")

        assert "다음 중 옳은 것을 고르시오" in result.text
        assert "핵 협상의 쟁점은 무엇인가" in result.text
        assert result.warnings == []

    def test_codes_that_all_share_the_notdef_glyph_keep_their_own_letters(self):
        """A fallback font without the glyphs (DejaVu) sends every Hangul code to CID 0. Looking ToUnicode up by CID turned
        every character into the same one ('평평평')."""
        data = _stream_cmap_pdf(_KOREAN_LINES, codes_are_cids=False, notdef=True)

        result = convert(data, "a.pdf")

        assert "다음 중 옳은 것을 고르시오" in result.text
        assert "핵 협상의 쟁점은 무엇인가" in result.text

    def test_page_with_a_font_pdfminer_cannot_decode_falls_back_to_pdfium_when_it_reads_clearly_more(self, monkeypatch):
        """Imitates the other cases where the embedded CMap cannot be read (an unknown CMap): with stream decoding turned off
        the CMap is empty, as before. Even so, if pdfium reads clearly more characters on that page, the page is switched
        to simple extraction and a warning says so."""
        monkeypatch.setattr(pdf_module, "_embedded_cmap", lambda stream: None)
        data = _stream_cmap_pdf(_KOREAN_LINES)

        result = convert(data, "a.pdf")

        assert "다음 중 옳은 것을 고르시오" in result.text
        assert any("simple extraction" in warning for warning in result.warnings)

    def test_page_whose_fonts_all_decode_is_not_compared_with_pdfium(self, monkeypatch):
        """The comparison is made only in documents where an undecodable font was met — a normal page on which pdfium counts
        more, such as one that draws the same text several times on top of itself, is not switched to simple extraction."""
        calls = []
        original = pdf_module._pdfium_page
        monkeypatch.setattr(pdf_module, "_pdfium_page", lambda *args: calls.append(args) or original(*args))
        data = _stream_cmap_pdf(_KOREAN_LINES)

        convert(data, "a.pdf")

        assert calls == []


class TestWordsSplitIntoTwoPiecesAtFullWidth:
    """A page of a PowerPoint PDF that splits each word into two pieces (two characters + one character) while the character
    width is the font's own width and only the gaps between words are wider. The piece lengths (1, 2) and the kinds of
    boundary (between words, inside a word) interlocked so that the bogus widths 'character width 0.67em, space 0.6em'
    fitted, and a space was put at every boundary ('상태 에 저장 된 문서 가'). A real Hangul character is never narrower than
    0.8 times the font width."""

    def test_no_spaces_are_put_inside_words(self):
        def draw(text):
            words = ["상태|에", "저장|된", "문서|가", "있는|지", "확인|해", "답변|을", "생성|한"]
            _draw_words(text, 72, 100, words, size=14.275, advance=0.972, space=0.30)
            _draw_words(text, 72, 130, ["질문|이", "수업|과", "관련|된", "내용|만", "검색|해"], size=14.275, advance=0.972,
                        space=0.30)

        result = convert(_pdf(draw), "a.pdf")

        assert "상태에 저장된 문서가 있는지 확인해 답변을 생성한" in result.text
        assert "질문이 수업과 관련된 내용만 검색해" in result.text


def _one_line_pages(count: int, header: str | None = None) -> bytes:
    """PDF of `count` pages, each with only one line in the middle that differs from page to page even without its digits
    ('n쪽의 내용은 … 각') (being the only line, it is an edge line). If `header` is given, the same header is put at the top
    of every page."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("HYSMyeongJo-Medium"))
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    for number in range(1, count + 1):
        pdf.setFont("HYSMyeongJo-Medium", 12)
        if header:
            pdf.drawString(72, 790, header)
        pdf.drawString(72, 400, f"{number}쪽의 내용은 서로 다른 문장으로 적었습니다 {chr(0xAC00 + number * 37)}")
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


class TestEdgeLinesThatCannotRepeatCount:
    """The limit check did not count edge lines, which may be removed as running headers. In a slide-like document with one
    or two lines per page every line is an edge line, so it counted as 0 characters and page analysis ran to the end even
    far past the limit (1,500 pages, 7–11 s). A line that would fall short of the repetition criteria (half of all pages,
    2/3 of the odd or even pages, at least 3 pages) even if it appeared on every remaining page cannot be removed, so it
    is counted."""

    def test_reading_stops_once_lines_that_cannot_repeat_exceed_the_limit(self, monkeypatch):
        calls = []
        original = pdf_module._read_page
        monkeypatch.setattr(pdf_module, "_read_page", lambda *args: calls.append(1) or original(*args))
        data = _one_line_pages(120)

        result = convert(data, "a.pdf", ConvertOptions(max_chars=300, extract_images=False))

        assert result.exceeded
        assert len(calls) < 90, ("from page 61 on a line falls short of half even if it appears on every remaining page, "
                                 "so it is counted, and reading stops at the limit")

    @pytest.mark.parametrize("slack", [0, -1])
    def test_limit_stays_exact_when_a_repeated_header_is_removed(self, slack):
        data = _one_line_pages(30, header="공통 머리글 문서 제목입니다")
        full = convert(data, "a.pdf", ConvertOptions(extract_images=False))
        assert "공통 머리글" not in full.text, "the running header is removed"

        result = convert(data, "a.pdf", ConvertOptions(max_chars=full.char_count + slack, extract_images=False))

        assert result.exceeded == (full.char_count > full.char_count + slack)
