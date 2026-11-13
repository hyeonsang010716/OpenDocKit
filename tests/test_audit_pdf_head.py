"""Audited defects in the first part of the PDF extractor: the decompression budget, pdfium failures on single pages and
quadratic scans inside a page."""

import zlib

import pytest

from opendockit import ConvertOptions, DocumentTooLargeError, convert
from tests.test_pdf import _raw_pdf


_FONT_OBJECT = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
_PAGE = (b"<< /Type /Page /Parent 2 0 R /MediaBox %s /Contents %d 0 R /Resources << /Font << /F1 5 0 R >> >> %s >>")


def _stream(body: bytes, extra: bytes = b"") -> bytes:
    return b"<< /Length %d %s >>\nstream\n" % (len(body), extra) + body + b"\nendstream"


def _flate(body: bytes) -> bytes:
    return _stream(zlib.compress(body, 9), b"/Filter /FlateDecode")


def _one_page(content: bytes, mediabox: bytes = b"[0 0 595 842]", page_extra: bytes = b"",
              objects: dict[int, bytes] | None = None) -> bytes:
    """A one-page PDF whose content stream is the object `content` (dictionary and stream included)."""
    return _raw_pdf({1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                     3: _PAGE % (mediabox, 4, page_extra), 4: content, 5: _FONT_OBJECT, **(objects or {})})


def _text(text: bytes, padding: int = 0) -> bytes:
    return b"BT /F1 12 Tf 72 700 Td (" + text + b") Tj ET\n" + b" " * padding


class TestStreamCompressedTwice:
    """Only the first Flate stage of a stream was measured, so `/Filter [/FlateDecode /FlateDecode]` passed the decompression
    limit and pdfminer then inflated every stage (a 1.1KB PDF inflated to 200MB under a 10MB limit)."""

    @staticmethod
    def _twice(body: bytes, filters: bytes = b"/Filter [/FlateDecode /FlateDecode]") -> bytes:
        return _one_page(_stream(zlib.compress(zlib.compress(body, 9), 9), filters))

    def test_second_flate_stage_that_inflates_past_the_limit_is_rejected(self):
        with pytest.raises(DocumentTooLargeError, match="becomes too large when decompressed"):
            convert(self._twice(_text(b"Visible body text", 3_000_000)), "a.pdf", ConvertOptions(max_uncompressed_bytes=1_000_000))

    def test_later_stage_is_not_inflated_beyond_the_limit_while_measuring(self, monkeypatch):
        from opendockit.extractors import pdf as pdf_module

        measured = []
        inflated_size = pdf_module._inflated_size

        def spy(data, limit):
            measured.append(inflated_size(data, limit))
            return measured[-1]

        monkeypatch.setattr(pdf_module, "_inflated_size", spy)

        with pytest.raises(DocumentTooLargeError):
            convert(self._twice(_text(b"Visible body text", 30_000_000)), "a.pdf", ConvertOptions(max_uncompressed_bytes=1_000_000))

        assert len(measured) == 2 and max(measured) <= 1_000_000 + pdf_module._INFLATE_CHUNK

    def test_stream_compressed_twice_within_the_limit_is_read_as_before(self):
        result = convert(self._twice(_text(b"Visible body text", 300_000)), "a.pdf", ConvertOptions(max_uncompressed_bytes=1_000_000))

        assert "Visible body text" in result.text

    def test_ascii_stage_between_the_two_flate_stages_is_followed(self):
        import base64

        body = base64.a85encode(zlib.compress(_text(b"Visible body text", 3_000_000), 9), adobe=True)[2:]
        data = _one_page(_stream(zlib.compress(body, 9), b"/Filter [/FlateDecode /ASCII85Decode /FlateDecode]"))

        assert "Visible body text" in convert(data, "a.pdf").text
        with pytest.raises(DocumentTooLargeError):
            convert(data, "a.pdf", ConvertOptions(max_uncompressed_bytes=1_000_000))

    def test_predictor_of_the_first_stage_is_applied_before_the_second_is_measured(self):
        """With a PNG predictor (one tag byte per row) on the first stage the second stage's input is not the inflated bytes
        themselves."""
        inner = zlib.compress(_text(b"Visible body text", 3_000_000), 9)
        rows = b"".join(b"\x00" + inner[start:start + 1] for start in range(len(inner)))
        data = _one_page(_stream(zlib.compress(rows, 9), b"/Filter [/FlateDecode /FlateDecode] "
                                                         b"/DecodeParms [<< /Predictor 10 /Columns 1 >> null]"))

        assert "Visible body text" in convert(data, "a.pdf").text
        with pytest.raises(DocumentTooLargeError):
            convert(data, "a.pdf", ConvertOptions(max_uncompressed_bytes=1_000_000))


def _scattered_pieces(count: int) -> bytes:
    """Content that places `count` short words apart from each other (more line pieces than text-box grouping allows)."""
    parts = [b"BT /F1 3 Tf\n"]
    parts.extend(b"1 0 0 1 %d %.1f Tm (w%d) Tj\n" % (20 + (index % 12) * 46, 830 - (index // 12) * 3.7, index)
                 for index in range(count))
    return b"".join(parts) + b"ET\n"


class TestStreamReadThroughTheUngroupedDocument:
    """A page with very many text pieces is read from a second pdfplumber document (text-box grouping off). Its stream objects
    are new ones, so every stream decoded there was charged a second time: a page inflating to 6.4MB used 12.8MB of the limit."""

    def test_dense_page_is_charged_once(self):
        content = _scattered_pieces(2600)
        data = _one_page(_flate(content + b" " * 600_000))
        assert len(content) < 150_000

        result = convert(data, "a.pdf", ConvertOptions(max_uncompressed_bytes=1_000_000))

        assert "w2599" in result.text
        assert any("too many text pieces" in warning for warning in result.warnings)  # the second document was used

    def test_dense_page_over_the_limit_is_still_rejected(self):
        data = _one_page(_flate(_scattered_pieces(2600) + b" " * 1_200_000))

        with pytest.raises(DocumentTooLargeError):
            convert(data, "a.pdf", ConvertOptions(max_uncompressed_bytes=1_000_000))

    def test_streams_without_an_object_number_are_each_charged(self):
        from pdfminer.pdftypes import PDFStream

        from opendockit.extractors import pdf as pdf_module

        budget = pdf_module._InflateBudget(1_000_000)
        packed = zlib.compress(b" " * 400_000)
        first, second = (PDFStream({"Filter": pdf_module.LITERALS_FLATE_DECODE[0]}, packed) for _ in range(2))

        pdf_module._charge(first, budget)
        pdf_module._charge(first, budget)
        pdf_module._charge(second, budget)

        assert budget.remaining == 200_000

    def test_same_object_reached_through_two_documents_is_charged_once(self):
        from pdfminer.pdftypes import PDFStream

        from opendockit.extractors import pdf as pdf_module

        budget = pdf_module._InflateBudget(1_000_000)
        packed = zlib.compress(b" " * 400_000)
        streams = [PDFStream({"Filter": pdf_module.LITERALS_FLATE_DECODE[0]}, packed) for _ in range(3)]
        for stream, objid in zip(streams, (4, 4, 6), strict=True):
            stream.set_objid(objid, 0)
            pdf_module._charge(stream, budget)

        assert budget.remaining == 200_000


def _two_kids(count: int, *bodies: bytes) -> bytes:
    """A PDF whose page tree lists one page per body but declares `/Count count`."""
    objects = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
               2: b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(b"%d 0 R" % (10 + index) for index in range(len(bodies))),
                                                               count),
               5: _FONT_OBJECT}
    for index, body in enumerate(bodies):
        objects[10 + index] = _PAGE % (b"[0 0 595 842]", 20 + index, b"")
        objects[20 + index] = _stream(body)
    return _raw_pdf(objects)


class TestPageOnlyPdfminerFinds:
    """With `/Count 1` over two `/Kids`, pdfminer sees two pages and pdfium one. Asking pdfium for the second page raised
    PdfiumError outside the page survey, and the whole document failed as corrupted although page 1 is readable."""

    def test_blank_page_pdfium_cannot_open_stays_an_empty_page(self):
        result = convert(_two_kids(1, _text(b"Hello world"), b""), "a.pdf")

        assert "Hello world" in result.text
        assert result.metadata["page_count"] == "2"

    def test_pages_beyond_the_time_budget_that_pdfium_cannot_open_keep_the_rest(self):
        result = convert(_two_kids(1, _text(b"Hello world"), _text(b"Second page")), "a.pdf", ConvertOptions(pdf_time_budget=0))

        assert "Hello world" in result.text
        assert any("exceeded the time budget" in warning for warning in result.warnings)

    def test_page_neither_library_reads_is_an_empty_page_with_the_failure_warning(self, monkeypatch):
        from pdfplumber.utils.exceptions import PdfminerException

        from opendockit.extractors import pdf as pdf_module

        read_page_from = pdf_module._read_page_from

        def read(source, page, placement_fit):
            if page.page_number == 2:
                raise PdfminerException(ValueError("broken font stream"))
            return read_page_from(source, page, placement_fit)

        monkeypatch.setattr(pdf_module, "_read_page_from", read)

        result = convert(_two_kids(1, _text(b"Hello world"), _text(b"Second page")), "a.pdf")

        assert "Hello world" in result.text and "Second page" not in result.text
        assert any("Structure analysis failed for 1 PDF page(s)" in warning for warning in result.warnings)

    def test_ordinary_two_page_document_is_unchanged(self):
        result = convert(_two_kids(2, _text(b"Hello world"), _text(b"Second page")), "a.pdf")

        assert "Hello world" in result.text and "Second page" in result.text and not result.warnings


def _one_row(count: int) -> bytes:
    """A page with `count` two-letter pieces on one baseline, 12pt apart (each piece is its own layout line)."""
    parts = [b"BT /F1 2 Tf\n"]
    parts.extend(b"1 0 0 1 %d 50 Tm (ab) Tj\n" % (10 + index * 12) for index in range(count))
    return _one_page(_flate(b"".join(parts) + b"ET\n"), mediabox=b"[0 0 %d 100]" % (20 + count * 12))


class _Piece:
    """Stand-in for a layout line: only the box and the text."""

    def __init__(self, x0: float, y0: float, x1: float, y1: float, text: str) -> None:
        self.x0, self.y0, self.x1, self.y1, self.text = x0, y0, x1, y1, text
        self.bbox, self.height = (x0, y0, x1, y1), y1 - y0

    def get_text(self) -> str:
        return self.text

    def __iter__(self):
        return iter(())


def _components(count: int, pairs: set[frozenset[int]]) -> set[frozenset[int]]:
    group = {index: {index} for index in range(count)}
    for pair in pairs:
        first, second = tuple(pair)
        if group[first] is not group[second]:
            group[first] |= group[second]
            for member in group[second]:
                group[member] = group[first]
    return {frozenset(members) for members in group.values()}


class TestManyPiecesOnOneBaseline:
    """Merging overlapping pieces compared every pair of pieces that overlap vertically, so one row of thousands of pieces that
    do not touch each other took the square of the piece count (6,000 pieces, an 8.5KB PDF: 28 seconds inside one page)."""

    def test_pieces_apart_on_one_row_are_not_compared_with_each_other(self, monkeypatch):
        from opendockit.extractors import pdf as pdf_module

        calls = []
        shares_baseline = pdf_module._shares_baseline
        monkeypatch.setattr(pdf_module, "_shares_baseline", lambda *args: calls.append(1) or shares_baseline(*args))

        result = convert(_one_row(2500), "a.pdf")

        assert result.text.count("ab") == 2500
        assert len(calls) < 2500  # 3,123,750 when every pair of the row was compared

    @pytest.mark.parametrize("seed", range(8))
    def test_merged_groups_are_those_of_comparing_every_pair(self, monkeypatch, seed):
        import random

        from opendockit.extractors import pdf as pdf_module

        rng = random.Random(seed)
        pieces = []
        for _ in range(300):
            x0, y0 = rng.choice([rng.uniform(0, 300), float(rng.randrange(0, 300, 20))]), float(rng.randrange(0, 60, 3))
            height = rng.choice([0.0, 2.0, 3.0, 4.0, 6.0, 40.0])
            pieces.append(_Piece(x0, y0 + rng.choice([0.0, 0.3, 1.0]), x0 + rng.choice([-1.0, 0.0, 5.0, 30.0, 400.0]),
                                 y0 + height, rng.choice(["", "ab", "cd"])))
        texts = [piece.text for piece in pieces]
        expected = {frozenset((first, second)) for first in range(300) for second in range(first)
                    if pdf_module._shares_baseline(pieces[first], pieces[second], texts[first], texts[second])}
        assert len(expected) > 20
        found = set()
        shares_baseline = pdf_module._shares_baseline

        def spy(piece, other, text, other_text):
            shared = shares_baseline(piece, other, text, other_text)
            if shared:
                found.add(frozenset((pieces.index(piece), pieces.index(other))))
            return shared

        monkeypatch.setattr(pdf_module, "_shares_baseline", spy)
        pdf_module._merged_overlapping_pieces(pieces, 0.1)

        assert found == expected
        assert _components(300, found) == _components(300, expected)

    def test_pieces_with_a_non_finite_box_are_still_compared(self, monkeypatch):
        from opendockit.extractors import pdf as pdf_module

        pieces = [_Piece(0.0, 0.0, 10.0, 4.0, "ab"), _Piece(5.0, 0.0, float("inf"), 4.0, "cd"), _Piece(50.0, 0.0, 60.0, 4.0, "ef")]

        merged, changed = pdf_module._merged_overlapping_pieces(pieces, 0.1)

        assert changed and len(merged) == 1
