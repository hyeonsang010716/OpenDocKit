"""Meeting invitations in a mail (text/calendar parts): the events of an iCalendar body as labelled lines."""

from datetime import date, timedelta
from email.message import Message
import re

from opendockit.extractors._encoding import decode_text
from opendockit.extractors._mail import _is_attachment, _mail_parts
from opendockit.extractors._package import decimal_int


# Event properties read from a meeting invitation (text/calendar) → label (`mail_calendar_events`). Written in this order.
_CALENDAR_FIELDS = {"SUMMARY": "Title", "DTSTART": "Start", "DTEND": "End", "RRULE": "Repeats", "LOCATION": "Location", "ORGANIZER": "Organizer",
                    "ATTENDEE": "Attendees"}
# Upper limits on the number of events (VEVENT) and attendees read and on the bytes of a calendar part. Anything beyond is
# dropped silently — a recurring event can have dozens of exception events.
_MAX_CALENDAR_EVENTS = 20
_MAX_CALENDAR_ATTENDEES = 30
_MAX_CALENDAR_BYTES = 1 << 20
# iCalendar date/time values (`20240307`, `20240307T150000`, `20240307T060000Z`) and escapes (`\,`, `\;`, `\n`, `\\`).
_CALENDAR_TIME = re.compile(r"(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})\d{2}(Z?))?")
_CALENDAR_ESCAPE = re.compile(r"\\(.)")
# The status property of an event, and its value for an event that was called off.
_STATUS = "STATUS"
_CANCELLED = "CANCELLED"
# Folded lines (line break + space or tab). Unfolded on the bytes (`mail_calendar_events`).
_CALENDAR_FOLD = re.compile(rb"\r?\n[ \t]")
# Splits a property line `name;parameters:value` at the first `:` outside quotes.
_CALENDAR_LINE = re.compile(r'((?:[^:"]|"[^"]*")*):(.*)')
_CALENDAR_PARAM_CN = re.compile(r";CN=(\"[^\"]*\"|[^;:]*)", re.IGNORECASE)
_CALENDAR_PARAM_TZID = re.compile(r";TZID=(\"[^\"]*\"|[^;:]*)", re.IGNORECASE)
# The written shape of the date value of an all-day event (the `2024-03-07` that `_calendar_value` produces).
_CALENDAR_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
# Recurrence rule (RRULE, `_recurrence`): the items rendered readably, frequency (interval 1 / interval N), weekdays, the ordinal
# before a weekday, the weekday value `[ordinal]weekday`.
# If there is any other item (BYSETPOS, BYMONTH, BYHOUR …) or frequency (HOURLY …), the rule is left as its original text.
_RRULE_KEYS = {"FREQ", "INTERVAL", "COUNT", "UNTIL", "BYDAY", "BYMONTHDAY", "WKST"}
_RRULE_FREQUENCIES = {"DAILY": ("daily", "days"), "WEEKLY": ("weekly", "weeks"), "MONTHLY": ("monthly", "months"),
                      "YEARLY": ("yearly", "years")}
_RRULE_WEEKDAYS = {"MO": "Monday", "TU": "Tuesday", "WE": "Wednesday", "TH": "Thursday", "FR": "Friday", "SA": "Saturday", "SU": "Sunday"}
_RRULE_ORDINALS = {1: "the first", 2: "the second", 3: "the third", 4: "the fourth", 5: "the fifth", -1: "the last"}
_RRULE_WEEKDAY = re.compile(r"([+-]?\d{1,2})?(MO|TU|WE|TH|FR|SA|SU)")


def mail_calendar_events(message: Message) -> list[tuple[str, list[tuple[str, str]]]]:
    """The events of the meeting invitations (text/calendar parts, attachments excluded) in a mail body: (block title `Event` or
    `Event (cancelled)`, [(label, value)]).

    An Outlook meeting invitation does not write the date/time and location in the text/plain and text/html bodies of the
    alternative, only in the text/calendar part. Reading only the body, the date/time and location vanished without a warning
    (Pinned by: test_core_mail_parts_and_icons.py::TestMeetingInvitation). Only the title, start, end (time zone as is),
    recurrence (`_recurrence`), location, organizer and attendees are read — the description (DESCRIPTION) is the same text as
    the body. A named .ics attachment keeps its attachment warning (`_is_attachment`).
    Silently an empty list when there is no event to read or it cannot be decoded.
    """
    events: list[tuple[str, list[tuple[str, str]]]] = []

    for part in _mail_parts(message):
        if part.get_content_type() != "text/calendar" or _is_attachment(part, message):
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes) or len(payload) > _MAX_CALENDAR_BYTES:
            continue
        # Folding is by bytes (75 octets), so Outlook splits even between the bytes of a Hangul character. Unfolding after decoding
        # to text garbled that character into '예��'
        # (Pinned by: test_core_html_mail_hidden_and_frames.py::TestCalendarLinesFoldedInsideACharacter).
        text = decode_text(_CALENDAR_FOLD.sub(b"", payload), part.get_content_charset())[0]
        # An event of its own STATUS:CANCELLED is cancelled too, as a whole cancellation notice (METHOD:CANCEL) is: a calendar
        # that kept the cancelled event published it as an ordinary `Event:` (Pinned by:
        # test_core_mail_bodies_and_events.py::TestCancelledEvent).
        cancelled = str(part.get_param("method") or "").upper() == "CANCEL" or "\nMETHOD:CANCEL" in text.upper()
        events += [("Event (cancelled)" if cancelled or own_status == _CANCELLED else "Event", fields)
                   for fields, own_status in _calendar_events(text)][:_MAX_CALENDAR_EVENTS - len(events)]
    return events


def mail_calendar_text(message: Message) -> str:
    """The events of a meeting invitation (`mail_calendar_events`) as text to place after the body: an `Event:` heading paragraph
    + a `- label: value` list. Empty text if there is none."""
    return "\n\n".join(f"{title}:\n\n" + "\n".join(f"- {label}: {value}" for label, value in fields)
                       for title, fields in mail_calendar_events(message))


def _calendar_events(text: str) -> list[tuple[list[tuple[str, str]], str]]:
    """([(label, value)], STATUS in capitals or '') for each VEVENT of iCalendar text. Unfolds folded lines (line break + space
    or tab) and leaves out events without a single value. Only the event's own properties are read: those of a component
    inside it (a VALARM reminder, which has its own SUMMARY and ATTENDEE) are not — an e-mail reminder written before the
    event's SUMMARY became the title, and its recipient an attendee (Pinned by: test_audit_web_fixes.py::TestReminderInsideAnEvent).
    A component that is opened and never closed before the event ends holds nothing: the lines after its BEGIN are the
    event's, as they were before components were told apart — the rest of the event went missing after one stray BEGIN
    (Pinned by: test_audit_web_fixes.py::TestUnclosedComponentInsideAnEvent)."""
    lines = re.sub(r"\r?\n[ \t]", "", text).splitlines()
    events: list[tuple[list[tuple[str, str]], str]] = []
    current: list[tuple[str, str, str]] | None = None  # (key, name with parameters, value) of the event's own lines so far
    opened: list[int] = []  # for each component open inside the event, how many lines the event had when it began

    for line in lines:
        split = _CALENDAR_LINE.match(line)  # do not split at a `:` inside quotes (`TZID="(UTC+09:00) 서울"`)
        name, rest = split.groups() if split else (line, "")
        key = name.split(";", 1)[0].strip().upper()
        component = rest.strip().upper()
        if key == "BEGIN" and component == "VEVENT":
            current, opened = [], []
        elif current is None:
            continue
        elif key == "BEGIN":
            opened.append(len(current))
        elif key == "END" and component != "VEVENT":
            if opened:
                del current[opened.pop():]
        elif key == "END":
            events.extend(_event_fields(current))
            current = None
        elif (key in _CALENDAR_FIELDS or key == _STATUS) and rest.strip():
            current.append((key, name, rest.strip()))
    return events


def _event_fields(lines: list[tuple[str, str, str]]) -> list[tuple[list[tuple[str, str]], str]]:
    """[([(label, value)], STATUS)] of one event from its own property lines (key, name with parameters, value), or [] when it
    has no value: the first value of each property, and up to `_MAX_CALENDAR_ATTENDEES` attendees."""
    event: dict[str, list[str]] = {}
    for key, name, value in lines:
        values = event.setdefault(key, [])
        if len(values) < (_MAX_CALENDAR_ATTENDEES if key == "ATTENDEE" else 1):
            values.append(_calendar_value(key, name, value))
    _end_of_all_day_event(event)
    fields = [(label, ", ".join(event[field])) for field, label in _CALENDAR_FIELDS.items() if event.get(field)]
    status = next(iter(event.get(_STATUS, [])), "").strip().upper()
    return [(fields, status)] if fields else []


def _end_of_all_day_event(event: dict[str, list[str]]) -> None:
    """Change the end of an all-day event (start and end both dates) to its last day. For a one-day event the end is removed.

    A DTEND that is a date value does not include that day (RFC 5545) — written as is, a one-day event on March 5 came out as
    'End: 2024-03-06' (Pinned by: test_core_mail_bodies_and_events.py::TestAllDayAndRecurringEvents). If the start is a time,
    the event does not follow the rule and is left as is.
    """
    start, end = event.get("DTSTART"), event.get("DTEND")
    if not (start and end and _CALENDAR_DATE.fullmatch(start[0]) and _CALENDAR_DATE.fullmatch(end[0])):
        return
    try:
        last = (date.fromisoformat(end[0]) - timedelta(days=1)).isoformat()
    except ValueError:
        return
    if last <= start[0]:
        del event["DTEND"]
    else:
        event["DTEND"] = [last]


def _recurrence(rule: str) -> str:
    """A recurrence rule (RRULE) as readable text: `weekly on Tuesday, 4 times`,
    `every 2 weeks on Monday, Wednesday, until 2024-06-30`, `monthly on the last Friday`.

    Recurrence rules were not read, so a recurring meeting read as a one-off
    (Pinned by: test_core_mail_bodies_and_events.py::TestAllDayAndRecurringEvents).
    Only the common items (`_RRULE_KEYS`) are rendered; if there is any other item or frequency, or a value does not fit, the
    rule is left as its original text.
    """
    parts: dict[str, str] = {}

    # An empty item (a trailing or doubled `;`) says nothing; it kept the whole rule as its raw text
    # (Pinned by: test_audit_web_fixes.py::TestRecurrenceRuleWithATrailingSemicolon)
    for item in filter(str.strip, rule.upper().split(";")):
        name, _, value = item.partition("=")
        parts[name.strip()] = value.strip()

    # Numbers are read with `decimal_int` — the `int()` ValueError on a COUNT/INTERVAL of more than 4,300 digits got the whole
    # mail rejected (Pinned by: test_core_malformed_mail_and_html.py::TestHugeNumbersInHtmlAndMail)
    frequency = _RRULE_FREQUENCIES.get(parts.get("FREQ", ""))
    interval = decimal_int(parts.get("INTERVAL", "1"), 0)
    if frequency is None or not parts.keys() <= _RRULE_KEYS or interval < 1:
        return rule

    head = frequency[0] if interval == 1 else f"every {interval} {frequency[1]}"
    days = []

    # weekdays (with an ordinal, `the second Tuesday`) and days of the month
    for day in filter(None, parts.get("BYDAY", "").split(",")):
        match = _RRULE_WEEKDAY.fullmatch(day)
        ordinal = _RRULE_ORDINALS.get(int(match.group(1))) if match and match.group(1) else ""
        if match is None or ordinal is None:
            return rule
        days.append(f"{ordinal} {_RRULE_WEEKDAYS[match.group(2)]}".strip())
    for day in filter(None, parts.get("BYMONTHDAY", "").split(",")):
        if day == "-1":
            days.append("the last day")
        elif 1 <= decimal_int(day, 0) <= 31:
            days.append(f"day {int(day)}")
        else:
            return rule

    # the end: count or date
    pieces = [" on ".join([head, ", ".join(days)]).removesuffix(" on ")]
    count, until = parts.get("COUNT"), parts.get("UNTIL")
    if count is not None:
        if decimal_int(count, None) is None:
            return rule
        pieces.append(f"{int(count)} times")
    if until is not None:
        match = _CALENDAR_TIME.fullmatch(until)
        if match is None:
            return rule
        pieces.append(f"until {match.group(1)}-{match.group(2)}-{match.group(3)}")
    return ", ".join(pieces)


def _calendar_value(key: str, params: str, value: str) -> str:
    """An event property value as readable text: a time as `2024-03-07 15:00 (time zone)`, `… UTC` or `2024-03-07`, a person as
    `name <address>`, and text with its escapes undone."""
    if key in ("DTSTART", "DTEND"):
        match = _CALENDAR_TIME.fullmatch(value)
        if match is None:
            return value
        year, month, day, hour, minute, utc = match.groups()
        when = f"{year}-{month}-{day}" + (f" {hour}:{minute}" if hour else "")
        zone = _CALENDAR_PARAM_TZID.search(params)
        if utc:
            return f"{when} UTC"
        return f"{when} ({zone.group(1).strip(chr(34))})" if zone and hour else when
    if key == "RRULE":
        return _recurrence(value)
    if key in ("ORGANIZER", "ATTENDEE"):
        address = re.sub(r"^mailto:", "", value, flags=re.IGNORECASE)
        name = _CALENDAR_PARAM_CN.search(params)
        name_text = name.group(1).strip().strip('"') if name else ""
        return f"{name_text} <{address}>" if name_text and name_text != address else address
    unescaped = _CALENDAR_ESCAPE.sub(lambda match: " " if match.group(1) in "nN" else match.group(1), value)
    return " ".join(unescaped.split())
