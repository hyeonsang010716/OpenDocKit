"""Converter boundary: limits, exception wrapping, assembly, async."""

import asyncio
import base64
from dataclasses import replace
from io import BytesIO
from pathlib import Path
import sys
import zipfile

import pytest

from opendockit import (
    ConversionError,
    ConvertOptions,
    CorruptedDocumentError,
    DocumentTooLargeError,
    EncryptedDocumentError,
    IsolatedConverter,
    UnsupportedFormatError,
    aconvert,
    convert,
    convert_file,
)
from opendockit.extractors import EXTRACTORS
from opendockit.models import DocumentFormat
from tests import builder


class TestLimits:
    @pytest.mark.parametrize(
        ("field", "value"),
        [("max_file_bytes", 0), ("max_uncompressed_bytes", -1), ("max_chars", -5), ("max_pages", 0), ("pdf_time_budget", -1.0),
         ("max_sheet_cols", 0), ("max_images", -1), ("min_image_side", -1), ("repeated_image_threshold", 0),
         ("image_max_side", 0), ("pdf_render_scale", 0.0)],
    )
    def test_negative_or_meaningless_zero_limits_are_rejected_when_options_are_made(self, field, value):
        """`max_chars=-5` cut the last five characters via a Python slice and reported "exceeded", and `max_images=-1` silently ran
        with only the last picture left out."""
        with pytest.raises(ValueError, match=field):
            ConvertOptions(**{field: value})

    @pytest.mark.parametrize(("field", "value"), [("max_chars", 0), ("max_chars", None), ("max_images", 0), ("max_pages", None),
                                                  ("pdf_time_budget", 0.0), ("min_image_side", 0)])
    def test_zero_and_none_are_allowed_where_they_mean_nothing_or_no_limit(self, field, value):
        options = ConvertOptions(**{field: value})

        assert getattr(options, field) == value

    @pytest.mark.parametrize(("field", "value"), [
        ("max_sheet_cols", None), ("max_file_bytes", None), ("max_uncompressed_bytes", None), ("min_image_side", None),
        ("repeated_image_threshold", None), ("image_max_side", None), ("pdf_render_scale", None),
        ("max_chars", 2.5), ("max_sheet_cols", 2.5), ("max_chars", "100"), ("max_images", "3"), ("pdf_time_budget", "60"),
        ("max_chars", True), ("max_pages", 1.5),
    ])
    def test_limit_of_the_wrong_type_is_rejected_when_options_are_made(self, field, value):
        """`max_sheet_cols=None` blew up as a "corrupted document" during Excel conversion, and `max_file_bytes=None` /
        `max_chars=2.5` as a TypeError during conversion. None is accepted only for limits where it means "no limit"
        (max_chars, max_pages, max_images, pdf_time_budget)."""
        with pytest.raises(TypeError, match=field):
            ConvertOptions(**{field: value})

    @pytest.mark.parametrize(("field", "value"), [
        ("page_markers", "false"), ("keep_links", 0), ("extract_images", 1), ("include_footnotes", None),
        ("include_hidden_sheets", "yes"),
    ])
    def test_switch_of_the_wrong_type_is_rejected_when_options_are_made(self, field, value):
        """The switches were not checked: `page_markers="false"` is a true string and kept the markers."""
        with pytest.raises(TypeError, match=f"ConvertOptions.{field} must be True or False"):
            ConvertOptions(**{field: value})

    def test_switches_take_true_and_false(self):
        options = ConvertOptions(page_markers=False, keep_links=True)

        assert options.page_markers is False

    @pytest.mark.parametrize(("field", "value"), [
        ("pdf_render_scale", float("nan")), ("pdf_render_scale", float("inf")), ("pdf_time_budget", float("nan")),
        ("pdf_time_budget", float("inf")), ("pdf_time_budget", float("-inf")), ("max_chars", float("nan")), ("max_chars", float("inf")),
    ])
    def test_non_finite_number_is_rejected_when_options_are_made(self, field, value):
        """`pdf_render_scale=nan` passed the lower-bound check (every comparison with NaN is false) and a valid PDF was rejected as
        "corrupted". Infinity is not accepted — no time limit is None."""
        with pytest.raises((ValueError, TypeError), match=field):
            ConvertOptions(**{field: value})

    @pytest.mark.parametrize(("field", "value", "expected"), [
        ("max_chars", 5e5, 500_000), ("max_sheet_cols", 50.0, 50), ("pdf_time_budget", 30, 30), ("pdf_render_scale", 1, 1),
    ])
    def test_whole_number_float_limit_becomes_an_integer(self, field, value, expected):
        """A whole number written as a float, like `max_chars=5e5`, is accepted. Integer limits are converted to int."""
        options = ConvertOptions(**{field: value})

        assert getattr(options, field) == expected
        assert type(getattr(options, field)) is type(expected)

    def test_zero_character_limit_means_any_text_exceeds(self):
        result = convert("본문".encode(), "a.txt", ConvertOptions(max_chars=0))

        assert result.exceeded and result.text == ""

    def test_rejects_file_over_size_limit(self):
        with pytest.raises(DocumentTooLargeError):
            convert(b"a" * 11, "a.txt", ConvertOptions(max_file_bytes=10))

    def test_rejects_zip_bomb_by_uncompressed_size(self):
        """Blocked first by the uncompressed size in the central directory. Rejecting before actually decompressing protects memory."""
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("word/document.xml", "0" * 2_000_000)

        with pytest.raises(DocumentTooLargeError):
            convert(buffer.getvalue(), "bomb.docx", ConvertOptions(max_uncompressed_bytes=1_000_000))

    def test_exceeding_the_character_limit_sets_exceeded_and_cuts_the_text(self):
        """The character limit is a stop condition. Past it, exceeded is set and the text is kept only up to the limit. The character
        count is the value counted so far."""
        result = convert(("가" * 100).encode(), "a.txt", ConvertOptions(max_chars=10))

        assert result.exceeded
        assert result.text == "가" * 10
        assert result.char_count == 100
        assert any("exceeded 10 characters" in warning for warning in result.warnings)

    def test_pictures_before_the_cut_keep_their_placeholder_when_the_text_alone_is_over(self):
        """With the text alone over the limit the pictures are not decoded, yet the part that is kept is the start of the full
        conversion: the alt text stays, a picture without one is `[Image]`, a repeated logo is erased. Every placeholder
        before the cut used to vanish, the author's alt text with it, while a document over the limit only once whitespace
        was counted kept them."""
        data = builder.docx_with_pictures_before_long_text(3000, logo_count=3)
        options = ConvertOptions(max_chars=1000, repeated_image_threshold=3)

        full = convert(data, "a.docx", replace(options, max_chars=None))
        cut = convert(data, "a.docx", options)

        assert cut.text.startswith("Intro paragraph.\n\n[Image: Quarterly revenue chart]\n\n[Image]\n\nPage\n\nPage\n\nPage\n\nx x ")
        assert cut.exceeded and cut.images == [] and len(cut.text) <= 1000
        assert cut.text == full.text[:1000].rstrip()
        assert [section.text for section in cut.sections] == [full.text[:1000].rstrip()]
        assert len(full.images) == 2

    @pytest.mark.parametrize("alts", [[None, None, None], [None, "Org chart", None], ["A one", "B two", None]])
    def test_pictures_that_cannot_be_decoded_keep_their_own_placeholder_past_the_limit(self, alts):
        """A picture that cannot be decoded (here a WMF) keeps its own alt text and is never taken for a logo, as in a full
        conversion: counted by their bytes, three copies vanished as a logo and one copy took the alt text of another."""
        from docx import Document
        from docx.shared import Inches

        document = Document()
        document.add_paragraph("Intro paragraph.")
        for number in range(3):
            document.add_paragraph(f"P{number}").add_run().add_picture(BytesIO(builder.png_bytes(300, 200, seed=1)), width=Inches(1))
        for shape, alt in zip(document.inline_shapes, alts, strict=True):
            if alt:
                shape._inline.docPr.set("descr", alt)
        document.add_paragraph("x " * 400)
        buffer = BytesIO()
        document.save(buffer)
        wmf = bytes.fromhex("d7cdc69a") + b"\0" * 60  # a placeable metafile header: not decoded here
        out = BytesIO()
        with zipfile.ZipFile(BytesIO(buffer.getvalue())) as source, zipfile.ZipFile(out, "w") as target:
            for name in source.namelist():
                target.writestr(name, wmf if name.startswith("word/media/") else source.read(name))
        data = out.getvalue()
        options = ConvertOptions(max_chars=100, repeated_image_threshold=3)

        full = convert(data, "a.docx", replace(options, max_chars=None))
        cut = convert(data, "a.docx", options)

        assert full.text.count("[Image") == 3
        assert cut.exceeded and cut.text == full.text[:100].rstrip()

    def test_pictures_that_decoding_drops_leave_nothing_past_the_limit(self):
        """A divider and an icon are dropped as decoration by decoding, and the size that drops them is read from their header
        past the limit: they left `[Image]` in the cut text where the full conversion has nothing."""
        from docx import Document
        from docx.shared import Inches
        from PIL import Image

        def png(width: int, height: int) -> bytes:
            buffer = BytesIO()
            Image.new("RGB", (width, height), (200, 30, 30)).save(buffer, "PNG")
            return buffer.getvalue()

        document = Document()
        document.add_paragraph("Intro paragraph.")
        for picture, alt in ((png(800, 3), "Divider"), (png(10, 10), None), (builder.png_bytes(300, 200, seed=1), "Org chart")):
            document.add_paragraph().add_run().add_picture(BytesIO(picture), width=Inches(2))
            if alt:
                document.inline_shapes[-1]._inline.docPr.set("descr", alt)
        document.add_paragraph("x " * 400)
        buffer = BytesIO()
        document.save(buffer)
        options = ConvertOptions(max_chars=100)

        full = convert(buffer.getvalue(), "a.docx", replace(options, max_chars=None))
        cut = convert(buffer.getvalue(), "a.docx", options)

        assert full.text.startswith("Intro paragraph.\n\n[Image: Org chart]\n\nx x")
        assert cut.exceeded and cut.images == [] and cut.text == full.text[:100].rstrip()

    def test_copies_of_a_picture_of_a_rarer_format_vanish_as_a_logo_past_the_limit(self):
        """A format Pillow reads beyond the common ones (here PCX) is decoded in a full conversion, so its copies are a logo
        there. Told by the magic bytes of the common formats only, it was taken past the limit for a picture that cannot be
        decoded and each copy kept `[Image]`."""
        from docx import Document
        from docx.shared import Inches
        from PIL import Image

        pcx = BytesIO()
        Image.open(BytesIO(builder.png_bytes(300, 200, seed=1))).convert("RGB").save(pcx, "PCX")
        document = Document()
        document.add_paragraph("Intro paragraph.")
        for number in range(3):
            document.add_paragraph(f"P{number}").add_run().add_picture(BytesIO(builder.png_bytes(300, 200, seed=1)), width=Inches(1))
        document.add_paragraph("x " * 400)
        buffer = BytesIO()
        document.save(buffer)
        out = BytesIO()
        with zipfile.ZipFile(BytesIO(buffer.getvalue())) as source, zipfile.ZipFile(out, "w") as target:
            for name in source.namelist():
                target.writestr(name, pcx.getvalue() if name.startswith("word/media/") else source.read(name))
        options = ConvertOptions(max_chars=100, repeated_image_threshold=3)

        full = convert(out.getvalue(), "a.docx", replace(options, max_chars=None))
        cut = convert(out.getvalue(), "a.docx", options)

        assert "[Image" not in full.text and full.text.startswith("Intro paragraph.\n\nP0\n\nP1\n\nP2")
        assert cut.exceeded and cut.text == full.text[:100].rstrip()

    def test_document_within_the_limit_is_not_exceeded(self):
        result = convert("본문 첫 줄\n둘째 줄".encode(), "a.txt")

        assert not result.exceeded
        assert result.char_count == len(result.text) == 11

    def test_default_limit_is_half_a_million_characters_and_can_be_disabled(self):
        data = ("가" * 600_000).encode()

        limited = convert(data, "a.txt")
        unlimited = convert(data, "a.txt", ConvertOptions(max_chars=None))

        assert limited.exceeded and len(limited.text) == 500_000
        assert not unlimited.exceeded and unlimited.char_count == 600_000

    def test_rejects_empty_file(self):
        with pytest.raises(CorruptedDocumentError):
            convert(b"", "a.txt")


class TestErrorBoundary:
    def test_wraps_parser_crash_as_corrupted_document(self, monkeypatch):
        def crash(data, options):
            raise ValueError("internal parser error")

        monkeypatch.setitem(EXTRACTORS, DocumentFormat.TEXT, crash)

        with pytest.raises(CorruptedDocumentError) as error:
            convert(b"hello", "a.txt")

        assert isinstance(error.value.__cause__, ValueError)

    def test_truncated_pdf_is_reported_as_corrupted(self):
        data = builder.pdf_bytes(pages=1)[:200]

        with pytest.raises(CorruptedDocumentError):
            convert(data, "broken.pdf")

    def test_xlsx_dtd_entities_are_never_expanded(self, tmp_path):
        """openpyxl uses lxml with a parser that has entity resolution turned off (sharedStrings), and rejects a DTD in sheet XML
        as a parse failure. Confirmed that adding defusedxml changes nothing on this path (regression guard)."""
        secret = tmp_path / "secret.txt"
        secret.write_text("SECRET-4411", encoding="utf-8")
        doctype = f'<!DOCTYPE x [<!ENTITY inner "expanded"><!ENTITY outer SYSTEM "file://{secret}">]>'.encode()

        def tampered(target: str, tag: bytes) -> bytes:
            source = zipfile.ZipFile(BytesIO(builder.xlsx_bytes()))
            buffer = BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                for name in source.namelist():
                    data = source.read(name)
                    if name == target:
                        data = data.replace(b"?>", b"?>" + doctype, 1).replace(tag, tag + b"&inner;&outer;", 1)
                    archive.writestr(name, data)
            return buffer.getvalue()

        strings = convert(tampered("xl/sharedStrings.xml", b"<t>"), "a.xlsx")

        assert "expanded" not in strings.text and "SECRET" not in strings.text
        with pytest.raises(CorruptedDocumentError):
            convert(tampered("xl/worksheets/sheet1.xml", b"<v>"), "a.xlsx")

    @pytest.mark.parametrize("damage", ["encrypted", "compression"])
    def test_zip_that_cannot_be_inspected_is_a_conversion_error(self, damage):
        """Format detection runs outside the extractors, so an encrypted ZIP (RuntimeError) or an unknown compression method
        (NotImplementedError) leaked out as an exception that is not a ConversionError. Every failure must be a ConversionError
        so that callers catch just one type."""
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("mimetype", "application/hwp+zip")
        data = bytearray(buffer.getvalue())
        central = data.find(b"PK\x01\x02")
        if damage == "encrypted":
            data[6] |= 1
            data[central + 8] |= 1
        else:
            data[8] = data[central + 10] = 99

        with pytest.raises(ConversionError):
            convert(bytes(data), "a.hwpx")

    def test_unknown_zip_is_unsupported(self):
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("readme.txt", "x")

        with pytest.raises(UnsupportedFormatError):
            convert(buffer.getvalue(), "a.zip")


def _epub_bytes() -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
                         '<rootfiles><rootfile full-path="c.opf" media-type="application/oebps-package+xml"/></rootfiles></container>')
        archive.writestr("c.opf", '<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><manifest>'
                         '<item id="a" href="a.xhtml" media-type="application/xhtml+xml"/></manifest><spine><itemref idref="a"/></spine></package>')
        archive.writestr("a.xhtml", '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>본문</p></body></html>')
    return buffer.getvalue()


def _odt_bytes() -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/vnd.oasis.opendocument.text")
        archive.writestr("content.xml", '<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                         'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:text><text:p>본문</text:p>'
                         '</office:text></office:body></office:document-content>')
        archive.writestr("META-INF/manifest.xml", "<manifest/>")
    return buffer.getvalue()


class TestPasswordProtectedZip:
    """A ZIP protected with a password (`zip -e`) was rejected as a corrupted file: zipfile refuses every encrypted part, so
    detection, the libraries and the extractors all failed on parts that read as nothing. docs/options.md promises the
    password notice, as for an Office file Office itself encrypted (the OLE `EncryptedPackage`)."""

    _OFFICE = "The Office document is password-protected. Please remove the password and upload it again."

    @pytest.mark.parametrize(("make", "name", "message"), [
        (builder.docx_bytes, "a.docx", _OFFICE),
        (builder.pptx_bytes, "a.pptx", _OFFICE),
        (builder.xlsx_bytes, "a.xlsx", _OFFICE),
        (builder.hwpx_bytes, "a.hwpx", "The HWP document is password-protected. Please remove the password and upload it again."),
        (_epub_bytes, "a.epub", "The EPUB is password-protected. Please remove the password and upload it again."),
    ], ids=["docx", "pptx", "xlsx", "hwpx", "epub"])
    def test_whole_archive_protected_with_a_password_is_an_encrypted_document(self, make, name, message):
        assert convert(make(), name).text  # readable without the password bit

        with pytest.raises(EncryptedDocumentError) as raised:
            convert(builder.zip_password(make()), name)

        assert str(raised.value) == message

    @pytest.mark.parametrize(("make", "name", "part"), [
        (builder.docx_bytes, "a.docx", "word/document.xml"),
        (builder.xlsx_bytes, "a.xlsx", "xl/worksheets/sheet1.xml"),
        (builder.hwpx_bytes, "a.hwpx", "Contents/section0.xml"),
        (_odt_bytes, "a.odt", "content.xml"),
    ], ids=["docx", "xlsx", "hwpx", "odt"])
    def test_protected_body_is_an_encrypted_document(self, make, name, part):
        """The part names and `mimetype` are readable, so detection names the format; the body the extractor needs is not."""
        with pytest.raises(EncryptedDocumentError, match="password-protected"):
            convert(builder.zip_password(make(), {part}), name)

    def test_protected_slides_are_an_encrypted_document(self):
        """The extractor skips a slide it cannot read with a warning, so a deck whose slides alone were protected came back as an
        empty success."""
        data = builder.pptx_bytes()
        slides = {name for name in zipfile.ZipFile(BytesIO(data)).namelist() if name.startswith("ppt/slides/slide")}

        with pytest.raises(EncryptedDocumentError, match="The Office document is password-protected"):
            convert(builder.zip_password(data, slides), "a.pptx")

    def test_document_with_content_left_keeps_converting_with_its_protected_parts_skipped(self):
        data = builder.pptx_bytes()
        names = sorted(name for name in zipfile.ZipFile(BytesIO(data)).namelist() if name.startswith("ppt/slides/slide"))

        result = convert(builder.zip_password(data, set(names[1:])), "a.pptx")

        assert result.text.strip() and any("could not be read" in warning for warning in result.warnings)

    def test_empty_document_without_a_password_is_not_encrypted(self):
        from docx import Document

        buffer = BytesIO()
        Document().save(buffer)

        assert convert(buffer.getvalue(), "a.docx").warnings == ["No text was extracted."]

    def test_damaged_archive_without_a_password_is_still_corrupted(self):
        data = builder.zip_with(builder.docx_bytes(), {"word/document.xml": lambda xml: xml.replace("<w:body>", "<w:body><w:p", 1)})

        with pytest.raises(CorruptedDocumentError):
            convert(data, "a.docx")


class TestPlaceholdersWithoutDecoding:
    """`decode_images=False` is text without decoding a picture: every picture keeps the placeholder a conversion without
    description gives it (`converter._placeholders_only`), so the alt text an author gave a picture stays, which
    `extract_images=False` drops (a text-only integration lost `[Image: Quarterly revenue chart: Q3 up 12%]`)."""

    @staticmethod
    def _docx_with_every_kind_of_picture() -> bytes:
        """A picture with alt text, one without, three copies of a logo, a divider and a WMF with alt text."""
        from docx import Document
        from docx.shared import Inches
        from PIL import Image

        divider = BytesIO()
        Image.new("RGB", (800, 3), (90, 90, 90)).save(divider, "PNG")
        pictures = [(builder.png_bytes(300, 200, seed=1), "Quarterly revenue chart: Q3 up 12%"), (builder.png_bytes(300, 200, seed=2), None),
                    *[(builder.png_bytes(260, 90, seed=3), None)] * 3, (divider.getvalue(), None), (builder.png_bytes(300, 200, seed=4), "Org chart")]
        document = Document()
        for number, (picture, alt) in enumerate(pictures):
            document.add_paragraph(f"Paragraph {number}")
            document.add_picture(BytesIO(picture), width=Inches(2))
            if alt:
                document.inline_shapes[-1]._inline.docPr.set("descr", alt)
        buffer = BytesIO()
        document.save(buffer)
        wmf = bytes.fromhex("d7cdc69a") + b"\0" * 60  # the last picture becomes a WMF: not decoded either way
        out = BytesIO()
        with zipfile.ZipFile(BytesIO(buffer.getvalue())) as source, zipfile.ZipFile(out, "w") as target:
            last = sorted(name for name in source.namelist() if name.startswith("word/media/"))[-1]
            for name in source.namelist():
                target.writestr(name, wmf if name == last else source.read(name))
        return out.getvalue()

    def test_text_is_that_of_the_default_conversion_and_no_picture_is_decoded(self, monkeypatch: pytest.MonkeyPatch):
        from opendockit import converter

        data = self._docx_with_every_kind_of_picture()
        full = convert(data, "a.docx")

        def refuse(*args, **kwargs):
            raise AssertionError("a picture was decoded")

        monkeypatch.setattr(converter, "prepare_image", refuse)
        result = convert(data, "a.docx", ConvertOptions(decode_images=False))

        assert result.text == full.text and result.images == []
        assert "[Image: Quarterly revenue chart: Q3 up 12%]" in result.text and "[Image: Org chart]" in result.text
        assert "[Image: Quarterly revenue chart" not in convert(data, "a.docx", ConvertOptions(extract_images=False)).text

    def test_pdf_and_image_files_are_read_as_without_extraction(self):
        pdf = builder.pdf_with_images()
        picture = builder.png_bytes(300, 200, seed=5)

        assert convert(pdf, "a.pdf", ConvertOptions(decode_images=False)).text == convert(pdf, "a.pdf", ConvertOptions(extract_images=False)).text
        result = convert(picture, "a.png", ConvertOptions(decode_images=False))

        assert result.text == "" and result.images == []
        assert "image decoding (decode_images) is turned off" in result.warnings[0]

    def test_switch_takes_only_true_or_false(self):
        with pytest.raises(TypeError):
            ConvertOptions(decode_images=0)


class TestRunningOutOfMemory:
    """Running out of memory anywhere in a conversion is 'too large or too complex' under plain `convert` too. A MemoryError
    inside an extractor was gathered with the parser failures ('Could not parse the … document. The file may be corrupted.'),
    while `IsolatedConverter` reported the same file as too large; one after extraction left `convert` as a MemoryError."""

    @staticmethod
    def _too_complex():
        from opendockit import TOO_COMPLEX_MESSAGE

        return pytest.raises(DocumentTooLargeError, match=f"^{TOO_COMPLEX_MESSAGE}$")

    def test_inside_an_extractor(self, monkeypatch):
        def out_of_memory(data, options):
            raise MemoryError

        monkeypatch.setitem(EXTRACTORS, DocumentFormat.TEXT, out_of_memory)

        with self._too_complex() as error:
            convert(b"hello", "a.txt")
        assert isinstance(error.value.__cause__, MemoryError)

    def test_wrapped_by_a_parser_inside_an_extractor(self, monkeypatch):
        def out_of_memory(data, options):
            try:
                raise MemoryError
            except MemoryError as inner:
                raise ValueError("parser") from inner

        monkeypatch.setitem(EXTRACTORS, DocumentFormat.TEXT, out_of_memory)

        with self._too_complex():
            convert(b"hello", "a.txt")

    def test_reported_by_an_extractor_as_another_failure(self, monkeypatch):
        def out_of_memory(data, options):
            try:
                raise MemoryError
            except MemoryError as inner:
                raise CorruptedDocumentError("Could not parse the TXT document. The file may be corrupted.") from inner

        monkeypatch.setitem(EXTRACTORS, DocumentFormat.TEXT, out_of_memory)

        with self._too_complex():
            convert(b"hello", "a.txt")

    def test_while_detecting_the_format(self, monkeypatch):
        import opendockit.converter as converter_module

        def out_of_memory(data, filename):
            raise MemoryError

        monkeypatch.setattr(converter_module, "detect_format", out_of_memory)

        with self._too_complex():
            convert(b"hello", "a.txt")

    def test_after_extraction(self, monkeypatch):
        import opendockit.converter as converter_module

        def out_of_memory(result):
            raise MemoryError

        monkeypatch.setattr(converter_module, "render", out_of_memory)

        with self._too_complex():
            convert(b"hello", "a.txt")

    def test_while_copying_the_content(self):
        """`bytes(data)` of a bytearray or memoryview is the first allocation of a conversion, and it was made before
        anything watched for a MemoryError."""
        class NoMemoryForACopy(bytearray):
            def __bytes__(self):
                raise MemoryError

        with self._too_complex() as error:
            convert(NoMemoryForACopy(b"hello"), "a.txt")
        assert isinstance(error.value.__cause__, MemoryError)
        with self._too_complex():
            asyncio.run(aconvert(NoMemoryForACopy(b"hello"), "a.txt"))

        async def isolated():
            async with IsolatedConverter() as converter:
                await converter.convert(NoMemoryForACopy(b"hello"), "a.txt")

        with self._too_complex() as error:
            asyncio.run(isolated())
        assert isinstance(error.value.__cause__, MemoryError)

    @pytest.mark.parametrize("stage", ["_content_bytes", "_check_arguments", "check_size"])
    def test_in_the_checks_before_the_conversion(self, monkeypatch, stage):
        import opendockit.converter as converter_module

        def out_of_memory(*_arguments):
            raise MemoryError

        monkeypatch.setattr(converter_module, stage, out_of_memory)

        with self._too_complex():
            convert(b"hello", "a.txt")

    def test_wrong_arguments_and_sizes_are_still_what_they_were(self):
        with pytest.raises(TypeError, match="convert takes the file content as bytes"):
            convert("a.txt")
        with pytest.raises(TypeError, match="The second argument is the file name"):
            convert(b"hello", ConvertOptions())
        with pytest.raises(TypeError, match="options must be a ConvertOptions"):
            convert(b"hello", "a.txt", options={"max_chars": 1})
        with pytest.raises(ValueError, match="max_chars"):
            convert(b"hello", "a.txt", ConvertOptions(max_chars=-1))
        with pytest.raises(CorruptedDocumentError, match=r"^The file is empty\.$"):
            convert(b"", "a.txt")
        with pytest.raises(DocumentTooLargeError, match=r"^The file is too large \(maximum 3 bytes\)\.$"):
            convert(b"hello", "a.txt", ConvertOptions(max_file_bytes=3))

    def test_memory_error_wrapped_by_the_pdf_page_reader(self, monkeypatch):
        """pdfplumber wraps whatever pdfminer raises while it reads a page, a MemoryError included: the page was read again
        with the simple extraction and the result only said that its structure analysis had failed."""
        from pdfminer import layout

        analyze = layout.LTLayoutContainer.analyze
        calls = []

        def out_of_memory_on_the_second_page(self, laparams):
            calls.append(1)
            if len(calls) == 2:
                raise MemoryError
            return analyze(self, laparams)

        data = builder.pdf_bytes(pages=3)
        assert "Structure analysis failed" not in " ".join(convert(data, "a.pdf").warnings)
        calls.clear()
        monkeypatch.setattr(layout.LTLayoutContainer, "analyze", out_of_memory_on_the_second_page)

        with self._too_complex():
            convert(data, "a.pdf")

    def test_page_the_pdf_reader_cannot_read_is_still_read_simply(self, monkeypatch):
        from pdfminer import layout

        analyze = layout.LTLayoutContainer.analyze
        calls = []

        def broken_second_page(self, laparams):
            calls.append(1)
            if len(calls) == 2:
                raise ValueError("broken font stream")
            return analyze(self, laparams)

        monkeypatch.setattr(layout.LTLayoutContainer, "analyze", broken_second_page)

        result = convert(builder.pdf_bytes(pages=3), "a.pdf")

        assert [warning for warning in result.warnings if warning.startswith("Structure analysis failed for 1 PDF page(s)")]

    def test_another_failure_is_still_what_it_was(self, monkeypatch):
        def too_large(data, options):
            raise DocumentTooLargeError("The file is too large when decompressed (maximum 500MB).")

        monkeypatch.setitem(EXTRACTORS, DocumentFormat.TEXT, too_large)

        with pytest.raises(DocumentTooLargeError, match="when decompressed"):
            convert(b"hello", "a.txt")


class TestAssembly:
    def test_warns_when_extension_disagrees_with_content(self):
        result = convert(b"<html><body><table><tr><td>a</td><td>b</td></tr></table></body></html>", "list.xls")

        assert result.warnings[0].startswith("The file extension (.xls)")

    def test_warns_when_nothing_extracted(self):
        result = convert(b"   \n\n", "blank.txt")

        assert result.is_empty
        assert "No text was extracted." in result.warnings

    def test_convert_file_uses_file_name_for_detection(self, tmp_path):
        path = tmp_path / "data.csv"
        path.write_text("a,b\n1,2\n", encoding="utf-8")

        result = convert_file(path)

        assert result.format is DocumentFormat.CSV

    def test_sheet_and_slide_titles_are_normalized_to_nfc(self):
        """Section headings (sheet/slide titles) did not go through cleanup (clean_text), so a decomposed-jamo (NFD) title left the
        result not in NFC."""
        import unicodedata

        import openpyxl

        workbook = openpyxl.Workbook()
        workbook.active.title = unicodedata.normalize("NFD", "매출")
        workbook.active.append(["a", 1])
        buffer = BytesIO()
        workbook.save(buffer)

        result = convert(buffer.getvalue(), "a.xlsx")

        assert result.text.startswith("## Sheet: 매출\n")
        assert unicodedata.is_normalized("NFC", result.text)

    @pytest.mark.parametrize("title", ["~~~ 감사합니다 ~~~", "```", "- > ~~~ 끝"])
    def test_title_or_alt_text_that_starts_like_a_fence_gets_no_backslash(self, title):
        """A sheet name, a slide title and the alt text of a picture are written inside a line (`## Sheet: …`, `[Image: …]`),
        where no fence can open. Cleaned as a text of their own they got the backslash of a line that opens one."""
        import base64

        import openpyxl
        from pptx import Presentation

        workbook = openpyxl.Workbook()
        workbook.active.title = title
        workbook.active.append(["a", 1])
        sheet = BytesIO()
        workbook.save(sheet)
        presentation = Presentation()
        presentation.slides.add_slide(presentation.slide_layouts[5]).shapes.title.text = title
        slides = BytesIO()
        presentation.save(slides)
        picture = base64.b64encode(builder.png_bytes()).decode()
        html = f'<p>a</p><img src="data:image/png;base64,{picture}" alt="{title}">'

        assert convert(sheet.getvalue(), "a.xlsx").text.startswith(f"## Sheet: {title}\n")
        assert convert(slides.getvalue(), "a.pptx").text == f"## Slide 1: {title}"
        assert convert(html.encode(), "a.html").text == f"a\n\n[Image: {title}]"

    def test_text_that_looks_like_an_image_placeholder_is_kept_as_text(self):
        """A literal `⟦IMG:1⟧` typed in the body was taken for a placeholder and removed. If a real picture #1 existed, that picture
        was inserted twice."""
        from docx import Document
        from docx.shared import Inches

        document = Document()
        document.add_paragraph("자리 표시 모양 ⟦IMG:1⟧ 은 글자다")
        document.add_paragraph("그림 앞").add_run().add_picture(BytesIO(builder.png_bytes(seed=4)), width=Inches(1))
        buffer = BytesIO()
        document.save(buffer)

        result = convert(buffer.getvalue(), "a.docx")

        assert "자리 표시 모양 ⟦IMG:1⟧ 은 글자다" in result.text
        assert result.text.count("[Image]") == 1

    def test_alt_text_is_escaped_in_table_cells_and_normalized(self):
        """Picture alt text went in without cleanup or escaping, so `|` and line breaks inside a table cell broke the cells and rows,
        and NFD remained."""
        import unicodedata

        from docx import Document
        from docx.shared import Inches

        document = Document()
        table = document.add_table(rows=1, cols=2)
        table.cell(0, 0).text = "사진"
        table.cell(0, 1).paragraphs[0].add_run().add_picture(BytesIO(builder.png_bytes(seed=5)), width=Inches(1))
        document.inline_shapes[0]._inline.docPr.set("descr", unicodedata.normalize("NFD", "가|나\n다"))
        buffer = BytesIO()
        document.save(buffer)

        result = convert(buffer.getvalue(), "a.docx")

        assert "| 사진 | [Image: 가\\|나 다] |" in result.text
        assert unicodedata.is_normalized("NFC", result.text)

    def test_picture_in_an_html_table_cell_keeps_its_text(self):
        """A table with merged cells is output as an HTML table. A picture description in such a cell is left as is, without
        turning `|` into `\\|` as in a Markdown cell; only table tags are neutralized (same rule as cell text). Line breaks are
        folded onto one line with `<br>`."""
        html = ("<table><tr><th>항목</th><th>값</th><th>비고</th></tr>"
                f"<tr><td colspan='2'>합계 <img src='{_data_uri(seed=4)}'></td><td>끝</td></tr></table>")

        result = asyncio.run(aconvert(html.encode(), "a.html", describer=_FixedDescriber("A<B | C & D</td>\n둘째 줄")))

        assert '<tr><td colspan="2">합계 [Image] A<B | C & D&lt;/td><br>둘째 줄</td><td>끝</td></tr>' in result.text

    def test_picture_block_inside_a_paragraph_is_followed_by_a_blank_line(self):
        """After a picture inside a paragraph was pulled out as a block, the next line was attached directly, so Markdown lazy
        continuation put the body text into the quote block."""
        html = f"<html><body><p>첫 줄 <img src='{_data_uri(seed=3)}'><br>둘째 줄은 본문이다</p></body></html>"

        result = asyncio.run(aconvert(html.encode(), "a.html", describer=_FixedDescriber("막대 차트")))

        assert result.text == "첫 줄\n\n> [Image] 막대 차트\n\n둘째 줄은 본문이다"

    def test_picture_in_nested_list_item_keeps_the_item_indentation(self):
        """The line left after removing the picture placeholder was stripped, so a nested item rose to the top level, and the spaces
        on both sides of the placeholder remained as two spaces."""
        html = (f"<html><body><ul><li>상위<ul><li>하위 A <img src='{_data_uri(seed=3)}' alt='매출 차트'> 끝</li>"
                "<li>하위 B</li></ul></li><li>상위 2</li></ul></body></html>")

        result = asyncio.run(aconvert(html.encode(), "a.html", describer=_FixedDescriber("막대 차트")))

        assert result.text == "- 상위\n  - 하위 A 끝\n\n> [Image] 막대 차트\n\n  - 하위 B\n- 상위 2"

    def test_aconvert_runs_off_the_event_loop(self):
        result = asyncio.run(aconvert("본문".encode(), "a.txt"))

        assert result.text == "본문"


class TestLimitIsExact:
    """At limit L, exceeded must be exactly "actual character count N > L". The checks that stop midway through extraction or
    description use an estimate, and if that estimate is larger than the real value a document within the limit is blocked as
    exceeded (in 4 real files a document with N = L came out as exceeded)."""

    @pytest.mark.parametrize(
        "make",
        [builder.docx_bytes, builder.pptx_bytes, builder.xlsx_bytes, builder.hwpx_bytes, builder.docx_with_images,
         builder.pptx_with_image, builder.xlsx_with_image, builder.hwpx_with_images, builder.pdf_with_images,
         lambda: builder.pdf_bytes(pages=3, two_columns=True), lambda: _pptx_with_icon()],
    )
    def test_exceeded_matches_the_real_length_at_the_boundary(self, make):
        data = make()
        length = convert(data, options=ConvertOptions(max_chars=None)).char_count

        results = {limit: convert(data, options=ConvertOptions(max_chars=limit)) for limit in (length - 1, length, length + 1)}

        assert [result.exceeded for result in results.values()] == [True, False, False]
        assert results[length].char_count == length
        assert results[length - 1].char_count > length - 1

    def test_decomposed_hangul_is_not_counted_three_times(self):
        """Decomposed-jamo (NFD) Hangul shrinks to 1/3 of the character count once normalized. Counting the pre-normalization length
        makes a document within the limit exceed it."""
        import unicodedata

        import openpyxl

        workbook = openpyxl.Workbook()
        for number in range(200):
            workbook.active.append([unicodedata.normalize("NFD", "한글로만쓴칸입니다"), number])
        buffer = BytesIO()
        workbook.save(buffer)
        length = convert(buffer.getvalue(), "a.xlsx", ConvertOptions(max_chars=None)).char_count

        result = convert(buffer.getvalue(), "a.xlsx", ConvertOptions(max_chars=length))

        assert not result.exceeded


def _pptx_with_icon() -> bytes:
    """Slide with small pictures that get filtered out. Their placeholders vanish from the text, so they must not be counted."""
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "구성도"
    for seed in range(3):
        slide.shapes.add_picture(BytesIO(builder.png_bytes(16, 16, seed=seed)), Inches(1 + seed), Inches(2))
    buffer = BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


class _FixedDescriber:
    """Stand-in describer that gives the same answer for every picture (no real vision model is called)."""

    def __init__(self, answer: str) -> None:
        self.answer = answer

    async def describe(self, image):
        return self.answer


def _data_uri(*, seed: int) -> str:
    import base64

    return "data:image/png;base64," + base64.b64encode(builder.png_bytes(200, 120, seed=seed)).decode()


class TestAuditedAssemblyGaps:
    @staticmethod
    def _docx_with_paragraph_picture(paragraph_text: str) -> bytes:
        from docx import Document
        from docx.shared import Inches

        document = Document()
        paragraph = document.add_paragraph(paragraph_text)
        paragraph.add_run().add_picture(BytesIO(builder.png_bytes(seed=5)), width=Inches(1))
        buffer = BytesIO()
        document.save(buffer)
        return buffer.getvalue()

    @pytest.mark.parametrize("paragraph_text", ["| 사용법 | ", "<tr> 태그 설명 "], ids=["pipe", "tr"])
    def test_paragraph_that_starts_like_a_table_row_gets_a_picture_block(self, paragraph_text):
        """A paragraph starting with `|` or `<tr>` was taken for a table row and the picture description was folded onto one line and
        appended. A table row ends with `|` or `</tr>`."""
        result = asyncio.run(aconvert(self._docx_with_paragraph_picture(paragraph_text), "a.docx",
                                      describer=_FixedDescriber("첫 줄\n둘째 줄 | 파이프")))

        assert result.text == f"{paragraph_text.strip()}\n\n> [Image] 첫 줄\n> 둘째 줄 | 파이프"

    def test_stray_code_fence_in_a_docx_does_not_swallow_the_rest(self):
        from docx import Document

        document = Document()
        document.add_paragraph("```python")
        document.add_heading("진짜 제목", 1)
        document.add_paragraph("항목    사이   공백")
        buffer = BytesIO()
        document.save(buffer)

        result = convert(buffer.getvalue(), "a.docx")

        assert result.text == "\\```python\n\n# 진짜 제목\n\n항목 사이 공백"

    def test_sections_are_cut_where_the_text_is_cut(self):
        """Past the limit only `text` was cut while `sections` kept everything, so consumers of sections (chunking, source
        attribution) received content beyond the limit."""
        result = convert(("가" * 100).encode(), "a.txt", ConvertOptions(max_chars=10))

        assert [section.text for section in result.sections] == [result.text] == ["가" * 10]

    def test_sections_of_a_paged_document_are_cut_at_the_same_point(self):
        data = builder.pdf_bytes(pages=3)
        full = convert(data, "a.pdf", ConvertOptions(max_chars=None))
        limit = len(full.text) // 2

        result = convert(data, "a.pdf", ConvertOptions(max_chars=limit))

        assert result.exceeded
        kept = [section for section in result.sections if section.text]
        assert kept and result.text.endswith(kept[-1].text)
        assert all(original.text.startswith(section.text)
                   for section, original in zip(result.sections, full.sections, strict=False))
        assert sum(len(section.text) for section in result.sections) <= len(result.text)

    def test_sections_are_cut_when_descriptions_cross_the_limit(self):
        data = builder.docx_with_images()
        result = asyncio.run(aconvert(data, "a.docx", ConvertOptions(max_chars=30), describer=_FixedDescriber("가" * 100)))

        assert result.exceeded
        assert [section.text for section in result.sections] == [result.text]


class TestPicturesInsideAQuote:
    """A line inside a quote (`> `) holding a picture was looked at with its quote marks: a row of a quoted table was not a table
    row, so the picture was cut out of its cell and its block put on lines of its own outside the quote — the table and the
    quote broke in two — and in a quoted paragraph the block landed outside the quote and left a stray `>` behind it."""

    _TABLE = ("<p>Reply</p><blockquote><table><tr><th>Name</th><th>Photo</th></tr>"
              "<tr><td>Kim</td><td><img src='{uri}' alt='Kim portrait'></td></tr>"
              "<tr><td>Lee</td><td>none</td></tr></table></blockquote>")

    def test_picture_in_a_quoted_table_stays_in_its_cell(self):
        html = self._TABLE.format(uri=_data_uri(seed=1))

        assert convert(html.encode(), "a.html").text == (
            "Reply\n\n> | Name | Photo |\n> | --- | --- |\n> | Kim | [Image: Kim portrait] |\n> | Lee | none |")

    def test_description_in_a_quoted_table_stays_in_its_cell(self):
        html = self._TABLE.format(uri=_data_uri(seed=1))

        result = asyncio.run(aconvert(html.encode(), "a.html", describer=_FixedDescriber("A man smiling\nwearing glasses")))

        assert result.text == ("Reply\n\n> | Name | Photo |\n> | --- | --- |\n"
                               "> | Kim | [Image] A man smiling<br>wearing glasses |\n> | Lee | none |")

    def test_picture_in_a_quoted_markdown_table_stays_in_its_cell(self):
        markdown = (f"Intro\n\n> | Name | Photo |\n> | --- | --- |\n> | Kim | ![Kim portrait]({_data_uri(seed=1)}) |\n"
                    "> | Lee | none |\n")

        assert convert(markdown.encode(), "a.md").text == (
            "Intro\n\n> | Name | Photo |\n> | --- | --- |\n> | Kim | [Image: Kim portrait] |\n> | Lee | none |")

    def test_picture_in_a_quoted_html_table_stays_in_its_cell(self):
        html = ("<blockquote><table><tr><th>a</th><th>b</th><th>c</th></tr>"
                f"<tr><td colspan=2>sum <img src='{_data_uri(seed=4)}'></td><td>end</td></tr></table></blockquote>")

        result = asyncio.run(aconvert(html.encode(), "a.html", describer=_FixedDescriber("x\ny")))

        assert '> <tr><td colspan="2">sum [Image] x<br>y</td><td>end</td></tr>' in result.text

    def test_picture_in_a_quoted_paragraph_stays_in_the_quote(self):
        html = (f"<p>My reply.</p><blockquote><p>Original line one <img src='{_data_uri(seed=2)}' alt='signed contract scan'>"
                " continues.</p><p>Original line two.</p></blockquote><p>Thanks</p>")

        assert convert(html.encode(), "a.html").text == (
            "My reply.\n\n> Original line one continues.\n>\n> [Image: signed contract scan]\n>\n> Original line two.\n\nThanks")

    def test_description_in_a_nested_quote_gets_every_quote_mark(self):
        html = (f"<blockquote><p>Outer</p><blockquote><p>Inner <img src='{_data_uri(seed=3)}'> text</p><p>Inner two</p>"
                "</blockquote><p>Outer two</p></blockquote>")

        result = asyncio.run(aconvert(html.encode(), "a.html", describer=_FixedDescriber("A chart\n\nwith two lines")))

        assert result.text == ("> Outer\n>\n> > Inner text\n> >\n> > > [Image] A chart\n> > >\n> > > with two lines\n> >\n"
                               "> > Inner two\n>\n> Outer two")

    def test_description_at_the_end_of_a_quote_ends_with_it(self):
        html = f"<blockquote><p>One <img src='{_data_uri(seed=3)}'></p></blockquote><p>After</p>"

        result = asyncio.run(aconvert(html.encode(), "a.html", describer=_FixedDescriber("A chart")))

        assert result.text == "> One\n>\n> > [Image] A chart\n\nAfter"

    @pytest.mark.parametrize(("html", "expected"), [
        ("<blockquote><p>One</p><p><img src='{uri}'></p><p>Two.</p></blockquote>", "> One\n>\n> Two."),
        ("<blockquote><p>One</p><p><img src='{uri}'></p></blockquote><p>After</p>", "> One\n\nAfter"),
        ("<blockquote><p><img src='{uri}'></p><p>Two.</p></blockquote>", "> Two."),
        ("<blockquote><p>One <img src='{uri}'> two</p></blockquote>", "> One two"),
    ], ids=["between-paragraphs", "at-the-end", "at-the-start", "inside-a-line"])
    def test_skip_answer_in_a_quote_leaves_no_stray_quote_line(self, html, expected):
        result = asyncio.run(aconvert(html.format(uri=_data_uri(seed=5)).encode(), "a.html", describer=_FixedDescriber("SKIP")))

        assert result.text == expected

    @pytest.mark.parametrize("limit", range(620, 1_740, 40))
    def test_description_stops_at_exactly_the_image_that_crosses_the_limit(self, limit):
        """The stop during description skips assembling the text and counts the most a picture can add
        (`image_length_bound`). Inside a quote every line of its block also gets the quote marks; counted without them the bound
        was short and the description ran past the picture that crossed the limit."""
        from opendockit.converter import render
        from opendockit.describe import describe_images

        def quoted(inner: str) -> str:
            return "<blockquote>" * 8 + inner + "</blockquote>" * 8

        parts = []
        for index in range(12):
            picture = f"<img src='{_data_uri(seed=index)}'>"
            parts.append(quoted([f"<p>before {index}</p><p>{picture}</p><p>after {index}</p>",
                                 f"<p>line {index} {picture} tail</p>", f"<p>a {index}<br>{picture}<br>b {index}</p>"][index % 3]))
        html = ("<html><body>" + "".join(parts) + "</body></html>").encode()
        answers = {image_id: "\n".join(["가" * (5 + image_id)] * (1 + image_id % 4)) for image_id in range(1, 13)}

        class Answers:
            async def describe(self, image):
                await asyncio.sleep(0)
                return answers[image.id]

        base = convert(html, "a.html", ConvertOptions(max_chars=limit))
        crossing = [count for count in range(1, 13) if render(replace(base, images=[
            replace(image, description=answers[image.id]) if index < count else replace(image, decorative=True)
            for index, image in enumerate(base.images)])).exceeded]
        assert crossing, "every limit swept lies between the text alone and the text with every description"

        result = asyncio.run(describe_images(base, Answers(), max_concurrency=1))

        assert result.exceeded
        assert sum(1 for image in result.images if image.description) == crossing[0]

    @pytest.mark.parametrize(("source", "name"), [
        ("> wordwordwo\n" + "> ![]({tiny})\n" * 5, "a.md"),
        ("<blockquote><p>wordwordwo</p>" + "<p><img src='{tiny}'></p>" * 5 + "</blockquote>", "a.html"),
        ("<blockquote><p>wordwordwo</p>" + "<blockquote><p><img src='{tiny}'></p></blockquote>" * 5 + "</blockquote>", "a.html"),
    ], ids=["markdown", "html", "nested-html"])
    def test_erased_pictures_in_a_quote_do_not_count_toward_the_limit(self, source, name):
        """The estimate before rendering counted the quote marks of a quoted line holding only a picture, and the empty quote
        lines around it, but rendering drops those lines when the picture is erased (too small to carry information): a
        12-character quote was reported over a limit of 12 to 15 (HTML: up to 20) and came back with `char_count` 16."""
        tiny = "data:image/png;base64," + base64.b64encode(builder.png_bytes(10, 10)).decode()
        data = source.format(tiny=tiny).encode()

        full = convert(data, name, ConvertOptions(max_chars=None))
        assert full.text == "> wordwordwo"
        for limit in range(len(full.text) - 2, len(full.text) + 10):
            result = convert(data, name, ConvertOptions(max_chars=limit))
            assert result.exceeded == (len(full.text) > limit), limit
            assert result.char_count == len(full.text), limit

    def test_erased_quoted_pictures_leave_the_real_picture_of_a_document_within_the_limit(self):
        """Counted over the limit, the document skipped decoding: the one real picture beside 20 erased quoted ones was not
        returned for description at a limit equal to the full length."""
        tiny = "data:image/png;base64," + base64.b64encode(builder.png_bytes(10, 10)).decode()
        data = (f"![sales]({_data_uri(seed=1)})\n\n> wordwordwo\n" + f"> ![]({tiny})\n" * 20).encode()

        full = convert(data, "a.md", ConvertOptions(max_chars=None))
        result = convert(data, "a.md", ConvertOptions(max_chars=len(full.text)))

        assert result.text == full.text == "[Image: sales]\n\n> wordwordwo"
        assert not result.exceeded
        assert len(result.images) == 1

    @pytest.mark.parametrize("share", [4, 2])
    def test_a_picture_after_a_quoted_one_counts_the_quote_marks_of_its_neighbour(self, share):
        """Whether the empty quote line after a quoted picture is kept depends on the line after it. When that line holds a
        picture outside the quote (a lazy continuation in Markdown), describing it brought back the empty quote line that a
        SKIP answer for the quoted one had dropped, which its own bound (no quote marks) did not count: with 30-deep quotes
        all 40 pictures were described where rendering every time stops at the twelfth."""
        from opendockit.converter import render
        from opendockit.describe import describe_images

        deep = "> " * 30
        source = "".join(f"{deep}x{index}\n{deep}![]({_data_uri(seed=index)})\n![]({_data_uri(seed=index + 1)})\n\n"
                         for index in range(0, 40, 2)).encode()
        answers = {image_id: "SKIP" if image_id % 2 else "d" for image_id in range(1, 41)}

        def described(document, count):
            return replace(document, images=[
                replace(image, description=answers[image.id]) if index < count and answers[image.id] != "SKIP"
                else replace(image, decorative=True) for index, image in enumerate(document.images)])

        full = convert(source, "a.md")
        floor, ceiling = render(described(full, 0)).char_count, render(described(full, 40)).char_count
        base = convert(source, "a.md", ConvertOptions(max_chars=floor + (ceiling - floor) // share))
        crossing = [count for count in range(1, 41) if render(described(base, count)).exceeded]

        class Answers:
            async def describe(self, image):
                await asyncio.sleep(0)
                return answers[image.id]

        result = asyncio.run(describe_images(base, Answers(), max_concurrency=1))

        answered = [index + 1 for index, image in enumerate(result.images) if image.description or image.decorative]
        assert result.exceeded
        assert max(answered) == crossing[0]


class TestInputTypes:
    """`convert` takes the file content (bytes). A memoryview failed in format detection (`startswith`) and a path string failed
    detection too, both rejected as "could not parse the file format (corrupted)", which hid the cause."""

    @pytest.mark.parametrize("wrap", [bytes, bytearray, memoryview], ids=["bytes", "bytearray", "memoryview"])
    def test_accepts_bytes_like_content(self, wrap):
        data = wrap("메모 한 줄".encode())

        result = convert(data, "a.txt")

        assert result.text == "메모 한 줄"

    def test_aconvert_accepts_memoryview(self):
        result = asyncio.run(aconvert(memoryview("메모 한 줄".encode()), "a.txt"))

        assert result.text == "메모 한 줄"

    @pytest.mark.parametrize("value", ["a.txt", Path("a.txt"), BytesIO(b"abc"), None], ids=["str", "path", "bytesio", "none"])
    def test_other_types_are_a_type_error_pointing_at_convert_file(self, value):
        with pytest.raises(TypeError, match="convert_file"):
            convert(value, "a.txt")


class WritingPipe:
    """A named pipe whose writer keeps sending `total` bytes until the reader closes it; `written` is what got through."""

    def __init__(self, path: Path, total: int, content: bytes = b"a") -> None:
        import os
        import threading

        os.mkfifo(path)
        self.path, self.written = path, 0
        self._thread = threading.Thread(target=self._write, args=(total, content), daemon=True)
        self._thread.start()

    def _write(self, total: int, content: bytes) -> None:
        chunk = (content * (1024 * 1024 // len(content) + 1))[:1024 * 1024] if total > len(content) else content
        try:
            with open(self.path, "wb") as stream:
                while self.written < total:
                    piece = chunk[:total - self.written]
                    stream.write(piece)
                    self.written += len(piece)
        except BrokenPipeError:   # the reader stopped reading and closed its end
            pass

    def finish(self) -> int:
        self._thread.join(timeout=10)
        return self.written


class TestHugeLimits:
    """A size limit of sys.maxsize or more is the natural way to say "no limit" (None is refused). zlib and zipfile take a
    length as a C ssize_t, so one past such a limit raised OverflowError: every compressed HWP and every ODT was reported
    corrupted, and a compressed XLS metafile was dropped."""

    @pytest.mark.parametrize("limit", [sys.maxsize, 10**20])
    def test_compressed_hwp_odt_and_xls_picture_convert_as_with_the_default_limit(self, monkeypatch, limit):
        from tests.test_hwp import _fake_hwp, _sample_section
        from tests.test_odf import _package

        metafile = builder.xls_picture_store(builder.xls_emf_blip(b"\x01\x00\x00\x00" + bytes(200)))
        samples = {
            "a.hwp": _fake_hwp(monkeypatch, flags=1, sections=[_sample_section()]),
            "a.odt": _package("text", "<text:p>본문</text:p>"),
            "a.xls": builder.xls_bytes([("S", [["가", "나"], ["1", "2"]], builder.xls_picture(1))], metafile),
        }
        for name, data in samples.items():
            expected = convert(data, name)

            result = convert(data, name, ConvertOptions(max_uncompressed_bytes=limit))

            assert (result.text, len(result.images)) == (expected.text, len(expected.images)), name
            assert expected.text


@pytest.mark.skipif(sys.platform == "win32", reason="named pipes are POSIX")
class TestReadingWithinTheSizeLimit:
    """`convert_file` refused a file over `max_file_bytes` from the size the file system reports, which is 0 for a pipe or a
    device: such a file was read to its end first (`opendockit /dev/zero` grew without bound). It is now read only up to the
    limit."""

    def test_pipe_is_read_only_up_to_the_size_limit(self, tmp_path):
        pipe = WritingPipe(tmp_path / "upload.txt", total=64 * 1024 * 1024)

        with pytest.raises(DocumentTooLargeError):
            convert_file(pipe.path, ConvertOptions(max_file_bytes=1024))

        assert pipe.finish() < 8 * 1024 * 1024, "the writer was cut off soon after the limit, not read to its end"

    def test_pipe_within_the_limit_is_converted(self, tmp_path):
        """Reading a pipe still works (`opendockit /dev/stdin`): only how much is read is bounded."""
        pipe = WritingPipe(tmp_path / "note.txt", total=len("hello pipe"), content=b"hello pipe")

        result = convert_file(pipe.path)

        assert result.text == "hello pipe"
        assert pipe.finish() == len("hello pipe")

    def test_regular_file_is_read_whole_and_over_the_limit_is_refused(self, tmp_path):
        path = tmp_path / "note.txt"
        path.write_bytes(b"x" * 2048)

        assert convert_file(path, ConvertOptions(max_file_bytes=2048)).text == "x" * 2048
        with pytest.raises(DocumentTooLargeError):
            convert_file(path, ConvertOptions(max_file_bytes=2047))

    def test_reading_stops_one_byte_past_the_limit(self):
        from opendockit.converter import read_limited

        class Endless:
            def __init__(self):
                self.handed = 0

            def read(self, size):
                self.handed += size
                return b"z" * size

        stream = Endless()
        data = read_limited(stream, 5 * 1024 * 1024)

        assert len(data) == 5 * 1024 * 1024 + 1 and stream.handed == len(data)
        assert read_limited(BytesIO(b"abc"), 10) == b"abc"
        assert read_limited(BytesIO(b"abc"), 10, size_hint=3) == b"abc"
        assert read_limited(BytesIO(b"abcdef"), 3, size_hint=2) == b"abcd"


class TestTimeLimitedResults:
    """`ConversionResult.time_limited` (internal) says that a wall-clock limit changed what was read, so a caller that keeps
    results (the MCP server) does not keep this one: the same file can read fully on a less busy machine."""

    def test_ordinary_conversion_is_not_time_limited(self):
        assert not convert(builder.pdf_bytes(pages=2), "a.pdf").time_limited
        assert not convert(builder.docx_bytes(), "a.docx").time_limited

    def test_pages_sent_to_simple_extraction_by_the_time_budget(self):
        result = convert(builder.pdf_bytes(pages=2), "a.pdf", ConvertOptions(pdf_time_budget=0.0))

        assert result.time_limited
        assert any("time budget" in warning for warning in result.warnings)

    def test_pages_left_unread_again_after_the_budget_without_a_warning(self, monkeypatch):
        """The budget can run out after the last page was read and before pending pages are read again: no page went to
        simple extraction, so there is no warning, but the reading still depends on the clock."""
        from opendockit.extractors import pdf as pdf_module
        from tests.test_audit_pdf_head import _stream
        from tests.test_audit_pdf_text import _pages

        clock = [0.0]
        read_page = pdf_module._read_page

        def read(page, placement_fit=None):
            result = read_page(page, placement_fit)
            if page.page_number == 1:
                result.placement_pending = True
            else:
                result.placement_fit = (0.9, 0.25)
                clock[0] = 1_000.0  # the budget is spent while the last page is read
            return result

        monkeypatch.setattr(pdf_module, "_read_page", read)
        monkeypatch.setattr(pdf_module.time, "perf_counter", lambda: clock[0])

        result = convert(_pages(2, _stream(b"BT /F1 12 Tf 72 700 Td (Hello world) Tj ET")), "a.pdf", ConvertOptions(pdf_time_budget=10))

        assert result.time_limited
        assert not any("time budget" in warning for warning in result.warnings)

    def test_flag_survives_description(self):
        from dataclasses import replace

        from opendockit.converter import render

        result = convert(builder.pdf_bytes(pages=2), "a.pdf", ConvertOptions(pdf_time_budget=0.0))

        assert render(replace(result)).time_limited


class TestMetadataIsCleaned:
    """Metadata was handed over as the file wrote it: not NFC, with zero-width and control characters, and with line breaks."""

    def test_docx_title_is_nfc_without_invisible_characters(self):
        import unicodedata

        from docx import Document

        document = Document()
        document.add_paragraph("본문")
        document.core_properties.title = unicodedata.normalize("NFD", "분기 보고서") + "​x"
        document.core_properties.subject = " ​ "
        buffer = BytesIO()
        document.save(buffer)

        result = convert(buffer.getvalue(), "a.docx")

        assert result.metadata["title"] == "분기 보고서x"
        assert "subject" not in result.metadata

    def test_pdf_metadata_is_one_line_without_control_characters(self):
        from reportlab.pdfgen import canvas

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer)
        pdf.setTitle("Quarterly\x01\x02  report")
        pdf.setAuthor("a\nb\n# Heading")
        pdf.drawString(100, 700, "hello")
        pdf.save()

        result = convert(buffer.getvalue(), "a.pdf")

        assert result.metadata["title"] == "Quarterly report"
        assert result.metadata["author"] == "a b # Heading"


_OTHER_FORMAT_LIBRARIES = ("openpyxl", "bs4", "pdfplumber", "pdfminer", "pypdfium2", "pptx", "html5lib")


def _libraries_loaded_by(script: str) -> set[str]:
    """Top-level names of `_OTHER_FORMAT_LIBRARIES` and python-docx that a fresh interpreter has imported after `script`."""
    import json
    import subprocess

    watched = sorted({*_OTHER_FORMAT_LIBRARIES, "docx"})
    probe = (f"{script}\nimport json, sys\n"
             f"print(json.dumps(sorted({{name.split('.')[0] for name in sys.modules}} & set({watched!r}))))")
    completed = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True, check=True, timeout=120)
    return set(json.loads(completed.stdout.strip().splitlines()[-1]))


# Six threads convert a PPTX after the main thread did, then parse names of their own at once (`TestXmlParsersPerThread`)
_SHARED_DICTIONARY_SCRIPT = """
import sys, threading
from lxml import etree
import opendockit
data = open(sys.argv[1], "rb").read()
opendockit.convert(data, "a.pptx")
wrong = []
barrier = threading.Barrier(6)
def work(thread):
    opendockit.convert(data, "a.pptx")
    barrier.wait()
    parser = etree.XMLParser()
    for count in range(400):
        names = [f"t{thread}n{count}x{index}" for index in range(30)]
        try:
            root = etree.fromstring(("<r>" + "".join(f"<{name}/>" for name in names) + "</r>").encode(), parser)
            wrong.extend(name for name, child in zip(names, root) if child.tag != name)
        except etree.XMLSyntaxError:
            wrong.append("syntax")
threads = [threading.Thread(target=work, args=(thread,)) for thread in range(6)]
for thread in threads:
    thread.start()
for thread in threads:
    thread.join()
print("wrong", len(wrong))
"""


class TestXmlParsersPerThread:
    """A parser kept by a module and used by threads converting at once made other threads' parses fail (python-docx read a
    sound `settings.xml` as 'Namespace prefix w on settings is not defined'), so a valid file came out as corrupted in 7 of
    200 runs of eight threads converting different formats. Each thread now parses with its own parser."""

    @pytest.mark.parametrize(("module", "name"), [("_package", "_xml_parser"), ("_xlsx_parts", "_vml_parser"),
                                                  ("epub", "_chapter_parser"), ("hwpx", "_xml_parser"), ("odf", "_xml_parser")])
    def test_each_thread_has_its_own_parser(self, module, name):
        from importlib import import_module
        import threading

        parser = getattr(import_module(f"opendockit.extractors.{module}"), name)
        others: list[object] = []
        thread = threading.Thread(target=lambda: others.extend([parser(), parser()]))
        thread.start()
        thread.join()

        assert parser() is parser() and others[0] is others[1]
        assert others[0] is not parser()

    def test_threads_converting_after_a_shared_parser_keep_their_own_dictionaries(self, tmp_path):
        """python-pptx parses with one parser for every thread, so a thread whose first parse was a PPTX took the string
        dictionary of the thread that used that parser last; threads parsing at once then added names to one dictionary:
        names came out wrong, or the process stopped on a bad access (each of five runs). Each converting thread now makes
        a dictionary of its own first."""
        import subprocess

        (tmp_path / "a.pptx").write_bytes(builder.pptx_bytes())
        completed = subprocess.run([sys.executable, "-c", _SHARED_DICTIONARY_SCRIPT, str(tmp_path / "a.pptx")],
                                   capture_output=True, text=True, timeout=120)

        assert completed.returncode == 0, completed.stderr[-2000:]
        assert completed.stdout.split() == ["wrong", "0"]


class TestExtractorsImportedOnFirstUse:
    """`import opendockit` imported every extractor and with them openpyxl, BeautifulSoup, pdfplumber, pypdfium2 and python-pptx:
    0.2-0.3 s and 100 MB before a page was read, paid by every command-line run and every Windows worker (a full interpreter
    start per file). An extractor's module is now imported the first time its format is converted."""

    def test_importing_the_package_loads_no_reader_library(self):
        assert _libraries_loaded_by("import opendockit") == set()

    def test_docx_does_not_load_the_readers_of_other_formats(self, tmp_path):
        path = tmp_path / "a.docx"
        path.write_bytes(builder.docx_bytes())

        loaded = _libraries_loaded_by(f"import opendockit\nassert opendockit.convert_file({str(path)!r}).text")

        assert loaded == {"docx"}

    def test_every_format_has_an_extractor_function_behind_it(self):
        from importlib import import_module

        from opendockit.extractors import EXTRACTOR_MODULES

        assert set(EXTRACTOR_MODULES) == set(DocumentFormat)
        for module, name in EXTRACTOR_MODULES.values():
            assert callable(getattr(import_module(f"opendockit.extractors.{module}"), name))

    def test_extractor_is_called_through_its_entry(self):
        result = convert(b"plain words", "a.txt")

        assert result.text == "plain words"
        assert EXTRACTORS[DocumentFormat.TEXT].__name__ == "extract_text"

    def test_threads_converting_their_first_documents_at_once(self, tmp_path):
        """Threads that converted their first EPUB, XLSX, HTML … at the same moment imported the same libraries at once; Python's
        import machinery stopped one of them with a `_DeadlockError`, and a valid file came out as corrupted (in 6 of 8 fresh
        processes). The documents are built here, so the interpreter that converts them has imported none of the libraries."""
        import json
        import subprocess

        from tests.test_epub import _epub

        documents = {"a.docx": builder.docx_bytes(), "a.xlsx": builder.xlsx_bytes(), "a.pptx": builder.pptx_bytes(),
                     "a.pdf": builder.pdf_bytes(1), "a.hwpx": builder.hwpx_bytes(), "a.html": b"<html><body><p>x</p></body></html>",
                     "a.epub": _epub({"c1.xhtml": "<html><body><p>chapter</p></body></html>"}),
                     "a.eml": b"Subject: s\nContent-Type: text/html\n\n<p>m</p>"}
        for name, data in documents.items():
            (tmp_path / name).write_bytes(data)
        script = (
            "import json, os, sys\nfrom concurrent.futures import ThreadPoolExecutor\nimport opendockit\n"
            "folder = sys.argv[1]\n"
            "documents = [(open(os.path.join(folder, n), 'rb').read(), n) for n in sorted(os.listdir(folder))] * 3\n"
            "def run(document):\n"
            "    try:\n        opendockit.convert(*document)\n"
            "    except Exception as exc:\n        return f'{document[1]}: {type(exc.__cause__ or exc).__name__}'\n"
            "with ThreadPoolExecutor(len(documents)) as pool:\n"
            "    print(json.dumps(sorted(filter(None, pool.map(run, documents)))))\n")

        for _ in range(4):
            completed = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, check=True,
                                       timeout=120)
            assert json.loads(completed.stdout.strip().splitlines()[-1]) == []

    def test_importing_them_all_goes_through_the_lock(self, monkeypatch):
        """`opendockit.extractors.all` imported its modules around the lock of the imports on first use: a thread importing it
        while others converted their first documents could import the same libraries as they did, at once."""
        from importlib import import_module

        from opendockit import extractors

        loaded: list[str] = []
        monkeypatch.delitem(sys.modules, "opendockit.extractors.all", raising=False)
        monkeypatch.setattr(extractors, "load", lambda module: loaded.append(module) or import_module(module))

        import_module("opendockit.extractors.all")

        assert sorted(loaded) == sorted({f"opendockit.extractors.{module}" for module, _ in extractors.EXTRACTOR_MODULES.values()})

    def test_forkserver_preloads_every_extractor(self, monkeypatch):
        import multiprocessing

        from opendockit import isolated

        if "forkserver" not in multiprocessing.get_all_start_methods():
            pytest.skip("no forkserver on this platform")
        preloaded: list[list[str]] = []
        monkeypatch.setattr(type(multiprocessing.get_context("forkserver")), "set_forkserver_preload",
                            lambda self, modules: preloaded.append(list(modules)))

        isolated._worker_context()

        assert preloaded == [["opendockit", "opendockit.extractors.all"]]
        loaded = _libraries_loaded_by("import opendockit.extractors.all")
        assert loaded >= {"openpyxl", "bs4", "pdfplumber", "pypdfium2", "pptx", "docx"}


_FORK_SCRIPT = """
import os, signal, sys, threading, time, warnings
warnings.simplefilter("ignore")   # Python's DeprecationWarning for a fork in a process with threads
import opendockit
from opendockit.extractors import load

folder, shape = sys.argv[1], sys.argv[2]
sys.path.insert(0, folder)
started = threading.Event()

if shape == "real":
    pdf = open(os.path.join(folder, "a.pdf"), "rb").read()
    thread = threading.Thread(target=opendockit.convert, args=(pdf, "a.pdf"))
    thread.start()
    while "pdfplumber" not in sys.modules and thread.is_alive():
        time.sleep(0.0005)
else:
    thread = threading.Thread(target=load, args=("slow_reader",))
    thread.start()
    started.wait()
pid = os.fork()
if pid == 0:
    signal.alarm(10)
    try:
        if shape == "same module":
            assert load("slow_reader").VALUE == 1
        else:
            assert opendockit.convert(b"hello world", "a.txt").text == "hello world"
        os.write(1, b"child ok\\n")
    finally:
        os._exit(0)
thread.join()
_, status = os.waitpid(pid, 0)
print("hung" if os.WIFSIGNALED(status) else "ended")
"""

# Imported on first use by a thread of the script above: it lets the main thread know it is being imported, and takes its time
_SLOW_READER = "import sys, time\nsys.modules['__main__'].started.set()\ntime.sleep(0.5)\nVALUE = 1\n"

# Forks while `load` imports it, with the lock held by the thread that forks
_FORKING_READER = """
import os, signal
import opendockit
pid = os.fork()
if pid == 0:
    signal.alarm(10)
    try:
        assert opendockit.convert(b"hello world", "a.txt").text == "hello world"
        os.write(1, b"child ok\\n")
    finally:
        os._exit(0)
_, status = os.waitpid(pid, 0)
print("hung" if os.WIFSIGNALED(status) else "ended")
"""


@pytest.mark.skipif(not hasattr(__import__("os"), "fork"), reason="only where processes fork")
@pytest.mark.timing
@pytest.mark.no_rerun  # starts processes and threads and checks how they end: one failure can be a real race
class TestImportsOnFirstUseAndFork:
    """A process forked from one thread while another imported a reader on first use got the lock of those imports held by a
    thread that does not exist in it: its first document of a format not yet loaded waited for ever. A fork now waits until
    no import on first use is under way. A child that only got the lock back still waited for ever on Python's own lock of the
    half-imported module (shown with a stand-in reader, `slow_reader`). A child converting the very format the thread goes on
    to read can still wait on that reader's own locks (an accepted gap, `docs/decisions/core.md`): the real reader below is
    checked with a child converting text."""

    @staticmethod
    def _run(tmp_path, shape: str) -> list[str]:
        import subprocess

        (tmp_path / "slow_reader.py").write_text(_SLOW_READER)
        (tmp_path / "a.pdf").write_bytes(builder.pdf_bytes(3))
        completed = subprocess.run([sys.executable, "-c", _FORK_SCRIPT, str(tmp_path), shape], capture_output=True, text=True,
                                   check=True, timeout=60)
        return completed.stdout.split()

    def test_child_converts_a_format_not_yet_loaded(self, tmp_path):
        assert self._run(tmp_path, "other format") == ["child", "ok", "ended"]

    def test_child_imports_the_module_that_was_being_imported(self, tmp_path):
        assert self._run(tmp_path, "same module") == ["child", "ok", "ended"]

    def test_fork_while_a_thread_imports_a_real_reader(self, tmp_path):
        assert self._run(tmp_path, "real") == ["child", "ok", "ended"]

    def test_thread_that_holds_the_lock_can_fork(self, tmp_path):
        """A library that forks while it is being imported: the thread that forks holds the lock already and takes it again."""
        import subprocess

        (tmp_path / "forking_reader.py").write_text(_FORKING_READER)
        script = ("import sys, warnings\nwarnings.simplefilter('ignore')\nsys.path.insert(0, sys.argv[1])\n"
                  "from opendockit.extractors import load\nload('forking_reader')\n")
        completed = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, check=True,
                                   timeout=60)

        assert completed.stdout.split() == ["child", "ok", "ended"]


class TestReaderThatCannotBeImported:
    """A reader library that cannot be imported (a broken wheel, a binary built for another Python) stopped `import opendockit`
    while every extractor was imported with the package. Imported on first use inside the boundary that gathers parser failures,
    it made every valid file of that format a `CorruptedDocumentError`, and HTML 'could not parse the file format'. It is an
    `ImportError` with a fixed sentence; the library's own wording is only its cause."""

    @staticmethod
    def _broken(monkeypatch, *modules: str) -> list[str]:
        """Make importing `modules` fail as a broken installation does; the names tried are listed in the result."""
        from opendockit import extractors

        tried: list[str] = []
        real = extractors.import_module

        def failing(name: str):
            if name in modules:
                tried.append(name)
                raise ModuleNotFoundError("No module named 'openpyxl.cell._writer'; /site-packages/openpyxl is broken")
            return real(name)

        monkeypatch.setattr(extractors, "import_module", failing)
        # A fresh entry for each format, as in a process that has not converted one yet
        for document_format, (module, name) in extractors.EXTRACTOR_MODULES.items():
            monkeypatch.setitem(extractors.EXTRACTORS, document_format, extractors._on_first_use(module, name))
        return tried

    def test_spreadsheet_reader_is_an_import_error_with_a_fixed_sentence(self, monkeypatch):
        from opendockit.extractors import IMPORT_FAILED_MESSAGE

        tried = self._broken(monkeypatch, "opendockit.extractors.spreadsheet")

        for _ in range(2):
            with pytest.raises(ImportError) as raised:
                convert(builder.xlsx_bytes(), "a.xlsx")
            assert not isinstance(raised.value, ConversionError)
            assert str(raised.value) == IMPORT_FAILED_MESSAGE
            assert isinstance(raised.value.__cause__, ModuleNotFoundError)
        assert tried == ["opendockit.extractors.spreadsheet"] * 2   # tried again, so a repaired installation is picked up

    def test_pdf_reader_is_not_tried_as_text(self, monkeypatch):
        from opendockit.extractors import IMPORT_FAILED_MESSAGE

        self._broken(monkeypatch, "opendockit.extractors.pdf")

        with pytest.raises(ImportError, match=IMPORT_FAILED_MESSAGE):
            convert(b"A note\n" + builder.pdf_bytes(1), "note.txt")
        with pytest.raises(ImportError, match=IMPORT_FAILED_MESSAGE):
            convert(builder.pdf_bytes(1), "a.pdf")

    def test_mail_readers_of_detection_are_not_a_corrupted_html(self, monkeypatch):
        from opendockit import detect_format
        from opendockit.extractors import IMPORT_FAILED_MESSAGE

        self._broken(monkeypatch, "opendockit.extractors._html_mail")

        with pytest.raises(ImportError, match=IMPORT_FAILED_MESSAGE):
            convert(b"<html><body><p>hello</p></body></html>", "a.html")
        with pytest.raises(ImportError, match=IMPORT_FAILED_MESSAGE):
            detect_format(b"<html><body><p>hello</p></body></html>", "a.html")

    def test_reader_of_imported_docx_content(self, monkeypatch):
        from opendockit.extractors import IMPORT_FAILED_MESSAGE
        from tests.test_office import _docx_with_chunks

        data = _docx_with_chunks('<w:altChunk r:id="rIdChunk"/>', {"rIdChunk": ("chunk1.html", "text/html", b"<p>imported</p>")})
        self._broken(monkeypatch, "opendockit.extractors.html")

        with pytest.raises(ImportError, match=IMPORT_FAILED_MESSAGE):
            convert(data, "a.docx")

    def test_import_a_library_makes_while_it_reads_gets_the_sentence_too(self, monkeypatch):
        from opendockit.extractors import IMPORT_FAILED_MESSAGE

        def reader(data, options):
            raise ImportError("cannot import name '_speedups' from 'somelib' (/opt/site-packages/somelib/__init__.py)")

        monkeypatch.setitem(EXTRACTORS, DocumentFormat.TEXT, reader)

        with pytest.raises(ImportError) as raised:
            convert(b"plain words", "a.txt")
        assert str(raised.value) == IMPORT_FAILED_MESSAGE

    def test_worker_of_isolated_converter_reports_it_the_same_way(self, tmp_path):
        """A broken BeautifulSoup in the installation: the forkserver's preload skips it, and the worker's conversion raises the
        `ImportError`, which reaches the caller as such."""
        import os
        import subprocess

        from opendockit.extractors import IMPORT_FAILED_MESSAGE

        (tmp_path / "bs4").mkdir()
        (tmp_path / "bs4" / "__init__.py").write_text("raise ImportError('a broken build of bs4')\n")
        script = ("import asyncio\nfrom opendockit import IsolatedConverter, convert\n"
                  "html = b'<html><body><p>hello</p></body></html>'\n"
                  "async def in_worker():\n    async with IsolatedConverter() as converter:\n"
                  "        await converter.convert(html, 'a.html')\n"
                  "for run in (lambda: convert(html, 'a.html'), lambda: asyncio.run(in_worker())):\n"
                  "    try:\n        run()\n    except Exception as exc:\n        print(type(exc).__name__, '|', exc)\n")
        environment = os.environ | {"PYTHONPATH": os.pathsep.join(filter(None, [str(tmp_path), os.environ.get("PYTHONPATH")]))}

        completed = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True, timeout=120,
                                   env=environment)

        assert completed.stdout.splitlines() == [f"ImportError | {IMPORT_FAILED_MESSAGE}"] * 2
