"""Mail: the plain body instead of an HTML alternative without text, the end date of an all-day event, recurring events."""

import base64

import pytest

from opendockit import convert
from tests import builder


def _alternative(html: str, plain: str = "계약 금액은 3억원이며 4월 1일 시작합니다.") -> bytes:
    return ("From: a@b.c\nTo: d@e.f\nSubject: alt\nMIME-Version: 1.0\nContent-Type: multipart/alternative; boundary=\"b\"\n\n"
            f"--b\nContent-Type: text/plain; charset=utf-8\n\n{plain}\n"
            f"--b\nContent-Type: text/html; charset=utf-8\n\n{html}\n--b--\n").encode()


class TestHtmlAlternativeWithoutText:
    """When the HTML alternative has no text (a single picture), the body of the plain alternative vanished without a warning and
    only the headers were left. When the HTML alternative renders without text, the plain body is placed after the HTML (picture)."""

    @pytest.mark.parametrize("html", [
        '<html><body><img src="cid:logo"></body></html>',
        "<html><head><style>p { color: red }</style><title>제목</title></head><body><div>&nbsp;</div></body></html>",
    ])
    def test_plain_body_is_used_when_the_html_part_has_no_text(self, html):
        result = convert(_alternative(html), "a.eml")

        assert result.text == "Subject: alt\nFrom: a@b.c\nTo: d@e.f\n\n계약 금액은 3억원이며 4월 1일 시작합니다."

    def test_picture_in_the_html_part_is_kept_before_the_plain_body(self):
        picture = base64.b64encode(builder.png_bytes(300, 200)).decode()
        html = f'<html><body><img alt="서명" src="data:image/png;base64,{picture}"></body></html>'

        result = convert(_alternative(html), "a.eml")

        assert result.text == "Subject: alt\nFrom: a@b.c\nTo: d@e.f\n\n[Image: 서명]\n\n계약 금액은 3억원이며 4월 1일 시작합니다."
        assert len(result.images) == 1

    def test_html_part_with_text_is_still_preferred(self):
        result = convert(_alternative("<p>HTML 본문</p>"), "a.eml")

        assert result.text.endswith("HTML 본문")
        assert "계약 금액" not in result.text


def _invite(event: str) -> bytes:
    calendar = f"BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\nSUMMARY:워크숍\r\n{event}END:VEVENT\r\nEND:VCALENDAR\r\n"
    return ("From: a@b.c\nTo: d@e.f\nSubject: w\nMIME-Version: 1.0\nContent-Type: multipart/alternative; boundary=\"b\"\n\n"
            "--b\nContent-Type: text/plain; charset=utf-8\n\n일정입니다.\n"
            "--b\nContent-Type: text/calendar; charset=utf-8; method=REQUEST\nContent-Transfer-Encoding: 8bit\n\n"
            f"{calendar}\n--b--\n").encode()


class TestAllDayAndRecurringEvents:
    """The DTEND (VALUE=DATE) of an all-day event does not include that day (RFC 5545), but it was written as is, so a one-day event
    came out as 'End: the next day', and the recurrence rule (RRULE) vanished without a warning. The end of an all-day event is the
    date one day earlier (no 'End' for a one-day event), and the recurrence is written readably on a `Repeats:` line."""

    def test_one_day_event_has_no_end_and_its_recurrence_is_readable(self):
        event = "DTSTART;VALUE=DATE:20240305\r\nDTEND;VALUE=DATE:20240306\r\nRRULE:FREQ=WEEKLY;COUNT=4;BYDAY=TU\r\n"

        result = convert(_invite(event), "a.eml")

        assert result.text.endswith("Event:\n\n- Title: 워크숍\n- Start: 2024-03-05\n- Repeats: weekly on Tuesday, 4 times")

    def test_end_of_a_multi_day_event_is_its_last_day(self):
        event = "DTSTART;VALUE=DATE:20240229\r\nDTEND;VALUE=DATE:20240302\r\n"

        result = convert(_invite(event), "a.eml")

        assert result.text.endswith("- Start: 2024-02-29\n- End: 2024-03-01")

    @pytest.mark.parametrize(("rule", "expected"), [
        ("FREQ=DAILY", "daily"),
        ("FREQ=WEEKLY;INTERVAL=2;BYDAY=MO,WE;UNTIL=20240630T145959Z", "every 2 weeks on Monday, Wednesday, until 2024-06-30"),
        ("FREQ=MONTHLY;BYDAY=-1FR", "monthly on the last Friday"),
        ("FREQ=MONTHLY;BYMONTHDAY=15;COUNT=6", "monthly on day 15, 6 times"),
        ("FREQ=YEARLY;INTERVAL=1;WKST=SU", "yearly"),
        ("FREQ=MONTHLY;BYDAY=2TU", "monthly on the second Tuesday"),
        ("FREQ=HOURLY;INTERVAL=3", "FREQ=HOURLY;INTERVAL=3"),
        ("FREQ=WEEKLY;BYSETPOS=1;BYDAY=MO", "FREQ=WEEKLY;BYSETPOS=1;BYDAY=MO"),
    ])
    def test_recurrence_rules(self, rule, expected):
        event = f"DTSTART:20240305T090000Z\r\nRRULE:{rule}\r\n"

        result = convert(_invite(event), "a.eml")

        assert result.text.endswith(f"- Start: 2024-03-05 09:00 UTC\n- Repeats: {expected}")

    def test_timed_event_end_is_unchanged(self):
        event = "DTSTART:20240305T090000Z\r\nDTEND:20240305T100000Z\r\n"

        result = convert(_invite(event), "a.eml")

        assert result.text.endswith("- Start: 2024-03-05 09:00 UTC\n- End: 2024-03-05 10:00 UTC")
