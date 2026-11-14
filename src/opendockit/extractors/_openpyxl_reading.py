"""What is changed in openpyxl while an XLSX is read, so that one odd cell, style or part does not reject the workbook.

Importing this module replaces functions of openpyxl's readers. Each replacement behaves as the original unless this
package is reading (`_READING`, a context variable), so other use of openpyxl in the same process is unaffected.
"""

from collections.abc import Callable, Iterator
from contextvars import ContextVar
import copy
from dataclasses import dataclass, field
from datetime import date
import math
import re
import sys

from lxml import etree
import openpyxl
from openpyxl.cell.rich_text import CellRichText
from openpyxl.cell.text import Text
from openpyxl.chartsheet import Chartsheet
from openpyxl.formula.tokenizer import TokenizerError
from openpyxl.formula.translate import TranslatorError
from openpyxl.packaging.relationship import RelationshipList
from openpyxl.reader import excel as _openpyxl_excel_reader
from openpyxl.styles.cell_style import CellStyleList
from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries
from openpyxl.utils.datetime import from_excel, from_ISO8601
from openpyxl.worksheet import _reader as _openpyxl_sheet_reader
from openpyxl.worksheet._read_only import ReadOnlyWorksheet
from openpyxl.worksheet.merge import MergeCell, MergeCells
from openpyxl.xml.constants import SHEET_MAIN_NS
from openpyxl.xml.functions import iterparse as _openpyxl_iterparse

from opendockit.extractors._package import _XML_PARSER, decimal_int


# When the value of a date-formatted cell is outside the date range (9999-12-31), openpyxl turns it into a '#VALUE!' error cell and
# emits a UserWarning per cell. Excel merely shows that cell as '####'; the value is still the number. Only while this module is
# reading an XLSX (`_RAW_OUT_OF_RANGE_SERIALS`) does the date conversion of openpyxl's sheet parser return the original number — the
# number is shown, as for XLS (`_xls_value`). It is a context variable, so other openpyxl use in the same process (other threads and
# tasks) is unaffected. Deliberate: a name in the parser's private module is swapped out. This conversion is the only place where the
# parser can see the number in the cell XML (Pinned by: test_spreadsheet.py::TestDateCellsOutsideTheDateRange). Negative serials
# are also returned as numbers instead of being converted to dates — they became not a date but a nonexistent day such as 1899-12-25.
# Whether one is shown as a time or as the original number is decided by `format_cell` (Pinned by: TestNegativeDateSerials).
_RAW_OUT_OF_RANGE_SERIALS: ContextVar[bool] = ContextVar("raw_out_of_range_serials", default=False)


def _from_excel_keeping_out_of_range(value: object, *args: object, **kwargs: object) -> object:
    if _RAW_OUT_OF_RANGE_SERIALS.get() and not kwargs.get("timedelta") and isinstance(value, (int, float)) and value < 0:
        return value
    try:
        return from_excel(value, *args, **kwargs)
    except (OverflowError, ValueError):
        if _RAW_OUT_OF_RANGE_SERIALS.get():
            return value
        raise


_openpyxl_sheet_reader.from_excel = _from_excel_keeping_out_of_range


@dataclass(frozen=True, slots=True)
class _CellPicture:
    """Value of a 'Place in cell' picture cell: the picture file path (inside the package) and its alt text.

    It is turned into text when the row is read (`_cell_text`)."""

    path: str
    alt: str | None = None


@dataclass(slots=True)
class _CellReading:
    """State that openpyxl's sheet parser uses for each cell while this module reads an XLSX (`_READING`).

    `style_count` is the number of cell formats after the styles part is applied, `rich_values` maps a cell's vm (value-metadata
    index, 1-based) → the visible text or a `_CellPicture`, and `picture_text` is the function that turns an in-cell picture into
    a placeholder (present only while a sheet is being read).
    """

    rich_values: dict[str, object] = field(default_factory=dict)
    style_count: int | None = None
    malformed: int = 0  # cells left as their raw text because they do not fit the format (blank for a shared-string index)
    unread_rich: int = 0  # in-cell picture / data-type cells whose value was not found (blank instead of the error value)
    picture_text: Callable[[_CellPicture], str] | None = None
    row_numbers: list[int] | None = None  # row numbers of the streamed sheet being read, in the order read (`_streamed_rows`)
    part_results: dict[tuple[str, ...], object] = field(default_factory=dict)  # what a part gave, kept for this workbook (`_once_per_part`)


# Deliberate: the cell interpretation of openpyxl's sheet parser (`WorkSheetParser.parse_cell`) and the place that knows the number
# of cell formats (`apply_stylesheet`) are wrapped only while this module reads an XLSX (the same context-variable approach as the
# `from_excel` replacement above, so other openpyxl use is unaffected). When the value or an attribute of a single cell broke the
# format ('1,5' or 'NaN' in a numeric cell, the boolean 'true', a non-ISO date, an out-of-range style or shared-string index),
# openpyxl rejected the whole document. Excel and LibreOffice open it. Full load and streaming both interpret cells in this one
# function, so both paths are fixed the same way
# (Pinned by: test_spreadsheet_cell_values_and_controls.py::TestOneMalformedCellValue).
_READING: ContextVar[_CellReading | None] = ContextVar("xlsx_cell_reading", default=None)
_parse_cell = _openpyxl_sheet_reader.WorkSheetParser.parse_cell
_apply_stylesheet = _openpyxl_excel_reader.apply_stylesheet
_ISO_DATE_ONLY = re.compile(r"\d{4}-\d{2}-\d{2}")


def _tolerant_parse_cell(parser: object, element: object) -> dict:
    """Call `WorkSheetParser.parse_cell`; a malformed cell becomes its raw text, an in-cell picture or data type (vm) its value."""
    reading = _READING.get()
    if reading is None:
        return _parse_cell(parser, element)
    counter = parser.col_counter
    try:
        cell = _parse_cell(parser, element)
        value = cell["value"]
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError  # `1E+400` becomes infinity: it came out as 'inf', or a date format rejected the whole document with OverflowError
        if isinstance(value, int) and not isinstance(value, bool) and abs(value) > sys.float_info.max:
            # Digits without `.` or `E` are read as an int, so 400 nines passed the check above and every number format
            # (General too) rejected the whole document when turning it into a float
            # (Pinned by: test_audit_spreadsheet_fixes.py::TestIntegerTooLargeForAFloat).
            raise ValueError
    except (TranslatorError, TokenizerError):
        # A formula openpyxl cannot read or cannot move to this cell (a shared formula whose reference moves off the sheet).
        # It rejected the whole workbook; the cell is left blank, as when values are read (`_unresolved_formulas`)
        # (Pinned by: test_audit_spreadsheet.py::TestSharedFormulasThatCannotBeMoved).
        parser.col_counter = counter
        cell = {**_raw_cell(parser, element), "value": None, "data_type": "n"}
        cell.pop("malformed")
    except (ValueError, IndexError, TypeError, OverflowError):
        parser.col_counter = counter  # restore the column counted before the failure and count again
        cell = _raw_cell(parser, element)
        reading.malformed += cell.pop("malformed")

    # A missing cell format (out-of-range or empty index) is the default format — openpyxl rejected it with IndexError/TypeError
    # when creating the cell
    style = cell["style_id"]
    if not isinstance(style, int) or style < 0 or (reading.style_count is not None and style >= reading.style_count):
        cell["style_id"] = 0

    # 'Place in cell' picture and data-type cells are written as the `#VALUE!` error value + vm (the real value is in xl/richData).
    # A value that cannot be found is left blank
    # (Pinned by: test_spreadsheet_cell_values_and_controls.py::TestPicturesAndDataTypesPlacedInCells)
    value_metadata = element.get("vm")
    if value_metadata is not None:
        rich = reading.rich_values.get(value_metadata.strip())
        if rich is not None:
            cell["value"], cell["data_type"] = rich, "s"
        elif cell["data_type"] == "e":
            cell["value"], cell["data_type"] = None, "n"
            reading.unread_rich += 1

    # openpyxl reads only the date of an ISO date and time joined by a space ('2024-01-05 13:00:00')
    # (Pinned by: test_spreadsheet_cell_values_and_controls.py::TestIsoDateCellsWithoutAFormat)
    if element.get("t") == "d" and type(cell["value"]) is date:
        text = (element.findtext(_openpyxl_sheet_reader.VALUE_TAG) or "").strip()
        if not _ISO_DATE_ONLY.fullmatch(text):
            try:
                cell["value"] = from_ISO8601(text.replace(" ", "T", 1)) or cell["value"]
            except ValueError:
                pass
    return cell


def _raw_cell(parser: object, element: object) -> dict:
    """A cell openpyxl could not interpret: if only its cell-format index is non-numeric, drop that and interpret it again. Otherwise
    use the text exactly as written in the file (the booleans 'true'/'false' become booleans; a shared-string index has no text to
    point to, so it is blank). `malformed` is the number of cells left as raw text (0 or 1)."""
    if not _is_number(element.get("s", "0")):
        stripped = copy.deepcopy(element)  # small, since it is a single cell. The original element belongs to the parser, so it is left untouched
        stripped.attrib.pop("s", None)
        counter = parser.col_counter
        try:
            return {**_parse_cell(parser, stripped), "malformed": 0}
        except (ValueError, IndexError, TypeError, OverflowError):
            parser.col_counter = counter

    try:
        row, column = coordinate_to_tuple(element.get("r") or "")
        parser.col_counter = column
    except (ValueError, TypeError, IndexError):
        parser.col_counter += 1
        row, column = parser.row_counter, parser.col_counter

    # An inline string may have only its character formatting malformed — the text is carried over as is, so it is not a cell left
    # as raw text
    # (Pinned by: test_spreadsheet_damaged_parts.py::TestMalformedTextRunProperties::test_inline_string_with_a_malformed_run_is_read_as_its_text)
    data_type = element.get("t", "n")
    inline = element.find(_openpyxl_sheet_reader.INLINE_STRING)
    style = int(element.get("s")) if _is_number(element.get("s")) else 0
    if data_type == "inlineStr" and inline is not None:
        return {"row": row, "column": column, "value": _plain_string(inline), "data_type": "s", "style_id": style,
                "malformed": 0}

    text = element.findtext(_openpyxl_sheet_reader.VALUE_TAG)
    if data_type == "s" or text is None:
        value = None
    elif data_type == "b" and text.strip().lower() in ("true", "false"):
        value = text.strip().lower() == "true"
    else:
        value = text
    return {"row": row, "column": column, "value": value, "data_type": "s" if isinstance(value, str) else "n",
            "style_id": style, "malformed": 1}


_STRING_ITEM = f"{{{SHEET_MAIN_NS}}}si"
_STRING_TEXT, _STRING_RUN = f"{{{SHEET_MAIN_NS}}}t", f"{{{SHEET_MAIN_NS}}}r"


def _plain_string(element: etree._Element) -> str:
    """Just the text of a shared string `<si>` or inline string `<is>` (no formatting or phonetic `rPh`), as openpyxl `Text.content`."""
    pieces = [child.text or "" for child in element if child.tag == _STRING_TEXT]
    pieces += [run.findtext(_STRING_TEXT) or "" for run in element if run.tag == _STRING_RUN]
    return "".join(pieces)


def _rich_or_plain(text: CellRichText) -> CellRichText | str:
    """Like openpyxl `read_rich_text`, return a string that is a single unformatted text as str."""
    if len(text) == 0:
        return ""
    return text[0] if len(text) == 1 and isinstance(text[0], str) else text


def _tolerant_string_table(original: Callable[..., list], rich: bool) -> Callable[..., list]:
    """Replace openpyxl's shared-string table readers (`read_string_table`, `read_rich_text`) only while this module is reading. When
    a character-format attribute of one string was outside the spec (`color rgb="#FF0000"`, `sz val="10,5"`), openpyxl rejected the
    whole table, and so the whole document. Excel and LibreOffice open it. Only that string drops its formatting and is read as
    text — the text is unchanged, so there is no warning
    (Pinned by: test_spreadsheet_damaged_parts.py::TestMalformedTextRunProperties). Strings already read are taken out of the
    tree, which openpyxl kept whole until the table ended (Pinned by: test_audit_spreadsheet_fixes.py::TestSheetScansKeepNoTree)."""
    def read(source: object) -> list:
        if _READING.get() is None:
            return original(source)

        strings: list = []
        table = None  # the root `<sst>`: strings already read are removed from it, so the parsed tree does not grow with the table
        for event, node in _openpyxl_iterparse(source, events=("start", "end")):
            if event == "start":
                table = node if table is None else table
                continue
            if node.tag != _STRING_ITEM:
                continue
            try:
                text = _rich_or_plain(CellRichText.from_tree(node)) if rich else Text.from_tree(node).content
            except (ValueError, TypeError, IndexError, OverflowError):
                text = _plain_string(node)
            if not rich:
                text = text.replace("x005F_", "")  # same as openpyxl
            node.clear()
            strings.append(text)
            if table is not None and len(table) and table[-1] is node:
                del table[:]
        return strings

    return read


def _apply_stylesheet_noting_styles(archive: object, workbook: openpyxl.Workbook) -> object:
    applied = _apply_stylesheet(archive, workbook)
    reading = _READING.get()
    if reading is not None:
        reading.style_count = len(workbook._cell_styles)
    return applied


_parse_row = _openpyxl_sheet_reader.WorkSheetParser.parse_row


def _tolerant_parse_row(parser: object, row: object) -> tuple[int, list]:
    """Call `WorkSheetParser.parse_row`; when the row number is not an integer (`r="abc"`, `r="1.5"`), read it as an unnumbered row
    (the one after the previous row). Cells are placed by their own address (`r="A2"`), so nothing is lost. The whole document used
    to be rejected
    (Pinned by: TestOneMalformedCellValue::test_row_number_that_is_not_a_whole_number_is_ignored)."""
    try:
        return _consecutive(_parse_row(parser, row))
    except ValueError:
        if _READING.get() is None:
            raise
    del row.attrib["r"]
    return _consecutive(_parse_row(parser, row))


def _consecutive(parsed: tuple[int, list]) -> tuple[int, list]:
    """While a streamed sheet is being read (`_streamed_rows`), hand the row to openpyxl under the next consecutive number and
    keep the number written in the file in `_CellReading.row_numbers`. openpyxl's streaming yields an empty row for every row
    number the file skips; rows 4,999 apart never reached the 5,000-empty-row stop, so a 14 KB file with 2,000 rows made ten
    million empty rows (7.6 s, 2.6 GB). A row whose number is not above the previous one gets number 0, which openpyxl drops, as
    it dropped that row before (Pinned by: test_audit_spreadsheet.py::TestStreamedRowsFarApart)."""
    reading = _READING.get()
    if reading is None or reading.row_numbers is None:
        return parsed
    number, cells = parsed
    if number < 1 or (reading.row_numbers and number <= reading.row_numbers[-1]):
        return 0, cells
    reading.row_numbers.append(number)
    return len(reading.row_numbers), cells


def _skipped_while_reading(original: Callable[..., object]) -> Callable[..., object]:
    """Row and column settings such as row height and column width are not used by the conversion. They are not interpreted while
    this module is reading — a single `<col min="0">` or `<row ht="abc">` used to reject the whole document
    (Pinned by: TestOneMalformedCellValue::test_malformed_row_or_column_setting_is_ignored)."""
    def skipped(reader: object, *args: object) -> object:
        return None if _READING.get() is not None else original(reader, *args)

    return skipped


_openpyxl_sheet_reader.WorkSheetParser.parse_cell = _tolerant_parse_cell
_openpyxl_sheet_reader.WorkSheetParser.parse_row = _tolerant_parse_row
_openpyxl_sheet_reader.WorkSheetParser.parse_column_dimensions = _skipped_while_reading(
    _openpyxl_sheet_reader.WorkSheetParser.parse_column_dimensions)
_openpyxl_sheet_reader.WorksheetReader.bind_col_dimensions = _skipped_while_reading(
    _openpyxl_sheet_reader.WorksheetReader.bind_col_dimensions)
_openpyxl_sheet_reader.WorksheetReader.bind_row_dimensions = _skipped_while_reading(
    _openpyxl_sheet_reader.WorksheetReader.bind_row_dimensions)
_openpyxl_excel_reader.apply_stylesheet = _apply_stylesheet_noting_styles
_openpyxl_excel_reader.read_string_table = _tolerant_string_table(_openpyxl_excel_reader.read_string_table, rich=False)
_openpyxl_excel_reader.read_rich_text = _tolerant_string_table(_openpyxl_excel_reader.read_rich_text, rich=True)
# For an out-of-range index openpyxl's style list prints '-99999 is out of range' to standard output and raises IndexError. That
# clutters server logs, so only the print is removed — `redirect_stdout` is process-wide and would also hide the output of concurrent
# conversions. The exception is the same (re-read by `_repaired_copies`. Pinned by:
# test_spreadsheet_damaged_parts.py::TestNamedStyleOutOfRange::test_out_of_range_style_number_prints_nothing)
CellStyleList.__getitem__ = lambda self, index: self.xf[index]


# The conversion uses only cells (`row`) and merges (`mergeCells`) from sheet XML. The rest — the table-definition list, hyperlinks,
# data validation, conditional formatting, auto filter, tab color (sheetPr), page breaks, protection, views, print settings,
# extensions — is merely parsed by openpyxl and attached to the sheet object, yet when one of those values was outside the spec
# (`dataValidation type="foo"`, `tabColor rgb="red"`, `hyperlink ref=""`) both full load and streaming rejected the whole document.
# Excel opens it (Pinned by: test_office_round44.py::TestSheetPartsTheConverterDoesNotUse). Hyperlink addresses are not converted
# (`_drawing_blocks`).
_SHEET_PARTS_LEFT_UNREAD = frozenset(
    getattr(_openpyxl_sheet_reader, name) for name in (
        "COL_TAG", "PROT_TAG", "EXT_TAG", "CF_TAG", "LEGACY_TAG", "ROW_BREAK_TAG", "COL_BREAK_TAG", "CUSTOM_VIEWS_TAG",
        "PRINT_TAG", "MARGINS_TAG", "PAGE_TAG", "HEADER_TAG", "FILTER_TAG", "VALIDATION_TAG", "PROPERTIES_TAG", "VIEWS_TAG",
        "FORMAT_TAG", "SCENARIOS_TAG", "TABLE_TAG", "HYPERLINK_TAG"))
_parse_sheet = _openpyxl_sheet_reader.WorkSheetParser.parse


def _parse_cells_and_merges(parser: object) -> Iterator[tuple[int, list]]:
    """While this module is reading, make `WorkSheetParser.parse` read only cells and merges (`_SHEET_PARTS_LEFT_UNREAD`). A row
    that has been read is also taken out of the tree — openpyxl only empties it, so one empty element per row stayed in memory
    until the sheet ended (Pinned by: test_audit_spreadsheet_fixes.py::TestSheetScansKeepNoTree)."""
    if _READING.get() is None:
        yield from _parse_sheet(parser)
        return

    data = None  # `<sheetData>`: rows already read are removed from it, so the parsed tree does not grow with the sheet
    for event, element in _openpyxl_sheet_reader.iterparse(parser.source, events=("start", "end")):
        if event == "start":
            if element.tag == _openpyxl_sheet_reader.DATA_TAG and data is None:
                data = element
            continue
        if element.tag == _openpyxl_sheet_reader.ROW_TAG:
            row = parser.parse_row(element)
            element.clear()
            if data is not None and len(data) and data[-1] is element:
                del data[:]
            yield row
        elif element.tag == _openpyxl_sheet_reader.MERGE_TAG:
            parser.merged_cells = _readable_merges(element)
            element.clear()
        elif element.tag in _SHEET_PARTS_LEFT_UNREAD:
            element.clear()


_MERGE_CELL_TAG = f"{{{SHEET_MAIN_NS}}}mergeCell"


def _readable_merges(element: etree._Element) -> MergeCells:
    """Carry over from the merge list only the merges usable as a cell range (with `ref` as the only attribute). A single reversed
    range (`B3:A2`), row 0 (`A0:A2`), column name without a row (`abc`) or unknown attribute (`foo="1"`) used to reject the whole
    document. Only that merge is dropped, without a warning — the value is still in its cell
    (Pinned by: test_office_round45.py::TestMalformedMergeRange, test_core_spreadsheet.py::TestMergeReferenceWithoutRows)."""
    merges = []
    for child in element:
        reference = (child.get("ref") or "").strip() if child.tag == _MERGE_CELL_TAG else ""
        try:
            min_col, min_row, max_col, max_row = range_boundaries(reference) if reference else (None,) * 4
        except (ValueError, TypeError):
            continue
        if None in (min_col, min_row, max_col, max_row) or not 1 <= min_col <= max_col or not 1 <= min_row <= max_row:
            continue
        merges.append(MergeCell(ref=reference))
    return MergeCells(mergeCell=merges)


# Of the parts attached to a sheet, comments, drawings (charts and pictures) and pivot tables are removed from the relationship list
# only while this module is reading, so that openpyxl does not parse them. Comments are read by `_xlsx_comments`, charts and
# pictures by `_drawing_blocks`, directly from the ZIP (`sheet._images`, `_charts` and cell comments are not used), and a pivot
# table's values are already in the cells. A single `authorId="x"` or broken XML in comments, `legendPos val="right"` in a chart, or
# missing `cacheId` in a pivot table used to reject the whole document
# (Pinned by: test_office_round43.py::TestPartsTheConverterReadsItself, test_office_round44.py)
_RELATIONS_LEFT_UNREAD = frozenset((
    _openpyxl_excel_reader.COMMENTS_NS, _openpyxl_excel_reader.SpreadsheetDrawing._rel_type,
    _openpyxl_excel_reader.TableDefinition.rel_type))
_get_dependents = _openpyxl_excel_reader.get_dependents


def _dependents_left_to_openpyxl(archive: object, filename: str) -> object:
    relations = _get_dependents(archive, filename)
    if _READING.get() is None:
        return relations

    kept = RelationshipList()
    for relation in relations:
        if relation.Type not in _RELATIONS_LEFT_UNREAD:
            kept.append(relation)
    return kept


_openpyxl_sheet_reader.WorkSheetParser.parse = _parse_cells_and_merges
_openpyxl_excel_reader.get_dependents = _dependents_left_to_openpyxl


# The conversion uses only the title (`metadata["title"]`) from the document properties. When openpyxl could not read a single
# created date, printed date or custom property value (the date-only `2024-01-05` that W3CDTF allows, `0000-00-00T00:00:00Z`, `abc`
# in a `vt:i4`, broken XML) it rejected the whole document. A DOCX or PPTX with the same core.xml was read. While reading, custom
# properties are not read, and when the document properties cannot be read only the title is looked up directly (no warning, same as
# `_package.core_properties` for DOCX/PPTX. Pinned by: test_office_round45.py::TestDocumentPropertiesTheConverterDoesNotNeed).
_read_properties = _openpyxl_excel_reader.ExcelReader.read_properties
_CORE_TITLE = "{http://purl.org/dc/elements/1.1/}title"


def _properties_or_title(reader: _openpyxl_excel_reader.ExcelReader) -> None:
    if _READING.get() is None:
        _read_properties(reader)
        return

    try:
        _read_properties(reader)
    except (ValueError, TypeError, SyntaxError, AttributeError, OverflowError):
        try:
            root = etree.fromstring(reader.archive.read(_openpyxl_excel_reader.ARC_CORE), parser=_XML_PARSER)
        except etree.XMLSyntaxError:
            return
        reader.wb.properties.title = root.findtext(_CORE_TITLE)


def _chartsheet_without_parts(reader: _openpyxl_excel_reader.ExcelReader, sheet: object, rel: object) -> None:
    """While reading, a chart sheet only gets its name put in the sheet list. openpyxl reads view/print settings and the drawing
    relationship from chart sheet XML, but the chart is read by `_drawing_blocks` directly from the ZIP. On a chart sheet without a
    relationships file (.rels) (one openpyxl created without a chart) openpyxl raised AttributeError and the whole document was
    rejected (Pinned by: test_office_round45.py::TestChartSheetWithoutRelationships)."""
    if _READING.get() is None:
        _read_chartsheet(reader, sheet, rel)
        return

    chartsheet = Chartsheet(parent=reader.wb)
    chartsheet.title = sheet.name
    reader.wb._add_sheet(chartsheet)


_read_chartsheet = _openpyxl_excel_reader.ExcelReader.read_chartsheet


# A streamed sheet looks for `<dimension>` when it is opened (`ReadOnlyWorksheet._get_size`). Without one, openpyxl's search runs to
# the end of `<sheetData>`, so the whole sheet was parsed once for nothing: a 13 MB workbook (600,000 rows) took 16.7 s instead of
# 3.7 s. The range is discarded before the rows are read anyway (`reset_dimensions`, `_streamed_rows`), so while this module is
# reading it is not looked for (Pinned by: test_audit_spreadsheet_fixes.py::TestStreamedSheetWithoutADimension).
_get_streamed_size = ReadOnlyWorksheet._get_size


def _size_unless_reading(sheet: ReadOnlyWorksheet) -> None:
    if _READING.get() is None:
        _get_streamed_size(sheet)


ReadOnlyWorksheet._get_size = _size_unless_reading
_openpyxl_excel_reader.ExcelReader.read_properties = _properties_or_title
_openpyxl_excel_reader.ExcelReader.read_custom = _skipped_while_reading(_openpyxl_excel_reader.ExcelReader.read_custom)
_openpyxl_excel_reader.ExcelReader.read_chartsheet = _chartsheet_without_parts


def _is_number(value: str | None) -> bool:
    # Digits `int()` refuses (more than 4,300 of them) are not a number either: a cell style index like that rejected the whole
    # document (Pinned by: test_audit_spreadsheet.py::TestIntegerAttributesTooLongToRead).
    return value is not None and value.strip().isascii() and decimal_int(value.strip(), None) is not None
