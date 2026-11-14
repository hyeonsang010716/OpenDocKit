"""Spreadsheets: small files that used to cost seconds or gigabytes, and single odd values that used to reject the whole
document (audit of `extractors/spreadsheet.py`)."""

from datetime import time as clock_time
from datetime import timedelta
from io import BytesIO
import struct
import time
from types import SimpleNamespace
import zipfile

import openpyxl
import pytest

from opendockit import convert
from opendockit.exceptions import CorruptedDocumentError
from opendockit.extractors import _sheet_layout, _xlsx_parts, spreadsheet, xls, xml_spreadsheet
from opendockit.extractors._sheet_format import format_cell
from opendockit.models import ConvertOptions
from tests import builder
from tests.test_spreadsheet_cell_values_and_controls import (
    _LOCAL_IMAGE,
    _rich_value_workbook,
    _vml_control,
    _workbook_with_controls,
)
from tests.timing import allowed


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
        sheet_text = _sheet_layout._sheet_text

        def counting(rows: list[list[str]], across: object = None) -> str:
            counts.append(len(rows))
            return sheet_text(rows, across)

        monkeypatch.setattr(_sheet_layout, "_sheet_text", counting)
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


def _moved(data: bytes, old: str = _SHEET, new: str = "xl/ws/sheet1.xml") -> bytes:
    """The same workbook with a sheet part stored at another path (relationship and content type follow)."""
    buffer = BytesIO()
    with zipfile.ZipFile(BytesIO(data)) as source, zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for name in source.namelist():
            content = source.read(name)
            if name in ("xl/_rels/workbook.xml.rels", "[Content_Types].xml"):
                assert f"/{old}".encode() in content
                content = content.replace(f"/{old}".encode(), f"/{new}".encode())
            target.writestr(new if name == old else name, content)
    return buffer.getvalue()


class TestSheetStoredAtAnotherPath:
    """The checks that decide streaming looked only at parts under `xl/worksheets/`. openpyxl follows the workbook relationships,
    so the same sheet stored at `xl/ws/sheet1.xml` was fully loaded: one merge A1:CV20000 took 9 s and 1.5 GB."""

    @pytest.fixture
    def load_modes(self, monkeypatch: pytest.MonkeyPatch) -> list[bool]:
        modes: list[bool] = []
        original_load = openpyxl.load_workbook

        def recording_load(*args: object, **kwargs: object) -> object:
            modes.append(kwargs.get("read_only"))
            return original_load(*args, **kwargs)

        monkeypatch.setattr(spreadsheet.openpyxl, "load_workbook", recording_load)
        return modes

    def test_huge_merge_in_a_moved_sheet_is_streamed(self, load_modes):
        data = _moved(_with_sheet_xml(_sheet_xml(_text_row(1, "name", "value") + _text_row(2, "a", "1"), _HUGE_MERGE)))

        result = convert(data, "a.xlsx")

        assert result.text == "## Sheet: Sheet\n\n| name | value |\n| --- | --- |\n| a | 1 |"
        assert load_modes == [True]

    def test_large_moved_sheet_is_streamed(self, load_modes, monkeypatch):
        monkeypatch.setattr(spreadsheet, "_FULL_LOAD_MAX_SHEET_BYTES", 100)

        result = convert(_moved(_workbook()), "a.xlsx")

        assert "name" in result.text
        assert load_modes == [True]

    def test_formulas_without_results_in_a_moved_sheet_are_copied(self, load_modes):
        workbook = openpyxl.Workbook()
        workbook.active.append(["a", 1, "=B1*2"])
        buffer = BytesIO()
        workbook.save(buffer)

        result = convert(_moved(buffer.getvalue()), "a.xlsx")

        assert "=B1*2" in result.text
        assert any("Formula results are not stored" in warning for warning in result.warnings)
        assert load_modes == [False]

    def test_small_moved_sheet_is_fully_loaded_with_its_merges(self, load_modes):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["group", "name"])
        sheet.append(["A", "x"])
        sheet.append([None, "y"])
        sheet.merge_cells("A2:A3")
        buffer = BytesIO()
        workbook.save(buffer)

        result = convert(_moved(buffer.getvalue()), "a.xlsx")

        assert result.text == "## Sheet: Sheet\n\n| group | name |\n| --- | --- |\n| A | x |\n| A | y |"
        assert load_modes == [False]


_CONTROL_RELATIONS = (
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/ctrlProp" Target="../ctrlProps/ctrlProp1.xml"/>'
    "</Relationships>")


@pytest.mark.timing
class TestUnclosedTagsInSheetXml:
    """The searches over raw sheet XML let an unclosed tag run to the end of the part, once per tag: quadratic time. A 4.7 KB
    file whose sheet is `<c ` 80,000 times took 50 s before openpyxl even saw it. The old code needed 20 s or more for each of
    these inputs."""

    def test_unclosed_cells(self):
        data = builder.zip_with(_workbook(), edit={_SHEET: lambda _: "<c " * 80_000})
        started = time.perf_counter()

        with pytest.raises(CorruptedDocumentError):
            convert(data, "a.xlsx")

        assert time.perf_counter() - started < allowed(5)

    def test_unclosed_formulas(self):
        with zipfile.ZipFile(BytesIO(builder.zip_with(_workbook(), edit={_SHEET: lambda _: "<f " * 200_000}))) as archive:
            started = time.perf_counter()

            assert spreadsheet._formulas_without_results(archive, [_SHEET]) == (True, set())

        assert time.perf_counter() - started < allowed(5)

    @pytest.mark.parametrize(("part", "tag"), [(_SHEET, "<calcPr "), ("xl/workbook.xml", "<sheet ")])
    def test_unclosed_settings(self, part, tag):
        data = builder.zip_with(_workbook(), edit={part: lambda _: tag * 100_000 + "<sheetViews></sheetViews>"})
        started = time.perf_counter()

        assert spreadsheet._without_view_settings(data) is not None

        assert time.perf_counter() - started < allowed(5)

    def test_unclosed_controls(self):
        data = builder.zip_with(_workbook(), edit={_SHEET: lambda _: "<control " * 100_000},
                                add={"xl/worksheets/_rels/sheet1.xml.rels": _CONTROL_RELATIONS})
        with zipfile.ZipFile(BytesIO(data)) as archive:
            started = time.perf_counter()

            assert _xlsx_parts._checked_control_shapes(archive, set(archive.namelist()), _SHEET) == set()

        assert time.perf_counter() - started < allowed(5)


class TestSheetXmlSearchesFindWhatTheyFound:
    """The searches bounded for `TestUnclosedTagsInSheetXml` still find the same things in ordinary XML."""

    def test_formula_cells(self):
        with_result = b'<c r="A1" s="1"><f>1+1</f><v>2</v></c><c r="B1"><f t="shared" si="0"/><v>3</v></c>'
        without = b'<x:c r="A1" s="1"><x:f>1+1</x:f></x:c>'

        assert len(spreadsheet._FORMULA_WITH_RESULT.findall(with_result)) == 2
        assert spreadsheet._FORMULA_WITHOUT_RESULT.search(with_result) is None
        assert spreadsheet._FORMULA_WITHOUT_RESULT.search(without).group() == without
        assert spreadsheet._FORMULA_WITHOUT_RESULT.search(b'<c r="A1"><f t="shared" si="0"/><v></v></c>') is not None
        assert spreadsheet._FORMULA_WITHOUT_RESULT.search(b'<c r="A1" t="str"><f>""</f><v></v></c>') is None

    def test_settings_and_sheet_state(self):
        xml = b'<a><calcPr calcId="1" fullCalcOnLoad="1"/><sheetViews><sheetView tabSelected="1"/></sheetViews><b/></a>'

        assert spreadsheet._VIEW_SETTINGS.sub(b"", xml) == b"<a><b/></a>"
        assert spreadsheet._SHEET_STATE.search(b'<sheet name="a" sheetId="1" state="Hidden" r:id="rId1"/>').group(2) == b'"Hidden"'
        assert spreadsheet._SHEET_STATE.search(b'<sheet name="a"/><sheets state="x"/>') is None

    def test_control_elements(self):
        xml = b'<controls><mc:x><control shapeId="1025" r:id="rId1" name="a"><controlPr/></control></mc:x></controls>'

        assert _xlsx_parts._CONTROL_ELEMENT.findall(xml) == [b'<control shapeId="1025" r:id="rId1" name="a">']


def _bof(kind: int) -> bytes:
    return builder.xls_record(0x0809, struct.pack("<HHHHII", 0x0600, kind, 0x0DBB, 0x07CC, 0, 6))


_WORKSHEET, _CHART, _EOF, _OTHER = _bof(0x0010), _bof(0x0020), builder.xls_record(0x000A), builder.xls_record(0x0200, b"\0" * 14)


def _biff_stream(sheets: list[int | None], body: bytes) -> bytes:
    """A workbook stream whose sheet list points at the given offsets inside `body` (None: past the end of the stream)."""
    head = _bof(0x0005)
    base = len(head) + len(sheets) * (4 + 9) + len(_EOF)
    listed = b"".join(builder.xls_record(0x0085, struct.pack("<IBBBB", 2**31 if offset is None else base + offset, 0, 0, 1, 0)
                                         + b"s") for offset in sheets)
    return head + listed + _EOF + body


class TestXlsChartScan:
    """Looking for a chart placed on a sheet walked the stream again from every sheet offset, to the end when no end-of-sheet
    record follows: 2,000 sheet entries over 200,000 empty records (808 KB) took 64 s."""

    def test_stream_is_walked_a_fixed_number_of_times(self, monkeypatch):
        """Pins: the chart scan reads the workbook stream in one pass, whatever the number of sheets."""
        read = 0
        records = xls._biff_records

        def counting(stream: bytes, start: int = 0) -> object:
            nonlocal read
            for record in records(stream, start):
                read += 1
                yield record

        monkeypatch.setattr(xls, "_biff_records", counting)
        stream = _biff_stream([index * 4 for index in range(300)], builder.xls_record(0x0001) * 20_000)

        assert xls._xls_sheets_holding_charts(stream) == set()
        assert read < 3 * 20_302

    def test_nested_sheets_are_walked_once(self, monkeypatch):
        read = 0
        records = xls._biff_records

        def counting(stream: bytes, start: int = 0) -> object:
            nonlocal read
            for record in records(stream, start):
                read += 1
                yield record

        monkeypatch.setattr(xls, "_biff_records", counting)
        stream = _biff_stream([index * len(_WORKSHEET) for index in range(300)], _WORKSHEET * 3_000 + _CHART)

        assert xls._xls_sheets_holding_charts(stream) == set(range(300))
        assert read < 3 * 3_302

    def test_which_sheets_hold_a_chart(self):
        parts = [
            _WORKSHEET + _OTHER + _CHART + _EOF + _EOF,   # 0: a chart placed on the sheet
            _WORKSHEET + _OTHER + _EOF,                    # 1: no chart
            _CHART + _EOF,                                 # 2: a chart of its own is not a chart placed on a sheet
            _OTHER + _WORKSHEET + _CHART + _EOF + _EOF,    # 3: the offset points before the start of the sheet
            _EOF,                                          # 4: the offset points at an end record
            _WORKSHEET + _WORKSHEET + _EOF + _CHART + _EOF + _EOF,   # 5: a chart after a nested part
            _WORKSHEET + _WORKSHEET + _CHART + _EOF + _EOF + _EOF,   # 6: a chart inside a nested part
            _CHART + _WORKSHEET + _EOF + _OTHER + _CHART,  # 7: never closed, a chart inside
        ]
        offsets = [sum(map(len, parts[:index])) for index in range(len(parts))]
        nested = offsets[6] + len(_WORKSHEET)  # 10: the nested part of 6 on its own

        stream = _biff_stream([*offsets, offsets[0], None, nested], b"".join(parts))

        assert xls._xls_sheets_holding_charts(stream) == {0, 3, 5, 6, 7, 8, 10}

    def test_offset_into_the_middle_of_a_record_is_not_looked_at(self):
        """Deliberate: such a sheet entry gets no chart warning (it would need a walk of its own)."""
        body = builder.xls_record(0x0200, _WORKSHEET + _CHART + _EOF + _EOF)

        assert xls._xls_sheets_holding_charts(_biff_stream([4], body)) == set()


def _xml_workbook(table: str, styles: str = "") -> bytes:
    return ('<?xml version="1.0" encoding="UTF-8"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
            f'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><Styles>{styles}</Styles>'
            f'<Worksheet ss:Name="s"><Table>{table}</Table></Worksheet></Workbook>').encode()


def _xml_cell(value: object, kind: str = "String", attributes: str = "") -> str:
    return f'<Cell {attributes}><Data ss:Type="{kind}">{value}</Data></Cell>'


_STYLED_ROWS = ("<Row>" + _xml_cell("name") + _xml_cell("amount") + "</Row>"
                "<Row>" + _xml_cell("part", attributes='ss:StyleID="s0"') + _xml_cell(1.5, "Number", 'ss:StyleID="s0"') + "</Row>")


class TestXmlSpreadsheetStyleChains:
    """Every style walked its own `ss:Parent` chain again: 8,000 chained styles (310 KB) took 44 s."""

    @staticmethod
    def _chain(length: int, last: str) -> str:
        return "".join(f'<Style ss:ID="s{index}" ss:Parent="s{index + 1}"/>' for index in range(length - 1)) + last

    def test_long_parent_chain_is_followed_once(self, monkeypatch):
        """Pins: the format and indent a style inherits are worked out once per style."""
        calls = 0
        attribute = xml_spreadsheet._ss

        def counting(element: object, name: str) -> str | None:
            nonlocal calls
            calls += 1
            return attribute(element, name)

        monkeypatch.setattr(xml_spreadsheet, "_ss", counting)
        styles = self._chain(600, '<Style ss:ID="s599"><NumberFormat ss:Format="0.00"/><Alignment ss:Indent="1"/></Style>')

        result = convert(_xml_workbook(_STYLED_ROWS, styles), "a.xml")

        assert result.text == "## Sheet: s\n\n| name | amount |\n| --- | --- |\n| └ part | 1.50 |"
        assert calls < 30 * 600

    def test_nearest_format_and_indent_win(self):
        styles = ('<Style ss:ID="s0" ss:Parent="s1"/><Style ss:ID="s1" ss:Parent="s2"><Alignment ss:Indent="0"/></Style>'
                  '<Style ss:ID="s2" ss:Parent="s3"><NumberFormat ss:Format="0.0"/><Alignment ss:Indent="2"/></Style>'
                  '<Style ss:ID="s3"><NumberFormat ss:Format="0.000"/></Style>')

        result = convert(_xml_workbook(_STYLED_ROWS, styles), "a.xml")

        assert result.text == "## Sheet: s\n\n| name | amount |\n| --- | --- |\n| part | 1.5 |"

    @pytest.mark.parametrize("styles", [
        '<Style ss:ID="s0" ss:Parent="s1"/><Style ss:ID="s1" ss:Parent="s2"/><Style ss:ID="s2" ss:Parent="s1"/>',
        '<Style ss:ID="s2" ss:Parent="s1"/><Style ss:ID="s1" ss:Parent="s2"/><Style ss:ID="s0" ss:Parent="s1"/>',
        '<Style ss:ID="s0" ss:Parent="s0"/>',
        '<Style ss:ID="s0" ss:Parent="missing"/>',
    ], ids=["into a loop", "loop first", "its own parent", "missing parent"])
    def test_parents_that_lead_nowhere_fall_back_to_the_default_style(self, styles):
        default = '<Style ss:ID="Default"><NumberFormat ss:Format="0.000"/><Alignment ss:Indent="3"/></Style>'

        result = convert(_xml_workbook(_STYLED_ROWS, default + styles), "a.xml")

        assert result.text == "## Sheet: s\n\n| name | amount |\n| --- | --- |\n| part | 1.500 |"


class TestNumberLikeText:
    """The test for cell text that looks like a number had two whitespace runs with only an optional currency sign between them,
    so a sign followed by N spaces and no digit was tried N² ways: a 156 KB CSV with four such cells took 17 s."""

    @pytest.mark.timing
    def test_sign_followed_by_many_spaces(self):
        data = (("-" + " " * 60_000 + "x\n\n") * 4).encode()  # one-cell rows between blank lines are tested for being notes
        started = time.perf_counter()

        result = convert(data, "a.csv", ConvertOptions(max_chars=None))

        assert result.text == "- x\n\n- x\n\n- x\n\n- x"
        assert time.perf_counter() - started < allowed(5)  # 30 s or more before

    @pytest.mark.parametrize("text", ["12", "-12", "- 12", "₩1,200", "₩ 1,200", "- ₩ 1,200", "-  ₩  1,200", "( $ 3.5 )", "(1,234)",
                                      "▲ 3.5%", "△12", "+ € 7", "1,200원", "12 명", "3.2 %p", "2024-01-02", "9:30", "-12:30:05",
                                      "− ¥\t5", " £ 5"])
    def test_numbers_are_still_numbers(self, text):
        assert _sheet_layout._NUMBER_LIKE.fullmatch(text) is not None

    @pytest.mark.parametrize("text", ["", "-", "₩", "- ₩", "x", "- x", "₩₩1", "-+1", "$ - 1", "1 2", "12a", "a12", "1월", "( )"])
    def test_other_text_is_still_text(self, text):
        assert _sheet_layout._NUMBER_LIKE.fullmatch(text) is None


class TestNegativeValueTooLargeForADuration:
    """A negative value under an elapsed-time format that does not fit in a duration (a billion days or more) raised
    OverflowError and the whole document was rejected. The positive value was already shown as the number."""

    @pytest.mark.parametrize(("value", "number_format", "expected"), [
        (-1e12, "[h]:mm", "-1000000000000"), (1e12, "[h]:mm", "1000000000000"), (-1e12, "[ss]", "-1000000000000"),
        (-0.5, "[h]:mm", "-12:00"), (-1e12, "h:mm", "-24000000000000:00:00"),
        (float("-inf"), "[h]:mm", "-inf"), (float("-inf"), "h:mm", "-inf"), (float("-inf"), "yyyy-mm-dd", "-inf"),
    ])
    def test_format_cell(self, value, number_format, expected):
        assert spreadsheet.format_cell(value, number_format) == expected

    def test_xlsx(self):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["name", "hours"])
        sheet.append(["a", -1e12])
        sheet.append(["b", 1.5])
        sheet["B2"].number_format = sheet["B3"].number_format = "[h]:mm"
        buffer = BytesIO()
        workbook.save(buffer)

        result = convert(buffer.getvalue(), "a.xlsx")

        assert result.text == "## Sheet: Sheet\n\n| name | hours |\n| --- | --- |\n| a | -1000000000000 |\n| b | 36:00 |"

    def test_xls(self):
        import xlrd

        cell = xlrd.sheet.Cell(xlrd.XL_CELL_DATE, -1234567890.0)

        assert spreadsheet.format_cell(xls._xls_value(cell, 0, "[h]:mm"), "[h]:mm") == "-1234567890"

    def test_xml_spreadsheet(self):
        styles = '<Style ss:ID="s0"><NumberFormat ss:Format="[h]:mm"/></Style>'
        rows = ("<Row>" + _xml_cell("name") + _xml_cell("hours") + "</Row>"
                "<Row>" + _xml_cell("a") + _xml_cell(-1e12, "Number", 'ss:StyleID="s0"') + "</Row>")

        result = convert(_xml_workbook(rows, styles), "a.xml")

        assert result.text == "## Sheet: s\n\n| name | hours |\n| --- | --- |\n| a | -1000000000000 |"


_TOO_LONG = "9" * 5_000  # decimal digits, yet int() refuses a number string of more than 4,300 digits


class TestIntegerAttributesTooLongToRead:
    """A whole number of more than 4,300 digits passes `isdecimal()` and `int()` raises ValueError: one attribute rejected the
    whole document."""

    @pytest.mark.parametrize("attribute", ["Index", "MergeAcross", "MergeDown"])
    def test_xml_cell_attribute_is_treated_as_absent_with_the_warning(self, attribute):
        rows = ("<Row>" + _xml_cell("name") + _xml_cell("amount") + "</Row>"
                "<Row>" + _xml_cell("part") + _xml_cell(3, "Number", f'ss:{attribute}="{_TOO_LONG}"') + "</Row>")

        result = convert(_xml_workbook(rows), "a.xml")

        assert result.text == "## Sheet: s\n\n| name | amount |\n| --- | --- |\n| part | 3 |"
        assert result.warnings == [f"Non-integer cell position and merge attributes ({attribute}) were ignored. "
                                   "Cells may have been placed in a different column."]

    def test_xml_row_and_column_attributes(self):
        rows = (f'<Column ss:Index="{_TOO_LONG}" ss:Span="{_TOO_LONG}"/><Row>' + _xml_cell("name") + _xml_cell("amount") + "</Row>"
                f'<Row ss:Index="{_TOO_LONG}">' + _xml_cell("part") + _xml_cell(3, "Number") + "</Row>")

        result = convert(_xml_workbook(rows), "a.xml")

        assert result.text == "## Sheet: s\n\n| name | amount |\n| --- | --- |\n| part | 3 |"
        assert result.warnings == ["Non-integer cell position and merge attributes (Index, Span) were ignored. "
                                   "Cells may have been placed in a different column."]

    def test_xml_style_indent_is_no_indent(self):
        styles = f'<Style ss:ID="s0"><Alignment ss:Indent="{_TOO_LONG}"/></Style>'

        result = convert(_xml_workbook(_STYLED_ROWS, styles), "a.xml")

        assert result.text == "## Sheet: s\n\n| name | amount |\n| --- | --- |\n| part | 1.5 |"

    @pytest.mark.parametrize("streamed", [False, True], ids=["full", "streamed"])
    def test_xlsx_cell_style_index_is_the_default_style(self, streamed, monkeypatch):
        if streamed:
            monkeypatch.setattr(spreadsheet, "_FULL_LOAD_MAX_SHEET_BYTES", 0)
        rows = (_text_row(1, "name", "amount")
                + f'<row r="2"><c r="A2" s="{_TOO_LONG}" t="inlineStr"><is><t>part</t></is></c><c r="B2" s="{_TOO_LONG}"><v>3</v></c></row>')

        result = convert(_with_sheet_xml(_sheet_xml(rows)), "a.xlsx")

        assert result.text == "## Sheet: Sheet\n\n| name | amount |\n| --- | --- |\n| part | 3 |"
        assert not result.warnings


class TestXmlDateTimeWithATimeZone:
    """A `DateTime` value with a time zone under an elapsed-time format was compared with a date without one: TypeError, and
    the whole document was rejected."""

    @staticmethod
    def _convert(value: str, number_format: str) -> str:
        styles = f'<Style ss:ID="s0"><NumberFormat ss:Format="{number_format}"/></Style>'
        rows = ("<Row>" + _xml_cell("name") + _xml_cell("time") + "</Row>"
                "<Row>" + _xml_cell("a") + _xml_cell(value, "DateTime", 'ss:StyleID="s0"') + "</Row>")
        result = convert(_xml_workbook(rows, styles), "a.xml")
        assert not result.warnings
        return result.text.removeprefix("## Sheet: s\n\n| name | time |\n| --- | --- |\n| a | ").removesuffix(" |")

    @pytest.mark.parametrize(("value", "number_format", "expected"), [
        ("1899-12-31T10:00:00Z", "[h]:mm", "10:00"),
        ("1899-12-31T10:00:00+09:00", "[h]:mm", "10:00"),
        ("1900-01-01T02:00:00Z", "[h]:mm", "26:00"),
        ("1900-01-01T02:00:00", "[h]:mm", "26:00"),
        ("1900-03-01T00:00:00Z", "[h]", "1464:00"),
        ("1900-03-01T00:00:00", "[h]", "1464:00"),
    ])
    def test_elapsed_time_is_the_time_as_written(self, value, number_format, expected):
        assert self._convert(value, number_format) == expected


class TestDamagedRichValueRelationships:
    """A relationships part of the in-cell pictures (`xl/richData/_rels/richValueRel.xml.rels`) that is not XML rejected the
    whole workbook. Only the pictures it points to are lost."""

    def test_pictures_are_left_blank_with_the_warning_and_data_types_are_kept(self):
        structures = _LOCAL_IMAGE + '<s t="_linkedEntity"><k n="%EntityId" t="s"/><k n="_DisplayString" t="s"/></s>'
        data = _rich_value_workbook(['<rv s="0"><v>0</v><v>5</v></rv>', '<rv s="1"><v>X2</v><v>Korea</v></rv>'], structures,
                                    pictures=1)
        data = builder.zip_with(data, edit={"xl/richData/_rels/richValueRel.xml.rels": lambda _: "<broken"})

        result = convert(data, "a.xlsx")

        assert result.sections[0].text == ("| 제품명 | 사진 | 단가 |\n| --- | --- | --- |\n| A형 펌프 | | 120000 |\n"
                                           "| B형 밸브 | Korea | 45000 |")
        assert result.warnings == ["1 in-cell picture or data-type value(s) could not be found and were left blank."]
        assert result.images == []


class TestPartsReferencedManyTimes:
    """A part was read and parsed again for every reference to it: 1,000 `<control>` elements pointing at one 2 MB control
    properties part (a 7 KB file) took 2.2 s, and the same for a chart placed many times."""

    @pytest.fixture
    def parsed(self, monkeypatch: pytest.MonkeyPatch) -> list[int]:
        """Sizes of the parts this module parsed."""
        sizes: list[int] = []
        parse_part = _xlsx_parts.parse_part

        def counting(data: bytes) -> object:
            sizes.append(len(data))
            return parse_part(data)

        monkeypatch.setattr(_xlsx_parts, "parse_part", counting)
        return sizes

    def test_control_properties_part_is_parsed_once(self, parsed):
        """Pins: a part is worked out once per conversion, however many references point at it."""
        shape = _vml_control("Checkbox", "agree", "1, 15, 4, 2, 2, 15, 5, 2", None).replace(
            "<v:shape ", '<v:shape id="_x0000_s1025" ')
        data = _workbook_with_controls(shape, control_properties={"1025": "Checked"})
        data = builder.zip_with(data, edit={_SHEET: lambda xml: xml.replace(
            "</controls>", '<control shapeId="1025" r:id="rIdC1" name="again"/>' * 300 + "</controls>")})

        result = convert(data, "a.xlsx")

        assert result.text.endswith("☒ agree")
        assert len(parsed) == 1

    def test_chart_placed_many_times_is_parsed_once(self, parsed):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        for row in (["month", "sales"], ["Jan", 10], ["Feb", 20]):
            sheet.append(row)
        chart = openpyxl.chart.BarChart()
        chart.title = "Sales"
        chart.add_data(openpyxl.chart.Reference(sheet, min_col=2, min_row=1, max_row=3), titles_from_data=True)
        sheet.add_chart(chart, "D2")
        buffer = BytesIO()
        workbook.save(buffer)
        once = convert(buffer.getvalue(), "a.xlsx").text
        parsed.clear()

        def repeated(xml: str) -> str:
            start, end = xml.index("<oneCellAnchor>"), xml.index("</oneCellAnchor>") + len("</oneCellAnchor>")
            return xml[:end] + xml[start:end] * 49 + xml[end:]

        result = convert(builder.zip_with(buffer.getvalue(), edit={"xl/drawings/drawing1.xml": repeated}), "a.xlsx")

        chart_text = once[once.index("[Chart]"):]
        assert result.text == once + ("\n\n" + chart_text) * 49
        assert len(parsed) == 1


def _long_format(length: int) -> str:
    """A number format of exactly `length` characters: two decimal places and spaces to be dropped."""
    return "0.00" + " " * (length - 4)


class TestVeryLongNumberFormat:
    """The number format is analysed again for every cell, in time proportional to its length: a 6 KB workbook with a 100 KB
    format on 200 cells took 1.6 s (7.8 ms per cell). Excel accepts no format of more than 255 characters."""

    @pytest.fixture
    def formats_analysed(self, monkeypatch: pytest.MonkeyPatch) -> list[int]:
        """Lengths of the formats handed to `format_cell`."""
        lengths: list[int] = []
        format_cell = spreadsheet.format_cell

        def recording(value: object, number_format: str | None) -> str:
            lengths.append(len(number_format or ""))
            return format_cell(value, number_format)

        for module in (spreadsheet, xml_spreadsheet, xls):
            monkeypatch.setattr(module, "format_cell", recording)
        return lengths

    @staticmethod
    def _xlsx(number_format: str) -> bytes:
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["name", "amount"])
        sheet.append(["a", 1.5])
        sheet["B2"].number_format = number_format
        buffer = BytesIO()
        workbook.save(buffer)
        return buffer.getvalue()

    @pytest.mark.parametrize("streamed", [False, True], ids=["full", "streamed"])
    def test_xlsx_format_over_the_limit_is_the_general_format(self, streamed, monkeypatch, formats_analysed):
        """Pins: a number format of more than 1,024 characters is not interpreted; the value is shown as it is stored."""
        if streamed:
            monkeypatch.setattr(spreadsheet, "_FULL_LOAD_MAX_SHEET_BYTES", 0)

        result = convert(self._xlsx(_long_format(1_025)), "a.xlsx")

        assert result.text == "## Sheet: Sheet\n\n| name | amount |\n| --- | --- |\n| a | 1.5 |"
        assert not result.warnings
        assert max(formats_analysed) < 100

    def test_xlsx_format_at_the_limit_is_applied(self):
        result = convert(self._xlsx(_long_format(1_024)), "a.xlsx")

        assert result.text == "## Sheet: Sheet\n\n| name | amount |\n| --- | --- |\n| a | 1.50 |"

    @pytest.mark.parametrize(("length", "expected"), [(1_024, "1.50"), (1_025, "1.5")])
    def test_xml_spreadsheet(self, length, expected, formats_analysed):
        styles = f'<Style ss:ID="s0"><NumberFormat ss:Format="{_long_format(length)}"/></Style>'

        result = convert(_xml_workbook(_STYLED_ROWS, styles), "a.xml")

        assert result.text == f"## Sheet: s\n\n| name | amount |\n| --- | --- |\n| part | {expected} |"
        assert max(formats_analysed) <= 1_024

    @pytest.mark.parametrize(("length", "expected"), [(1_024, "1.50"), (1_025, "1.5")])
    def test_xls(self, length, expected, formats_analysed):
        import xlrd

        book = SimpleNamespace(format_map={7: SimpleNamespace(format_str=_long_format(length))},
                               xf_list=[SimpleNamespace(format_key=7)], datemode=0)
        cell = xlrd.sheet.Cell(xlrd.XL_CELL_NUMBER, 1.5, 0)

        assert xls._xls_cell_text(cell, book) == expected
        assert max(formats_analysed) <= 1_024


class TestXmlCellAtPositionZero:
    """`ss:Index` counts from 1. A cell with `ss:Index="0"` was put at a column that does not exist and dropped without a
    warning (`LOST | b | c` came out as `b | c`)."""

    def test_cell_is_kept_where_it_stands_with_the_warning(self):
        rows = ("<Row>" + _xml_cell("LOST", attributes='ss:Index="0"') + _xml_cell("b") + _xml_cell("c") + "</Row>"
                "<Row>" + _xml_cell("1", "Number") + _xml_cell("2", "Number") + _xml_cell("3", "Number", 'ss:Index="0"') + "</Row>")

        result = convert(_xml_workbook(rows), "a.xml")

        assert result.text == "## Sheet: s\n\n| LOST | b | c |\n| --- | --- | --- |\n| 1 | 2 | 3 |"
        assert result.warnings == ["Non-integer cell position and merge attributes (Index) were ignored. "
                                   "Cells may have been placed in a different column."]

    def test_other_positions_are_read_as_before(self):
        rows = ('<Row ss:Index="0">' + _xml_cell("a", attributes='ss:Index="1"') + _xml_cell("b", attributes='ss:Index="3"')
                + _xml_cell("c", attributes='ss:MergeAcross="0" ss:MergeDown="0"') + "</Row>"
                "<Row>" + "".join(_xml_cell(number, "Number") for number in range(1, 5)) + "</Row>")

        result = convert(_xml_workbook('<Column ss:Index="0" ss:Span="0"/>' + rows), "a.xml")

        assert result.text == "## Sheet: s\n\n| a | | b | c |\n| --- | --- | --- | --- |\n| 1 | 2 | 3 | 4 |"
        assert not result.warnings


@pytest.mark.timing
class TestManyEmptyRowBlocks:
    """Every block after an empty row was compared with the table above by working out that table's header and column count
    again from all its rows: quadratic in the number of blocks. A CSV of 300,000 one-row blocks took 226 s (80,000: 8 s)."""

    def test_csv_of_one_row_blocks(self):
        data = ("h,v\n" + "1\n\n" * 100_000).encode()
        started = time.perf_counter()

        result = convert(data, "a.csv", ConvertOptions(max_chars=None))

        assert time.perf_counter() - started < allowed(5)  # about 1 s now, 13 s or more before
        assert result.text.startswith("| h | v |\n| --- | --- |\n| 1 | |\n| 1 | |")
        assert result.text.count("| 1 | |") == 100_000

    def test_xlsx_rows_with_gaps(self):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["h", "v"])
        for index in range(40_000):
            sheet.cell(row=3 + 2 * index, column=1, value=1)
        buffer = BytesIO()
        workbook.save(buffer)
        started = time.perf_counter()

        result = convert(buffer.getvalue(), "a.xlsx", ConvertOptions(max_chars=None))

        assert time.perf_counter() - started < allowed(8)  # about 2 s now, 25 s or more before
        assert result.text.count("| 1 | |") == 40_000

    def test_a_new_table_below_many_blocks_is_still_split(self):
        rows = "h,v\n" + "1,2\n\n" * 300 + "name,count,unit\nx,3,kg\n\nh,v\n4,5\n"

        text = convert(rows.encode(), "a.csv").text

        assert text.endswith("| 1 | 2 |\n\n| name | count | unit |\n| --- | --- | --- |\n| x | 3 | kg |\n\n"
                             "| h | v |\n| --- | --- |\n| 4 | 5 |")  # the header of the new table is compared, not the first one


class TestSharedFormulasThatCannotBeMoved:
    """A shared formula whose reference moves off the sheet when it is rewritten for another cell (a dependent cell left of the
    first one, `=A1` moved one column left) made openpyxl raise TranslatorError, and the whole workbook was rejected as
    corrupted. Such a cell is left blank, as one whose formula cannot be read."""

    _HEADER = _text_row(1, "a", "b", "c")
    _SHARED = ('<row r="2"><c r="C2"><f t="shared" ref="B2:C3" si="0">A1</f></c></row>'
               '<row r="3"><c r="B3"><f t="shared" si="0"/></c></row>')

    def test_when_formulas_are_read(self):
        """No formula has a result, so openpyxl reads formulas (`data_only=False`) and moves the shared formula itself."""
        result = convert(_with_sheet_xml(_sheet_xml(self._HEADER + self._SHARED)), "a.xlsx")

        assert result.text == "## Sheet: Sheet\n\n| a | b | c |\n| --- | --- | --- |\n| | | =A1 |"
        assert result.warnings == ["Formula results are not stored in the file, so the formulas were copied as written "
                                   "(opening and re-saving the file in Excel converts them to values)."]

    def test_when_values_are_read(self):
        """One formula has a result, so values are read and the formulas without results are looked up in the sheet XML."""
        stored = '<row r="4"><c r="A4"><f>1+1</f><v>2</v></c></row>'

        result = convert(_with_sheet_xml(_sheet_xml(self._HEADER + self._SHARED + stored)), "a.xlsx")

        assert result.text == "## Sheet: Sheet\n\n| a | b | c |\n| --- | --- | --- |\n| | | =A1 |\n| 2 | | |"
        assert result.warnings == ["Sheet 'Sheet': 1 formula(s) with no stored result were copied as written "
                                   "(opening and re-saving the file in Excel converts them to values)."]

    def test_formula_that_cannot_be_tokenized(self):
        """An unterminated string in a shared formula made openpyxl raise TokenizerError."""
        rows = self._HEADER + '<row r="2"><c r="A2"><f t="shared" ref="A2:A3" si="0">"abc</f></c><c r="B2"><v>5</v></c></row>'

        result = convert(_with_sheet_xml(_sheet_xml(rows)), "a.xlsx")

        assert result.text == "## Sheet: Sheet\n\n| a | b | c |\n| --- | --- | --- |\n| | 5 | |"


class TestElapsedMinutes:
    """An elapsed `[h]:mm` value was rounded to the minute (1:29:45 → `1:30`) while the same value under `h:mm` was truncated
    (`1:29`), the rule recorded for time formats. Both truncate now."""

    @pytest.mark.parametrize(("value", "number_format", "expected"), [
        (timedelta(hours=1, minutes=29, seconds=45), "[h]:mm", "1:29"),
        (timedelta(hours=1, minutes=29, seconds=45), '[h]"h" mm"m"', "1h 29m"),
        (timedelta(hours=26, minutes=59, seconds=59), "[h]:mm", "26:59"),
        (timedelta(hours=26), "[h]:mm", "26:00"),
        (timedelta(hours=1, minutes=29, seconds=45), "[h]:mm:ss", "1:29:45"),
        (timedelta(seconds=-45), "[h]:mm", "0:00"),  # truncated to zero: no sign
        (timedelta(seconds=-89), "[h]:mm", "-0:01"),
        (timedelta(seconds=-45), '[h]"h" mm"m"', "0h 00m"),
        (timedelta(seconds=-61), '[h]"h" mm"m"', "-0h 01m"),
        (timedelta(seconds=-5), "[h]:mm:ss", "-0:00:05"),
    ])
    def test_below_the_minute_is_truncated(self, value, number_format, expected):
        assert format_cell(value, number_format) == expected

    def test_same_as_a_clock_time(self):
        assert format_cell(clock_time(1, 29, 45), "h:mm") == format_cell(timedelta(hours=1, minutes=29, seconds=45), "[h]:mm")


class TestXlsNumberThatIsNotFinite:
    """An XLS number cell holding NaN or infinity came out as `nan` / `inf` without a warning. XLSX and Excel 2003 XML keep the
    text written in the file with a count warning; an XLS has no such text, so the cell is blank, with the count warning."""

    def test_cells_are_blank_with_one_warning(self):
        data = builder.raw_biff_xls([["name", "value"], ["a", float("nan")], ["b", float("inf")], ["c", -float("inf")],
                                     ["d", 12.5]])

        result = convert(data, "a.xls")

        assert result.text == "## Sheet: Sheet 1\n\n| name | value |\n| --- | --- |\n| a | |\n| b | |\n| c | |\n| d | 12.5 |"
        assert result.warnings == ["3 number cell value(s) that are not finite numbers were left blank."]


class TestQuestionMarkPlaces:
    """A `?` digit place without a significant digit is a space in Excel (it keeps a column aligned on the decimal point). A `?`
    decimal place showed its 0 (1.5 under `0.??` came out as `1.50`) and a `?` integer place its 0 (`?.??` → `0.50`)."""

    @pytest.mark.parametrize(("value", "number_format", "expected"), [
        (1.5, "0.??", "1.5 "),
        (0.5, "0.0?", "0.5 "),
        (0.5, "?.??", " .5 "),
        (1.05, "0.??", "1.05"),
        (12, "0.??", "12.  "),
        (5, "???", "  5"),
        (1234.5, "#,##0.0?", "1,234.5 "),
        (12, "0.###", "12"),  # `#` places show nothing, as before
        (1.5, "0.0#", "1.5"),
        (1.5, "0.00", "1.50"),
    ])
    def test_place_without_a_digit(self, value, number_format, expected):
        assert format_cell(value, number_format) == expected

    def test_cell_text_has_no_padding_spaces(self):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["item", "weight"])
        sheet.append(["a", 1.5])
        sheet["B2"].number_format = '0.?? "kg"'
        buffer = BytesIO()
        workbook.save(buffer)

        assert convert(buffer.getvalue(), "a.xlsx").text.endswith("| a | 1.5 kg |")  # runs of spaces in a cell are one space
