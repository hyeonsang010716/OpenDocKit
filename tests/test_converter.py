"""Converter boundary: limits, exception wrapping, assembly, async."""

import asyncio
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
