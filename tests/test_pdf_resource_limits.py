"""Resource limits for PDF: streams that grow when decompressed."""

from io import BytesIO
import zlib

import pytest

from opendockit import ConvertOptions, DocumentTooLargeError, convert
from opendockit.extractors import _pdf_model, _pdf_pdfminer, _pdf_ruled_tables, _pdf_text
from tests.pdf_modules import replace_everywhere
from tests.test_pdf import _raw_pdf


_FONT = b"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >>"
_PAGE = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents %d 0 R " + _FONT + b" >>"


def _flate_stream(body: bytes, extra: bytes = b"") -> bytes:
    packed = zlib.compress(body, 9)
    return b"<< /Length %d /Filter /FlateDecode %s >>\nstream\n" % (len(packed), extra) + packed + b"\nendstream"


def _padded_text(text: bytes, padding: int) -> bytes:
    """A content stream that draws one line of text followed by `padding` bytes of spaces (a few KB when compressed)."""
    return b"BT /F1 12 Tf 72 700 Td (" + text + b") Tj ET\n" + b" " * padding


def _pages_pdf(*contents: bytes) -> bytes:
    """A PDF with one page per content stream."""
    objects = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
               2: b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % (3 + index * 2) for index in range(len(contents))),
                                                               len(contents))}
    for index, content in enumerate(contents):
        objects[3 + index * 2] = _PAGE % (4 + index * 2)
        objects[4 + index * 2] = _flate_stream(content)
    return _raw_pdf(objects)


def _object_stream_pdf(padding: int) -> bytes:
    """A PDF with the font dictionary (object 5) inside an object stream (object 6) that has `padding` bytes of spaces appended at its end (the
    cross-reference stream is object 7)."""
    font = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    header = b"5 0 "
    objects = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
               2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
                  b"/Resources << /Font << /F1 5 0 R >> >> >>",
               4: _flate_stream(_padded_text(b"Inside object stream", 0)),
               6: _flate_stream(header + font + b" " * padding, b"/Type /ObjStm /N 1 /First %d" % len(header))}
    data, offsets = b"%PDF-1.7\n", {}
    for number in sorted(objects):
        offsets[number] = len(data)
        data += b"%d 0 obj\n%s\nendobj\n" % (number, objects[number])
    offsets[7] = len(data)
    rows = [b"\x00" + (0).to_bytes(4, "big") + b"\xff\xff"]
    for number in range(1, 8):
        if number == 5:
            rows.append(b"\x02" + (6).to_bytes(4, "big") + (0).to_bytes(2, "big"))
        else:
            rows.append(b"\x01" + offsets[number].to_bytes(4, "big") + b"\x00\x00")
    table = b"".join(rows)
    data += (b"7 0 obj\n<< /Type /XRef /Size 8 /W [1 4 2] /Root 1 0 R /Length %d >>\nstream\n" % len(table)
             + table + b"\nendstream\nendobj\n")
    return data + b"startxref\n%d\n%%%%EOF\n" % offsets[7]


class TestDecompressedStreamBudget:
    """`max_uncompressed_bytes` applied only to ZIP and HWP, so the content stream of a 760KB PDF used 2.5GB while inflating (the pdfium survey and
    pdfminer each inflated it in full)."""

    def test_content_stream_that_inflates_past_the_limit_is_rejected_before_pdfium_reads_it(self, monkeypatch):
        from opendockit.extractors import pdf as pdf_module

        def survey(*args):
            raise AssertionError("pdfium opened a page that exceeds the limit")

        monkeypatch.setattr(pdf_module, "_survey_page", survey)
        data = _pages_pdf(_padded_text(b"Visible body text", 3_000_000))

        with pytest.raises(DocumentTooLargeError, match="becomes too large when decompressed"):
            convert(data, "a.pdf", ConvertOptions(max_uncompressed_bytes=1_000_000))

    def test_limit_is_a_total_over_the_pages_of_the_document(self):
        data = _pages_pdf(_padded_text(b"Page one", 600_000), _padded_text(b"Page two", 600_000))

        with pytest.raises(DocumentTooLargeError):
            convert(data, "a.pdf", ConvertOptions(max_uncompressed_bytes=1_000_000))

    def test_streams_within_the_limit_are_read_as_before(self):
        data = _pages_pdf(_padded_text(b"Page one", 600_000), _padded_text(b"Page two", 600_000))

        result = convert(data, "a.pdf", ConvertOptions(max_uncompressed_bytes=2_000_000))

        assert "Page one" in result.text and "Page two" in result.text

    def test_object_stream_that_inflates_past_the_limit_is_rejected_instead_of_read_with_pdfium(self, monkeypatch):
        """An object stream is inflated in full when pdfminer looks up an object. Treating the failure as 'structure analysis failed' and handing
        over to pdfium makes pdfium inflate it again."""
        from opendockit.extractors import pdf as pdf_module

        def pdfium_page(*args):
            raise AssertionError("fell back to simple pdfium extraction")

        monkeypatch.setattr(pdf_module, "_pdfium_page", pdfium_page)
        monkeypatch.setattr(pdf_module, "_read_with_pdfium", pdfium_page)
        assert "Inside object stream" in convert(_object_stream_pdf(10), "a.pdf").text

        with pytest.raises(DocumentTooLargeError):
            convert(_object_stream_pdf(3_000_000), "a.pdf", ConvertOptions(max_uncompressed_bytes=1_000_000))


def _big_grid_pdf(pages: int, rows: int = 55, columns: int = 22) -> bytes:
    """A large ruled table with a rectangle drawn for every cell and a number in it (A3 landscape, 1,210 cells per page)."""
    from io import BytesIO
    import random

    from reportlab.lib.pagesizes import A3, landscape
    from reportlab.pdfgen import canvas

    rng = random.Random(3)
    width, height = landscape(A3)
    cell_width, row_height = (width - 60) / columns, (height - 60) / rows
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(width, height))
    for page in range(pages):
        pdf.setFont("Helvetica", 6.5)
        for row in range(rows):
            for column in range(columns):
                x, y = 30 + column * cell_width, height - 30 - (row + 1) * row_height
                pdf.rect(x, y, cell_width, row_height)
                text = f"C{column}" if row == 0 else f"{rng.randint(-99999, 999999):,}" if column else f"P{page}R{row}"
                pdf.drawRightString(x + cell_width - 2, y + 2.5, text)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


class TestLargeRuledGrid:
    """A page with a ruled table of 1,210 cells has fewer than 2,000 character chunks, so it went through pdfminer's text box grouping (quadratic
    time) as usual and took 3.6 seconds per page; a 30-page document went over the time budget from page 18 on and fell back to simple extraction
    without tables. The cell text moves into the table, so there is no order left for the grouping to decide."""

    def test_page_that_is_mostly_a_ruled_grid_is_read_without_box_grouping_and_keeps_its_table(self, monkeypatch):
        from pdfminer.layout import LTLayoutContainer

        calls = []
        original = LTLayoutContainer.group_textboxes

        def counting(self, laparams, boxes):
            calls.append(len(boxes))
            return original(self, laparams, boxes)

        monkeypatch.setattr(LTLayoutContainer, "group_textboxes", counting)

        result = convert(_big_grid_pdf(1), "a.pdf")

        assert calls == []
        assert "| C0 | C1 | C2 |" in result.text and "| P0R54 |" in result.text
        assert not result.warnings


class TestGlyphByGlyphText:
    """Chrome places Hangul glyph by glyph, so the number of pdfium character chunks (`count_rects`) is almost the same as the number of
    characters. One page of a two-column paper (about 2,900 chars) exceeded the text box grouping limit (2,000) and was read without grouping, so
    the column order got mixed up and a warning was raised."""

    def test_two_columns_of_glyph_by_glyph_text_keep_their_reading_order(self):
        from tests.test_pdf import _FONT as KOREAN
        from tests.test_pdf import _korean_pdf

        def draw(pdf, index):
            pdf.setFont(KOREAN, 9)
            for side, left in (("왼", 40), ("오", 310)):
                for line in range(60):
                    x = float(left)
                    for glyph in f"{side}{line:02d} 지역 거점 기관과 협력하여 수요 조사를":
                        pdf.drawString(x, 800 - line * 12.5, glyph)  # place each glyph separately
                        x += pdf.stringWidth(glyph, KOREAN, 9)

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert not result.warnings
        assert result.text.index("왼59") < result.text.index("오00")


def _cid_font_pdf(widths: bytes = b"", to_unicode: bytes = b"", fonts: int = 1) -> bytes:
    """A PDF that writes 'Hi' with Identity-H CID fonts (`fonts` of them, with the same /W and ToUnicode). The mapping body (`to_unicode`) is
    appended after the bfchar for 'Hi'."""
    cmap = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /T def "
            b"1 begincodespacerange <0000> <FFFF> endcodespacerange "
            b"2 beginbfchar <0048> <0048> <0069> <0069> endbfchar " + to_unicode +
            b" endcmap CMapName currentdict /CMap defineresource pop end end")
    objects = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
               2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               4: _flate_stream(b"".join(b"BT /F%d 12 Tf 72 %d Td <00480069> Tj ET\n" % (index, 700 - index * 20)
                                         for index in range(fonts))),
               5: _flate_stream(cmap)}
    names = []
    for index in range(fonts):
        objects[10 + index] = (b"<< /Type /Font /Subtype /Type0 /BaseFont /T /Encoding /Identity-H /ToUnicode 5 0 R "
                               b"/DescendantFonts [<< /Type /Font /Subtype /CIDFontType2 /BaseFont /T "
                               b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
                               b"/DW 500 /W [" + widths + b"] >>] >>")
        names.append(b"/F%d %d 0 R" % (index, 10 + index))
    objects[3] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
                  b"/Resources << /Font << " + b" ".join(names) + b" >> >> >>")
    return _raw_pdf(objects)


class TestFontRangeBounds:
    """pdfminer expanded the ranges of a CID font's /W (`40000 400000000 500`) and the bfrange/cidrange of ToUnicode entry by entry, so a 3KB PDF
    used tens of GB. The decompression limit does not measure this expansion."""

    def test_huge_width_range_is_cut_and_the_text_is_read_with_a_warning(self, monkeypatch):
        from pdfminer import pdffont

        sizes = []
        bounded = pdffont.get_widths
        monkeypatch.setattr(pdffont, "get_widths", lambda seq: sizes.append(len(widths := bounded(seq))) or widths)

        result = convert(_cid_font_pdf(widths=b"1 2000000 500 72 [600 600]"), "a.pdf")

        assert "Hi" in result.text
        assert sizes and max(sizes) <= 1 << 17
        assert any("ranges of a PDF font are abnormally large" in warning for warning in result.warnings)

    def test_huge_to_unicode_ranges_are_cut(self, monkeypatch):
        from pdfminer.cmapdb import FileUnicodeMap

        added = []
        original = FileUnicodeMap.add_cid2unichr
        monkeypatch.setattr(FileUnicodeMap, "add_cid2unichr", lambda self, cid, code: added.append(cid) or original(self, cid, code))
        to_unicode = (b"1 beginbfrange <00010000> <00200000> <0041> endbfrange "
                      b"1 begincidrange <00010000> <00200000> 65536 endcidrange")

        result = convert(_cid_font_pdf(to_unicode=to_unicode), "a.pdf")

        assert "Hi" in result.text
        assert len(added) <= 2 * (1 << 17) + 2
        assert any("ranges of a PDF font are abnormally large" in warning for warning in result.warnings)

    def test_ranges_are_capped_over_the_whole_document(self, monkeypatch):
        """With only a per-font cap, thousands of small font dictionaries achieve the same thing. A cap is also placed on the total over the whole
        document."""
        from pdfminer import pdffont

        from opendockit.extractors import pdf as pdf_module

        monkeypatch.setattr(pdf_module, "_DOCUMENT_FONT_RANGE_ENTRIES", 1_000)
        sizes = []
        bounded = pdffont.get_widths
        monkeypatch.setattr(pdffont, "get_widths", lambda seq: sizes.append(len(widths := bounded(seq))) or widths)

        result = convert(_cid_font_pdf(widths=b"1 600 500", fonts=4), "a.pdf")

        assert result.text.count("Hi") == 4
        assert sum(sizes) <= 1_000
        assert any("ranges of a PDF font are abnormally large" in warning for warning in result.warnings)

    def test_ordinary_ranges_are_read_without_a_warning(self):
        result = convert(_cid_font_pdf(widths=b"1 17000 500", to_unicode=b"1 beginbfrange <0100> <01FF> <0100> endbfrange"),
                         "a.pdf")

        assert "Hi" in result.text
        assert not result.warnings


class TestPdfminerLogNoise:
    """pdfminer logs a 'Could not get FontBBox' warning for every font that lacks a font descriptor, so the server's stderr was flooded with the
    same line on every conversion. The result is the same, and reporting is the job of the warnings list."""

    def test_pdfminer_warnings_are_not_logged_during_conversion(self, caplog):
        import logging

        before = logging.getLogger("pdfminer").level

        result = convert(_cid_font_pdf(), "a.pdf")

        assert "Hi" in result.text
        assert not [record for record in caplog.records if record.name.startswith("pdfminer")]
        assert logging.getLogger("pdfminer").level == before


class TestOverprintedText:
    """A page with the same text drawn thousands of times on the same spot (a repeated content stream, 106KB) grew with the square of the number of
    copies in pdfminer's line grouping and piece merging, and used 110 seconds and 2.2GB."""

    def test_copies_beyond_a_few_are_dropped_before_layout_analysis(self, monkeypatch):

        sizes = []
        original = _pdf_text._merged_overlapping_pieces
        monkeypatch.setattr(_pdf_text, "_merged_overlapping_pieces",
                            lambda pieces, margin: sizes.append(len(pieces)) or original(pieces, margin))
        content = b"BT /F1 12 Tf 72 700 Td (Overprinted title) Tj ET\n" * 400 + b"BT /F1 12 Tf 72 650 Td (Body line) Tj ET"

        result = convert(_pages_pdf(content), "a.pdf")

        assert result.text.count("Overprinted title") == 1 and "Body line" in result.text
        assert sizes and max(sizes) <= 10


def _form_chain_pdf(levels: int, fan: int = 10, straight: int = 0) -> bytes:
    """Page 1 is ordinary text; page 2 is a chain of `levels` levels in which form X1 draws X2 `fan` times, X2 draws X3 `fan` times … (the last
    form holds one line of text). The file is a few KB but `fan ** (levels - 1)` texts are drawn. The first `straight` levels draw the next form
    only once each."""
    font = b"/Font << /F1 5 0 R >>"
    objects = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
               2: b"<< /Type /Pages /Kids [3 0 R 6 0 R] /Count 2 >>",
               3: _PAGE % 4,
               4: _flate_stream(_padded_text(b"Plain first page", 0)),
               5: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               6: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 7 0 R /Resources << %s /XObject << /X 8 0 R >> >> >>"
                  % font,
               7: _flate_stream(b"/X Do")}
    for level in range(levels):
        number = 8 + level
        if level < levels - 1:
            body = b" ".join(b"q 1 0 0 1 0 -1 cm /X Do Q" for _ in range(1 if level < straight else fan))
            resources = b"<< %s /XObject << /X %d 0 R >> >>" % (font, number + 1)
        else:
            body, resources = b"BT /F1 8 Tf 50 700 Td (Leaf text) Tj ET", b"<< %s >>" % font
        objects[number] = _flate_stream(body, b"/Type /XObject /Subtype /Form /BBox [0 0 595 842] /Resources " + resources)
    return _raw_pdf(objects)


class TestRepeatedFormXObjects:
    """A 7-level chain in which each form draws the next one 10 times (3.4KB) used 5.9GB merely by pdfium opening the page. Streams are measured
    only once each, so the decompression limit cannot stop it, and drawn objects were counted only after pdfium had opened the page. The real size
    uses up the memory, so the tests lower the limit."""

    def test_page_whose_forms_multiply_past_the_limit_is_opened_by_neither_pdfium_nor_pdfminer(self, monkeypatch):
        from opendockit.extractors import pdf as pdf_module

        surveyed, read = [], []
        original_survey, original_read = pdf_module._survey_page, pdf_module._read_page
        monkeypatch.setattr(pdf_module, "_survey_page", lambda document, index: surveyed.append(index + 1)
                            or original_survey(document, index))
        monkeypatch.setattr(pdf_module, "_read_page", lambda page, *args: read.append(page.page_number)
                            or original_read(page, *args))
        monkeypatch.setattr(pdf_module, "_MAX_DRAWN_OBJECTS", 500)

        result = convert(_form_chain_pdf(4), "a.pdf")

        assert surveyed == [1] and read == [1]
        assert "Plain first page" in result.text and "Leaf text" not in result.text
        assert any("forms (Form XObject)" in warning and "(2)" in warning and "were not read" in warning for warning in result.warnings)

    def test_forms_drawn_a_few_times_are_read_as_before(self, monkeypatch):
        from opendockit.extractors import pdf as pdf_module

        monkeypatch.setattr(pdf_module, "_MAX_DRAWN_OBJECTS", 500)

        result = convert(_form_chain_pdf(2), "a.pdf")

        assert "Leaf text" in result.text and "Plain first page" in result.text
        assert not result.warnings

    def test_estimate_multiplies_each_form_by_the_times_it_is_drawn(self):
        import pdfplumber

        from opendockit.extractors import pdf as pdf_module

        with pdfplumber.open(BytesIO(_form_chain_pdf(4))) as document:
            plain, chained = (pdf_module._drawn_objects(page, 10**9) for page in document.pages)

        assert plain == 1  # a page without forms also counts drawing operators (1 text)
        assert chained == 1 + 10 * (1 + 10 * (1 + 10 * (1 + 1)))  # page draws X1 once, X1-X3 draw the next 10 times each, the last form is 1 text

    def test_chain_deeper_than_the_nesting_limit_counts_as_past_the_limit(self, monkeypatch):
        """A chain deeper than 32 levels was counted as one object, so a 10KB file that multiplies below that depth did not finish in over 12
        minutes (pdfminer has no depth limit). The test lowers the depth limit."""
        import pdfplumber

        from opendockit.extractors import pdf as pdf_module

        replace_everywhere(monkeypatch, "_FORM_NESTING_MAX", 3)

        with pdfplumber.open(BytesIO(_form_chain_pdf(6, fan=1))) as document:
            deep = pdf_module._drawn_objects(document.pages[1], 1000)

        assert deep > 1000


def _paint_ops_pdf(paints: int, pages: int = 1) -> bytes:
    """Page 1 is ordinary text; the `pages` pages after it share one content stream (one line of text + a 1pt rectangle filled `paints` times)."""
    font = b"/Resources << /Font << /F1 4 0 R >> >>"
    kids = b" ".join(b"%d 0 R" % (6 + index) for index in range(pages))
    objects = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
               2: b"<< /Type /Pages /Kids [3 0 R %s] /Count %d >>" % (kids, pages + 1),
               3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 5 0 R %s >>" % font,
               4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               5: _flate_stream(_padded_text(b"Plain first page", 0)),
               6 + pages: _flate_stream(_padded_text(b"Painted page", 0) + b"0 0 1 1 re f\n" * paints)}
    for index in range(pages):
        objects[6 + index] = b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents %d 0 R %s >>" % (6 + pages, font)
    return _raw_pdf(objects)


class TestManyPaintOperators:
    """A 253KB PDF with no forms that repeats `0 0 1 1 re f` 10 million times in its content stream used 3.6GB and 19 seconds merely by pdfium
    opening the page. For a page without forms the drawn objects were not counted before opening it. With 40 pages sharing one stream, it was
    opened again for every page and took 143 seconds."""

    def test_page_with_too_many_paint_operators_is_opened_by_neither_pdfium_nor_pdfminer(self, monkeypatch):
        from opendockit.extractors import pdf as pdf_module

        surveyed, read = [], []
        original_survey, original_read = pdf_module._survey_page, pdf_module._read_page
        monkeypatch.setattr(pdf_module, "_survey_page", lambda document, index: surveyed.append(index + 1)
                            or original_survey(document, index))
        monkeypatch.setattr(pdf_module, "_read_page", lambda page, *args: read.append(page.page_number)
                            or original_read(page, *args))
        monkeypatch.setattr(pdf_module, "_MAX_DRAWN_OBJECTS", 500)

        result = convert(_paint_ops_pdf(600, pages=3), "a.pdf")

        assert surveyed == [1] and read == [1]
        assert "Plain first page" in result.text and "Painted page" not in result.text
        assert any("(2, 3, 4)" in warning and "were not read" in warning for warning in result.warnings)

    def test_content_stream_shared_by_pages_is_counted_once(self, monkeypatch):
        from opendockit.extractors import pdf as pdf_module

        scanned = []
        original = _pdf_pdfminer._paint_operators
        monkeypatch.setattr(_pdf_pdfminer, "_paint_operators", lambda data, limit: scanned.append(len(data))
                            or original(data, limit))
        monkeypatch.setattr(pdf_module, "_MAX_DRAWN_OBJECTS", 500)

        convert(_paint_ops_pdf(600, pages=5), "a.pdf")

        assert len(scanned) == 2  # one stream for page 1, one stream shared by pages 2-6

    def test_page_with_a_few_paint_operators_is_read_as_before(self, monkeypatch):
        from opendockit.extractors import pdf as pdf_module

        monkeypatch.setattr(pdf_module, "_MAX_DRAWN_OBJECTS", 500)

        result = convert(_paint_ops_pdf(400), "a.pdf")

        assert "Painted page" in result.text and not result.warnings


class TestDeepFormsAreSurveyedToTheBottom:
    """12 levels of forms drawing once each followed by 5 levels drawing 11 times each (6.8KB, 160,000 objects) took 20.9 seconds and 3.3GB without
    a warning. The page survey counted only down to pypdfium2's default depth (15 levels), so the page did not hit the drawn-object limit
    (`_MAX_PAGE_OBJECTS`) and went on to pdfminer."""

    def test_objects_below_fifteen_levels_of_forms_count_toward_the_page_object_limit(self, monkeypatch):
        from opendockit.extractors import pdf as pdf_module

        read = []
        original_read = pdf_module._read_page
        monkeypatch.setattr(pdf_module, "_read_page", lambda page, *args: read.append(page.page_number)
                            or original_read(page, *args))
        monkeypatch.setattr(pdf_module, "_MAX_PAGE_OBJECTS", 50)

        result = convert(_form_chain_pdf(20, fan=10, straight=17), "a.pdf")

        assert read == [1]
        assert any("have too many drawing elements and were" in warning and "(2)" in warning for warning in result.warnings)


class TestStackedRules:
    """A 2KB PDF that fills a 1pt rectangle 45,000 times on the same spot took 457 seconds. Joining horizontal rules (`_joined_rules`) scanned the
    list of rules ending at the same height and end x from the start for every rule, and since none of the overlapping rules joined, the list grew
    without end."""

    @pytest.mark.timing
    def test_rules_stacked_on_the_same_spot_take_linear_time(self):
        import time


        edges = [{"x0": 100.0, "x1": 101.0, "top": 50.0} for _ in range(20_000)]

        started = time.perf_counter()
        joined = _pdf_ruled_tables._joined_rules(edges, lambda x, top: True)

        assert time.perf_counter() - started < 2, "before the fix 20,000 rules took more than 30 seconds"
        assert len(joined) == 20_000

    def test_pieces_meeting_at_a_column_border_are_still_joined(self):

        edges = [{"x0": 0.0, "x1": 50.0, "top": 10.0}, {"x0": 50.0, "x1": 100.0, "top": 10.0},
                 {"x0": 0.0, "x1": 50.0, "top": 10.0}]

        joined = _pdf_ruled_tables._joined_rules(edges, lambda x, top: x == 50.0)

        assert sorted(joined) == [(0.0, 50.0, 10.0), (0.0, 100.0, 10.0)]


class TestOverprintedCopiesOfOneLine:
    """A 3.2KB PDF that draws the same text 16,000 times on the same spot through forms took 52 seconds. For every copy line all the removed
    characters (copies) at that height were scanned again: lines × copies. A line with the same position and text as a line already dropped as a
    copy is dropped without scanning."""

    def test_copies_of_one_line_are_not_each_compared_with_every_copy(self, monkeypatch):
        from opendockit.extractors import pdf as pdf_module

        examined = []
        original = pdf_module._chars_at_line_level
        monkeypatch.setattr(pdf_module, "_chars_at_line_level",
                            lambda line, chars, tops: examined.append(len(found := original(line, chars, tops))) or found)

        result = convert(_form_chain_pdf(4, fan=12), "a.pdf")

        assert result.text.count("Leaf text") == 1 and "Plain first page" in result.text
        assert sum(examined) < 50_000, "before the fix each of the 1,728 copy lines scanned every character at its height: 26.87 million"


@pytest.mark.skipif(not hasattr(__import__("signal"), "setitimer"), reason="platform without SIGALRM")
class TestStreamWithBytesAfterTheZlibEnd:
    """When a content stream whose `/Length` also counts the trailing line break (`\\r\\n`) inflated to more than 1MB, the conversion never
    finished (100% CPU, and it did not obey the time budget either). The size measurement (`_inflated_size`) kept feeding the bytes left after the
    zlib end back in. pdfium and pdfminer read the stream normally."""

    @pytest.mark.timing
    def test_size_of_a_stream_with_bytes_after_the_end_is_measured(self):

        body = b"0 0 1 1 re f\n" * 100_000

        size = _measure_within(lambda: _pdf_pdfminer._inflated_size(zlib.compress(body) + b"\r\n", 10**9), 5)

        assert size == len(body)

    @pytest.mark.timing
    def test_page_whose_length_counts_the_line_break_is_read(self):
        from tests.test_pdf import _convert_within

        packed = zlib.compress(_padded_text(b"Visible body text", 1_500_000), 9) + b"\r\n"
        stream = b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(packed) + packed + b"endstream"
        data = _raw_pdf({1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                         3: _PAGE % 4, 4: stream})

        result = _convert_within(data, 20)

        assert "Visible body text" in result.text


def _measure_within(call, seconds: float):
    """_Hung if it does not finish within `seconds` (so that an infinite loop does not stall the test runner)."""
    import signal

    from tests.test_pdf import _Hung

    def interrupt(signum, frame):
        raise _Hung

    previous = signal.signal(signal.SIGALRM, interrupt)
    signal.setitimer(signal.ITIMER_REAL, seconds, 1.0)
    try:
        return call()
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class TestSimpleReadingOfPagesWithText:
    """A 110KB PDF in which 300 pages share one form with 400,000 drawing operators took 225 seconds, about 1 second per page, even after going
    over the time budget (60 seconds). Simple extraction (`_simple_page`) walked all 400,000 objects of the page in Python to find out whether it
    has pictures. Whether there are pictures is used only for pages without text (scan detection)."""

    def test_page_with_text_is_not_scanned_for_pictures(self, monkeypatch):
        import pypdfium2

        scanned = []
        original = pypdfium2.PdfPage.get_objects
        monkeypatch.setattr(pypdfium2.PdfPage, "get_objects",
                            lambda page, *args, **kwargs: scanned.append(1) or original(page, *args, **kwargs))

        result = convert(_pages_pdf(_padded_text(b"Page one", 0), _padded_text(b"Page two", 0)), "a.pdf",
                         ConvertOptions(pdf_time_budget=0))

        assert "Page one" in result.text and "Page two" in result.text
        assert scanned == []  # a page past the time budget is not surveyed (`_survey_page`) either

    def test_page_without_text_still_reports_its_picture(self):
        from opendockit.extractors import pdf as pdf_module

        image = b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray /BitsPerComponent 8 /Length 1 >>\nstream\n\x80\nendstream"
        content = b"q 500 0 0 700 40 60 cm /Im1 Do Q"
        data = _raw_pdf({1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                         3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
                            b"/Resources << /XObject << /Im1 5 0 R >> >> >>",
                         4: b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream", 5: image})
        document = pdf_module._open_pdfium(data)

        page = pdf_module._pdfium_page(document, 0)

        assert page.has_images and not page.lines


def _shared_form_pdf(pages: int, paints: int) -> bytes:
    """`pages` pages each draw the same form X (a 1pt rectangle filled `paints` times) from their own content stream (one line of text + `/X Do`)."""
    font = b"/Font << /F1 3 0 R >>"
    kids = b" ".join(b"%d 0 R" % (5 + index * 2) for index in range(pages))
    objects = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
               2: b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, pages),
               3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               4: _flate_stream(b"0 0 1 1 re f\n" * paints, b"/Type /XObject /Subtype /Form /BBox [0 0 595 842]")}
    for index in range(pages):
        objects[5 + index * 2] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents %d 0 R "
                                  b"/Resources << %s /XObject << /X 4 0 R >> >> >>" % (6 + index * 2, font))
        objects[6 + index * 2] = _flate_stream(_padded_text(b"Page %d text" % (index + 1), 0) + b"/X Do\n")
    return _raw_pdf(objects)


class TestFormSharedByPages:
    """In a PDF where 300 pages draw the same form (400,000 drawing operators), the drawn-object estimate (`_drawn_objects`) counted the form again
    for every page and spent 0.2 seconds more per page. The count inside a form does not depend on the page, so it is counted once per document."""

    def test_form_drawn_by_several_pages_is_counted_once(self, monkeypatch):

        scanned = []
        original = _pdf_pdfminer._paint_operators
        monkeypatch.setattr(_pdf_pdfminer, "_paint_operators", lambda data, limit: scanned.append(len(data))
                            or original(data, limit))

        result = convert(_shared_form_pdf(4, 300), "a.pdf")

        assert all(f"Page {number} text" in result.text for number in range(1, 5))
        assert scanned.count(len(b"0 0 1 1 re f\n" * 300)) == 1

    def test_shared_form_still_counts_against_each_page(self, monkeypatch):
        from opendockit.extractors import pdf as pdf_module

        monkeypatch.setattr(pdf_module, "_MAX_DRAWN_OBJECTS", 200)

        result = convert(_shared_form_pdf(3, 300), "a.pdf")

        assert "Page 3 text" not in result.text
        assert any("(1, 2, 3)" in warning and "were not read" in warning for warning in result.warnings)


class TestManyCellsOnOnePage:
    """One page of a ruled table with thousands of cells took tens of seconds (68 seconds for 7,500 filled cells). Every character was checked
    against all the cells to see whether it is inside one (`_inside_any`), and for every cell all the fill rectangles were scanned to measure the
    filled ratio (`_fill_cover`)."""

    @staticmethod
    def _grid(rows: int, columns: int) -> list[tuple[float, float, float, float]]:
        return [(column * 20.0, row * 10.0, column * 20.0 + 20, row * 10.0 + 10) for row in range(rows) for column in range(columns)]

    def test_box_index_answers_like_a_scan_of_every_box(self):
        import random

        from opendockit.extractors import pdf as pdf_module

        rng = random.Random(7)
        boxes = [(x, y, x + rng.uniform(1, 80), y + rng.uniform(1, 120))
                 for x, y in ((rng.uniform(0, 500), rng.uniform(0, 800)) for _ in range(300))]
        objs = [{"x0": x, "x1": x + 4, "top": y, "bottom": y + 9}
                for x, y in ((rng.uniform(-10, 600), rng.uniform(-10, 900)) for _ in range(3000))]

        index = pdf_module._BoxIndex(boxes)

        assert [index.holds(obj) for obj in objs] == [_pdf_model._inside_any(obj, boxes) for obj in objs]

    @pytest.mark.timing
    def test_characters_are_matched_against_many_cells_in_linear_time(self):
        import time

        from opendockit.extractors import pdf as pdf_module

        boxes = self._grid(300, 25)
        chars = [{"x0": x0 + 2, "x1": x0 + 6, "top": top + 1, "bottom": top + 9} for x0, top, _, _ in boxes for _ in range(4)]

        started = time.perf_counter()
        index = pdf_module._BoxIndex(boxes)
        inside = sum(map(index.holds, chars))

        assert time.perf_counter() - started < 2, "before the fix 7,500 cells and 30,000 characters took more than 9 seconds"
        assert inside == len(chars)

    @pytest.mark.timing
    def test_fills_are_matched_to_rows_in_linear_time(self):
        import time
        from types import SimpleNamespace


        boxes = self._grid(300, 25)
        rows = [SimpleNamespace(bbox=(0.0, row * 10.0, 500.0, row * 10.0 + 10)) for row in range(300)]
        fills = [{"x0": x0, "x1": x1, "top": top, "bottom": bottom} for x0, top, x1, bottom in boxes]

        started = time.perf_counter()
        near = _pdf_ruled_tables._fills_by_row(rows, fills)
        covers = [_pdf_ruled_tables._fill_cover(cell, near[int(cell[1] // 10)]) for cell in boxes]

        assert time.perf_counter() - started < 2, "before the fix 7,500 cells took more than 19 seconds"
        assert all(cover == 1.0 for cover in covers)
        assert all(len(rects) <= 25 * 3 for rects in near)


class TestCellsReachingFarOffThePage:
    """A 1.6KB PDF with a ruled cell reaching very far below the page used more than 13GB. The cell index (`_BoxIndex`) made height/16pt bands for
    every cell."""

    @pytest.mark.timing
    def test_box_index_of_a_very_tall_box_is_made_at_once(self):
        import time

        from opendockit.extractors import pdf as pdf_module

        boxes = [(0.0, 0.0, 100.0, 1e12), (0.0, 0.0, 100.0, 50.0), (200.0, float("nan"), 300.0, 10.0)]
        objs = [{"x0": 10, "x1": 14, "top": y, "bottom": y + 9} for y in (5.0, 70.0, 5e11)]

        started = time.perf_counter()
        index = pdf_module._BoxIndex(boxes)
        held = [index.holds(obj) for obj in objs]

        assert time.perf_counter() - started < 1, "before the fix a single cell of height 1e12 made tens of billions of bands"
        assert held == [_pdf_model._inside_any(obj, boxes) for obj in objs] == [True, True, True]

    @pytest.mark.timing
    def test_ruled_table_with_rows_far_below_the_page_converts_quickly(self):
        import time

        bottoms = [680 - 30_000_000 * row for row in range(1, 9)]
        lines = b"".join(b"72 %d m 460 %d l S\n" % (y, y) for y in [700, 680, *bottoms])
        lines += b"".join(b"%d 700 m %d %d l S\n" % (x, x, bottoms[-1]) for x in (72, 200, 330, 460))
        text = b"".join(b"BT /F1 10 Tf %d 686 Td (Head%d) Tj ET\n" % (x + 4, column) for column, x in enumerate((72, 200, 330)))
        text += b"BT /F1 10 Tf 76 600 Td (inside a tall cell) Tj ET\n"

        started = time.perf_counter()
        result = convert(_pages_pdf(lines + text))

        assert time.perf_counter() - started < 2, "before the fix the cell band index used over 3GB and 4 seconds (never finished for longer cells)"
        assert "| Head0 | Head1 | Head2 |" in result.text
        assert "| inside a tall cell |" in result.text

    @pytest.mark.timing
    def test_cells_under_many_fills_reaching_over_the_whole_table_are_measured_quickly(self):
        """Fuzzing sample (a page inflated from 1.6KB): 90,000 cells under thousands of filled rectangles covering the whole table took 78 seconds,
        adding up every rectangle for every cell."""
        import time
        from types import SimpleNamespace


        boxes = TestManyCellsOnOnePage._grid(300, 10)
        rows = [SimpleNamespace(bbox=(0.0, row * 10.0, 200.0, row * 10.0 + 10)) for row in range(300)]
        fills = [{"x0": -float(index), "x1": 200.0 + index, "top": -1.0, "bottom": 3001.0} for index in range(3000)]

        started = time.perf_counter()
        near = _pdf_ruled_tables._fills_by_row(rows, fills)
        covers = [_pdf_ruled_tables._fill_cover(cell, near[int(cell[1] // 10)]) for cell in boxes]

        assert time.perf_counter() - started < 1, "before the fix all 3,000 rectangles were added up for every cell"
        assert all(cover == 1.0 for cover in covers)

    def test_fill_cover_stops_early_but_measures_like_a_full_sum(self):
        import random


        rng = random.Random(11)
        for _ in range(500):
            x0, top = rng.uniform(0, 100), rng.uniform(0, 100)
            cell = (x0, top, x0 + rng.uniform(1, 40), top + rng.uniform(1, 40))
            fills = [{"x0": x, "x1": x + rng.uniform(1, 60), "top": y, "bottom": y + rng.uniform(1, 60)}
                     for x, y in ((rng.uniform(-20, 140), rng.uniform(-20, 140)) for _ in range(rng.randint(0, 8)))]
            x0, top, x1, bottom = cell
            whole = sum(max(0.0, min(x1, f["x1"]) - max(x0, f["x0"])) * max(0.0, min(bottom, f["bottom"]) - max(top, f["top"]))
                        for f in fills)

            cover = _pdf_ruled_tables._fill_cover(cell, fills)

            assert cover == pytest.approx(min(whole / ((x1 - x0) * (bottom - top)), 1.0))


class TestHugeGlyphNextToOrdinaryText:
    """A 1.4KB PDF with a single glyph whose font size is 1e9 never finished. Looking for pieces to join walked the integer x values one by one
    across the space width (font size × 0.35) (`_piece_joints`)."""

    @pytest.mark.timing
    def test_page_with_a_huge_glyph_converts_quickly(self):
        import time

        content = (b"BT /F1 11 Tf 72 760 Td (Ordinary line of text before.) Tj ET\n"
                   b"BT /F1 1 Tf 1 0 0 1000000000 100000000 700 Tm (Ab) Tj ET\n"
                   b"BT /F1 11 Tf 72 650 Td (Another ordinary line.) Tj ET\n")

        started = time.perf_counter()
        result = convert(_pages_pdf(content))

        assert time.perf_counter() - started < 10, "before the fix it never finished (10 seconds at font size 1e8)"
        assert "Ordinary line of text before." in result.text
        assert "Another ordinary line." in result.text
