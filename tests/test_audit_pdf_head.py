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
