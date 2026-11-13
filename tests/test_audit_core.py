"""Audited defects of the shared code (detection, assembly, shared extractor helpers): patterns that took quadratic time on a small
hostile input, one odd value that got a whole document rejected as 'corrupted', and warnings or numbers that came out wrong."""

import time

import pytest

from opendockit import ConvertOptions, convert
from opendockit.detect import detect_format
from opendockit.extractors import _shared
from opendockit.markdown import neutralize_table_tags
from opendockit.models import DocumentFormat


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
