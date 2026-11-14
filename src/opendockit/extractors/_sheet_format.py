"""Excel number formats applied to a cell value, shared by XLSX, XLS and XML Spreadsheet (and ODS through the value text):
dates, times, elapsed time, percentages, digit places, sections and literal text.
"""

from collections.abc import Iterator
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
import math
import re

from openpyxl.styles.numbers import is_date_format


# Formats that show only the weekday (`aaa` = 목, `aaaa` = 목요일). openpyxl and xlrd do not treat a format without y, m, d, h, s as a
# date and return the serial number as is. The weekday letters are not imitated; the date is shown
# (Pinned by: test_spreadsheet.py::TestDateFormats).
_WEEKDAY_ONLY_FORMAT = re.compile(r"a{3,4}", re.IGNORECASE)


# A display format longer than this is not interpreted: the value is shown as under the General format. The format is analysed
# again for every cell in time proportional to its length, so a 6 KB workbook with a 100 KB format on 200 cells took 1.6 s (7.8 ms
# per cell). Excel accepts no format of more than 255 characters. Common to XLSX, XLS and Excel 2003 XML
# (Pinned by: test_audit_spreadsheet.py::TestVeryLongNumberFormat).
_MAX_FORMAT_LENGTH = 1024


def _format_within_length(number_format: str | None) -> str | None:
    return number_format if number_format is None or len(number_format) <= _MAX_FORMAT_LENGTH else None


# The format the General Date name of Excel 2003 XML stands for (`xml_spreadsheet._XML_NAMED_FORMATS`). The marker in front lets
# `format_cell` show only the date at midnight, as Excel does for that name.
_XML_GENERAL_DATE = "[$-x-general-date]yyyy-mm-dd h:mm:ss"


# For interpreting number formats. Colours and conditions ([Red], [>=100]) are discarded and only the currency symbol ([$₩-412]) is kept.
_FORMAT_BRACKET = re.compile(r"\[([^\]]*)\]")
_FORMAT_QUOTED = re.compile(r'"([^"]*)"')
_FORMAT_ESCAPED = re.compile(r'\\([^"])')
_FORMAT_DATE_TOKENS = re.compile(r"[yYdDhHsS]|(?<![#0])[mM]")
# `_x` leaves a space as wide as the character x, `*x` fills the cell with x (the `_-` and `* ` of the accounting format
# `_-* #,##0_-`). They are for on-screen alignment, so they are not put into the value.
_FORMAT_PADDING = re.compile(r"[_*].?")


def format_cell(value: object, number_format: str | None) -> str:
    """Turn a cell value into a string the way it appears on screen in Excel.

    Deliberate: emitting 108.2% as 1.082 or 125,000,000 as 125000000 makes an LLM misread the figures. Only percent, thousands
    separators, scaling by thousands, zero padding, decimal places, currency symbols and leading/trailing literals ("원") are
    applied; complex things such as dates, fractions, exponents and conditional formats use the original value.
    Alignment padding (`_-`, `* `) is not part of the value, so it is removed
    (Pinned by: test_quality.py::TestSpreadsheetNumberFormats).
    """
    if isinstance(value, timedelta):
        return _elapsed_text(value, number_format)
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value < 0 and number_format \
            and is_date_format(number_format):
        return _negative_serial_text(float(value), number_format)
    if isinstance(value, datetime) and number_format and number_format.lower() != "general":
        if not _shows_time(number_format):
            return _date_text(value, number_format)  # a date-only format gets no time appended
        if not _shows_date(number_format):
            # For a value beyond one day (1.5) in a time-only format (`h:mm`), Excel shows `12:00`. No date (1900-01-01) is added
            # (Pinned by: test_spreadsheet.py::TestTimeDisplay).
            return _time_text(value.time(), number_format)
        if number_format != _XML_GENERAL_DATE:
            # A format showing both date and time shows the time at midnight too. Only midnight came out as `2024-01-01`, looking
            # different from the other cells of the same column (Pinned by: test_spreadsheet.py::TestDateTimeAtMidnight). XML
            # General Date shows the date only at midnight, as Excel does (deliberate).
            # Seconds only when the format shows them (Pinned by: test_spreadsheet_notes_pictures_and_times.py::TestTimeFormats).
            shown = _whole_seconds(value)
            return shown.isoformat(sep=" ", timespec="seconds" if _shows_seconds(number_format) else "minutes")
    if isinstance(value, time) and number_format and number_format.lower() != "general" and not _shows_date(number_format):
        return _time_text(value, number_format)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not number_format:
        return _format_value(value)
    formatted = _apply_number_format(float(value), number_format)
    return formatted if formatted is not None else _format_value(value)


def _negative_serial_text(value: float, number_format: str) -> str:
    """A negative serial number with a date/time format. A time-only format gives a signed time (`-01:30:00`; `-1:30` in Excel's
    1904 system), and a format showing a date gives the original number (Excel shows `####`). Converting it to a date emitted a
    non-existent day (1899-12-25) or a time wrapped around the day (22:30) as if it were fact. Common to XLSX, XLS and XML and to
    the 1900 and 1904 systems (Pinned by: test_spreadsheet.py::TestNegativeDateSerials)."""
    if _ELAPSED_FORMAT.search(number_format):
        try:
            return _elapsed_text(timedelta(days=value), number_format)
        except OverflowError:
            # A value that does not fit in a duration (a billion days or more, or infinity) is shown as the number, as the
            # positive one is. It rejected the whole document
            # (Pinned by: test_audit_spreadsheet.py::TestNegativeValueTooLargeForADuration).
            return _format_value(value)
    if _shows_date(number_format) or math.isinf(value):
        return _format_value(value)
    seconds = round(-value * 86400)
    return f"-{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


# Elapsed time formats (`[h]:mm`, `[mm]:ss`, `[ss]`). Beyond 24 hours the value does not roll over into days but is shown cumulatively.
_ELAPSED_FORMAT = re.compile(r"\[(?:h+|m+|s+)\]", re.IGNORECASE)


def _shows_time(number_format: str) -> bool:
    """Whether the format shows hours or seconds. Letters in quoted text and in bracket sections (colour, locale `[$-en-US]`,
    `[$-x-sysdate]`) are not format codes — the s/h inside them were taken as time and `00:00:00` was appended to date cells. Only
    elapsed time (`[h]`) is a time even inside brackets
    (Pinned by: test_spreadsheet.py::TestDateFormats::test_letters_inside_a_locale_bracket_are_not_time_codes)."""
    codes = _FORMAT_BRACKET.sub(lambda match: match.group(0) if _ELAPSED_FORMAT.fullmatch(match.group(0)) else "",
                                _FORMAT_QUOTED.sub("", number_format))
    return re.search(r"[hHsS]", codes) is not None


# AM/PM markers. Removed first so that A, M and P are not taken as date/minute codes when counting date letters.
_FORMAT_AM_PM = re.compile(r"AM/PM|A/P", re.IGNORECASE)


def _shows_date(number_format: str) -> bool:
    """Whether the format shows a date (year, month, day, weekday, era). `m` is minutes when the code right before it is `h` or the
    code right after it is `s` (same rule as Excel). Quoted text, backslash-escaped characters, bracket sections and AM/PM markers
    are not format codes."""
    letters = _code_letters(number_format)
    if re.search(r"[ydeg]|a{3}", letters):
        return True
    return any(letters[:run.start()][-1:] != "h" and letters[run.end():run.end() + 1] != "s"
               for run in re.finditer(r"m+", letters))


def _code_letters(number_format: str) -> str:
    """Only the letters of the format codes (lower case). Quoted text, backslash-escaped characters, bracket sections and AM/PM
    markers are left out."""
    codes = _FORMAT_AM_PM.sub("", _FORMAT_ESCAPED.sub("", _FORMAT_BRACKET.sub("", _FORMAT_QUOTED.sub("", number_format))))
    return re.sub(r"[^A-Za-z]", "", codes).lower()


def _date_text(value: datetime, number_format: str) -> str:
    """Value of a date format that shows no time. A format that shows neither the day (d) nor the weekday (aaa) gives `YYYY-MM`,
    and `YYYY` if it does not show the month either (year or era only).

    `yyyy"년" m"월"`, `mmm-yy` and `yyyy` cells came out as `2024-01-01` with a day that is not there (the 1st) attached, and read
    as if the value were known down to the day. The shape is unified as a shortened date shape (`YYYY-MM-DD`) — the '년'/'월'
    literals of the format are not imitated (common to XLSX, XLS and XML, `format_cell`)
    (Pinned by: test_spreadsheet.py::TestDateFormatsWithoutADay).
    """
    letters = _code_letters(number_format)
    if "d" in letters or "aaa" in letters:
        return value.date().isoformat()
    if "m" in letters:
        return f"{value.year:04d}-{value.month:02d}"
    if re.search(r"[yeg]", letters):
        return f"{value.year:04d}"
    return value.date().isoformat()


def _is_weekday_only(number_format: str | None) -> bool:
    """Whether the format codes of the first section are a weekday only (`aaa`, `aaaa`). Quoted text, bracket sections,
    backslash-escaped characters and symbols are ignored."""
    if not number_format or "a" not in number_format.lower():
        return False
    section = _FORMAT_SECTION.match(number_format)
    codes = _FORMAT_ESCAPED.sub("", _FORMAT_BRACKET.sub("", _FORMAT_QUOTED.sub("", section.group(0) if section else "")))
    return _WEEKDAY_ONLY_FORMAT.fullmatch(re.sub(r"[^A-Za-z]", "", codes)) is not None


def _elapsed_text(value: timedelta, number_format: str | None) -> str:
    """Turn a duration into Excel's elapsed-time shape (`26:00`). Python's default shape (`1 day, 2:00:00`) is easy for an LLM to
    misread (Pinned by: test_spreadsheet.py::TestElapsedTime). The literals of the format (`[h]"시간" mm"분"` → `36시간 00분`) are
    kept by `_clock_text`.

    Below the smallest unit shown the value is truncated, as for `h:mm` (`_clock_text`): 1:29:45 under `[h]:mm` is `1:29`. It was
    rounded to `1:30` while the same value under `h:mm` gave `1:29` (Pinned by: test_audit_spreadsheet.py::TestElapsedMinutes).
    A value that truncates to zero carries no sign."""
    pattern = (number_format or "[h]:mm:ss").lower()
    seconds = round(value.total_seconds())
    sign, seconds = ("-" if seconds < 0 else ""), abs(seconds)
    if number_format and _FORMAT_QUOTED.search(_first_section(number_format)):
        clock = _clock_text(seconds, number_format, elapsed=True)
        if clock is not None:
            return (sign if seconds >= _smallest_unit(number_format) else "") + clock
    units = {match.group(0)[1] for match in _ELAPSED_FORMAT.finditer(pattern)}  # a colour name (`[Magenta]`) is not an elapsed section
    if units == {"s"}:
        return f"{sign}{seconds}"
    if "h" not in units and "m" in units:
        return f"{sign}{seconds // 60}:{seconds % 60:02d}"
    if "s" not in _FORMAT_QUOTED.sub("", pattern):
        minutes = seconds // 60
        return f"{sign if minutes else ''}{minutes // 60}:{minutes % 60:02d}"
    return f"{sign}{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _shows_seconds(number_format: str) -> bool:
    """Whether the format shows seconds (s). Quoted text, backslash-escaped characters and bracket sections (elapsed seconds `[ss]`
    do count as seconds) are not format codes."""
    codes = _FORMAT_ESCAPED.sub("", _FORMAT_BRACKET.sub(
        lambda match: match.group(0) if _ELAPSED_FORMAT.fullmatch(match.group(0)) else "", _FORMAT_QUOTED.sub("", number_format)))
    return "s" in codes.lower()


def _smallest_unit(number_format: str) -> int:
    """Seconds in the smallest time unit the first section shows: 1 for seconds, 60 for minutes, otherwise 3,600. Quoted text,
    backslash-escaped characters and bracket sections other than elapsed ones (`[Magenta]`) are not format codes."""
    codes = _FORMAT_ESCAPED.sub("", _FORMAT_BRACKET.sub(
        lambda match: match.group(0) if _ELAPSED_FORMAT.fullmatch(match.group(0)) else "",
        _FORMAT_QUOTED.sub("", _first_section(number_format)))).lower()
    return 1 if "s" in codes else 60 if "m" in codes else 3600


def _first_section(number_format: str) -> str:
    section = _FORMAT_SECTION.match(number_format)
    return section.group(0) if section else number_format


def _time_text(value: time, number_format: str) -> str:
    """The time of a time-only format. A format showing hours (h) follows the format (`_clock_text`); `mm:ss` without hours stays
    `00:01:44` as before.

    `h:mm`, `h:mm AM/PM` and `h"시" mm"분"` cells came out as 24-hour `18:03:40` with seconds attached (Pinned by:
    test_spreadsheet_notes_pictures_and_times.py::TestTimeFormats). Deliberate: showing `mm:ss` without hours as `01:44`, as the
    format says, is easily read as 1 hour 44 minutes, so the hours are included (test_spreadsheet.py::TestTimeDisplay).
    """
    shown = _whole_seconds(value)
    if "h" in _code_letters(_first_section(number_format)):
        clock = _clock_text(shown.hour * 3600 + shown.minute * 60 + shown.second, number_format, elapsed=False)
        if clock is not None:
            return clock
    return _format_value(shown)


# Time format tokens: quoted text, backslash-escaped character, bracket section, AM/PM, hour/minute/second codes (fractional
# seconds `.0` are dropped), padding (`_x`, `*x`), any other character.
_CLOCK_TOKEN = re.compile(r'"([^"]*)"|\\(.)|\[([^\]]*)\]|(AM/PM|A/P)|([hms])\5*(?:\.0+)?|[_*].?|(.)', re.IGNORECASE | re.DOTALL)
_KOREAN_LOCALE = re.compile(r"\$-(?:412|ko)\b", re.IGNORECASE)


def _clock_text(seconds: int, number_format: str, *, elapsed: bool) -> str | None:
    """Write a number of seconds according to the first section of a time format: `h`/`hh` hours (two digits when doubled),
    `m`/`mm` minutes, `s`/`ss` seconds, `AM/PM` (12-hour clock; the Korean locale `[$-412]` gives 오전/오후), elapsed sections
    `[h]`/`[mm]`/`[ss]` as cumulative values, quoted and backslash-escaped characters as they are; colour/locale sections and
    padding are left out.
    None if there is no time code at all. It is called for time-only formats, so `m` is always minutes. As in Excel, anything below
    the smallest shown unit is truncated (no rounding)."""
    section = _first_section(number_format)
    tokens = list(_CLOCK_TOKEN.finditer(section))
    twelve = any(token.group(4) for token in tokens)
    korean = any(token.group(3) is not None and _KOREAN_LOCALE.match(token.group(3)) for token in tokens)
    units = {"h": 3600, "m": 60, "s": 1}
    largest = next((units[token.group(3)[0].lower()] for token in tokens
                    if elapsed and token.group(3) and _ELAPSED_FORMAT.fullmatch(token.group(0))), None)

    pieces, shown = [], False
    for token in tokens:
        quoted, escaped, bracket, am_pm, code, other = token.groups()
        if quoted is not None or escaped is not None:
            pieces.append(quoted if quoted is not None else escaped)
        elif bracket is not None:
            if elapsed and _ELAPSED_FORMAT.fullmatch(token.group(0)):
                width = len(bracket)
                pieces.append(f"{seconds // units[bracket[0].lower()]:0{width}d}")
                shown = True
        elif am_pm:
            morning = seconds // 3600 % 24 < 12
            if am_pm.upper() == "A/P":
                pieces.append("A" if morning else "P")
            else:
                pieces.append(("오전" if morning else "오후") if korean else ("AM" if morning else "PM"))
        elif code:
            unit = units[code.lower()]
            value = seconds // unit
            if largest is None or unit < largest:
                value %= 24 if unit == 3600 else 60
            if unit == 3600 and twelve:
                value = value % 12 or 12
            width = 2 if len(token.group(0).split(".")[0]) >= 2 else 1
            pieces.append(f"{value:0{width}d}")
            shown = True
        elif other is not None:
            pieces.append(other)

    return "".join(pieces).strip() if shown else None


# Splits a format string into `;` sections. A `;` inside quotes or after a backslash is a character.
_FORMAT_SECTION = re.compile(r'(?:"[^"]*"|\\.|[^;"\\])+|(?<=;)(?=;|$)|^(?=;)')


def _format_section(value: float, number_format: str) -> tuple[str, float, bool]:
    """Pick the section matching the sign of the value (positive;negative;zero;text), the value to put into that section, and
    whether the format itself shows the sign.

    Using only the first section turned -0.052 under `+0.0%;-0.0%` into `+-5.2%` and -1200 under `"▲"#,##0;"▼"#,##0` into
    `▲-1,200`. The negative section receives the absolute value (the sign is shown by the section's literals). An empty section
    shows nothing in Excel, but the first section is used so the value is not lost.
    The zero section is used only when it has a `0` place — imitating the `"-"??` of accounting formats emitted 0 as "-0", like a
    negative number (Pinned by: test_spreadsheet.py::TestNumberFormatSections). A format whose first section has no digit places
    (`"예";"예";"아니요"`) shows text per section, so the zero section is used as is — using the first section would turn 0 into
    "예" (Pinned by: test_spreadsheet.py::TestTextOnlyNumberFormats).
    Deliberate: if the negative section has no literal revealing that the value is negative (no literals, or the same as the
    positive section — `#,##0;[Red]#,##0`), the negative value is passed in as is so that `-` is attached. Excel signals the
    negative with red, but colours are discarded, so emitting only the absolute value would make a loss read as a profit
    (Pinned by: test_spreadsheet.py::TestNegativeSectionWithoutSign).
    """
    sections = [match.group(0) for match in _FORMAT_SECTION.finditer(number_format)]
    if value < 0 and len(sections) > 1 and sections[1].strip():
        marked = _section_literals(sections[1]) not in ("", _section_literals(sections[0]))
        return sections[1], (-value if marked else value), marked
    if value == 0 and len(sections) > 2 and ("0" in _FORMAT_QUOTED.sub("", sections[2]) or not _has_digit_places(sections[0])):
        return sections[2], value, False
    return (sections[0] if sections else ""), value, False


def _has_digit_places(section: str) -> bool:
    """Whether the section has digit places (`0`, `#`, `?`). Quoted text, backslash-escaped characters and bracket sections are not
    looked into."""
    return re.search(r"[0#?]", _FORMAT_BRACKET.sub("", _FORMAT_ESCAPED.sub("", _FORMAT_QUOTED.sub("", section)))) is not None


# What is removed to keep only the literals of a section: bracket sections (colour, condition, currency), padding (`_x`, `*x`),
# digit places and separators, whitespace.
_FORMAT_NOT_LITERAL = re.compile(r'"([^"]*)"|\\(.)|\[[^\]]*\]|[_*].?|[0#?.,%\s]')


def _section_literals(section: str) -> str:
    """The literals a format section shows around the value (the `()` of `(#,##0)`, the `▼` of `"▼"#,##0`, the `-` of `-#,##0`)."""
    return _FORMAT_NOT_LITERAL.sub(lambda match: match.group(1) or match.group(2) or "", section)


# Scientific formats (not imitated; the original value is used). Besides `E+` Excel also uses `E-`, `e+` and `e-` — looking only for
# `E+` turned 0.0000123 under `0.00E-00` into `0.0000`
# (Pinned by: test_core_spreadsheet.py::TestScientificFormatsWithAMinusSign). Quoted text and backslash-escaped characters are ignored.
_SCIENTIFIC = re.compile(r"E[+-]", re.IGNORECASE)
# Symbols and whitespace that can be used as literals without quotes in an Excel format.
_FORMAT_BARE_SYMBOLS = re.compile(r"[\s\-+()$:!^&'~{}<>=]*")


def _apply_number_format(value: float, number_format: str) -> str | None:
    if re.search(r"\[[<>=]", number_format):
        # A conditional format ([<=9999999]…;…) changes section depending on the value. It is not imitated; the original value is used
        return None
    if not math.isfinite(value):
        return None

    section, value, signed_by_format = _format_section(value, number_format)
    section = section.lstrip()  # trailing whitespace may be part of `_ ` (underscore + space), so it is not stripped
    if section.lower() in ("general", "@", "") or _SCIENTIFIC.search(_FORMAT_QUOTED.sub("", _FORMAT_ESCAPED.sub("", section))):
        return None

    def bracket(match: re.Match[str]) -> str:
        # A currency symbol (`[$₩-412]`) is a literal at the position where it sits in the format. Collecting it at the very front
        # emitted the negative section `\-[$₩-412]#,##0` as `₩-…` instead of Excel's `-₩…`
        # (Pinned by: test_spreadsheet.py::TestCurrencyPosition). Colour and locale sections are removed.
        symbol = match.group(1)[1:].split("-")[0] if match.group(1).startswith("$") else ""
        return f'"{symbol}"' if symbol and '"' not in symbol else ""

    section = _FORMAT_BRACKET.sub(bracket, section)
    section = _FORMAT_ESCAPED.sub(lambda match: f'"{match.group(1)}"', section)  # `\%` is the same literal as `"%"`
    literals = _FORMAT_QUOTED.findall(section)
    plain = _FORMAT_PADDING.sub("", _FORMAT_QUOTED.sub("\x00", section))
    if plain.replace("\x00", "").strip().lower() == "general":
        # The General format with only literals attached before/after (`General"명"`, `"약 "General`). The whole format was
        # discarded for having no digit places, and the unit was lost
        # (Pinned by: test_spreadsheet.py::TestGeneralFormatWithLiterals). `[Red]General`, with a colour but no literals, is the
        # original value.
        if not literals:
            return None
        before = plain.lower().index("general")
        pieces = iter(literals)
        prefix, suffix = ("".join(next(pieces, "") for _ in range(part.count("\x00")))
                          for part in (plain[:before], plain[before + len("general"):]))
        sign = "-" if value < 0 and not signed_by_format else ""
        return f"{sign}{prefix.lstrip()}{_format_value(abs(value))}{suffix.rstrip()}"
    if _FORMAT_DATE_TOKENS.search(plain.replace("\x00", "")) or "/" in plain:
        return None  # date and fraction (# ?/?) formats are not imitated; the original value is used

    def literal_text(part: str, pieces: Iterator[str] | None = None) -> str:
        pieces = iter(literals) if pieces is None else pieces
        return "".join(next(pieces, "") if char == "\x00" else char for char in part)

    # In a format that starts with the decimal point and has no integer places (`.00`), the number starts at the decimal point.
    # Cutting from the first digit place made the `.` a leading literal and the decimal places were read as integer places, so 0.5
    # became `.01` (Pinned by: test_spreadsheet.py::TestFormatsWithoutIntegerDigits).
    # A comma in front of the very first digit place is a thousands separator too (`\#,##0` → #1,234,567).
    start = re.search(r",*[0#?]|\.(?=[0#?])", plain)
    if start is None:
        # A section with only quoted/backslash-escaped literals and no digit places (`"매우 좋음"`) shows just those literals on
        # screen in Excel. Emitting the original value makes a rating of 5 read as 5 instead of "매우 좋음"
        # (Pinned by: test_spreadsheet.py::TestTextOnlyNumberFormats). A section without literals (colour-only `[Red]`, a bare
        # symbol `-`) or one mixed with a text placeholder (`@`) or unknown format codes is the original value.
        text = literal_text(plain).strip()
        if not literals or not text or not _FORMAT_BARE_SYMBOLS.fullmatch(plain.replace("\x00", "")):
            return None
        return f"{'-' if value < 0 and not signed_by_format else ''}{text}"
    first = start.start()
    last = max(index for index, char in enumerate(plain) if char in "0#?.,")

    prefix_raw, core, suffix_raw = plain[:first], plain[first:last + 1], plain[last + 1:]
    literals_before = prefix_raw.count("\x00")
    # Leading/trailing literals are attached in the order they appear, with the spaces inside and between quotes kept
    # (`"약 "#,##0"원"` → 약 1,000원, `0.00 "USD"` → 0.00 USD). Only the spaces at the outer ends are removed — removing all spaces
    # gave `약1,000원` and `0.00USD`, unlike the LibreOffice and Excel screens
    # (Pinned by: test_spreadsheet.py::TestNumberFormatLiteralsAndOptionalDecimals).
    prefix = literal_text(prefix_raw).lstrip()
    suffix = literal_text(suffix_raw.replace("%", ""), iter(literals[literals_before + core.count("\x00"):])).rstrip()
    # Deliberate: a % shielded by quotes or a backslash is a literal. Searching the original format string turned 2.5 under
    # `0.0"%p"` into "250.0%%p", 100 times too large (Pinned by: test_quality.py::TestSpreadsheetNumberFormats).
    percent = "%" in plain
    # A comma between digit places is a thousands separator; commas attached after the digit places mean dividing by 1,000 once
    # per comma for display (`#,##0,` = in thousands).
    # Ignoring the trailing commas emits the 1,235 of a "단위: 천원" (unit: thousand won) table as 1,234,567, read as 1,000 times
    # too large.
    whole, _, fraction = core.partition(".")
    scale = len(whole) - len(whole.rstrip(",")) + len(fraction) - len(fraction.rstrip(","))
    whole = whole.rstrip(",")
    places = "".join(char for char in fraction if char in "0#?")
    decimals = len(places)
    try:
        number = _rounded(value, percent, scale, decimals)
    except InvalidOperation:
        return None  # a format with hundreds of decimal places — the original value is used
    text = f"{number:,.{decimals}f}" if "," in whole else f"{number:.{decimals}f}"
    # The sign goes before the leading literals (currency symbol included) (Excel: -₩1,200). For a value that used the negative
    # section the sign is shown by the format's literals.
    sign = "-" if text.startswith("-") and not signed_by_format else ""
    integer, dot, rest = text.removeprefix("-").partition(".")
    if places.endswith(("#", "?")):
        rest = _without_trailing_zeros(places, rest)
        dot = dot if rest else ""  # 12 under `0.###` → 12 (same as LibreOffice)
    if any(char not in "0#?,%" for char in fraction):
        # Literals between decimal places (`0.0\E+0`, `0.00" "00`) stay where they are, the decimal digits filling the places from
        # the left as in Excel. They were dropped, so 5 under `0.0\E+0` came out as `5.00` instead of `5.0E+0`
        # (Pinned by: test_audit_spreadsheet_fixes.py::TestLiteralsBetweenDecimalPlaces).
        rest = _fill_decimals(fraction, literals[literals_before + whole.count("\x00"):], rest)
    if (any(char not in "0#?," for char in whole) or "?" in whole) and "," not in whole:
        # A format with literals between digit places (phone number `0##\-####\-####`, date shape `0000\-00\-00`) fills the places
        # from the right and inserts the literals. Dropping the literals turned 010-1234-5678 into 1012345678
        # (Pinned by: test_quality.py::TestSpreadsheetNumberFormats). An integer place `?` without a digit is a space, as in
        # Excel (0.5 under `?.??` → ` .5 `, Pinned by: test_audit_spreadsheet.py::TestQuestionMarkPlaces).
        integer = _fill_digits(whole, literals[literals_before:], integer)
    elif whole.count("0") > 1 and "," not in whole:
        # The 0s of the integer part are the minimum number of digits (employee number / postal code 001234 written as `000000`)
        integer = integer.zfill(whole.count("0"))
    elif not whole and integer == "0":
        integer = ""  # for a format without integer places Excel does not show the 0 either (0.5 under `.00` → .50)
    return f"{sign}{prefix}{integer}{dot}{rest}{'%' if percent else ''}{suffix}"


def _without_trailing_zeros(places: str, digits: str) -> str:
    """The decimal digits with the zeros at the end that fall on optional places taken away: a `#` place shows nothing and a `?`
    place a space, as in Excel. A `0` place or a digit other than 0 ends it.

    A `?` place showed its 0, so 1.5 under `0.??` came out as `1.50` instead of Excel's `1.5 ` (the space keeps the column
    aligned on the decimal point; Pinned by: test_audit_spreadsheet.py::TestQuestionMarkPlaces)."""
    kept = list(digits)
    for index in range(len(places) - 1, -1, -1):
        if places[index] == "0" or kept[index] != "0":
            break
        kept[index] = " " if places[index] == "?" else ""
    return "".join(kept)


def _rounded(value: float, percent: bool, scale: int, decimals: int) -> Decimal:
    """Apply percent and scaling by thousands as the format says and round to `decimals` decimal places.

    Excel rounds 0.5 away from zero. With Python format specs (half to even, binary representation) 72.5 became 72 and 0.125
    became 0.12. Rounding is done on the decimal representation shown on screen (`repr`)
    (Pinned by: test_spreadsheet.py::TestNumberFormatRounding).
    """
    with localcontext() as context:
        context.prec = 400  # even 1e308 keeps its integer part
        number = Decimal(repr(value))
        if percent:
            number *= 100
        return number.scaleb(-3 * scale).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)


def _fill_decimals(template: str, literals: list[str], digits: str) -> str:
    """Put the decimal digits into the decimal-part format (`0`/`#`/`?` places and literals) from the left. A `?` without a digit is
    a space and a `#` without one is nothing; scaling commas and `%` are not shown here."""
    pieces, remaining = iter(literals), iter(digits)
    filled = []
    for char in template:
        if char in "0#?":
            filled.append(next(remaining, " " if char == "?" else ""))
        elif char == "\x00":
            filled.append(next(pieces, ""))
        elif char not in ",%":
            filled.append(char)
    return "".join(filled)


def _fill_digits(template: str, literals: list[str], integer: str) -> str:
    """Fill the digits of the integer into the integer-part format (`0`/`#`/`?` places and literals) from the right. Leftover digits
    are put at the very front (same as Excel)."""
    pieces = iter(literals)
    tokens = [(char in "0#?", next(pieces, "") if char == "\x00" else char) for char in template]
    remaining = integer.lstrip("0")

    filled = []
    for is_digit, text in reversed(tokens):
        if not is_digit:
            filled.append(text)
        elif remaining:
            filled.append(remaining[-1])
            remaining = remaining[:-1]
        elif text != "#":
            filled.append("0" if text == "0" else " ")
    return remaining + "".join(reversed(filled))


def _format_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() and abs(value) < 1e15 else format(value, ".15g")
    if isinstance(value, (datetime, time)):
        value = _whole_seconds(value)
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time(0) else value.isoformat(sep=" ")
    if isinstance(value, (date, time)):
        return value.isoformat()
    return str(value).strip()


def _whole_seconds(value: datetime | time) -> datetime | time:
    """Round below the second. A time converted from a serial number keeps floating-point error and fractional seconds, and came
    out as `00:01:43.680000` (Excel shows `01:44`). A time wraps to 0 o'clock when it passes the day (same as Excel's time
    formats). Known accepted gap: fractional-second formats (`ss.0`) are also shown only down to the second
    (Pinned by: test_spreadsheet.py::TestTimeDisplay)."""
    if value.microsecond < 500_000:
        return value.replace(microsecond=0)
    if isinstance(value, time):
        return (datetime.combine(date.min, value.replace(microsecond=0)) + timedelta(seconds=1)).time()
    try:
        return value.replace(microsecond=0) + timedelta(seconds=1)
    except OverflowError:
        return value.replace(microsecond=0)
