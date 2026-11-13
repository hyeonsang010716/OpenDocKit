"""Audited defects of the HTML and text extractors: single values that got a whole document rejected, content that was lost or
duplicated without a warning, and markup of a few hundred KB that took many seconds."""

import base64
import time

import pytest

from opendockit import convert
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
