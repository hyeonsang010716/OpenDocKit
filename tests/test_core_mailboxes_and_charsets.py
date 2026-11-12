"""Mail: an mbox that starts with an HTML mail, mail without MIME-Version, a forwarded mail wrapped in base64, encrypted mail, a
plain-text mail named .html. Character encodings: 7-bit bodies declared as ISO-2022-JP, ISO-2022-KR or UTF-7."""

import base64

import pytest

from opendockit import DocumentFormat, EncryptedDocumentError, convert
from opendockit.extractors.text import decode_text


def _b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


_HTML_MAIL = """From kim@example.com Mon Sep  1 10:00:00 2025
From: kim@example.com
To: lee@example.com
Subject: first
MIME-Version: 1.0
Content-Type: text/html; charset=utf-8

<html><body><p>FIRST BODY</p></body></html>
"""
_PLAIN_MAIL = """From park@example.com Mon Sep  1 11:00:00 2025
From: park@example.com
To: lee@example.com
Subject: second
MIME-Version: 1.0
Content-Type: text/plain; charset=utf-8

SECOND BODY
"""
_RELATED_MAIL = """From choi@example.com Mon Sep  1 12:00:00 2025
From: choi@example.com
To: lee@example.com
Subject: third
MIME-Version: 1.0
Content-Type: multipart/related; boundary="b1"

--b1
Content-Type: text/html; charset=utf-8

<p>THIRD BODY</p>
--b1--
"""
_HEADS = {"first": "Subject: first\nFrom: kim@example.com\nTo: lee@example.com\n\nFIRST BODY",
          "second": "Subject: second\nFrom: park@example.com\nTo: lee@example.com\n\nSECOND BODY",
          "third": "Subject: third\nFrom: choi@example.com\nTo: lee@example.com\n\nTHIRD BODY"}


class TestMailboxThatStartsWithAnHtmlMail:
    """An mbox whose first mail is a single text/html part or multipart/related was caught first by MHTML detection, so the later
    mails were appended to the first mail's body as raw text (text/html) or vanished (related). Like an mbox that starts with plain
    text, each mail is read under `## Message N`."""

    @pytest.mark.parametrize("order", [("first", "second", "third"), ("third", "second", "first")])
    def test_every_mail_is_read_with_its_own_head(self, order):
        mails = {"first": _HTML_MAIL, "second": _PLAIN_MAIL, "third": _RELATED_MAIL}
        data = "".join(mails[name] for name in order).encode()

        result = convert(data, "box.mbox")

        assert result.format is DocumentFormat.HTML
        assert result.text == "\n\n".join(f"## Message {number}\n\n{_HEADS[name]}" for number, name in enumerate(order, start=1))
        assert result.warnings == []

    def test_single_html_mail_after_a_separator_line_is_still_one_mail(self):
        data = _HTML_MAIL.encode()

        result = convert(data, "a.eml")

        assert result.text == _HEADS["first"]


_ENCODED_HEAD = f"From: =?UTF-8?B?{_b64('홍길동')}?= <kim@a.com>\r\nTo: lee@a.com\r\nSubject: =?UTF-8?B?{_b64('결재 요청')}?=\r\n"


class TestMailWithoutMimeVersion:
    """A mail missing the MIME-Version line (sent by old groupware or scripts) was not detected as mail, so it was text with its
    encoded subject and base64 body as is. It is read as mail when it has a sender, one of recipient, subject or date, and a mail
    Content-Type or Content-Transfer-Encoding."""

    def test_multipart_mail_without_mime_version_is_read_as_a_mail(self):
        body = (f"Content-Type: multipart/alternative; boundary=b\r\n\r\n--b\r\nContent-Type: text/plain; charset=utf-8\r\n"
                f"Content-Transfer-Encoding: base64\r\n\r\n{_b64('본문 평문 입니다')}\r\n--b\r\n"
                f"Content-Type: text/html; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n\r\n"
                f"{_b64('<html><body><p>본문 HTML 입니다</p></body></html>')}\r\n--b--\r\n")
        data = (_ENCODED_HEAD + body).encode()

        result = convert(data, "a.eml")

        assert result.format is DocumentFormat.HTML
        assert result.text == "Subject: 결재 요청\nFrom: 홍길동 <kim@a.com>\nTo: lee@a.com\n\n본문 HTML 입니다"

    def test_single_part_mail_with_only_a_transfer_encoding_is_read_as_a_mail(self):
        data = (_ENCODED_HEAD + f"Content-Transfer-Encoding: base64\r\n\r\n{_b64('평문 본문')}\r\n").encode()

        result = convert(data, "a.eml")

        assert result.format is DocumentFormat.TEXT
        assert result.text == "Subject: 결재 요청\nFrom: 홍길동 <kim@a.com>\nTo: lee@a.com\n\n평문 본문"

    def test_single_html_part_without_mime_version_keeps_the_mail_head(self):
        data = (_ENCODED_HEAD + "Content-Type: text/html; charset=utf-8\r\n\r\n<p>HTML 본문</p>\r\n").encode()

        result = convert(data, "a.eml")

        assert result.format is DocumentFormat.HTML
        assert result.text == "Subject: 결재 요청\nFrom: 홍길동 <kim@a.com>\nTo: lee@a.com\n\nHTML 본문"

    @pytest.mark.parametrize("note", [
        "From: 김팀장\nTo: 이대리\nSubject: 회의 메모\n\n내일 10시 회의입니다.\n",
        "Subject: 메모\nContent-Type: text/plain\n\n보낸 사람이 없는 글\n",
        "From: 김팀장\nContent-Transfer-Encoding: base64\n\n받는 사람·제목이 없는 글\n",
    ])
    def test_note_that_only_looks_like_a_mail_head_stays_text(self, note):
        data = note.encode()

        result = convert(data, "memo.txt")

        assert result.text == note.strip()

    def test_mbox_whose_first_mail_has_no_mime_header_is_split(self):
        first = "From kim@example.com Mon Sep  1 10:00:00 2025\nFrom: kim@example.com\nTo: lee@example.com\nSubject: plain old\n\nOLD PLAIN BODY\n\n"
        second = (f"From park@example.com Mon Sep  1 11:00:00 2025\nFrom: park@example.com\nTo: lee@example.com\n"
                  f"Subject: =?UTF-8?B?{_b64('결재')}?=\nMIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n"
                  f"Content-Transfer-Encoding: base64\n\n{_b64('두번째 메일 본문')}\n\n")
        data = (first + second).encode()

        result = convert(data, "box.mbox")

        assert result.format is DocumentFormat.TEXT
        assert result.text == ("## Message 1\n\nSubject: plain old\nFrom: kim@example.com\nTo: lee@example.com\n\nOLD PLAIN BODY\n\n"
                               "## Message 2\n\nSubject: 결재\nFrom: park@example.com\nTo: lee@example.com\n\n두번째 메일 본문")


_OUTER_HEAD = ("From: kim@a.com\r\nTo: lee@a.com\r\nSubject: test\r\nDate: Mon, 1 Sep 2025 10:00:00 +0900\r\nMIME-Version: 1.0\r\n")
_INNER_MAIL = ("From: park@a.com\r\nTo: kim@a.com\r\nSubject: 원본 메일\r\nMIME-Version: 1.0\r\n"
               "Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: 8bit\r\n\r\n원본 본문 내용입니다.\r\n")


class TestForwardedMailInATransferEncoding:
    """Exchange sometimes wraps a mail forwarded 'as attachment' (message/rfc822) in base64. The base64 came out as is in place of
    the forwarded mail."""

    @pytest.mark.parametrize("encoding", ["base64", "quoted-printable"])
    def test_forwarded_mail_wrapped_in_a_transfer_encoding_is_decoded(self, encoding):
        import quopri

        inner = _INNER_MAIL.encode()
        wrapped = base64.encodebytes(inner) if encoding == "base64" else quopri.encodestring(inner)
        data = (_OUTER_HEAD + "Content-Type: multipart/mixed; boundary=b\r\n\r\n--b\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
                "FYI\r\n--b\r\nContent-Type: message/rfc822\r\n" f"Content-Transfer-Encoding: {encoding}\r\n"
                "Content-Disposition: attachment; filename=orig.eml\r\n\r\n").encode() + wrapped + b"\r\n--b--\r\n"

        result = convert(data, "a.eml")

        assert result.text == ("Subject: test\nFrom: kim@a.com\nTo: lee@a.com\nDate: Mon, 1 Sep 2025 10:00:00 +0900\n\nFYI\n\n"
                               "## Forwarded message\n\nSubject: 원본 메일\nFrom: park@a.com\nTo: kim@a.com\n\n원본 본문 내용입니다.")
        assert result.warnings == []


class TestEncryptedMail:
    """A mail encrypted with S/MIME (top-level application/pkcs7-mime) was a 'successful' result of headers and base64 ciphertext.
    With PGP/MIME encryption (multipart/encrypted) only the headers were left. It is reported with `EncryptedDocumentError`, like a
    password-protected document."""

    @pytest.mark.parametrize("content_type", [
        "application/pkcs7-mime; smime-type=enveloped-data; name=smime.p7m",
        "application/x-pkcs7-mime; name=smime.p7m",
        'multipart/encrypted; protocol="application/pgp-encrypted"; boundary=p',
    ])
    def test_encrypted_mail_is_reported_as_encrypted(self, content_type):
        data = (_OUTER_HEAD + f"Content-Type: {content_type}\r\nContent-Transfer-Encoding: base64\r\n\r\n"
                "MIAGCSqGSIb3DQEHA6CAMIACAQAxggE=\r\n").encode()

        with pytest.raises(EncryptedDocumentError):
            convert(data, "a.eml")

    def test_signed_mail_is_still_read(self):
        data = (_OUTER_HEAD + 'Content-Type: multipart/signed; protocol="application/pkcs7-signature"; boundary=s\r\n\r\n'
                "--s\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n서명된 본문\r\n--s\r\n"
                "Content-Type: application/pkcs7-signature; name=smime.p7s\r\nContent-Transfer-Encoding: base64\r\n"
                "Content-Disposition: attachment; filename=smime.p7s\r\n\r\nMIAGCSqG\r\n--s--\r\n").encode()

        result = convert(data, "a.eml")

        assert result.text.endswith("서명된 본문")


class TestPlainMailNamedAsHtml:
    """A plain-text mail named .html/.htm went to the HTML extractor by its extension, which squashed the headers and body into one
    line. Mail detection is followed instead."""

    @pytest.mark.parametrize("filename", ["a.html", "a.htm"])
    def test_plain_mail_named_html_is_read_as_a_mail(self, filename):
        data = b"From: a@b\nTo: c@d\nSubject: s\nMIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\nbody\nsecond line\n"

        result = convert(data, filename)

        assert result.format is DocumentFormat.TEXT
        assert result.text == "Subject: s\nFrom: a@b\nTo: c@d\n\nbody\nsecond line"

    def test_html_mail_named_html_is_still_html(self):
        data = b"From: a@b\nTo: c@d\nSubject: s\nMIME-Version: 1.0\nContent-Type: text/html; charset=utf-8\n\n<p>body</p>\n"

        result = convert(data, "a.html")

        assert result.text == "Subject: s\nFrom: a@b\nTo: c@d\n\nbody"


class TestSevenBitEscapeCharsets:
    """ISO-2022-JP, ISO-2022-KR and UTF-7 are 7-bit, so strict UTF-8 decoding accepted them first and gave `$B2q5D…(B` with the
    escapes (ESC) erased. When the declared charset is one of these codecs it is used before UTF-8."""

    @pytest.mark.parametrize(("codec", "label"), [("iso2022_jp", "ISO-2022-JP"), ("iso2022_kr", "ISO-2022-KR"), ("utf-7", "UTF-7")])
    def test_mail_body_in_a_seven_bit_charset_is_decoded(self, codec, label):
        body = "会議の件、了解しました。" if codec == "iso2022_jp" else "안녕하세요 회의 자료입니다."
        data = (f"From: a@b.jp\r\nTo: c@d.jp\r\nSubject: t\r\nMIME-Version: 1.0\r\nContent-Type: text/plain; charset={label}\r\n"
                "Content-Transfer-Encoding: 7bit\r\n\r\n").encode() + body.encode(codec)

        result = convert(data, "a.eml")

        assert result.text == f"Subject: t\nFrom: a@b.jp\nTo: c@d.jp\n\n{body}"

    def test_html_meta_charset_iso_2022_jp_is_used(self):
        data = '<html><head><meta charset="iso-2022-jp"></head><body><p>会議の件</p></body></html>'.encode("iso2022_jp")

        result = convert(data, "a.html")

        assert result.text == "会議の件"

    def test_plain_ascii_declared_as_utf7_is_left_alone(self):
        data = b"price 1+2 = 3, a+b"

        text, _ = decode_text(data, "utf-7")

        assert text == "price 1+2 = 3, a+b"
