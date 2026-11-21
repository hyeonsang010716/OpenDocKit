"""A number format is worked out once per format and sign (`_sheet_format._number_plan`); only the value is handled per cell.
The text is what Excel shows, whatever the sign, the section taken or the decimal context of the calling thread."""

from decimal import InvalidOperation, localcontext

import pytest

from opendockit.extractors import _sheet_format
from opendockit.extractors._sheet_format import format_cell


@pytest.mark.parametrize(("number_format", "value", "expected"), [
    ("#,##0.00", 1234.565, "1,234.57"),
    ("#,##0.00", -1234.5, "-1,234.50"),
    ("#,##0.00", -0.0, "0.00"),
    ("#,##0.00", -0.001, "-0.00"),
    ("0.0%", 0.125, "12.5%"),
    ('"▲"#,##0;"▼"#,##0', -1200, "▼1,200"),
    ('"▲"#,##0;"▼"#,##0', 1200, "▲1,200"),
    ("#,##0;[Red]#,##0", -5, "-5"),
    ('_-* #,##0_-;-* #,##0_-;_-* "-"??_-;_-@_-', 0, "0"),  # a zero section without a 0 place is not used
    ('"예";"예";"아니요"', 0, "아니요"),
    ('"예";"예";"아니요"', -1, "예"),
    ('General"명"', 12, "12명"),
    ('General"명"', -12, "-12명"),
    ("000000", 1234, "001234"),
    ("00,000", 1234, "01,234"),
    ("#.##", 0.5, ".5"),
    ("0.??", 1.5, "1.5 "),
    ("0##\\-####\\-####", 1012345678, "010-1234-5678"),
    ("0.0\\E+0", 5, "5.0E+0"),
    ("#,##0,", 1234567, "1,235"),
    ('"약 "#,##0"원"', 1000, "약 1,000원"),
])
def test_values_take_the_section_of_their_sign(number_format, value, expected):
    _sheet_format._number_plan.cache_clear()
    assert format_cell(value, number_format) == expected
    assert format_cell(value, number_format) == expected  # the second time from the plan kept for the format


def test_a_format_is_worked_out_once_per_sign():
    _sheet_format._number_plan.cache_clear()
    for index in range(1_000):
        format_cell(index * 1.5 - 300, "#,##0.00;[Red]-#,##0.00;0")
    assert _sheet_format._number_plan.cache_info().misses == 3


def test_the_callers_decimal_context_changes_nothing():
    expected = [format_cell(value, "0.00") for value in (2.675, 72.5, 1e15 + 0.5)]
    with localcontext() as context:
        context.prec = 3
        context.traps[InvalidOperation] = False
        assert [format_cell(value, "0.00") for value in (2.675, 72.5, 1e15 + 0.5)] == expected
        # A format with more places than can be computed shows the value, as in any other context
        assert format_cell(1.5, "0." + "0" * 500) == "1.5"
    assert expected == ["2.68", "72.50", "1000000000000000.00"]  # rounded from 15 significant digits, as Excel shows it
