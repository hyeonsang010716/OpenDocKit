"""A single wrong mail or HTML value got the whole document rejected or the body lost: a Western European subject written in
ISO-8859-1, a NUL byte in the charset, a multipart without a boundary, a bounce report, numbers of more than 4,300 digits, very
deeply nested multipart."""

import base64

import pytest

from opendockit import convert
from opendockit.exceptions import CorruptedDocumentError
from tests.timing import fastest


_HEAD = "From: a@b.c\r\nTo: d@e.f\r\nSubject: test\r\nMIME-Version: 1.0\r\n"


class TestLatinSubjectWithAccentedLetterPairs:
    """Because `=?iso-8859-1?…?=` words were tried with strict CP949 decoding first, a Western European word with two adjacent
    non-ASCII letters (ção, öß, üß) decoded as one CP949 Hanja character and `Informações` became `Informa寤es`. When the text read
    as CP949 is not everyday Hangul, it is read as declared."""

    @pytest.mark.parametrize("word", ["Informações", "Größe", "Grüße", "Ação", "São Paulo"])
    def test_western_word_labeled_latin1_keeps_its_letters(self, word):
        encoded = base64.b64encode(word.encode("latin-1")).decode()
        data = (f"From: a@b.c\r\nTo: d@e.f\r\nSubject: =?iso-8859-1?b?{encoded}?=\r\nMIME-Version: 1.0\r\n"
                "Content-Type: text/plain; charset=utf-8\r\n\r\nbody\r\n").encode()

        result = convert(data, "a.eml")

        assert result.text.startswith(f"Subject: {word}\n")


class TestCharsetWithANulByte:
    """`codecs.lookup` raised ValueError on a NUL byte in the charset parameter (`utf-8\\x00`), so the whole mail was rejected as
    'corrupted'."""

    @pytest.mark.parametrize("kind", ["plain", "html"])
    def test_body_is_read(self, kind):
        data = (_HEAD.encode() + f"Content-Type: text/{kind}; charset=\"utf-8\x00\"\r\n\r\n<p>본문 글</p>\r\n".encode())

        result = convert(data, "a.eml")

        assert "본문 글" in result.text


_BOUNCE = ("From: MAILER-DAEMON@mx.example.com\r\nTo: kim@example.com\r\nSubject: Undelivered Mail Returned to Sender\r\n"
           "MIME-Version: 1.0\r\nContent-Type: multipart/report; report-type=delivery-status; boundary=\"R\"\r\n\r\n"
           "--R\r\nContent-Type: text/plain\r\n\r\nYour message could not be delivered.\r\n"
           "--R\r\nContent-Type: message/delivery-status\r\n\r\nReporting-MTA: dns; mx.example.com\r\n\r\n"
           "Final-Recipient: rfc822; lee@example.org\r\nAction: failed\r\nStatus: 5.1.1\r\n--R--\r\n")
_WITHOUT_BOUNDARY = ("From: old@example.com\r\nTo: kim@example.com\r\nSubject: quote\r\nMIME-Version: 1.0\r\n"
                     "Content-Type: multipart/mixed\r\n\r\nThe quotation is 1,200,000 won.\r\n")


def _forwarding(inner: str) -> bytes:
    return ("From: kim@example.com\r\nTo: it@example.com\r\nSubject: Fwd: check\r\nMIME-Version: 1.0\r\n"
            "Content-Type: multipart/mixed; boundary=\"OUT\"\r\n\r\n--OUT\r\nContent-Type: text/plain\r\n\r\n"
            "Please check the attached mail.\r\n--OUT\r\nContent-Type: message/rfc822\r\nContent-Disposition: attachment\r\n\r\n"
            + inner + "\r\n--OUT--\r\n").encode()


class TestMultipartSplitAtTheDelimiterInUse:
    """A mail that declares one boundary and writes another had no part for the parser, so its body was text: a mail of one
    HTML part in base64 came out as the delimiter line, two header lines and 23,000 characters of base64, with no warning."""

    _HTML = base64.b64encode("<html><body><p>회의는 내일 오전 열 시입니다.</p></body></html>".encode()).decode()

    def _mail(self, content_type: str, body: str) -> bytes:
        return (_HEAD + f"Content-Type: {content_type}\r\n\r\n{body}").encode()

    @pytest.mark.parametrize("content_type", ['multipart/alternative; boundary="AAA"', "multipart/alternative", "multipart/mixed"],
                             ids=["another-boundary", "no-boundary", "mixed"])
    def test_parts_are_read_at_the_delimiter_the_body_uses(self, content_type):
        body = f"--BBB\r\nContent-Type: text/html; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n\r\n{self._HTML}\r\n--BBB--\r\n"

        result = convert(self._mail(content_type, body), "a.eml")

        assert result.text.endswith("회의는 내일 오전 열 시입니다.")
        assert "Content-Transfer-Encoding" not in result.text and self._HTML[:20] not in result.text

    def test_closing_line_of_another_boundary_closes_the_last_part(self):
        """Left in the last part, the line is decoded with it as base64 and ends the text in bytes of nothing."""
        body = ("\r\n----=_One\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n첫째 부분\r\n"
                f"----=_One\r\nContent-Type: text/html; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n\r\n{self._HTML}\r\n"
                "\r\n----=_Another--\r\n")

        result = convert(self._mail('multipart/mixed; boundary="--=_Declared"', body), "a.eml")

        assert result.text.endswith("첫째 부분\n\n회의는 내일 오전 열 시입니다.")

    @pytest.mark.parametrize("body", [
        "본문 글 하나\r\n--BBB\r\nContent-Type: text/plain\r\n\r\n둘\r\n--BBB--\r\n",
        "--BBB\r\n본문 글 하나\r\nContent-Type: text/plain\r\n",
        "-- \r\n김철수 드림\r\nContent-Type: none\r\n",
        "--\r\nContent-Type: text/plain\r\n\r\n본문\r\n",
    ], ids=["text-before-the-delimiter", "no-header-under-the-delimiter", "signature-mark", "delimiter-without-a-boundary"])
    def test_body_that_does_not_open_with_a_part_stays_text(self, body):
        result = convert(self._mail('multipart/mixed; boundary="AAA"', body), "a.eml")

        assert [line.strip() for line in body.split("\r\n") if line.strip()] == [
            line for line in result.text.split("\n\n", 1)[1].split("\n") if line.strip()]

    @pytest.mark.parametrize("body", [
        "--BBB\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n본문\r\n--BBB--\r\n이 꼬리말은 목록 서버가 붙였습니다.\r\n",
        "--BBB\r\nContent-Type: text/plain\r\n\r\nOne\r\n--BBB--\r\n\r\n--CCC\r\nContent-Type: text/plain\r\n\r\nTwo\r\n--CCC--\r\n",
        "--Summary\r\nContent-wise: we agreed on three points.\r\nFirst, the budget.\r\n",
        "---\r\nContent-Type: text/plain\r\n\r\nTitle\r\n---\r\nSection two\r\n---\r\n",
        "--BBB\r\nContent-Type: text/plain\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\nRegards\r\n--Kim--\r\n",
        "--BBB\r\nContent-Type: multipart/alternative; boundary=X\r\n\r\nInner preamble line.\r\n--X\r\nContent-Type: text/plain\r\n\r\n"
        "hello\r\n--X--\r\nInner footer line.\r\n--BBB--\r\n",
        "--BBB\nContent-Type: text/plain\n\nHello\nContent-Transfer-Encoding: base64\r\n\r\nmore text\r\n--Kim--\r\n",
        "--BBB\r\nContent-Type: text/plain\r\n\r\nHello text\r\n--BBBx\r\nContent-Transfer-Encoding: base64\r\n\r\nquoted line\r\n--Kim--\r\n",
        "--BBB\r\nContent-Type: text/plain\r\nSummary: three points were agreed.\r\n\r\nFirst, the budget.\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: text/plain\r\n  an indented sentence that was shown.\r\n\r\nFirst, the budget.\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: text/calendar\r\n\r\nLine one of the part.\r\nLine two of the part.\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: application/json\r\n\r\nLine one of the part.\r\n--BBB--\r\n",
    ], ids=["text-behind-the-closing-line", "pieces-under-another-token", "sentence-that-looks-like-a-header", "rules-of-dashes",
            "signature-shaped-like-a-closing-line", "text-around-the-parts-of-a-multipart-inside",
            "line-that-says-base64-inside-a-text-part", "token-that-begins-another-line", "sentence-among-the-headers-of-a-part",
            "sentence-folded-into-a-header", "part-of-a-type-that-is-not-read", "part-that-is-no-text-and-has-no-name"])
    def test_no_line_that_was_shown_is_lost(self, body):
        """A reader of MIME drops what stands behind the closing line and reads a header where a sentence was; as text all of
        it was shown, so such a body stays text, and a last line that only looks like a closing line stays in its part."""
        result = convert(self._mail('multipart/mixed; boundary="AAA"', body), "a.eml")

        shown = result.text.split("\n\n", 1)[1]
        for line in body.replace("\r\n", "\n").split("\n"):
            if not line.startswith(("--BBB", "--X", "Content-T")) or line.startswith("--BBBx"):
                assert line.strip() in shown

    _B64 = base64.b64encode(b"Hello from the body of the mail.").decode()

    @pytest.mark.parametrize("body", [
        "--BBB\r\nContent-Type: application/pdf; name=a.pdf\r\nContent-ID: <a@b>\r\n\r\nLine one that was shown.\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: application/octet-stream; name=notes\r\nContent-Location: notes\r\n\r\nLine one that was shown.\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: application/pgp-signature; name=sig.asc\r\n\r\nLine one that was shown.\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: message/partial; id=x; number=1; total=2\r\n\r\nLine one that was shown.\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: message/x-note\r\n\r\nLine one that was shown.\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: text/plain\r\nContent-wise: we agreed on three points.\r\n\r\nFirst, the budget.\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: text/plain;\r\n  an indented sentence that was shown.\r\n\r\nFirst, the budget.\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: text/plain\r\nContent-Transfer-Encoding: 8bit\r\n  an indented sentence that was shown.\r\n\r\nFirst.\r\n--BBB--\r\n",
        f"--BBB\r\nContent-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\n{_B64}\r\n-- \r\nSent through the list of the office.\r\n",
        f"--BBB\r\nContent-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\n{_B64}\r\nSent by the list\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: text/plain\r\n\r\nFirst part.\r\n--BBB\r\nContent-Type: multipart/mixed\r\nContent-Transfer-Encoding: base64\r\n\r\n"
        "A line that was shown, inside.\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: text/plain\r\n\r\nFirst part.\r\n--BBB\r\nContent-Type: multipart/mixed\r\nContent-Disposition: attachment\r\n\r\n"
        "A line that was shown, inside.\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: message/delivery-status\r\nA line without a colon\r\nContent-wise: a sentence\r\n\r\nAction: failed\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: message/delivery-status\r\n\r\nReporting-MTA: dns; a.b\r\n\r\nAction: failed\r\nA sentence among the fields.\r\n"
        "More of it.\r\n--BBB--\r\n",
        f"--BBB\r\nContent-Type: message/rfc822\r\n\r\nSubject: inner\r\nContent-Transfer-Encoding: base64\r\n\r\n{_B64}\r\n"
        "The rest of the mail, which no delimiter ended.\r\n",
        "--BBB\r\nContent-Type: image/png; name=p.png\r\n\r\nLine one.\r\n--CCC\r\nContent-Type: text/plain\r\n\r\nThe body of the mail.\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: text/rfc822-headers\r\n\r\nSubject: bounced\r\nFrom: x@y.z\r\n\r\nA line that was shown.\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: message/rfc822\r\n\r\nSubject: inner\r\nContent-Type: multipart/mixed; boundary=IN\r\n\r\n"
        "--IN\r\nContent-Type: text/plain\r\n\r\nInner body.\r\n--IN--\r\nA line behind the closing line of the mail inside.\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: message/rfc822\r\n\r\nSubject: inner\r\nContent-Type: multipart/mixed; boundary=IN\r\n\r\n"
        "A line before its first part.\r\n--IN\r\nContent-Type: text/plain\r\n\r\nInner body.\r\n--IN--\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: message/rfc822\r\n\r\nSubject: inner\r\nContent-Type: multipart/mixed; boundary=IN\r\n\r\n"
        "--IN\r\nContent-Type: message/partial\r\n\r\nA line that was shown.\r\n--IN--\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: message/rfc822\r\n\r\nSubject: inner\r\nContent-Type: application/x-thing\r\n\r\nA line that was shown.\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: message/rfc822\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\nSubject: inner\r\n\r\nInner body.\r\n--BBB--\r\n",
        f"--BBB\r\nContent-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\n{_B64}\r\nRegards\r\nKim\r\n--BBB--\r\n",
        f"--BBB\r\nContent-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\n{_B64}\r\n\r\nUnsubscribe\r\n",
        f"--BBB\r\nContent-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\n{_B64[:20]}\r\n{_B64[20:28]}\r\n{_B64[28:]}\r\n--BBB--\r\n",
        f"--BBB\r\nContent-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\n{_B64}\r\n-- Sent through the list of the office --\r\n",
        "--BBB\r\nContent-Type: text/plain;\r\n  x=1 is what the indented sentence began with.\r\n\r\nFirst, the budget.\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: text/plain\r\n  and a sentence under the type.\r\n\r\nFirst, the budget.\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: text/x-thing\r\nContent-Transfer-Encoding: base64\r\n\r\n" + _B64 + "\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: text/plain\r\nContent-Transfer-Encoding: 8bit;\r\n x=1\r\n\r\nFirst, the budget.\r\n--BBB--\r\n",
    ], ids=["named-part-the-body-points-at", "named-part-with-a-location", "signature", "partial-message", "message-of-no-known-kind",
            "sentence-under-a-header-of-a-part", "sentence-folded-behind-a-semicolon", "sentence-folded-under-another-header",
            "footer-behind-base64", "line-of-words-in-base64", "multipart-inside-under-base64", "multipart-inside-set-aside",
            "headers-ending-at-a-line-that-is-none", "report-with-a-sentence", "forwarded-mail-in-base64-with-the-rest-behind",
            "attachment-that-holds-the-rest-of-the-mail", "returned-headers-with-text-behind", "text-behind-the-mail-inside",
            "text-before-the-parts-of-the-mail-inside", "unread-part-of-the-mail-inside", "mail-inside-that-is-no-text",
            "mail-inside-under-an-encoding", "words-behind-base64", "a-word-behind-base64", "lines-of-base64-of-other-lengths",
            "words-between-dashes-behind-base64", "folded-sentence-with-an-equals-sign", "sentence-folded-under-a-type-without-a-semicolon",
            "text-of-another-kind-in-base64", "parameter-under-a-header-that-has-none"])
    def test_body_a_split_would_read_less_of_stays_text(self, body):
        """Each of these came out shorter, or as nothing, once it was split: a part neither read nor reported, a sentence taken
        for a header, lines decoded as base64, the rest of the mail set aside as one attachment."""
        result = convert(self._mail('multipart/mixed; boundary="AAA"', body), "a.eml")

        shown = result.text.split("\n\n", 1)[1]
        for line in body.replace("\r\n", "\n").split("\n"):
            assert line.strip() in shown

    @pytest.mark.parametrize("parts", [
        "--BBB\r\nContent-Type: text/html\r\n\r\n<p>Page form.</p>\r\n--BBB\r\nContent-Type: message/delivery-status\r\n\r\n"
        "Reporting-MTA: dns; a.b\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: text/html\r\n\r\n<p>First page.</p>\r\n--BBB\r\nContent-Type: text/html\r\n\r\n<p>Second page.</p>\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: text/plain\r\n\r\nOne text.\r\n--BBB\r\nContent-Type: text/plain\r\n\r\nAnother text.\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: multipart/mixed; boundary=M1\r\n\r\n--M1\r\nContent-Type: text/html\r\n\r\n<p>First page.</p>\r\n--M1--\r\n"
        "--BBB\r\nContent-Type: multipart/mixed; boundary=M2\r\n\r\n--M2\r\nContent-Type: text/html\r\n\r\n<p>Second page.</p>\r\n--M2--\r\n--BBB--\r\n",
        "--BBB\r\nContent-Type: text/html\r\n\r\n<p>First page.</p>\r\n"
        "--BBB\r\nContent-Type: multipart/related; boundary=M2\r\n\r\n--M2\r\nContent-Type: text/html\r\n\r\n<p>Second page.</p>\r\n--M2--\r\n--BBB--\r\n",
    ], ids=["a-part-that-is-no-form-of-the-text", "two-pages", "two-texts", "two-multiparts", "a-page-and-a-multipart"])
    def test_alternatives_that_are_not_one_text_in_two_forms_stay_text(self, parts):
        """Of the parts of a multipart/alternative one is read."""
        result = convert(self._mail('multipart/alternative; boundary="AAA"', parts), "a.eml")

        assert "--BBB" in result.text and all(line in result.text for line in parts.replace("\r\n", "\n").split("\n") if line)

    def test_plain_and_html_form_of_one_text_are_split(self):
        parts = ("--BBB\r\nContent-Type: text/plain\r\n\r\nPlain form.\r\n--BBB\r\nContent-Type: text/html;\r\n\tcharset=\"utf-8\"\r\n\r\n"
                 "<p>Page form.</p>\r\n--BBB--\r\n")

        result = convert(self._mail('multipart/alternative; boundary="AAA"', parts), "a.eml")

        assert result.text.endswith("Page form.") and "--BBB" not in result.text

    @pytest.mark.parametrize("header", ['Content-Type: text/plain;\r\n\tcharset="utf-8"', "Content-Type: text/plain;\r\n charset=utf-8;\r\n format=flowed",
                                        "Content-Type: text/plain\r\nContent-ID:\r\n <part1.0123@example.org>",
                                        "Content-Type: text/plain\r\nContent-Description: notes\r\nContent-MD5: Q2hlY2sgSW50ZWdyaXR5IQ==",
                                        "Content-Type: text/plain\r\nMIME-Version: 1.0\r\nContent-Transfer-Encoding: 7bit"],
                             ids=["parameter-on-its-own-line", "two-parameters", "id-on-its-own-line", "description-and-checksum", "version"])
    def test_headers_a_part_has_do_not_keep_it_from_being_split(self, header):
        body = f"--BBB\r\n{header}\r\n\r\nHello body\r\n--BBB--\r\n"

        result = convert(self._mail('multipart/mixed; boundary="AAA"', body), "a.eml")

        assert result.text.endswith("Hello body") and "--BBB" not in result.text

    @pytest.mark.parametrize("encoding", ["7bit", "8bit", "binary"])
    def test_body_under_an_encoding_that_changes_nothing_is_split(self, encoding):
        body = "--BBB\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nHello body\r\n--BBB--\r\n"
        data = (_HEAD + f'Content-Type: multipart/mixed; boundary="AAA"\r\nContent-Transfer-Encoding: {encoding}\r\n\r\n' + body).encode()

        assert convert(data, "a.eml").text.endswith("Hello body")

    def test_plain_form_beside_a_page_with_its_picture_is_split(self):
        """The usual shape of a mail with pictures: the picture the page points at is data in base64, which was never text."""
        picture = base64.b64encode(bytes(range(256)) * 2).decode()
        lines = "\r\n".join(picture[at:at + 76] for at in range(0, len(picture), 76))
        parts = ("--BBB\r\nContent-Type: text/plain\r\n\r\nPlain form.\r\n--BBB\r\nContent-Type: multipart/related; boundary=REL\r\n\r\n"
                 '--REL\r\nContent-Type: text/html\r\n\r\n<p>Page form.</p><img src="cid:a@b">\r\n'
                 "--REL\r\nContent-Type: image/png; name=p.png\r\nContent-ID: <a@b>\r\nContent-Transfer-Encoding: base64\r\n\r\n"
                 f"{lines}\r\n--REL--\r\n--BBB--\r\n")

        result = convert(self._mail('multipart/alternative; boundary="AAA"', parts), "a.eml")

        assert "Page form." in result.text and "--BBB" not in result.text and picture[:30] not in result.text

    def test_signed_mail_is_split(self):
        signature = base64.b64encode(b"signature " * 12).decode()
        body = ("--BBB\r\nContent-Type: text/plain\r\n\r\nThe signed text.\r\n--BBB\r\nContent-Type: application/pkcs7-signature; name=smime.p7s\r\n"
                f"Content-Transfer-Encoding: base64\r\nContent-Disposition: attachment; filename=smime.p7s\r\n\r\n{signature}\r\n--BBB--\r\n")

        result = convert(self._mail('multipart/signed; boundary="AAA"', body), "a.eml")

        assert result.text.endswith("The signed text.") and not result.warnings

    @pytest.mark.parametrize("header", [
        'Content-Type: text/plain;\r\n name="=?utf-8?B?7ZqM7J2Y66Gd?=\r\n =?utf-8?B?LnR4dA==?="\r\nContent-Disposition: inline',
        "Content-Type: text/plain;\r\n name==?utf-8?B?7ZqM7J2Y66Gd?=\r\n =?utf-8?B?LnR4dA==?=\r\nContent-Disposition: inline",
        'Content-Type: text/plain\r\nContent-Disposition: inline;\r\n filename="a long name\r\n that goes on.txt"',
        "Content-Type: text/plain\r\nContent-Description: notes of the meeting\r\n that go on",
        "Content-Type: text/plain\r\nContent-Features: (color=yes)\r\nContent-Duration: 3\r\nContent-Length: 12",
        "Content-Type: text/plain\r\nX-Attachment-Id: f_abc123",
        "Content-Type: text/plain\r\nContent-Location: notes.txt\r\nContent-Language: en\r\nContent-Base: http://a.example/\r\nContent-Class: x",
    ], ids=["name-in-encoded-words-over-two-lines", "the-same-without-quotes", "name-in-quotes-over-two-lines", "description-over-two-lines",
            "features-duration-length", "id-of-an-attachment", "location-language-base-class"])
    def test_headers_mailers_write_into_a_part_do_not_keep_it_from_being_split(self, header):
        body = f"--BBB\r\n{header}\r\n\r\nHello body\r\n--BBB--\r\n"

        result = convert(self._mail('multipart/mixed; boundary="AAA"', body), "a.eml")

        assert result.text.endswith("Hello body") and "--BBB" not in result.text

    @pytest.mark.parametrize("data", [
        "{0}\r\n=\r\n", " {0}\r\n", "{0} \r\n", "\r\n{0}\r\n\r\n",
    ], ids=["padding-on-its-own-line", "indented", "space-behind", "blank-lines-around"])
    def test_base64_written_in_these_ways_is_base64(self, data):
        text = base64.b64encode(b"Hello from the body.").decode()   # 28 letters, one of them padding
        body = "--BBB\r\nContent-Type: text/plain\r\nContent-Transfer-Encoding: base64\r\n\r\n" + data.format(
            text.rstrip("=") if "=\r\n" in data else text) + "--BBB--\r\n"

        result = convert(self._mail('multipart/mixed; boundary="AAA"', body), "a.eml")

        assert result.text.endswith("Hello from the body.")

    @pytest.mark.parametrize(("body", "base64_only"), [
        ("QUJDREVGR0g=\n", True), ("QUJD\nREVG\nR0g=\n", True), ("QUJD\r\nREVG\r\nR0g=", True), ("QUJD\rREVG\r", True),
        ("QUJDREVGR0g\n=\n", True), ("  QUJD\n  REVG\n", True), ("QUJD\x00\x00\n", True), ("", True),
        ("QUJD\nREVGR0hJ\n", False), ("QUJDR\n", False), ("QUJD\nRE=G\n", False), ("QU==\nQUJD\n", False),
        ("QUJD QUJD\nQUJD\n", False), ("QUJDQUJD\nQUJ DQUJ\n", False), ("QUJD\nQUI.\n", False), ("QUJD\nQUJDQUJD\nQUJD\n", False),
    ], ids=["one-line", "lines", "cr-lf", "cr", "padding-on-its-own-line", "indented", "nul-behind", "empty",
            "last-line-longer", "letters-that-are-no-bytes", "equals-sign-inside", "padding-before-the-end",
            "two-groups-on-a-line", "two-groups-on-the-last-line", "character-of-no-base64", "lines-of-other-lengths"])
    def test_body_in_base64_is_asked_whether_it_is_only_base64(self, body, base64_only):
        import email

        from opendockit.extractors import _mail

        part = email.message_from_string("Content-Type: text/plain\nContent-Transfer-Encoding: base64\n\n" + body)

        assert _mail._is_all_base64(part) is base64_only

    def test_attachment_among_the_parts_is_reported(self):
        body = ("--BBB\r\nContent-Type: text/plain\r\n\r\nThe body.\r\n--BBB\r\nContent-Type: application/pdf; name=a.pdf\r\n"
                f"Content-Transfer-Encoding: base64\r\n\r\n{self._B64}\r\n--BBB--\r\n")

        result = convert(self._mail('multipart/mixed; boundary="AAA"', body), "a.eml")

        assert result.text.endswith("The body.") and any("a.pdf" in warning for warning in result.warnings)

    def test_forwarded_mail_and_report_among_the_parts_are_read(self):
        body = ("--BBB\r\nContent-Type: message/delivery-status\r\n\r\nReporting-MTA: dns; a.b\r\n\r\nAction: failed\r\n"
                "--BBB\r\nContent-Type: message/rfc822\r\n\r\nSubject: inner\r\nContent-Transfer-Encoding: base64\r\n\r\n"
                f"{self._B64}\r\n--BBB--\r\n")

        result = convert(self._mail('multipart/mixed; boundary="AAA"', body), "a.eml")

        assert "Action: failed" in result.text and "Hello from the body of the mail." in result.text and "--BBB" not in result.text

    @pytest.mark.parametrize("ending", ["\r\n--One--\r\n--Two--\r\n", "\r\n--One--\r\n \t\r\n--Two-- \r\n\r\n", "\x00\x00\r\n\r\n--One--\r\n"],
                             ids=["two-closing-lines", "blank-lines-between", "padding-of-nul-bytes"])
    def test_every_closing_line_behind_base64_is_taken_out(self, ending):
        body = f"--BBB\r\nContent-Type: text/html; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n\r\n{self._HTML}{ending}"

        result = convert(self._mail('multipart/mixed; boundary="AAA"', body), "a.eml")

        assert result.text.endswith("회의는 내일 오전 열 시입니다.")

    def test_byte_outside_ascii_in_base64_keeps_the_body_text(self):
        """The standard library reads such a body in the charset of the part and puts a mark for the byte: cut and written back,
        the mark was decoded as base64 letters and everything behind it came out wrong."""
        html = base64.b64encode(("<html><body>" + "".join(f"<p>Paragraph number {number} of the letter.</p>" for number in range(12))
                                 + "</body></html>").encode()).decode()
        body = ("--BBB\r\nContent-Type: text/html; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n\r\n"
                f"{html[:40]}\udce9{html[40:]}\r\n--CCC--\r\n")
        data = (_HEAD + 'Content-Type: multipart/mixed; boundary="AAA"\r\n\r\n' + body).encode("ascii", "surrogateescape")

        assert "--BBB" in convert(data, "a.eml").text

    def test_last_part_with_a_charset_that_cannot_be_named_is_read_as_it_is(self):
        """Looking at the last part for a stray closing line reads its text, and a charset with a NUL in it raised there:
        the mail, read before, was refused as corrupted."""
        body = ("--BBB\r\nContent-Type: text/plain; charset=\x00euc-kr\r\nContent-Transfer-Encoding: base64\r\n\r\n"
                "\udcc3\udcb9\r\n--Other--\r\n")
        data = (_HEAD + 'Content-Type: multipart/mixed; boundary="AAA"\r\n\r\n' + body).encode("ascii", "surrogateescape")

        assert convert(data, "a.eml").text.startswith("Subject: test")

    def test_blank_lines_before_the_delimiter_are_skipped(self):
        body = "\r\n" * 2000 + "--BBB\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n본문 글\r\n--BBB--\r\n"

        assert convert(self._mail('multipart/mixed; boundary="AAA"', body), "a.eml").text.endswith("본문 글")

    def test_parts_nested_deeper_than_a_split_may_hold_stay_text(self):
        nested = "".join(f'--B{level}\r\nContent-Type: multipart/mixed; boundary="B{level + 1}"\r\n\r\n' for level in range(6))
        result = convert(self._mail('multipart/mixed; boundary="AAA"', nested + "본문\r\n"), "a.eml")

        assert "--B0" in result.text and "본문" in result.text

    def test_body_under_a_transfer_encoding_is_not_split(self):
        """The body of a multipart under base64 or quoted-printable is not its bytes as written."""
        body = "--BBB\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nHello body\r\n--BBB--\r\n"
        data = (_HEAD + 'Content-Type: multipart/mixed; boundary="AAA"\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\n'
                + body).encode()

        assert "--BBB" in convert(data, "a.eml").text

    def test_header_of_a_part_is_known_in_any_case(self):
        body = "--BBB\r\ncontent-type: text/plain; charset=utf-8\r\n\r\nHello body\r\n--BBB--\r\n"

        assert convert(self._mail('multipart/mixed; boundary="AAA"', body), "a.eml").text.endswith("Hello body")

    def test_body_parsed_again_counts_also_when_no_part_came_of_it(self):
        """Two bodies that are parsed again and stay text use the allowance up: the third is not parsed."""
        wrong = "--B%d\r\nContent-Type: text/plain\r\nSummary: no header of a part\r\n\r\ntext %d\r\n--B%d--\r\n"
        right = "--B3\r\nContent-Type: text/plain\r\n\r\nthird body\r\n--B3--\r\n"
        parts = "".join(f'--OUT\r\nContent-Type: multipart/mixed; boundary="X{number}"\r\n\r\n{body}'
                        for number, body in ((1, wrong % (1, 1, 1)), (2, wrong % (2, 2, 2)), (3, right)))
        result = convert((_HEAD + 'Content-Type: multipart/mixed; boundary="OUT"\r\n\r\n' + parts + "--OUT--\r\n").encode(), "a.eml")

        assert "--B3" in result.text and "third body" in result.text

    def test_mail_forwarded_inside_is_split_too(self):
        inner = (_HEAD + 'Content-Type: multipart/mixed; boundary="AAA"\r\n\r\n'
                 "--BBB\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n전달된 본문\r\n--BBB--\r\n")
        data = (_HEAD + 'Content-Type: multipart/mixed; boundary="OUT"\r\n\r\n--OUT\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n'
                f"겉 본문\r\n--OUT\r\nContent-Type: message/rfc822\r\n\r\n{inner}\r\n--OUT--\r\n").encode()

        result = convert(data, "a.eml")

        assert "전달된 본문" in result.text and "--BBB" not in result.text

    def test_parts_nested_too_deep_inside_stay_text(self):
        """The nesting is counted before the body is parsed again: past the limit nothing is split and nothing is refused."""
        nested = "".join(f'--B{level}\r\nContent-Type: multipart/mixed; boundary="B{level + 1}"\r\n\r\n' for level in range(40))
        result = convert(self._mail('multipart/mixed; boundary="AAA"', nested + "본문\r\n"), "a.eml")

        assert "--B0" in result.text and "본문" in result.text

    def test_only_a_few_multiparts_of_a_mail_are_split_again(self):
        """Each wrong boundary inside the last costs one more parse of what is left; from the third on the body is text."""
        body = "".join(f'--U{level}\r\nContent-Type: multipart/mixed; boundary="D{level}"\r\n\r\n' for level in range(8))
        result = convert(self._mail('multipart/mixed; boundary="AAA"', body + "본문\r\n"), "a.eml")

        assert "본문" in result.text and "--U1" not in result.text and "--U2" in result.text


class TestReadablePartsOfForwardedMails:
    """The delivery report of a bounce and the body of a multipart without a usable boundary are made readable before the
    body is looked for, and that was done for the outer mail only: a bounce forwarded to the IT desk as an attachment lost its
    report, and a forwarded mail without a usable boundary its body (with a warning of an unnamed attachment), while the same
    mails on their own read whole."""

    def test_parts_of_a_report_are_read(self):
        """A read receipt's `message/disposition-notification` and a bounce's `text/rfc822-headers` (the headers of the mail
        that bounced) vanished without a warning. The receipt is `name: value` lines, as a delivery report is; of the
        returned headers only those written before a body are kept, the rest being routing and signatures."""
        data = ("From: lee@example.org\r\nTo: kim@example.com\r\nSubject: Read: Q3\r\nMIME-Version: 1.0\r\n"
                "Content-Type: multipart/report; report-type=disposition-notification; boundary=B\r\n\r\n"
                "--B\r\nContent-Type: text/plain\r\n\r\nYour message was displayed.\r\n"
                "--B\r\nContent-Type: message/disposition-notification\r\n\r\nFinal-Recipient: rfc822; lee@example.org\r\n"
                "Disposition: manual-action/MDN-sent-manually; displayed\r\n\r\n--B\r\nContent-Type: text/rfc822-headers\r\n\r\n"
                "Received: from a by b\r\nSubject: Q3 contract\r\nFrom: kim@example.com\r\nDKIM-Signature: v=1; a=rsa\r\n--B--\r\n")

        result = convert(data.encode(), "a.eml")

        assert result.text.endswith("Your message was displayed.\n\nFinal-Recipient: rfc822; lee@example.org\n"
                                    "Disposition: manual-action/MDN-sent-manually; displayed\n\nSubject: Q3 contract\n"
                                    "From: kim@example.com")
        assert result.warnings == []

    @pytest.mark.parametrize(("inner", "expected"), [
        (_BOUNCE, "Your message could not be delivered.\n\nReporting-MTA: dns; mx.example.com\n\n"
                  "Final-Recipient: rfc822; lee@example.org\nAction: failed\nStatus: 5.1.1"),
        (_WITHOUT_BOUNDARY, "The quotation is 1,200,000 won."),
    ], ids=["bounce", "multipart without a boundary"])
    def test_forwarded_mail_reads_as_it_does_on_its_own(self, inner, expected):
        alone = convert(inner.encode(), "a.eml")
        result = convert(_forwarding(inner), "a.eml")

        assert alone.text.endswith(expected)
        assert result.text.endswith(expected)
        assert result.warnings == []


class TestMultipartWithoutUsableBoundary:
    """A multipart whose boundary is missing or never appears in the body has no parts, so the body vanished without a warning and
    only the headers were left."""

    @pytest.mark.parametrize("content_type", ["multipart/mixed", 'multipart/mixed; boundary="XX"'])
    def test_body_without_parts_is_kept(self, content_type):
        data = (_HEAD + f"Content-Type: {content_type}\r\n\r\n본문 글 하나 둘 셋\r\n").encode()

        result = convert(data, "a.eml")

        assert "본문 글 하나 둘 셋" in result.text

    def test_delivery_status_of_a_bounce_report_is_kept(self):
        """The message/delivery-status (recipient, status code, diagnostic) of a bounce report (multipart/report) was dropped
        without a trace."""
        data = (_HEAD + 'Content-Type: multipart/report; report-type=delivery-status; boundary="R"\r\n\r\n'
                "--R\r\nContent-Type: text/plain\r\n\r\nDelivery failed.\r\n"
                "--R\r\nContent-Type: message/delivery-status\r\n\r\nReporting-MTA: dns; mx.example\r\n\r\n"
                "Final-Recipient: rfc822; kim@example.com\r\nStatus: 5.1.1\r\nDiagnostic-Code: smtp; 550 5.1.1 user unknown\r\n"
                "--R--\r\n").encode()

        result = convert(data, "a.eml")

        assert "Delivery failed." in result.text
        assert "Final-Recipient: rfc822; kim@example.com" in result.text
        assert "550 5.1.1 user unknown" in result.text

    @pytest.mark.timing
    @pytest.mark.parametrize("build", [
        lambda count: _HEAD + "Content-Type: multipart/mixed" + ";x" * count + "\r\n\r\nbody\r\n",
        lambda count: _HEAD + "Content-Type: multipart/mixed" + "".join(f";x{index}=y" for index in range(count)) + "\r\n\r\nbody\r\n",
        lambda count: (_HEAD + 'Content-Type: multipart/report; boundary="R"\r\n\r\n--R\r\nContent-Type: text/plain\r\n\r\nfailed\r\n'
                       "--R\r\nContent-Type: message/delivery-status" + "".join(f";x{index}=y" for index in range(count))
                       + "\r\n\r\nReporting-MTA: dns; x\r\n\r\nFinal-Recipient: rfc822; a@b\r\n--R--\r\n"),
    ], ids=["bare", "distinct", "delivery report"])
    def test_many_parameters_take_linear_time(self, build):
        """Turned into text/plain with `Message.set_type`, which adds every parameter back with `set_param` and parses the whole
        header again each time: `;x` 4,000 times, an 8 KB mail, took 18.6 s, and 1,000 distinct ones 2.1 s."""
        small, large = build(250).encode(), build(1_000).encode()

        assert fastest(lambda: convert(large, "a.eml")) < 8 * fastest(lambda: convert(small, "a.eml")) + 0.05

    @pytest.mark.parametrize(("parameters", "expected"), [
        ("; charset=euc-kr", "본문 글"),
        ('; charset="euc-kr"; format=flowed', "본문 글 이어지는 줄"),
        ("; charset*=euc-kr''", "본문 글"),
    ])
    def test_the_parameters_read_from_a_text_part_are_kept(self, parameters, expected):
        """Only the charset, the flowed format and the name are read from a part once it is text/plain."""
        data = (_HEAD + f"Content-Type: multipart/mixed{parameters}; x=1\r\n\r\n").encode() + "본문 글 \r\n이어지는 줄\r\n".encode("euc-kr")

        assert expected in convert(data, "a.eml").text

    @pytest.mark.parametrize("kind", ["multipart/mixed", "message/delivery-status", "text/rfc822-headers"])
    @pytest.mark.parametrize("parameter", [
        b"name*=euc-kr''%C7%D1.txt", b"name*=ks_c_5601-1987''%C7%D1.txt", b"name*=x-unknown''%C7%D1.txt",
        b"name*=utf-8''%ED%95%9C.txt", b"name*0*=utf-8''%ED%95%9C; name*1=.txt",
        b'name="' + "한.txt".encode("cp949") + b'"', b'name="' + "한.txt".encode() + b'"',
    ], ids=["euc-kr", "ks_c_5601-1987", "unknown charset", "utf-8", "continued", "raw cp949", "raw utf-8"])
    def test_parameters_are_kept_as_written(self, kind, parameter):
        """Decoded and written out again, an RFC 2231 name was encoded back with its own charset: a Korean or unknown charset
        failed and the whole mail was rejected as corrupted, UTF-8 was encoded twice (`í .txt`), and raw 8-bit bytes became
        replacement characters (`��.txt`)."""
        data = (_HEAD.encode() + b'Content-Type: multipart/mixed; boundary="B"\r\n\r\n--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n'
                b"--B\r\nContent-Type: " + kind.encode() + b"; " + parameter + b"\r\n\r\nStatus: 5.0.0\r\n--B--\r\n")

        assert convert(data, "a.eml").warnings == ["1 mail attachment(s) (한.txt) were not read. Convert attachments separately."]

    @pytest.mark.parametrize("parameters", ["; charset*=x-unknown''%C7%D1", "; charset*=euc-kr''euc-kr; format*=euc-kr''%C7%D1"])
    def test_a_charset_that_cannot_be_written_back_does_not_reject_the_mail(self, parameters):
        data = (_HEAD + f"Content-Type: multipart/mixed{parameters}\r\n\r\n").encode() + "본문 글\r\n".encode("euc-kr")

        assert "본문 글" in convert(data, "a.eml").text

    def test_a_name_makes_it_an_attachment_as_before(self):
        data = (_HEAD + 'Content-Type: multipart/mixed; boundary="B"\r\n\r\n--B\r\nContent-Type: text/plain\r\n\r\nbody\r\n'
                '--B\r\nContent-Type: multipart/mixed; name="notes.txt"; x=1\r\n\r\nnotes\r\n--B--\r\n').encode()

        assert convert(data, "a.eml").warnings == ["1 mail attachment(s) (notes.txt) were not read. Convert attachments separately."]


_HUGE = "1" * 5000


class TestHugeNumbersInHtmlAndMail:
    """`int()` raises ValueError on a decimal number of more than 4,300 digits (Python's integer string length limit), so one value
    got the whole document rejected. Like Office (`_package.decimal_int`), it is treated as an odd value."""

    @pytest.mark.parametrize("body", [f'<table><tr><td colspan="{_HUGE}">a</td><td>b</td></tr><tr><td>1</td><td>2</td></tr></table>',
                                      f'<div role="grid"><div role="row"><div role="gridcell" aria-colindex="{_HUGE}">a</div></div></div>',
                                      f'<ul class="lst-kix_abc-{_HUGE}"><li>항목</li></ul>'],
                             ids=["colspan", "aria-colindex", "google-docs-list-level"])
    def test_html_attribute(self, body):
        result = convert(f"<html><body>{body}<p>본문</p></body></html>".encode(), "a.html")

        assert "본문" in result.text

    @pytest.mark.parametrize("rule", [f"FREQ=WEEKLY;COUNT={_HUGE}", f"FREQ=DAILY;INTERVAL={_HUGE}",
                                      f"FREQ=MONTHLY;BYMONTHDAY={_HUGE}", f"FREQ=MONTHLY;BYDAY={_HUGE}FR"],
                             ids=["count", "interval", "month-day", "weekday-ordinal"])
    def test_recurrence_rule_of_an_invitation(self, rule):
        ics = ("BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\nSUMMARY:주간 회의\r\nDTSTART:20240305T100000Z\r\n"
               f"RRULE:{rule}\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        data = (_HEAD + 'Content-Type: multipart/alternative; boundary="B"\r\n\r\n'
                "--B\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n회의 본문입니다.\r\n"
                f"--B\r\nContent-Type: text/calendar; method=REQUEST; charset=utf-8\r\n\r\n{ics}--B--\r\n").encode()

        result = convert(data, "a.eml")

        assert "회의 본문입니다." in result.text
        assert "주간 회의" in result.text


def _nested_mail(depth: int, lines: int) -> bytes:
    return (_HEAD.replace("\r\n", "\n") + "".join(f'Content-Type: multipart/mixed; boundary="b{n}"\n\n--b{n}\n' for n in range(depth))
            + "Content-Type: text/plain\n\nhello\n" + "x\n" * lines).encode()


class TestDeeplyNestedMultipart:
    """The standard library mail parser checks every open boundary on every line. 900 levels + a body of 200,000 lines (438KB) took
    18 seconds (lines × depth). Real mail is a few dozen levels deep even after repeated forwarding — anything deeper is rejected
    as a corrupted document before parsing."""

    @pytest.mark.timing
    def test_too_deep_nesting_is_rejected_before_parsing(self):
        import time

        data = _nested_mail(900, 50_000)
        started = time.perf_counter()

        with pytest.raises(CorruptedDocumentError):
            convert(data, "a.eml")

        assert time.perf_counter() - started < 1.0

    def test_ordinary_nesting_is_read(self):
        result = convert(_nested_mail(8, 10), "a.eml")

        assert "hello" in result.text

    def test_many_sibling_parts_are_not_counted_as_depth(self):
        parts = "".join(f'--A\nContent-Type: multipart/alternative; boundary="c{n}"\n\n--c{n}\nContent-Type: text/plain\n\n부속 {n}\n--c{n}--\n'
                        for n in range(300))
        data = (_HEAD.replace("\r\n", "\n") + 'Content-Type: multipart/mixed; boundary="A"\n\n' + parts + "--A--\n").encode()

        result = convert(data, "a.eml")

        assert "부속 299" in result.text

    def test_boundary_mentioned_in_body_text_is_not_counted(self):
        body = "".join(f"설명 {n}: 경계는 boundary=x{n} 처럼 적는다.\n" for n in range(300))
        data = (_HEAD.replace("\r\n", "\n") + f"Content-Type: text/plain; charset=utf-8\n\n{body}").encode()

        result = convert(data, "a.eml")

        assert "설명 299" in result.text
