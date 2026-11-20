"""Mail: the plain body instead of an HTML alternative without text, the end date of an all-day event, recurring events."""

import base64

import pytest

from opendockit import convert
from tests import builder, needs_html5_tokenizer


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
    @needs_html5_tokenizer
    def test_plain_body_is_used_when_the_html_part_has_no_text(self, html):
        result = convert(_alternative(html), "a.eml")

        assert result.text == "Subject: alt\nFrom: a@b.c\nTo: d@e.f\n\n계약 금액은 3억원이며 4월 1일 시작합니다."

    @needs_html5_tokenizer
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


def _invite_without_a_body(event: str, *, html: str | None = None) -> bytes:
    calendar = f"BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VEVENT\r\nSUMMARY:분기 점검\r\n{event}END:VEVENT\r\nEND:VCALENDAR\r\n"
    head = "From: a@b.c\nTo: d@e.f\nSubject: w\nMIME-Version: 1.0\n"
    if html is None:
        return f"{head}Content-Type: text/calendar; charset=utf-8; method=REQUEST\n\n{calendar}".encode()
    return (f"{head}Content-Type: multipart/alternative; boundary=\"b\"\n\n--b\nContent-Type: text/html; charset=utf-8\n\n{html}\n"
            f"--b\nContent-Type: text/calendar; charset=utf-8; method=REQUEST\n\n{calendar}\n--b--\n").encode()


class TestInvitationWithoutABody:
    """The description of an event is the text of the body, so it is not read; an invitation sent as a text/calendar part alone
    has no body, and its agenda was gone without a word. An event timed with DURATION instead of DTEND (RFC 5545) had no end."""

    _AGENDA = "DESCRIPTION:안건:\\n1. 예산 초과 12%\\n\\n2. 채용 동결\r\n"

    def test_description_of_a_mail_without_a_body_is_read(self):
        result = convert(_invite_without_a_body("DTSTART:20250301T090000Z\r\n" + self._AGENDA), "a.eml")

        assert result.text.endswith("- Title: 분기 점검\n- Start: 2025-03-01 09:00 UTC\n"
                                    "- Description: 안건:\n  1. 예산 초과 12%\n  2. 채용 동결")

    def test_description_beside_a_body_with_text_is_not_read(self):
        result = convert(_invite("DTSTART:20250301T090000Z\r\n" + self._AGENDA), "a.eml")

        assert "Description" not in result.text and "예산" not in result.text

    def test_description_beside_an_html_body_of_white_space_is_read(self):
        """An HTML mail writes the lines of a list item on one line, as it does for every `<li>`."""
        result = convert(_invite_without_a_body("DTSTART:20250301T090000Z\r\n" + self._AGENDA, html=" "), "a.eml")

        assert result.text.endswith("- Description: 안건: 1. 예산 초과 12% 2. 채용 동결")

    def test_mail_with_an_empty_html_part_is_mail(self):
        """Detection takes an empty HTML part for no HTML body and sends the mail to the text extractor, which took it for one
        and gave the mail back raw."""
        result = convert(_invite_without_a_body("DTSTART:20250301T090000Z\r\n" + self._AGENDA, html=""), "a.eml")

        assert result.text == ("Subject: w\nFrom: a@b.c\nTo: d@e.f\n\nEvent:\n\n- Title: 분기 점검\n- Start: 2025-03-01 09:00 UTC\n"
                               "- Description: 안건:\n  1. 예산 초과 12%\n  2. 채용 동결")

    @pytest.mark.parametrize(("event", "expected"), [
        ("DTSTART:20250301T090000Z\r\nDURATION:PT1H30M\r\n", "- Start: 2025-03-01 09:00 UTC\n- End: 2025-03-01 10:30 UTC"),
        ('DTSTART;TZID="Korea Standard Time":20250301T233000\r\nDURATION:PT45M\r\n',
         "- Start: 2025-03-01 23:30 (Korea Standard Time)\n- End: 2025-03-02 00:15 (Korea Standard Time)"),
        ("DTSTART:20250301T090000Z\r\nDURATION:P1DT2H\r\n", "- Start: 2025-03-01 09:00 UTC\n- End: 2025-03-02 11:00 UTC"),
        ("DTSTART;VALUE=DATE:20250301\r\nDURATION:P1D\r\n", "- Start: 2025-03-01"),
        ("DTSTART;VALUE=DATE:20250301\r\nDURATION:P1W\r\n", "- Start: 2025-03-01\n- End: 2025-03-07"),
        ("DTSTART:20250301T090000Z\r\nDTEND:20250301T100000Z\r\nDURATION:PT5H\r\n",
         "- Start: 2025-03-01 09:00 UTC\n- End: 2025-03-01 10:00 UTC"),
        ("DTSTART:20250301T090000Z\r\nDURATION:-PT1H\r\n", "- Start: 2025-03-01 09:00 UTC"),
        ("DTSTART:20250301T090000Z\r\nDURATION:P\r\n", "- Start: 2025-03-01 09:00 UTC"),
        ("DTSTART;VALUE=DATE:20250301\r\nDURATION:PT3H\r\n", "- Start: 2025-03-01"),
        ("DTSTART:99991231T230000Z\r\nDURATION:P2D\r\n", "- Start: 9999-12-31 23:00 UTC"),
    ])
    def test_end_from_the_duration(self, event, expected):
        result = convert(_invite(event), "a.eml")

        assert result.text.endswith(expected)


class TestCancelledEvent:
    """An event with its own STATUS:CANCELLED in an ordinary invitation (METHOD:REQUEST or PUBLISH) was written as an ordinary
    `Event:`, as if the meeting would take place."""

    @pytest.mark.parametrize("html", [False, True])
    def test_event_is_marked_cancelled(self, html):
        data = _invite("STATUS:CANCELLED\r\nDTSTART;VALUE=DATE:20240305\r\n")
        if html:
            data = data.replace("text/plain; charset=utf-8\n\n일정입니다.".encode(), "text/html; charset=utf-8\n\n<p>일정입니다.</p>".encode())

        result = convert(data, "a.eml")

        assert result.text.endswith("일정입니다.\n\nEvent (cancelled):\n\n- Title: 워크숍\n- Start: 2024-03-05")

    def test_confirmed_event_is_an_ordinary_one(self):
        result = convert(_invite("STATUS:CONFIRMED\r\nDTSTART;VALUE=DATE:20240305\r\n"), "a.eml")

        assert result.text.endswith("일정입니다.\n\nEvent:\n\n- Title: 워크숍\n- Start: 2024-03-05")
