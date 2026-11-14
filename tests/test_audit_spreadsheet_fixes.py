"""Spreadsheets and delimited text: fixes from the audit of the spreadsheet extractors (merge caps that could be cancelled out,
values that rejected a whole workbook, quotes that swallowed a CSV, scans that kept the whole tree, sheets parsed twice)."""

from io import BytesIO
import struct

import openpyxl
from openpyxl.worksheet import _reader as openpyxl_sheet_reader

from opendockit import convert
from opendockit.extractors import _openpyxl_reading, spreadsheet, xls
from opendockit.extractors._sheet_format import format_cell
from opendockit.models import ConvertOptions
from tests import builder
from tests.test_audit_spreadsheet import _SHEET, _sheet_xml, _text_row, _with_sheet_xml, _xml_cell, _xml_workbook


def _saved(workbook: openpyxl.Workbook) -> bytes:
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _formatted_workbook(*formats: str) -> bytes:
    """Row 1 `name | value`, then one row per format: `row N | 1` with the value cell in that format."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["name", "value"])
    for index, number_format in enumerate(formats, start=1):
        sheet.append([f"row {index}", 1])
        sheet.cell(row=index + 1, column=2).number_format = number_format
    return _saved(workbook)


def _xls_label(row: int, column: int, value: str) -> bytes:
    return builder.xls_record(0x0204, struct.pack("<HHHH", row, column, 0, len(value)) + b"\x01" + value.encode("utf-16-le"))


def _xls_merges(ranges: list[tuple[int, int, int, int]]) -> bytes:
    """One MERGEDCELLS record. A range is (first row, last row, first column, last column), ends included."""
    return builder.xls_record(0x00E5, struct.pack("<H", len(ranges)) + b"".join(struct.pack("<HHHH", *r) for r in ranges))


class TestReversedXlsMergeRanges:
    """xlrd keeps a merge range as written, so a reversed one (last row above the first) had a negative area. Counted toward the
    merge cap it cancelled out large merges: 20,000 merges down to row 65,535 plus about 120 reversed ranges (461 KB) took 17 s
    without the 'too many merged cells' warning."""

    def test_reversed_range_does_not_cancel_out_the_merge_cap(self, monkeypatch):
        monkeypatch.setattr(xls, "_MAX_MERGED_CELLS", 2)
        rows = [["구분", "값"], ["가", "1"], ["", "2"], ["", "3"]]
        merges = _xls_merges([(1, 3, 0, 0), (3, 0, 0, 5)])  # 3 cells, then a reversed range worth -12
        data = builder.xls_bytes([("Sheet1", rows, merges)])

        extraction = xls.extract_xls(data, ConvertOptions())

        assert extraction.sections[0].text == "| 구분 | 값 |\n| --- | --- |\n| 가 | 1 |\n|  | 2 |\n|  | 3 |"
        assert extraction.warnings == [
            "Sheet 'Sheet1': there are too many merged cells, so vertical merge values were left in the first cell only."]

    def test_reversed_range_alone_changes_nothing(self):
        rows = [["구분", "값"], ["가", "1"], ["", "2"]]
        data = builder.xls_bytes([("Sheet1", rows, _xls_merges([(2, 1, 0, 0), (1, 2, 1, 0)]))])

        extraction = xls.extract_xls(data, ConvertOptions())

        assert extraction.sections[0].text == "| 구분 | 값 |\n| --- | --- |\n| 가 | 1 |\n|  | 2 |"
        assert not extraction.warnings


class TestIntegerTooLargeForAFloat:
    """Digits without `.` or `E` are read by openpyxl as an int. 400 nines passed the check for infinite floats, and turning them
    into a float for the number format (General too) rejected the whole workbook with OverflowError."""

    def test_huge_integer_is_kept_as_its_text_under_every_format(self):
        big = "9" * 400
        data = builder.zip_with(_formatted_workbook("General", "0.00", "yyyy-mm-dd"),
                                edit={_SHEET: lambda xml: xml.replace("<v>1</v>", f"<v>{big}</v>")})

        result = convert(data, "a.xlsx")

        assert result.text.count(f"| {big} |") == 3
        assert "| name | value |" in result.text
        assert result.warnings == ["3 malformed cell value(s) were copied as the text written in the file "
                                   "(a shared-string index that points to no text is left blank)."]

    def test_integer_a_float_can_hold_is_still_a_number(self):
        data = builder.zip_with(_formatted_workbook("#,##0"),
                                edit={_SHEET: lambda xml: xml.replace("<v>1</v>", f"<v>{'9' * 300}</v>")})

        result = convert(data, "a.xlsx")

        assert "| row 1 | 1,000,000,000" in result.text
        assert not result.warnings


class TestUnclosedQuoteInASmallCsv:
    """The lenient csv module ends the file inside an unclosed quote without an error, so in a file smaller than the field limit
    every row after the quote was swallowed into one cell without a warning."""

    def test_rows_after_an_unclosed_quote_are_split_with_a_warning(self):
        result = convert(b'name,city\n"Kim,Seoul\nLee,Busan\nPark,Daegu\n', "a.csv")

        assert result.text == ('| name | city |\n| --- | --- |\n| "Kim | Seoul |\n| Lee | Busan |\n| Park | Daegu |')
        assert result.warnings == [
            "The quoting in the CSV is inconsistent from line 2 on, so the rest was split by the delimiter only."]

    def test_text_after_a_closing_quote_is_joined_as_before(self):
        """Strict mode also stops at `"a"b`; that row is read as the lenient reader did, and reading goes on without a warning."""
        result = convert(b'x,y\n"a"b,c\n"q" ,r\nd,"e"f"g"\n', "a.csv")

        assert result.text == '| x | y |\n| --- | --- |\n| ab | c |\n| q | r |\n| d | ef"g" |'
        assert not result.warnings

    def test_quoted_line_breaks_that_close_are_unchanged(self):
        result = convert(b'name,note\nKim,"line1\nline2"\nLee,x\n', "a.csv")

        assert result.text == "| name | note |\n| --- | --- |\n| Kim | line1<br>line2 |\n| Lee | x |"
        assert not result.warnings


def _element_count_after(monkeypatch, module: object, name: str, run: object) -> int:
    """Number of elements left in the tree an iterparse call built, once `run()` has finished with it."""
    parses = []
    original = getattr(module, name)

    def recording(*args: object, **kwargs: object) -> object:
        parse = original(*args, **kwargs)
        parses.append(parse)
        return parse

    monkeypatch.setattr(module, name, recording)
    run()
    assert parses
    return max(sum(1 for _ in parse.root.iter()) for parse in parses)


def _formula_rows(count: int) -> str:
    return "".join(f'<row r="{row}"><c r="A{row}"><f>1-1</f><v>0</v></c><c r="B{row}"><f>2-2</f><v>0</v></c></row>'
                   for row in range(1, count + 1))


class TestSheetScansKeepNoTree:
    """The scans over sheet XML only emptied each cell, so an empty element per cell and row stayed in the tree: a 13 MB workbook
    (600,000 rows) grew by 948 MB in the zero-result check that was meant to read the sheet without keeping it. The element count
    left in the tree is measured, not memory."""

    def test_zero_result_check_keeps_no_rows(self, monkeypatch):
        data = _with_sheet_xml(_sheet_xml(_formula_rows(3_000)))
        with spreadsheet.zipfile.ZipFile(BytesIO(data)) as archive:
            left = _element_count_after(monkeypatch, spreadsheet.etree, "iterparse",
                                        lambda: spreadsheet._only_zero_results(archive, [_SHEET]))

        assert left < 10  # 3 + 3,000 rows × 7 elements before

    def test_formula_scan_keeps_no_rows(self, monkeypatch):
        xml = _sheet_xml(_formula_rows(3_000).replace("<v>0</v>", "")).encode()
        found: dict = {}

        def scan() -> None:
            found.update(spreadsheet._unresolved_formulas(xml, 100))

        left = _element_count_after(monkeypatch, spreadsheet.etree, "iterparse", scan)

        assert left < 10
        assert len(found) == 3_000 and found[3_000] == {1: "=1-1", 2: "=2-2"}

    def test_sheet_parser_keeps_no_rows(self, monkeypatch):
        rows = "".join(_text_row(row, f"item {row}", "x") for row in range(1, 3_001))
        data = _with_sheet_xml(_sheet_xml(rows))
        result = {}

        def run() -> None:
            result["text"] = convert(data, "a.xlsx", options=ConvertOptions(max_chars=None)).text

        left = _element_count_after(monkeypatch, openpyxl_sheet_reader, "iterparse", run)

        assert left < 10
        assert result["text"].count("| item ") == 3_000 and result["text"].endswith("| item 3000 | x |")

    def test_shared_string_table_keeps_no_strings(self, monkeypatch):
        strings = "".join(f"<si><t>text {index}</t></si>" for index in range(3_000))
        rows = "".join(f'<row r="{index + 1}"><c r="A{index + 1}" t="s"><v>{index}</v></c></row>' for index in range(3_000))
        data = builder.zip_with(_with_sheet_xml(_sheet_xml(rows)), edit={
            "[Content_Types].xml": lambda xml: xml.replace("</Types>", '<Override PartName="/xl/sharedStrings.xml" ContentType='
                                                          '"application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings'
                                                          '+xml"/></Types>')},
            add={"xl/sharedStrings.xml": f'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">{strings}</sst>'})
        result = {}

        def run() -> None:
            result["text"] = convert(data, "a.xlsx", options=ConvertOptions(max_chars=None)).text

        left = _element_count_after(monkeypatch, _openpyxl_reading, "_openpyxl_iterparse", run)

        assert left < 10
        assert result["text"].count("text ") == 3_000 and result["text"].endswith("text 2999")


class TestStreamedSheetWithoutADimension:
    """A streamed sheet looks for `<dimension>` when it is opened, and without one openpyxl's search runs to the end of the sheet:
    the whole sheet was parsed once for nothing (a 13 MB workbook took 16.7 s instead of 3.7 s)."""

    def _counting(self, monkeypatch) -> list[int]:
        calls: list[int] = []
        original = openpyxl_sheet_reader.WorkSheetParser.parse_dimensions

        def counting(parser: object) -> object:
            calls.append(1)
            return original(parser)

        monkeypatch.setattr(openpyxl_sheet_reader.WorkSheetParser, "parse_dimensions", counting)
        return calls

    def test_dimension_is_not_looked_for_while_converting(self, monkeypatch):
        calls = self._counting(monkeypatch)
        monkeypatch.setattr(spreadsheet, "_FULL_LOAD_MAX_SHEET_BYTES", 0)  # stream
        rows = "".join(_text_row(row, f"item {row}") for row in range(1, 101))

        result = convert(_with_sheet_xml(_sheet_xml(rows)), "a.xlsx")

        assert result.text.endswith("item 100")
        assert calls == []

    def test_streamed_sheet_opened_outside_a_conversion_still_has_its_size(self, monkeypatch):
        calls = self._counting(monkeypatch)
        data = _with_sheet_xml(_sheet_xml(_text_row(1, "a", "b") + _text_row(2, "c", "d")).replace(
            "<sheetData>", '<dimension ref="A1:B2"/><sheetData>'))

        workbook = openpyxl.load_workbook(BytesIO(data), read_only=True)

        assert workbook.active.max_row == 2 and workbook.active.max_column == 2
        assert calls == [1]


class TestXmlNumberThatIsNotFinite:
    """An Excel 2003 XML number cell `1E+400` or `NaN` came out as `inf` / `nan`. It is kept as written with a warning, as in XLSX."""

    def test_number_that_is_not_finite_is_kept_as_written(self):
        table = ("<Row>" + _xml_cell("name") + _xml_cell("value") + "</Row>"
                 "<Row>" + _xml_cell("big") + _xml_cell("1E+400", "Number") + "</Row>"
                 "<Row>" + _xml_cell("none") + _xml_cell("NaN", "Number") + "</Row>"
                 "<Row>" + _xml_cell("one") + _xml_cell("1", "Number") + "</Row>")

        result = convert(_xml_workbook(table), "a.xml")

        assert result.text.endswith("| big | 1E+400 |\n| none | NaN |\n| one | 1 |")
        assert result.warnings == ["2 number cell value(s) that are not finite numbers were copied as the text written in the file."]


class TestLiteralsBetweenDecimalPlaces:
    """Literals between decimal places were dropped and only the digits kept: 5 under `0.0\\E+0` came out as `5.00` (Excel
    `5.0E+0`)."""

    def test_literals_between_decimal_places_stay_in_place(self):
        assert format_cell(5, "0.0\\E+0") == "5.0E+0"
        assert format_cell(1.25, '0.0" "0') == "1.2 5"
        assert format_cell(1234.5678, '#,##0.00" "00') == "1,234.56 78"
        assert format_cell(-1.5, '0.0"x"0') == "-1.5x0"

    def test_decimal_places_without_literals_are_unchanged(self):
        assert format_cell(1234.5, "#,##0.00") == "1,234.50"
        assert format_cell(12, "0.###") == "12"
        assert format_cell(1234.5, "0.0,") == "1.2"
        assert format_cell(0.25, "0.0%") == "25.0%"


class TestOnlySheetPartsAreMeasured:
    """Every file under `xl/worksheets/` counted toward the 20 MB sheet size that switches to streaming (which has no merges), so
    a large non-sheet file kept there lost the merges of a small sheet."""

    def test_large_file_beside_the_sheets_does_not_force_streaming(self):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        for row in (["구분", "값"], ["가", "1"], [None, "2"]):
            sheet.append(row)
        sheet.merge_cells("A2:A3")
        data = builder.zip_with(_saved(workbook), add={"xl/worksheets/notes.bin": "0" * (21 * 1024 * 1024)})

        result = convert(data, "a.xlsx")

        assert result.text.endswith("| 가 | 1 |\n| 가 | 2 |")


class TestSerialSixtyIsTheDayBefore:
    """Known accepted gap: Excel's 1900 system counts the non-existent 1900-02-29 as serial 60. Python dates cannot hold that day,
    so serial 60 shows as 1900-02-28, the same as serial 59 (openpyxl and xlrd convert it the same way). Pins the current shape."""

    def test_serial_sixty_shows_as_february_28(self):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["name", "date"])
        for serial in (59, 60, 61):
            sheet.append([f"serial {serial}", serial])
            sheet.cell(row=sheet.max_row, column=2).number_format = "yyyy-mm-dd"

        result = convert(_saved(workbook), "a.xlsx")

        assert result.text.endswith("| serial 59 | 1900-02-28 |\n| serial 60 | 1900-02-28 |\n| serial 61 | 1900-03-01 |")

    def test_xml_spreadsheet_serial_sixty_shows_as_february_28(self):
        styles = '<Style ss:ID="d"><NumberFormat ss:Format="yyyy-mm-dd"/></Style>'
        table = "<Row>" + _xml_cell("name") + _xml_cell("date") + "</Row>" + "".join(
            "<Row>" + _xml_cell(f"serial {serial}") + _xml_cell(serial, "Number", 'ss:StyleID="d"') + "</Row>"
            for serial in (59, 60))

        result = convert(_xml_workbook(table, styles), "a.xml")

        assert result.text.endswith("| serial 59 | 1900-02-28 |\n| serial 60 | 1900-02-28 |")
