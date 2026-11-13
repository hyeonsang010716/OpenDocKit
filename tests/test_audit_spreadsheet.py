"""Spreadsheets: small files that used to cost seconds or gigabytes, and single odd values that used to reject the whole
document (audit of `extractors/spreadsheet.py`)."""

from io import BytesIO
import zipfile

import openpyxl
import pytest

from opendockit import convert
from opendockit.extractors import spreadsheet
from opendockit.models import ConvertOptions
from tests import builder


_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_SHEET = "xl/worksheets/sheet1.xml"
_HUGE_MERGE = '<mergeCells count="1"><mergeCell ref="A1:CV20000"/></mergeCells>'  # more cells than a full load accepts: streamed


def _workbook() -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["name", "value"])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _with_sheet_xml(xml: str, data: bytes | None = None) -> bytes:
    return builder.zip_with(data or _workbook(), edit={_SHEET: lambda _: xml})


def _text_row(number: int | str, *values: str) -> str:
    cells = "".join(f'<c t="inlineStr"><is><t>{value}</t></is></c>' if value else "<c/>" for value in values)
    return f'<row r="{number}">{cells}</row>'


def _sheet_xml(rows: str, after: str = "") -> str:
    return f'<worksheet xmlns="{_MAIN}"><sheetData>{rows}</sheetData>{after}</worksheet>'


class TestStreamedRowsFarApart:
    """A streamed sheet got an empty row for every row number the file skips. Rows 4,999 apart never reached the 5,000-empty-row
    stop, so a 14 KB file with 2,000 rows made ten million empty rows (7.6 s, 2.6 GB)."""

    def _collected(self, monkeypatch: pytest.MonkeyPatch) -> list[int]:
        """Number of rows handed to the renderer, per sheet."""
        counts: list[int] = []
        sheet_text = spreadsheet._sheet_text

        def counting(rows: list[list[str]], across: object = None) -> str:
            counts.append(len(rows))
            return sheet_text(rows, across)

        monkeypatch.setattr(spreadsheet, "_sheet_text", counting)
        return counts

    def test_skipped_row_numbers_cost_one_empty_row(self, monkeypatch):
        """Pins: one empty row per gap in a streamed sheet, whatever the row numbers."""
        counts = self._collected(monkeypatch)
        rows = "".join(_text_row(index * 4_999, f"item {index}") for index in range(1, 301))  # past row 1,048,576 of Excel

        result = convert(_with_sheet_xml(_sheet_xml(rows, _HUGE_MERGE)), "a.xlsx")

        assert result.text.count("item ") == 300
        assert result.text.endswith("item 300")
        assert not result.warnings
        assert counts == [600]

    def test_gaps_render_as_they_did(self, monkeypatch):
        monkeypatch.setattr(spreadsheet, "_FULL_LOAD_MAX_SHEET_BYTES", 0)
        rows = (_text_row(2, "Sales") + _text_row(3, "team", "amount") + _text_row(4, "A", "10") + _text_row(7, "B", "20")
                + _text_row(20, "Staff") + _text_row(21, "team", "people", "seats") + _text_row(22, "A", "3", "4")
                + "<row r=\"30\"/><row r=\"31\"><c r=\"A31\"/></row>" + _text_row(40, "* source: HR"))

        result = convert(_with_sheet_xml(_sheet_xml(rows)), "a.xlsx")

        assert result.text == ("## Sheet: Sheet\n\nSales\n\n| team | amount |\n| --- | --- |\n| A | 10 |\n| B | 20 |\n\n"
                               "Staff\n\n| team | people | seats |\n| --- | --- | --- |\n| A | 3 | 4 |\n\n* source: HR")
        assert not result.warnings

    def test_rows_out_of_order_are_dropped_as_before(self, monkeypatch):
        monkeypatch.setattr(spreadsheet, "_FULL_LOAD_MAX_SHEET_BYTES", 0)
        rows = (_text_row(0, "zero", "x") + _text_row(5, "five", "x") + _text_row(3, "three", "x") + _text_row(5, "again", "x")
                + "<row><c t=\"inlineStr\"><is><t>next</t></is></c><c t=\"inlineStr\"><is><t>x</t></is></c></row>"
                + _text_row("abc", "unnumbered", "x"))

        result = convert(_with_sheet_xml(_sheet_xml(rows)), "a.xlsx")

        assert result.text == "## Sheet: Sheet\n\n| five | x |\n| --- | --- |\n| next | x |\n| unnumbered | x |"

    @pytest.mark.parametrize(("rows", "first_unread"), [
        (_text_row(1, "first") + _text_row(6_000, "far"), 2),
        (_text_row(1, "first") + '<row r="3000"/>' + _text_row(6_000, "far"), 2),
        (_text_row(1, "first") + _text_row(3, "third") + "".join(f'<row r="{number}"/>' for number in range(4, 3_000))
         + _text_row(8_100, "far"), 4),
        (_text_row(5_001, "far"), 1),
    ], ids=["gap", "empty row in the gap", "empty rows then a gap", "gap at the top"])
    def test_a_run_of_five_thousand_empty_rows_still_ends_the_sheet(self, monkeypatch, rows, first_unread):
        monkeypatch.setattr(spreadsheet, "_FULL_LOAD_MAX_SHEET_BYTES", 0)

        result = convert(_with_sheet_xml(_sheet_xml(rows)), "a.xlsx")

        assert "far" not in result.text
        assert result.warnings[0] == (
            f"Sheet 'Sheet': 5,000 consecutive empty rows were found, so nothing was read from row {first_unread:,} on.")

    def test_a_gap_just_short_of_the_stop_is_read_through(self, monkeypatch):
        monkeypatch.setattr(spreadsheet, "_FULL_LOAD_MAX_SHEET_BYTES", 0)
        rows = _text_row(1, "first") + '<row r="3000"/>' + _text_row(5_001, "far")

        result = convert(_with_sheet_xml(_sheet_xml(rows)), "a.xlsx")

        assert result.text == "## Sheet: Sheet\n\nfirst\n\nfar"
        assert not result.warnings

    def test_formula_without_a_result_in_a_skipped_row_keeps_its_place(self):
        rows = (_text_row(1, "name", "total") + '<row r="2"><c r="A2" t="inlineStr"><is><t>a</t></is></c>'
                '<c r="B2"><f>1+1</f><v>2</v></c></row>'
                + '<row r="4000"><c r="B4000"><f>SUM(B1:B2)</f></c></row>' + _text_row(8_000, "end", "9"))

        result = convert(_with_sheet_xml(_sheet_xml(rows, _HUGE_MERGE)), "a.xlsx")

        assert result.text == "## Sheet: Sheet\n\n| name | total |\n| --- | --- |\n| a | 2 |\n| | =SUM(B1:B2) |\n| end | 9 |"
