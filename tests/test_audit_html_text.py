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
