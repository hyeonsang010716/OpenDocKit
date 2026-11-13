"""Audited defects of the HTML and text extractors: single values that got a whole document rejected, content that was lost or
duplicated without a warning, and markup of a few hundred KB that took many seconds."""

import base64
import time

import pytest

from opendockit import convert
from opendockit.exceptions import CorruptedDocumentError
from tests.builder import png_bytes


_MAIL_HEAD = "From: a@b.c\r\nTo: d@e.f\r\nMIME-Version: 1.0\r\n"


class TestEncodedWordThatCannotBeDecoded:
    """`email.header.decode_header` raises `HeaderParseError` (not a ValueError) on an RFC 2047 word whose base64 is broken, so a
    single subject or attachment name rejected the whole mail as 'corrupted'."""

    @pytest.mark.parametrize("kind", ["plain", "html"])
    def test_body_is_read_when_the_subject_is_a_broken_word(self, kind):
        data = (_MAIL_HEAD + f"Subject: =?utf-8?b?A?=\r\nContent-Type: text/{kind}\r\n\r\n<p>body text</p>\r\n").encode()

        result = convert(data, "a.eml")

        assert "body text" in result.text
        assert "Subject: =?utf-8?b?A?=" in result.text

    def test_body_is_read_when_an_attachment_name_is_a_broken_word(self):
        data = (_MAIL_HEAD + "Subject: s\r\nContent-Type: multipart/mixed; boundary=b\r\n\r\n"
                "--b\r\nContent-Type: text/plain\r\n\r\nbody text\r\n"
                "--b\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; filename=\"=?utf-8?b?A?=\"\r\n\r\nxx\r\n"
                "--b--\r\n").encode()

        result = convert(data, "a.eml")

        assert "body text" in result.text
        assert any(warning.startswith("1 mail attachment(s)") for warning in result.warnings)


class TestHugeCssLengthInATableCell:
    """A CSS length of hundreds of digits became `inf`, and rounding the indentation depth of the cell raised OverflowError: one
    style attribute rejected the whole document."""

    @pytest.mark.parametrize("length", ["9" * 400 + "px", "9" * 300 + "px", "1" + "0" * 308 + "em"])
    def test_table_is_read(self, length):
        html = ("<table><tr><th>name</th><th>value</th></tr>"
                f"<tr><td style='padding-left:{length}; margin-left:{length}'>inner</td><td>1</td></tr>"
                "<tr><td>outer</td><td>2</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text == "| name | value |\n| --- | --- |\n| └ inner | 1 |\n| outer | 2 |"

    def test_ordinary_indentation_is_marked_as_before(self):
        html = ("<table><tr><th>name</th><th>value</th></tr><tr><td>outer</td><td>2</td></tr>"
                "<tr><td style='padding-left:20px'>inner</td><td>1</td></tr>"
                "<tr><td style='padding-left:40px'>innermost</td><td>1</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text == "| name | value |\n| --- | --- |\n| outer | 2 |\n| └ inner | 1 |\n| └─ innermost | 1 |"


def _mhtml(page: str, *, location: str = "http://site.example/a.html") -> bytes:
    """A saved web page with a second text/html part (a frame) and a picture part."""
    picture = base64.b64encode(png_bytes()).decode()
    return ("MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary=b\r\n\r\n"
            f"--b\r\nContent-Type: text/html\r\nContent-Location: {location}\r\n\r\n{page}\r\n"
            "--b\r\nContent-Type: text/html\r\nContent-Location: http://site.example/frame.html\r\n\r\n<p>frame</p>\r\n"
            "--b\r\nContent-Type: image/png\r\nContent-Transfer-Encoding: base64\r\n"
            f"Content-Location: http://site.example/logo.png\r\n\r\n{picture}\r\n--b--\r\n").encode()


class TestAddressThatCannotBeResolvedInMhtml:
    """`urljoin` raises ValueError ('Invalid IPv6 URL') on an address with an unclosed `[`, so one `src` (or the location of the
    page itself) rejected the whole saved page."""

    @pytest.mark.parametrize("tag", ["img", "iframe", "frame"])
    def test_page_is_read(self, tag):
        result = convert(_mhtml(f'<p>body text</p><{tag} src="//[bad">'), "a.mht")

        assert result.text == "body text"
        assert result.images == []

    def test_page_whose_own_location_cannot_be_parsed_is_read(self):
        result = convert(_mhtml('<p>body text</p><img src="logo.png"><iframe src="frame.html"></iframe>', location="http://[bad/a.html"),
                         "a.mht")

        assert result.text == "body text"

    def test_picture_and_frame_with_a_relative_address_are_still_found(self):
        result = convert(_mhtml('<p>t</p><img src="logo.png"><iframe src="frame.html"></iframe>'), "a.mht")

        assert len(result.images) == 1
        assert any("inside a frame" in warning for warning in result.warnings)


def _nested_aria_grids(depth: int) -> bytes:
    grid = ("<div role=grid><div role=row><div role=cell>a</div><div role=cell>%s</div></div>"
            "<div role=row><div role=cell>c</div><div role=cell>d</div></div></div>")
    html = "innermost"
    for _ in range(depth):
        html = grid % html
    return html.encode()


class TestDeeplyNestedAriaGrids:
    """A grid built from ARIA roles inside the cell of another one was rendered by recursion without a depth limit (unlike layout
    tables, embedded tables, lists and definition lists): 400 levels (57KB) rejected the document with a recursion-limit error."""

    @pytest.mark.parametrize("depth", [21, 400, 3000])
    def test_document_is_read(self, depth):
        result = convert(_nested_aria_grids(depth), "a.html")

        assert "innermost" in result.text
        assert result.text.startswith("| a | ")

    def test_grid_inside_a_grid_cell_is_rendered_as_before(self):
        result = convert(_nested_aria_grids(2), "a.html")

        assert result.text == ("| a | \\| a \\| innermost \\|<br>\\| --- \\| --- \\|<br>\\| c \\| d \\| |\n"
                               "| --- | --- |\n| c | d |")


class TestStylePropertiesThatOnlyEndLikeAHidingOne:
    """The hidden-style pattern matched inside a longer property name: `backface-visibility:hidden` (a 3D-transform hint on an
    element that is fully visible) removed the element and its text without a warning."""

    @pytest.mark.parametrize("style", ["backface-visibility:hidden", "-webkit-backface-visibility: hidden;color:red",
                                       "transform:none;BACKFACE-VISIBILITY : HIDDEN", "--card-display:none"])
    def test_element_is_kept(self, style):
        result = convert(f'<p>before</p><div style="{style}">BODY</div><p>after</p>'.encode(), "a.html")

        assert result.text == "before\n\nBODY\n\nafter"

    @pytest.mark.parametrize("style", ["display:none", "DISPLAY : NONE", "visibility:hidden", "color:red;visibility: hidden",
                                       "color:red; display:none !important", "content-visibility:hidden",
                                       "backface-visibility:hidden;display:none"])
    def test_hidden_element_is_still_removed(self, style):
        result = convert(f'<p>before</p><div style="{style}">SECRET</div><p>after</p>'.encode(), "a.html")

        assert result.text == "before\n\nafter"


_TWO_ROWS = "<tr><td>1</td><td>2</td></tr><tr><td>3</td><td>4</td></tr>"


class TestCaptionOfATableInsideACell:
    """The caption of a table was looked for among all descendants, so an outer table without a caption took the caption of a
    table nested in one of its cells: it came out twice, once above the outer table and once at the inner one."""

    def test_outer_table_does_not_take_the_caption_of_the_inner_table(self):
        html = (f"<table><tr><td>a</td><td><table><caption>INNERCAP</caption>{_TWO_ROWS}</table></td></tr>"
                "<tr><td>b</td><td>c</td></tr><tr><td>d</td><td>e</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text == "a\n\nINNERCAP\n\n| 1 | 2 |\n| --- | --- |\n| 3 | 4 |\n\nb\n\nc\n\nd\n\ne"

    def test_outer_caption_and_inner_caption_each_come_out_once(self):
        html = (f"<table><caption>OUTERCAP</caption><tr><td>a</td><td><table><caption>INNERCAP</caption>{_TWO_ROWS}</table></td></tr>"
                "<tr><td>b</td><td>c</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text.count("OUTERCAP") == 1
        assert result.text.count("INNERCAP") == 1
        assert result.text.startswith("OUTERCAP\n\na\n\nINNERCAP\n\n| 1 | 2 |")

    def test_caption_of_a_table_two_levels_down_comes_out_once(self):
        deepest = f"<table><caption>DEEPCAP</caption>{_TWO_ROWS}</table>"
        inner = f"<table><tr><th>h</th><th>i</th></tr><tr><td>x</td><td>{deepest}</td></tr></table>"
        html = (f"<table><tr><th>a</th><th colspan=2>z</th></tr><tr><td>k</td><td>q</td><td>{inner}</td></tr>"
                "<tr><td>b</td><td>c</td><td>d</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text == ('<table>\n<tr><th>a</th><th colspan="2">z</th></tr>\n'
                               "<tr><td>k</td><td>q</td><td>h i x DEEPCAP 1 2 3 4</td></tr>\n"
                               "<tr><td>b</td><td>c</td><td>d</td></tr>\n</table>")

    @pytest.mark.parametrize("table", [
        f"<table><caption>CAP</caption>{_TWO_ROWS}</table>",
        f"<table>{_TWO_ROWS}<caption>CAP</caption></table>",
        f"<table><tbody><caption>CAP</caption>{_TWO_ROWS}</tbody></table>",
        f"<table><form><caption>CAP</caption>{_TWO_ROWS}</form></table>",
    ])
    def test_caption_of_the_table_itself_is_found_wherever_the_parser_left_it(self, table):
        result = convert(table.encode(), "a.html")

        assert result.text == "CAP\n\n| 1 | 2 |\n| --- | --- |\n| 3 | 4 |"


class TestDoctypeInsideTheBody:
    """A `<!DOCTYPE …>` in the middle of the body (pages pasted together, a template included twice) is kept by the parser as a
    node of its own. Only the ones at the top of the document were skipped, so its name came out as a paragraph, `html`."""

    @pytest.mark.parametrize(("html", "expected"), [
        ("<p>a</p><!DOCTYPE html><p>b</p>", "a\n\nb"),
        ("<div>a<!DOCTYPE html PUBLIC '-//W3C//DTD XHTML 1.0 Transitional//EN'>b</div>", "ab"),
        ("<ul><li>a</li><!DOCTYPE html><li>b</li></ul>", "- a\n- b"),
        ("<table><tr><td>a<!DOCTYPE html></td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>",
         "| a | b |\n| --- | --- |\n| c | d |"),
        ("<h2>title<!DOCTYPE html></h2><p>b</p>", "## title\n\nb"),
    ])
    def test_declaration_is_not_text(self, html, expected):
        result = convert(f"<html><body>{html}</body></html>".encode(), "a.html")

        assert result.text == expected

    def test_text_that_only_looks_like_a_declaration_is_kept(self):
        result = convert(b"<p>write &lt;!DOCTYPE html&gt; first</p>", "a.html")

        assert result.text == "write <!DOCTYPE html> first"


_FROM_LINE = "From - Mon Jan  1 00:00:00 2024\r\n"
_ESCAPED_BODY = "<p>one</p>\r\n>From here on<br>\r\n<p>two</p>\r\n"


class TestFromLineEscapeInASingleHtmlPartMail:
    """An .eml saved by Thunderbird (a leading mbox separator line) has the `>` that mbox put before a body line starting with
    `From ` stripped (docs/decisions/mail.md). A mail with a single text/html part is read on the MHTML path, which parsed the
    bytes as they were and kept `>From here on` — the same mail as a multipart gave `From here on`."""

    def test_single_html_part_is_unescaped_like_a_multipart(self):
        single = (_FROM_LINE + _MAIL_HEAD + "Subject: s\r\nContent-Type: text/html\r\n\r\n" + _ESCAPED_BODY).encode()
        multipart = (_FROM_LINE + _MAIL_HEAD + "Subject: s\r\nContent-Type: multipart/alternative; boundary=b\r\n\r\n"
                     "--b\r\nContent-Type: text/html\r\n\r\n" + _ESCAPED_BODY + "--b--\r\n").encode()

        expected = "Subject: s\nFrom: a@b.c\nTo: d@e.f\n\none\n\nFrom here on\n\ntwo"
        assert convert(multipart, "a.eml").text == expected
        assert convert(single, "a.eml").text == expected

    def test_mail_without_a_separator_line_is_not_changed(self):
        single = (_MAIL_HEAD + "Subject: s\r\nContent-Type: text/html\r\n\r\n" + _ESCAPED_BODY).encode()

        assert convert(single, "a.eml").text == "Subject: s\nFrom: a@b.c\nTo: d@e.f\n\none\n\n>From here on\n\ntwo"


class TestDataUriWrittenWithCapitalsOrLeadingSpace:
    """The scheme and the `;base64,` mark of a data: URI are not case-sensitive and a browser strips whitespace around the
    address, but only the SVG check allowed for that: `src=" data:image/png…"` and `DATA:image/png…` gave no picture at all."""

    @pytest.mark.parametrize("prefix", ["data:image/png;base64,", " data:image/png;base64,", "\n\tdata:image/png;base64,",
                                        "DATA:image/png;base64,", "Data:Image/PNG;Base64,", "data:image/png;charset=utf-8;BASE64,"])
    def test_picture_is_extracted(self, prefix):
        encoded = base64.b64encode(png_bytes()).decode()

        result = convert(f'<p>text</p><img alt="chart" src="{prefix}{encoded}">'.encode(), "a.html")

        assert len(result.images) == 1
        assert result.warnings == []

    def test_lazy_placeholder_and_svg_keep_only_the_alt_text_as_before(self):
        encoded = base64.b64encode(png_bytes()).decode()
        html = (f'<p>text</p><img alt="lazy" data-src="/real.png" src=" DATA:image/png;base64,{encoded}">'
                '<img alt="icon" src=" DATA:image/svg+xml;base64,PHN2Zy8+">')

        result = convert(html.encode(), "a.html")

        assert result.images == []
        assert result.text == "text\n\n[Image: lazy] [Image: icon]"


class TestDamagedTextBehindAByteOrderMark:
    """Text that starts with a BOM was decoded with replacement characters but never reported it: a lone UTF-16 surrogate or a
    broken UTF-8 byte became `�` with no warning, unlike every other replacement decoding (docs/decisions/encoding.md)."""

    @pytest.mark.parametrize(("data", "text", "name"), [
        (b"\xff\xfe" + "hi".encode("utf-16-le") + b"\x00\xd8" + "x".encode("utf-16-le") + b"\x00\xdc", "hi�x�", "UTF-16"),
        (b"\xfe\xff" + "hi".encode("utf-16-be") + b"\xd8\x00" + "x".encode("utf-16-be"), "hi�x", "UTF-16"),
        (b"\xef\xbb\xbf" + "가나".encode() + b"\xff" + "다".encode(), "가나�다", "UTF-8-SIG"),
        (b"\xff\xfe\x00\x00" + "hi".encode("utf-32-le") + b"\x00\x00\x11\x00", "hi�", "UTF-32"),
    ])
    @pytest.mark.parametrize("file_name", ["a.txt", "a.md"])
    def test_replacement_is_reported(self, data, text, name, file_name):
        result = convert(data, file_name)

        assert result.text == text
        assert result.warnings == [f"The text was read as {name}, but some bytes could not be decoded and were replaced with �."]

    @pytest.mark.parametrize("data", [b"\xff\xfe" + "한글 text".encode("utf-16-le"), b"\xfe\xff" + "한글 text".encode("utf-16-be"),
                                      b"\xef\xbb\xbf" + "한글 text".encode(), b"\xff\xfe\x00\x00" + "한글 text".encode("utf-32-le")])
    def test_sound_text_has_no_warning_and_the_same_encoding_name(self, data):
        result = convert(data, "a.txt")

        assert result.text == "한글 text"
        assert result.warnings == []
        assert result.metadata["encoding"] in ("utf-8-sig", "utf-16", "utf-32")


def _mail_with_attachment(disposition: str, *, body: str = "plain") -> bytes:
    return (_MAIL_HEAD + "Subject: s\r\nContent-Type: multipart/mixed; boundary=b\r\n\r\n"
            f"--b\r\nContent-Type: text/{body}\r\n\r\n<p>body text</p>\r\n"
            f"--b\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment; {disposition}\r\n\r\nxx\r\n--b--\r\n").encode()


class TestAttachmentNameInTheWarning:
    """The attachment warning names the attachments (docs/decisions/mail.md), and a name is whatever the mail says: an RFC 2231
    name with encoded line breaks put several lines into the warning, and a name of any length was written out whole."""

    @pytest.mark.parametrize("body", ["plain", "html"])
    def test_name_with_line_breaks_and_control_characters_stays_on_one_line(self, body):
        data = _mail_with_attachment("filename*=utf-8''evil%0Aline2%0D%0A%09line3%00%1B%5B31m.pdf", body=body)

        result = convert(data, "a.eml")

        assert result.warnings == ["1 mail attachment(s) (evil line2 line3 [31m.pdf) were not read. Convert attachments separately."]

    def test_very_long_name_is_cut(self):
        result = convert(_mail_with_attachment(f"filename*=utf-8''{'A' * 5000}.pdf"), "a.eml")

        assert result.warnings == [f"1 mail attachment(s) ({'A' * 99}…) were not read. Convert attachments separately."]

    def test_ordinary_names_are_written_as_before(self):
        name = "2024년 3분기 실적 보고서 (최종).pdf"
        encoded = base64.b64encode(name.encode()).decode()

        result = convert(_mail_with_attachment(f'filename="=?utf-8?b?{encoded}?="'), "a.eml")

        assert result.warnings == [f"1 mail attachment(s) ({name}) were not read. Convert attachments separately."]

    def test_name_of_only_line_breaks_counts_as_unnamed(self):
        result = convert(_mail_with_attachment("filename*=utf-8''%0A%0D"), "a.eml")

        assert result.warnings == ["1 mail attachment(s) (1 unnamed attachment(s)) were not read. Convert attachments separately."]


def _nested_multipart(levels: int, lines: int, parameter: str) -> bytes:
    """`levels` multiparts opened inside each other, the boundary of level N declared as `parameter` formatted with `bN`."""
    opened = "".join(f"Content-Type: multipart/mixed; {parameter.format(f'b{level}')}\r\n\r\n--b{level}\r\n" for level in range(levels))
    return (_MAIL_HEAD + "Subject: s\r\n" + opened + "Content-Type: text/plain\r\n\r\n" + "line\r\n" * lines).encode()


_EXTENDED_BOUNDARIES = ["boundary*=us-ascii''{}", "boundary*0={}", "boundary*0*=us-ascii''{}", 'boundary*="{}"', "BOUNDARY* = {}"]


class TestMultipartNestingDeclaredWithExtendedParameters:
    """The nesting check before parsing (32 levels) only saw `boundary=`. The standard-library parser also honours the RFC 2231
    forms (`boundary*=us-ascii''b1`, `boundary*0=b1`), so those went around the cap: 600 levels + 200,000 lines (1MB) took 9 s."""

    @pytest.mark.parametrize("parameter", _EXTENDED_BOUNDARIES)
    def test_deep_nesting_is_rejected_before_parsing(self, parameter):
        with pytest.raises(CorruptedDocumentError, match="nested deeper than 32 levels"):
            convert(_nested_multipart(40, 10, parameter), "a.eml")

    @pytest.mark.parametrize("parameter", ["boundary={}", *_EXTENDED_BOUNDARIES])
    def test_mail_within_the_cap_is_read(self, parameter):
        result = convert(_nested_multipart(5, 3, parameter), "a.eml")

        assert result.text == "Subject: s\nFrom: a@b.c\nTo: d@e.f\n\nline\nline\nline"

    def test_extended_boundaries_inside_a_closed_part_are_not_counted_again(self):
        # 40 sibling parts, each a multipart of its own declared the RFC 2231 way: one level deep at any time
        parts = "".join(f"--outer\r\nContent-Type: multipart/mixed; boundary*=us-ascii''in{number}\r\n\r\n"
                        f"--in{number}\r\nContent-Type: text/plain\r\n\r\npart {number}\r\n--in{number}--\r\n" for number in range(40))
        data = (_MAIL_HEAD + "Subject: s\r\nContent-Type: multipart/mixed; boundary=outer\r\n\r\n" + parts + "--outer--\r\n").encode()

        result = convert(data, "a.eml")

        assert "part 0" in result.text and "part 39" in result.text

    @pytest.mark.timing
    def test_deep_nesting_with_a_long_body_is_decided_quickly(self):
        data = _nested_multipart(600, 200_000, "boundary*=us-ascii''{}")

        started = time.perf_counter()
        with pytest.raises(CorruptedDocumentError):
            convert(data, "a.eml")

        assert time.perf_counter() - started < 3


def _seconds(data: bytes, file_name: str) -> tuple[float, str]:
    started = time.perf_counter()
    result = convert(data, file_name)
    return time.perf_counter() - started, result.text


class TestUnclosedImgTagWithManySources:
    """The `<img …>` branches of the Markdown picture pattern looked, for every `src=` in a tag, all the way to the end of the
    tag for its `>`. A tag that is never closed made that quadratic: 8,000 `src="data:…"` in one `<img` (234KB) took 19 s."""

    @pytest.mark.timing
    @pytest.mark.parametrize("ending", ["", "<", "<p>text</p>"])
    @pytest.mark.parametrize("source", ['src="data:image/png;base64,A" ', "src='data:image/png;base64,A' ", "src=data:image/png;base64,A ",
                                        'src="data:image/svg+xml,x" ', "src=data:image/svg+xml,x "])
    def test_tag_left_open_takes_linear_time(self, source, ending):
        text = "<img " + source * 8000 + ending

        seconds, converted = _seconds(text.encode(), "a.md")

        assert converted == text.strip()
        assert seconds < 3

    @pytest.mark.parametrize(("markdown", "expected"), [
        ('<img alt="a" src="data:image/svg+xml,%3Csvg%3E"> text', "[Image: a] text"),
        ('<img src="data:image/svg+xml;utf8,<svg xmlns=\'x\'><path d=\'M0 0\'/></svg>" alt="b"> text', "[Image: b] text"),
        ("<img src='data:image/svg+xml;utf8,<svg><text>a > b</text></svg>' alt='c'> text", "[Image: c] text"),
        ('<img src="data:image/svg+xml,x>y" alt="d"> text', "[Image: d] text"),
        ('<img src=data:image/svg+xml,x alt=e> text', "[Image: e] text"),
        ('<img src=data:image/svg+xml,x alt=f <b>text</b>', "<img src=data:image/svg+xml,x alt=f <b>text</b>"),
        ('<img src="data:image/gif,x" alt="g" <b>text</b>', '<img src="data:image/gif,x" alt="g" <b>text</b>'),
        ('<img src=data:image/gif,x src="data:image/gif,<y>" alt="h"> text', "[Image: h] text"),
    ])
    def test_pictures_that_are_not_base64_are_found_as_before(self, markdown, expected):
        assert convert(markdown.encode(), "a.md").text == expected

    def test_base64_picture_in_a_tag_is_extracted_as_before(self):
        encoded = base64.b64encode(png_bytes()).decode()

        result = convert(f'<img alt="chart" class="x" src="data:image/png;base64,{encoded}" width="3"> text'.encode(), "a.md")

        assert len(result.images) == 1
