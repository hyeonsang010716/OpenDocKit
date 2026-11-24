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
    ], ids=["text-behind-the-closing-line", "pieces-under-another-token", "sentence-that-looks-like-a-header", "rules-of-dashes",
            "signature-shaped-like-a-closing-line", "text-around-the-parts-of-a-multipart-inside",
            "line-that-says-base64-inside-a-text-part", "token-that-begins-another-line", "sentence-among-the-headers-of-a-part"])
    def test_no_line_that_was_shown_is_lost(self, body):
        """A reader of MIME drops what stands behind the closing line and reads a header where a sentence was; as text all of
        it was shown, so such a body stays text, and a last line that only looks like a closing line stays in its part."""
        result = convert(self._mail('multipart/mixed; boundary="AAA"', body), "a.eml")

        shown = result.text.split("\n\n", 1)[1]
        for line in body.replace("\r\n", "\n").split("\n"):
            if not line.startswith(("--BBB", "--X", "Content-T")) or line.startswith("--BBBx"):
                assert line.strip() in shown

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
