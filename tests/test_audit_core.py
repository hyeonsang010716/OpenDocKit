"""Audited defects of the shared code (detection, assembly, shared extractor helpers): patterns that took quadratic time on a small
hostile input, one odd value that got a whole document rejected as 'corrupted', and warnings or numbers that came out wrong."""

import base64
import time

import pytest

from opendockit import ConvertOptions, convert
from opendockit.detect import detect_format
from opendockit.extractors import _shared
from opendockit.markdown import neutralize_table_tags
from opendockit.models import DocumentFormat
from tests import builder


class TestPdfBodySearchAfterALateHeader:
    """A file with `%PDF-` after its first byte is searched for body structure (`N N obj`). The search started again at every digit
    of a run of digits, so 40,000 digits after the header took 5 seconds — in the first check every upload goes through."""

    @pytest.mark.timing
    def test_long_run_of_digits_takes_linear_time(self):
        data = b"x%PDF-1.4\n" + b"1" * 100_000
        started = time.perf_counter()

        detected = detect_format(data, "note.txt")

        assert time.perf_counter() - started < 5.0
        assert detected is DocumentFormat.TEXT

    @pytest.mark.parametrize("body", [b"12 0 obj\n<< >>\nendobj\n", b"0000000012 00000 obj", b"x12 0 obj", b"1\n\n2\tobj",
                                      b"startxref\n0\n"])
    def test_object_header_after_a_late_header_is_still_a_pdf(self, body):
        assert detect_format(b"junk\n%PDF-1.4\n" + body, "a.pdf") is DocumentFormat.PDF

    @pytest.mark.parametrize("body", [b"12 0 object", b"no 12 obj", b"a 0 obj", b"12 0obj", b"1" * 50 + b" " * 50])
    def test_text_without_an_object_header_is_not_a_pdf(self, body):
        assert detect_format(b"note: %PDF-1.4\n" + body, "a.txt") is DocumentFormat.TEXT


class TestUnclosedImagePlaceholderText:
    """Text that only looks like the start of a picture placeholder (`[Image: ` with no `]`) was scanned to the end of the line from
    every `[`, so 10,000 of them next to a bold word took 5 seconds."""

    @pytest.mark.timing
    def test_many_unclosed_placeholder_starts_take_linear_time(self):
        data = ("<p><b>b</b> " + "[Image: " * 25_000 + "</p>").encode()
        started = time.perf_counter()

        result = convert(data, "a.html", ConvertOptions(max_chars=None))

        assert time.perf_counter() - started < 5.0
        assert result.text.startswith("**b** [Image: [Image: ") and result.char_count > 150_000

    @pytest.mark.timing
    def test_many_unclosed_footnote_mark_starts_take_linear_time(self):
        data = ("<p><b>b</b> " + "[^a" * 60_000 + "</p>").encode()
        started = time.perf_counter()

        result = convert(data, "a.html", ConvertOptions(max_chars=None))

        assert time.perf_counter() - started < 5.0
        assert result.text.startswith("**b** [^a[^a")

    def test_placeholders_are_still_told_from_words(self):
        assert _shared._is_placeholder_text("[Image: 조직도] [^1] [Image]")
        assert not _shared._is_placeholder_text("[Image: 조직도] 본문")
        assert _shared._INLINE_PLACEHOLDER.split("앞 [Image: 로고]뒤[^12]") == ["앞 ", "[Image: 로고]", "뒤", "[^12]", ""]


class TestUnclosedTableTagsInCellText:
    """Cell text of an HTML table (a table with a horizontal merge) is searched for table tags to neutralize. A tag name with no
    `>` after it was scanned to the end of the text from every `<`, so 80,000 of them in one cell took 8 seconds."""

    @pytest.mark.timing
    def test_many_unclosed_tag_starts_take_linear_time(self):
        data = ("<table><tr><td colspan=2>" + "&lt;td" * 200_000 + "</td></tr><tr><td>a</td><td>b</td></tr></table>").encode()
        started = time.perf_counter()

        result = convert(data, "a.html", ConvertOptions(max_chars=None))

        assert time.perf_counter() - started < 5.0
        assert result.text.startswith('<table>\n<tr><th colspan="2"><td<td<td') and result.char_count > 600_000

    @pytest.mark.parametrize(("text", "expected"), [
        ("a <td> b </TR> c", "a &lt;td> b &lt;/TR> c"),
        ("<td <td> <td", "&lt;td <td> <td"),
        ('<td title="x<5">y<br', '&lt;td title="x<5">y<br'),
        ("x<5 <tdx> <br/>\n<th\nscope=col>", "x<5 <tdx> &lt;br/>\n&lt;th\nscope=col>"),
        ("<td<td<td", "<td<td<td"),
        (">", ">"),
        ("", ""),
    ])
    def test_the_same_tags_are_neutralized_as_before(self, text, expected):
        assert neutralize_table_tags(text) == expected


def _markdown_picture(alt: str, seed: int) -> str:
    return f"![{alt}](data:image/png;base64,{base64.b64encode(builder.png_bytes(seed=seed)).decode()})"


class TestSpacesOnALineWithAPicture:
    """The spaces around a picture placeholder inside a paragraph are folded into one. The search for 'spaces, placeholder, spaces'
    started again at every space of a run, so one Markdown line with 80,000 spaces before a picture took 4 seconds."""

    @pytest.mark.timing
    def test_long_run_of_spaces_before_a_picture_takes_linear_time(self):
        data = ("x" + " " * 300_000 + "y " + _markdown_picture("p", 1)).encode()
        started = time.perf_counter()

        result = convert(data, "a.md", ConvertOptions(max_chars=None))

        assert time.perf_counter() - started < 5.0
        assert result.text.startswith("x  ") and result.text.endswith(" y\n\n[Image: p]") and result.char_count > 300_000

    def test_spaces_around_placeholders_are_folded_as_before(self):
        a, b, c, d, e, f, g = (_markdown_picture(alt, seed) for seed, alt in enumerate("abcdefg", 1))
        lines = [f"앞   말  {a}   가운데  말   {b}   뒤  말", f"  - 항목 {c}{d} 끝", f"{e}   {f}", f"\t탭 {g}\t탭"]

        result = convert("\n\n".join(lines).encode(), "a.md")

        assert result.text == ("앞   말 가운데  말 뒤  말\n\n[Image: a]\n\n[Image: b]\n\n"
                               "  - 항목  끝\n\n[Image: c]\n\n[Image: d]\n\n"
                               "[Image: e]\n\n[Image: f]\n\n"
                               "\t탭 \t탭\n\n[Image: g]")


def _docx_with_alt_text(alt: str) -> bytes:
    """The sample document with pictures, the alt text (wp:docPr descr) of its first picture replaced (`alt` is XML attribute text)."""
    return builder.zip_with(builder.docx_with_images(), edit={
        "word/document.xml": lambda xml: xml.replace('descr="분기별 매출 차트"', f'descr="{alt}"')})


class TestAltTextWithManyLineBreaks:
    """Alt text is checked for the line Office adds below its own description ('Description automatically generated'). The check
    looked for that line after every line break, each time skipping the breaks that follow, so alt text with 16,000 line breaks
    took 2.4 seconds."""

    @pytest.mark.timing
    def test_alt_text_with_many_line_breaks_takes_linear_time(self):
        data = _docx_with_alt_text("a" + "&#10;" * 56_000 + "b")
        started = time.perf_counter()

        result = convert(data, "a.docx")

        assert time.perf_counter() - started < 5.0
        assert result.text.startswith("차트 앞 문단입니다.\n\n[Image: a b]\n\n")

    @pytest.mark.parametrize("alt", [
        "로고, 폰트이(가) 표시된 사진&#10;&#10;자동 생성된 설명",
        "A picture containing text, clock&#10;&#10;Description automatically generated",
        "Chart&#10; &#9;&#10;  Description automatically&#10;generated.",
        "Diagram  &#10;Description generated with very high confidence",
        "텍스트&#10;&#10;&#10;AI 생성 콘텐츠는 정확하지 않을 수 있습니다.",
        "a&#10;b&#10;AI-generated content may be incorrect.",
    ])
    def test_description_office_generated_is_still_dropped(self, alt):
        result = convert(_docx_with_alt_text(alt), "a.docx")

        assert result.text.startswith("차트 앞 문단입니다.\n\n[Image]\n\n")

    @pytest.mark.parametrize(("alt", "shown"), [
        ("Description automatically generated", "Description automatically generated"),
        ("조직도&#10;Description automatically generated 2", "조직도 Description automatically generated 2"),
        ("조직도 Description automatically generated", "조직도 Description automatically generated"),
        ("조직도&#10;설명 Description automatically generated", "조직도 설명 Description automatically generated"),
    ])
    def test_alt_text_a_person_wrote_is_still_kept(self, alt, shown):
        result = convert(_docx_with_alt_text(alt), "a.docx")

        assert result.text.startswith(f"차트 앞 문단입니다.\n\n[Image: {shown}]\n\n")
