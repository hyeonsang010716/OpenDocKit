"""A single wrong mail or HTML value got the whole document rejected or the body lost: a Western European subject written in
ISO-8859-1, a NUL byte in the charset, a multipart without a boundary, a bounce report, numbers of more than 4,300 digits, very
deeply nested multipart."""

import base64

import pytest

from opendockit import convert
from opendockit.exceptions import CorruptedDocumentError


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


_HUGE = "1" * 5000


class TestHugeNumbersInHtmlAndMail:
    """`int()` raises ValueError on a decimal number of more than 4,300 digits (Python's integer string length limit), so one value
    got the whole document rejected. Like Office (`_shared.decimal_int`), it is treated as an odd value."""

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
