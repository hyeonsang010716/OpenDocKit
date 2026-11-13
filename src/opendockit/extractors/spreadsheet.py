"""XLSX → one Markdown table per sheet, with the pictures, charts, text boxes, comments and form controls of the sheet."""

from collections import Counter, deque
from collections.abc import Callable, Iterator
from contextvars import ContextVar
import copy
from dataclasses import dataclass, field
from datetime import date, datetime, time
import html
from io import BytesIO
import math
import posixpath
import re
import zipfile
from zipfile import BadZipFile

from lxml import etree
import openpyxl
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import Text
from openpyxl.chartsheet import Chartsheet
from openpyxl.formula.translate import Translator
from openpyxl.packaging.relationship import RelationshipList
from openpyxl.reader import excel as _openpyxl_excel_reader
from openpyxl.styles.cell_style import CellStyleList
from openpyxl.styles.numbers import BUILTIN_FORMATS
from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries
from openpyxl.utils.datetime import from_excel, from_ISO8601
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.worksheet import _reader as _openpyxl_sheet_reader
from openpyxl.worksheet.formula import ArrayFormula
from openpyxl.worksheet.merge import MergeCell, MergeCells
from openpyxl.xml.constants import SHEET_MAIN_NS
from openpyxl.xml.functions import iterparse as _openpyxl_iterparse

from opendockit.exceptions import CorruptedDocumentError, UnsupportedFormatError
from opendockit.extractors._charts import package_loader, render_chart, render_chartex, render_smartart
from opendockit.extractors._inline import SUBSCRIPT, SUPERSCRIPT, mark_script, resolve_scripts
from opendockit.extractors._package import _XML_PARSER, decimal_int, parse_part
from opendockit.extractors._sheet_format import _format_value, _format_within_length, _is_weekday_only, _shows_date, format_cell
from opendockit.extractors._sheet_layout import (
    _CHECKED,
    _MAX_MERGED_CELLS,
    _UNCHECKED,
    _Across,
    _append_sheet,
    _CellComment,
    _collect_rows,
    _comment_block,
    _fill_merges,
    _level_marked,
    _SheetRows,
    _until_long_empty_gap,
)
from opendockit.markdown import solid_length
from opendockit.models import ConvertOptions, Extraction, quiet_warnings


# When the sheet XML (uncompressed) totals at most this size, the workbook is fully loaded. Only a full load carries merged-cell
# information; workbooks with many cells are streamed (read_only) to protect memory (fully loading a 130k-row, 60MB sheet takes
# 3.6s and 570MB).
# Deliberate: the split is by sheet XML size, not file size. A workbook with pictures is large because of the pictures, so splitting
# by file size sent it to streaming even with only a few rows of cells, losing merges and pictures
# (Pinned by: test_image.py::test_xlsx_that_is_large_only_because_of_its_picture_keeps_merges_and_picture).
_FULL_LOAD_MAX_SHEET_BYTES = 20 * 1024 * 1024
_RELATIONSHIPS = "{http://schemas.openxmlformats.org/package/2006/relationships}Relationship"
_BLIP = "{http://schemas.openxmlformats.org/drawingml/2006/main}blip"
_EMBED = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"


# In a full load openpyxl turns the pivot caches the workbook points to (definition + records) entirely into objects. The conversion
# does not use those values, yet a 2.8MB file with 9MB of records took 14s and 1.5GB (its sheet XML was 18MB, so the size threshold
# did not catch it). When the uncompressed sizes total more than this, the workbook is streamed, which does not read pivot caches
# (1MB ≈ 1.5s)
# (Pinned by: test_office.py::TestReportedGaps::test_xlsx_with_a_large_pivot_cache_is_streamed).
_MAX_PIVOT_CACHE_BYTES = 1024 * 1024
_PIVOT_CACHE_DEFINITION = "/pivotCacheDefinition"
# Merge and hyperlink ranges in sheet XML (`<mergeCell ref="A1:B2"/>`, `<hyperlink ref=…>`). An element name cannot be written another
# way in XML, but an attribute value can vary in quotes, whitespace and character references, so attributes are skipped one at a
# time to find ref, and the value is decoded with html.unescape.
_MERGE_REF = re.compile(rb"<(?:[\w.-]+:)?(?:mergeCell|hyperlink)\b(?:\s+[\w:.-]+\s*=\s*(?:\"[^\"]*\"|'[^']*'))*?\s+ref\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")  # noqa: E501


# Formula cells (<f>) in sheet XML, and formula cells stored together with their computed result (<v>value</v>).
# The inside of a tag never runs past the next `<` (`[^<>]*`, in these patterns and in `_VIEW_SETTINGS`, `_SHEET_STATE` and
# `_CONTROL_ELEMENT`): with `[^>]*` every unclosed tag was scanned to the end of the part, so a 4.7 KB file whose sheet is `<c `
# 80,000 times took 50 s (Pinned by: test_audit_spreadsheet.py::TestUnclosedTagsInSheetXml).
_FORMULA = re.compile(rb"<(?:\w+:)?f[\s>/]")
_FORMULA_WITH_RESULT = re.compile(rb"(?:</(?:\w+:)?f>|<(?:\w+:)?f\b[^<>]*/>)\s*<(?:\w+:)?v>[^<]")
# A formula cell stored without a result: no `<v>` after the formula, or an empty one. A formula computed to an empty string
# (`t="str"` with an empty `<v>`) does have a result.
_FORMULA_WITHOUT_RESULT = re.compile(
    rb"<(?:\w+:)?c\b(?![^<>]*\bt\s*=\s*[\"']str[\"'])[^<>]*>\s*<(?:\w+:)?f\b(?:[^<>]*/>|[^<>]*>[^<]*</(?:\w+:)?f>)\s*"
    rb"(?:<(?:\w+:)?v\s*/>|<(?:\w+:)?v>\s*</(?:\w+:)?v>)?\s*</(?:\w+:)?c>")


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
        if isinstance(cell["value"], float) and not math.isfinite(cell["value"]):
            raise ValueError  # `1E+400` becomes infinity: it came out as 'inf', or a date format rejected the whole document with OverflowError
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
    (Pinned by: test_spreadsheet_damaged_parts.py::TestMalformedTextRunProperties)."""
    def read(source: object) -> list:
        if _READING.get() is None:
            return original(source)

        strings: list = []
        for _, node in _openpyxl_iterparse(source):
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
    """While this module is reading, make `WorkSheetParser.parse` read only cells and merges (`_SHEET_PARTS_LEFT_UNREAD`)."""
    if _READING.get() is None:
        yield from _parse_sheet(parser)
        return

    for _, element in _openpyxl_sheet_reader.iterparse(parser.source):
        if element.tag == _openpyxl_sheet_reader.ROW_TAG:
            row = parser.parse_row(element)
            element.clear()
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
_openpyxl_excel_reader.ExcelReader.read_properties = _properties_or_title
_openpyxl_excel_reader.ExcelReader.read_custom = _skipped_while_reading(_openpyxl_excel_reader.ExcelReader.read_custom)
_openpyxl_excel_reader.ExcelReader.read_chartsheet = _chartsheet_without_parts


def extract_xlsx(data: bytes, options: ConvertOptions) -> Extraction:
    # The UserWarning openpyxl emits for each part it cannot read (data validation and conditional formatting of the Excel x14
    # extension, no default style, etc.) is suppressed only while reading. Notices irrelevant to the conversion result cluttered the
    # caller's stderr. Streaming (read_only) emits them while rows are being read, so the whole extraction is wrapped, not just the
    # load. Afterwards the warning settings are unchanged — `models.quiet_warnings` instead of the thread-unsafe catch_warnings
    # (Pinned by: test_core_spreadsheet.py::TestOpenpyxlWarnings).
    token = _RAW_OUT_OF_RANGE_SERIALS.set(True)
    reading = _READING.set(_CellReading())
    try:
        with quiet_warnings((UserWarning, r"openpyxl\.")):
            return _extract_xlsx(data, options)
    finally:
        _READING.reset(reading)
        _RAW_OUT_OF_RANGE_SERIALS.reset(token)


def _extract_xlsx(data: bytes, options: ConvertOptions) -> Extraction:
    data, repeated = _without_repeated_sheets(data)
    with zipfile.ZipFile(BytesIO(data)) as archive:
        # openpyxl reads the sheets the workbook's sheet list points to through relationships, wherever they are stored. Looking
        # only under `xl/worksheets/`, a sheet at `xl/ws/sheet1.xml` dodged every check below: one merge A1:CV20000 was fully
        # loaded (9 s, 1.5 GB) (Pinned by: test_audit_spreadsheet.py::TestSheetStoredAtAnotherPath).
        listed = _listed_sheet_parts(archive)
        sheets = [info for info in archive.infolist() if info.filename.startswith("xl/worksheets/") or info.filename in listed]
        sheet_names = [info.filename for info in sheets]
        large = sum(info.file_size for info in sheets) > _FULL_LOAD_MAX_SHEET_BYTES
        # In a full load openpyxl decompresses a picture again for every reference (a 56KB file pointing to one picture 40 times used
        # 1.2GB) and creates a cell object for every position a merge covers (a 4.8KB file with one merge used 800MB). When that
        # much exceeds the limit, the workbook is streamed, which reads each picture once and does not read merges
        # (Pinned by: test_image.py::TestResourceBounds::test_xlsx_picture_referenced_many_times_is_read_once,
        # test_xlsx_picture_reference_count_cannot_be_dodged, test_office.py::TestXlsx::test_huge_merged_range_is_read_without_expanding_it).
        read_only = (large or _picture_reference_bytes(archive) > options.max_uncompressed_bytes
                     or _merged_cell_count(archive, sheet_names) > _MAX_MERGED_CELLS
                     or _pivot_cache_bytes(archive) > _MAX_PIVOT_CACHE_BYTES)
        # A workbook large enough for its sheet XML to be big was saved by Excel, so it contains results. The sheet XML is not
        # scanned a second time. Small workbooks that are streamed because of pictures or merges are checked
        # (Pinned by: test_office.py::TestXlsx::test_formula_check_still_runs_when_pictures_force_streaming).
        formulas_only, unresolved = (False, set()) if large else _formulas_without_results(archive, sheet_names)
        # A 0 written in the result slot by a library that does not calculate is not a result. Sheets are scanned only when the
        # producing program's mark is present, so this is checked even when the sheet XML is large — libraries that write big
        # export files (POI SXSSF) produce exactly this shape.
        placeholder_zeros = (not formulas_only and _written_without_calculation(archive)
                             and _only_zero_results(archive, sheet_names))
        formulas_only = formulas_only or placeholder_zeros
        reading = _READING.get()
        reading.rich_values = _rich_values(archive, set(archive.namelist()))
    styles_reduced = formats_lost = False
    try:
        # rich_text: read superscript/subscript on part of the characters in a cell (full load only — streaming gives plain text).
        workbook = _load_workbook(BytesIO(data), read_only=read_only, data_only=not formulas_only, rich_text=True)
    except (InvalidFileException, BadZipFile) as exc:
        raise CorruptedDocumentError("The XLSX document structure could not be read.") from exc
    except KeyError as exc:
        # The part itself is missing (e.g. a sheet the workbook points to). openpyxl does not follow a sheet's table, hyperlink and
        # drawing relationships while reading (`_SHEET_PARTS_LEFT_UNREAD`, `_RELATIONS_LEFT_UNREAD`), so a broken relationship does
        # not end up here — broken pictures and charts are left out by `_drawing_blocks` with a warning
        # (Pinned by: test_spreadsheet.py::TestMissingPartRelationships)
        raise CorruptedDocumentError("The XLSX document structure could not be read.") from exc
    except (TypeError, ValueError, IndexError, OverflowError) as exc:
        # openpyxl rejects a stylesheet that violates the spec with TypeError (empty <fill>, unknown attribute), ValueError (a value
        # outside the allowed list — wrapped as 'could not read stylesheet'), IndexError (a missing font/fill/border index in a
        # named style) or OverflowError (fillId/xfId above 2³¹). Instead of telling them apart by message,
        # read once more with a stylesheet reduced to display formats. Only the styles changed, so if it succeeds the cause was the
        # styles, and if it fails again the document is rejected with the first exception
        # (Pinned by: test_spreadsheet.py::TestDamagedStylesheet, test_spreadsheet_damaged_parts.py::TestNamedStyleOutOfRange).
        # If that is still rejected, read a copy without the view/print settings the result does not use (`_repaired_copies`).
        workbook = None

        for copy, styles_reduced, formats_lost in _repaired_copies(data):
            try:
                workbook = _load_workbook(BytesIO(copy), read_only=read_only, data_only=not formulas_only, rich_text=True)
            except MemoryError:
                raise  # keep the cause so the server can report 'document too large' (docs/server.md, resource limits)
            except Exception:  # noqa: BLE001 — any failure means: try the next repaired copy
                continue
            data = copy
            break
        if workbook is None:
            raise CorruptedDocumentError("The XLSX document structure could not be read.") from exc

    extraction = Extraction()
    if repeated:
        extraction.warnings.append("The workbook points to the same sheet more than once, so it was read only once.")
    if styles_reduced:
        extraction.warnings.append("Some cell style information is corrupted, so some display formats could not be read. "
                                   "Dates and amounts may appear as numbers."
                                   if formats_lost else
                                   "Some cell style information is corrupted, so only number and date display formats were kept.")
    pictures: dict[str, bytes] = {}  # picture file path → content. The same picture is read only once, even across sheets
    if placeholder_zeros:
        extraction.warnings.append("All formula results are stored as 0 (the file was made by a program that does not calculate), "
                                   "so the formulas were copied as written "
                                   "(opening and re-saving the file in Excel converts them to values).")
    elif formulas_only:
        extraction.warnings.append("Formula results are not stored in the file, so the formulas were copied as written "
                                   "(opening and re-saving the file in Excel converts them to values).")
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            names = set(archive.namelist())
            paths, hidden = _sheet_paths(archive, names)
            people = _xlsx_people(archive, names)
            reading.picture_text = lambda picture: _cell_picture_text(picture, archive, names, pictures, extraction, options)
            # Chart sheets (Excel F11) are not in `workbook.worksheets`, so a workbook whose source sheet is hidden gave an empty
            # result. They are kept in sheet order (Pinned by: test_spreadsheet.py::TestChartSheets). `workbook[name]` scans the
            # sheets on every lookup, which is quadratic in the sheet count, so the list is used
            for sheet in workbook._sheets:
                if (sheet.sheet_state != "visible" or sheet.title in hidden) and not options.include_hidden_sheets:
                    extraction.warnings.append(f"Hidden sheet '{sheet.title}' was excluded.")
                    continue

                path = str(getattr(sheet, "_worksheet_path", None) or paths.get(sheet.title, "")).lstrip("/")
                if isinstance(sheet, Chartsheet):
                    _append_sheet(extraction, sheet.title, [], _drawing_blocks(archive, names, path, pictures, extraction, options))
                    if extraction.exceeded:
                        break
                    continue

                # The column limit counts from the first used column (`_first_used_column`). Streamed sheets and sheets with
                # formula cells lacking a value count from column A (known accepted gap)
                offset = 0 if read_only or path in unresolved else _first_used_column(sheet)
                cells = (_streamed_rows(sheet, options.max_sheet_cols) if read_only
                         else _loaded_rows(sheet, options.max_sheet_cols, offset))
                if path in unresolved and not formulas_only:
                    formulas = _unresolved_formulas(archive.read(path), options.max_sheet_cols)
                    cells = _with_formulas(cells, formulas, sheet.title, extraction.warnings)
                if read_only:
                    cells = _until_long_empty_gap(cells, sheet.title, extraction.warnings)
                numbers: list[int] = []  # which row of the sheet each row in rows is (for filling merges)
                rows = _collect_rows(_noting_numbers(cells, numbers), options, sheet.title, extraction)
                across: _Across = {}
                if not read_only:
                    across = _fill_merges(sheet, rows, numbers, options.max_sheet_cols, offset)
                row_at = _SheetRows(dict(zip(numbers, rows, strict=False)), offset)  # text of the row a picture or comment sits on
                if not read_only:
                    rows = _with_blank_gaps(rows, numbers)

                drawn = [] if extraction.exceeded else _drawing_blocks(archive, names, path, pictures, extraction, options,
                                                                       row_at)
                if not extraction.exceeded:
                    comments, damaged = _xlsx_comments(archive, names, path, people)
                    if damaged:
                        extraction.warnings.append(
                            f"The cell comment part of sheet '{sheet.title}' is corrupted, so its comments could not be read.")
                    drawn.append(_comment_block(comments, row_at, extraction, options))
                _append_sheet(extraction, sheet.title, rows, drawn, across)
                if extraction.exceeded:
                    break
    finally:
        reading.picture_text = None
        workbook.close()

    if reading.malformed:
        extraction.warnings.append(f"{reading.malformed:,} malformed cell value(s) were copied as the text written in the file "
                                   "(a shared-string index that points to no text is left blank).")
    if reading.unread_rich:
        extraction.warnings.append(f"{reading.unread_rich:,} in-cell picture or data-type value(s) could not be found and were left blank.")
    extraction.metadata["sheet_count"] = str(len(extraction.sections))
    if workbook.properties.title:
        extraction.metadata["title"] = workbook.properties.title
    return extraction


_DETACHED_PARTS_WARNING = "Pictures and charts of a sheet that have no relationship information were left out."


def _load_workbook(source: BytesIO, **options: bool) -> openpyxl.Workbook:
    """openpyxl load. The UserWarning etc. that openpyxl emits for a workbook without a default style (made by Apache POI) is
    suppressed by `extract_xlsx` while reading — openpyxl uses a default style instead, so the conversion is unaffected. The cell
    interpretation counts start from zero on every re-read."""
    reading = _READING.get()
    if reading is not None:
        reading.malformed = reading.unread_rich = 0
    return openpyxl.load_workbook(source, **options)


_SPREADSHEET_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_STRICT_SPREADSHEET_MAIN = "http://purl.oclc.org/ooxml/spreadsheetml/main"
_RELATIONSHIP_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def _without_repeated_sheets(data: bytes) -> tuple[bytes, bool]:
    """When workbook.xml points to the same sheet part more than once, make a copy with the second and later ones removed.

    openpyxl re-reads the sheet from the start for every `<sheet>`, so a 44KB file pointing to the same sheet 1000 times used 23s
    and 2.6GB. The character limit is only judged after loading finishes, so this is fixed before loading
    (Pinned by: test_office.py::TestXlsx::test_sheet_listed_many_times_is_read_once).
    """
    with zipfile.ZipFile(BytesIO(data)) as archive:
        names = set(archive.namelist())
        workbook_path = _workbook_path(archive, names)
        if workbook_path not in names:
            return data, False
        try:
            root = etree.fromstring(archive.read(workbook_path), parser=_XML_PARSER)
        except etree.XMLSyntaxError:
            return data, False  # openpyxl rejects it for the same reason
        if root.tag == f"{{{_STRICT_SPREADSHEET_MAIN}}}workbook":
            # A workbook saved as 'Strict Open XML Spreadsheet' has entirely different namespaces. openpyxl found no sheets at all,
            # so an empty result went out as a 'success' without a warning
            # (Pinned by: test_spreadsheet.py::test_strict_open_xml_workbook_is_reported_as_unsupported).
            raise UnsupportedFormatError("Excel documents in the Strict Open XML format are not supported. "
                                         "Re-save the file in Excel as 'Excel Workbook (.xlsx)'.")
        targets = _related(archive, names, workbook_path)
        seen: set[str] = set()
        repeated = []
        for sheet in root.iter(f"{{{_SPREADSHEET_MAIN}}}sheet"):
            target = targets.get(sheet.get(_RELATIONSHIP_ID, ""), sheet.get(_RELATIONSHIP_ID, ""))
            if target in seen:
                repeated.append(sheet)
            seen.add(target)
        if not repeated:
            return data, False

        for sheet in repeated:
            sheet.getparent().remove(sheet)
        workbook = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as copy:
            for info in archive.infolist():
                copy.writestr(info.filename, workbook if info.filename == workbook_path else archive.read(info))
    return buffer.getvalue(), True


_STYLES_PATH = "xl/styles.xml"  # openpyxl reads only this path, without following relationships


def _with_number_formats_only(data: bytes) -> tuple[bytes, bool] | None:
    """A copy whose styles part is replaced by a minimal stylesheet keeping only display formats (numFmts and each cell format's
    numFmtId), and whether any display format was unreadable and dropped (such cells get the General format, so dates come out as
    serial numbers — the warning differs). None when it cannot be replaced.

    Cells point to a position in the cell-format list with `s="n"`, so the number and order of cell formats are kept. Without
    display formats dates come out as serial numbers and amounts as raw values, so the styles are not discarded entirely. The dxf
    entries that conditional formatting points to by position are also kept in number, as empty ones. Fonts, fills and borders are
    dropped (superscript/subscript applied to a whole cell disappears with them).
    """
    with zipfile.ZipFile(BytesIO(data)) as archive:
        try:
            root = etree.fromstring(archive.read(_STYLES_PATH), parser=_XML_PARSER)
        except (KeyError, etree.XMLSyntaxError):
            return None

        main = f"{{{_SPREADSHEET_MAIN}}}"
        # Formats whose id is not a number are dropped, and cell formats pointing to such an id become General (0) — carried over
        # as is, they would be rejected again.
        number_formats = root.findall(f"{main}numFmts/{main}numFmt")
        cell_xfs = root.findall(f"{main}cellXfs/{main}xf")
        lost = (any(not _is_number(fmt.get("numFmtId")) or not fmt.get("formatCode") for fmt in number_formats)
                or any(xf.get("numFmtId") is not None and not _is_number(xf.get("numFmtId")) for xf in cell_xfs))
        formats = "".join(f'<numFmt numFmtId="{fmt.get("numFmtId")}" formatCode="{html.escape(fmt.get("formatCode", ""))}"/>'
                          for fmt in number_formats if _is_number(fmt.get("numFmtId")))
        cell_formats = "".join(f'<xf numFmtId="{xf.get("numFmtId") if _is_number(xf.get("numFmtId")) else 0}" applyNumberFormat="1"/>'
                               for xf in cell_xfs) or '<xf numFmtId="0"/>'
        differential = "<dxf/>" * len(root.findall(f"{main}dxfs/{main}dxf"))
        styles = (f'<styleSheet xmlns="{_SPREADSHEET_MAIN}"><numFmts>{formats}</numFmts><fonts><font/></fonts>'
                  '<fills><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
                  '<borders><border/></borders><cellStyleXfs><xf numFmtId="0"/></cellStyleXfs>'
                  f'<cellXfs>{cell_formats}</cellXfs><cellStyles><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
                  f"<dxfs>{differential}</dxfs></styleSheet>")
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as copy:
            for info in archive.infolist():
                copy.writestr(info.filename, styles.encode() if info.filename == _STYLES_PATH else archive.read(info))
    return buffer.getvalue(), lost


def _repaired_copies(data: bytes) -> Iterator[tuple[bytes, bool, bool]]:
    """Copies to retry a workbook that openpyxl rejected with TypeError, ValueError or IndexError (copy, whether styles were reduced,
    whether display formats were lost). In order: a stylesheet reduced to display formats (`_with_number_formats_only`), then one
    without view/print settings (`_without_view_settings`)."""
    reduced = _with_number_formats_only(data)
    if reduced is not None:
        yield reduced[0], True, reduced[1]
    cleaned = _without_view_settings(data)
    if cleaned is not None:
        yield cleaned, False, False


# View, print and calculation settings unrelated to values, formats and merges (the workbook's window layout, calculation options and
# defined names; a sheet's window, default row height, margins and print setup). Each occurs once, at a fixed position at the front
# or back of its part. Defined names (print areas, named ranges) are not used by the conversion. The search for a closing tag does
# not cross an opening tag of the same name — with many unclosed opening tags it would scan to the end every time, taking quadratic
# time
# (Pinned by: test_core_spreadsheet.py::TestMalformedViewAndPrintSettings::test_unclosed_settings_take_linear_time).
_VIEW_SETTINGS = re.compile(
    rb"<((?:[\w.-]+:)?(?:bookViews|calcPr|definedNames|sheetViews|sheetFormatPr|pageMargins|pageSetup|printOptions))\b"
    rb"(?:[^<>]*/>|(?:(?!<\1\b).)*?</\1\s*>)", re.DOTALL)
# Sheet state in the workbook's sheet list (`<sheet … state="VeryHidden">`). A single value with the wrong letter case made openpyxl
# reject the whole document.
_SHEET_STATE = re.compile(rb"(<(?:[\w.-]+:)?sheet\b[^<>]*?\sstate\s*=\s*)(\"[^\"]*\"|'[^']*')")
_SHEET_STATES = {"visible": b"visible", "hidden": b"hidden", "veryhidden": b"veryHidden"}


def _without_view_settings(data: bytes) -> bytes | None:
    """A copy with the view, print and calculation settings (`_VIEW_SETTINGS`) removed from the workbook and sheet parts and the sheet
    state corrected to its spec name. None when there is nothing to fix.

    When a single attribute of those was malformed (`pageMargins top="abc"`, `workbookView tabRatio="x"`, `definedName foo="1"`)
    openpyxl rejected the whole document. The result does not use these settings, so nothing is lost by removing them and there is
    no warning. Sheet state is read case-insensitively and an unknown value is treated as hidden (the side that does not reveal
    hidden content — the hidden-sheet warning reports it). A sheet's settings are not interpreted by openpyxl while reading
    (`_SHEET_PARTS_LEFT_UNREAD`)(Pinned by: test_core_spreadsheet.py::TestMalformedViewAndPrintSettings,
    test_office_round45.py::TestWorkbookListOddities).
    """
    def sheet_state(match: re.Match[bytes]) -> bytes:
        value = match.group(2)[1:-1].decode("utf-8", errors="replace").strip().lower()
        return match.group(1) + b'"' + _SHEET_STATES.get(value, b"hidden") + b'"'

    rewritten: dict[str, bytes] = {}
    with zipfile.ZipFile(BytesIO(data)) as archive:
        names = set(archive.namelist())
        workbook_path = _workbook_path(archive, names)
        for name in sorted(names):
            if name != workbook_path and not (name.startswith(("xl/worksheets/", "xl/chartsheets/")) and name.endswith(".xml")):
                continue
            xml = archive.read(name)
            cleaned = _VIEW_SETTINGS.sub(b"", xml)
            if name == workbook_path:
                cleaned = _SHEET_STATE.sub(sheet_state, cleaned)
            if cleaned != xml:
                rewritten[name] = cleaned
        if not rewritten:
            return None

        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as copy:
            for info in archive.infolist():
                copy.writestr(info.filename, rewritten.get(info.filename) or archive.read(info))
    return buffer.getvalue()


def _is_number(value: str | None) -> bool:
    # Digits `int()` refuses (more than 4,300 of them) are not a number either: a cell style index like that rejected the whole
    # document (Pinned by: test_audit_spreadsheet.py::TestIntegerAttributesTooLongToRead).
    return value is not None and value.strip().isascii() and decimal_int(value.strip(), None) is not None


_OFFICE_DOCUMENT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"


def _workbook_path(archive: zipfile.ZipFile, names: set[str]) -> str:
    """The workbook part the package relationships point to (usually `xl/workbook.xml`). Even under another name
    (`xl/workbook2.xml`) openpyxl follows this relationship to read it, so the resource checks must look at the same part
    (Pinned by: test_detect.py::TestRenamedMainPart)."""
    if "_rels/.rels" in names:
        try:
            root = etree.fromstring(archive.read("_rels/.rels"), parser=_XML_PARSER)
        except etree.XMLSyntaxError:
            return "xl/workbook.xml"
        for relationship in root.iter(_RELATIONSHIPS):
            if relationship.get("Type") == _OFFICE_DOCUMENT:
                return relationship.get("Target", "").lstrip("/")
    return "xl/workbook.xml"


def _sheet_paths(archive: zipfile.ZipFile, names: set[str]) -> tuple[dict[str, str], set[str]]:
    """(sheet name → sheet part path, names of hidden sheets). A fully loaded sheet object does not know its own part path (a
    streamed sheet knows it as `_worksheet_path`). openpyxl does not carry the hidden state (`state`) over to chart sheets, so
    hidden is read from workbook.xml."""
    workbook_path = _workbook_path(archive, names)
    try:
        root = etree.fromstring(archive.read(workbook_path), parser=_XML_PARSER)
    except (KeyError, etree.XMLSyntaxError):
        return {}, set()
    targets = _related(archive, names, workbook_path)
    sheets = list(root.iter(f"{{{_SPREADSHEET_MAIN}}}sheet"))
    return ({sheet.get("name", ""): targets.get(sheet.get(_RELATIONSHIP_ID, ""), "") for sheet in sheets},
            {sheet.get("name", "") for sheet in sheets if sheet.get("state", "visible") != "visible"})


def _listed_sheet_parts(archive: zipfile.ZipFile) -> set[str]:
    """Paths of the parts the workbook's sheet list points to through relationships. The relationship type is not looked at —
    openpyxl reads whatever part a `<sheet>` points to as a sheet. Empty when the workbook part or its relationships cannot be
    read (openpyxl rejects the document for the same reason)."""
    names = set(archive.namelist())
    workbook_path = _workbook_path(archive, names)
    try:
        root = etree.fromstring(archive.read(workbook_path), parser=_XML_PARSER)
        targets = _related(archive, names, workbook_path)
    except (KeyError, etree.XMLSyntaxError):
        return set()
    return {targets.get(sheet.get(_RELATIONSHIP_ID, ""), "") for sheet in root.iter(f"{{{_SPREADSHEET_MAIN}}}sheet")}


def _formulas_without_results(archive: zipfile.ZipFile, sheet_names: list[str]) -> tuple[bool, set[str]]:
    """(whether the workbook has formulas but not a single stored result, the sheet parts that appear to have formula cells without
    a result).

    Excel writes the computed result next to every formula when saving. Files made by libraries (openpyxl etc.) write only the
    formula, so reading values only (`data_only=True`) leaves all those cells blank. Such files are read as formulas and shown with
    a warning. If there is even one result the file was saved by Excel, so values are read — for a formula computed to an empty
    string an empty result is normal
    (Pinned by: test_office.py::test_formulas_without_stored_results_are_shown_as_formulas, test_formulas_with_stored_results_show_the_results).
    A workbook whose values are read can also contain formulas without results (a template saved by Excel into which a library
    wrote more formulas). For such sheets `_unresolved_formulas` fills in just those cells with the formula.
    """
    has_formula = has_result = False
    unresolved = set()

    for name in sheet_names:
        xml = archive.read(name)
        has_result = has_result or _FORMULA_WITH_RESULT.search(xml) is not None
        if _FORMULA_WITHOUT_RESULT.search(xml):
            unresolved.add(name)
        has_formula = has_formula or name in unresolved or _FORMULA.search(xml) is not None
    return has_formula and not has_result, unresolved


_EXTENDED_PROPERTIES = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties"


def _written_without_calculation(archive: zipfile.ZipFile) -> bool:
    """Whether the workbook carries the mark of a library that does not calculate formulas and writes 0 in the result slot.

    Apache POI writes 'Apache POI' as Application in the document properties (docProps/app.xml); xlsxwriter writes 'Microsoft
    Excel' as Application but adds a recalculate-on-open flag (`<calcPr fullCalcOnLoad="1"/>`). Excel and LibreOffice save
    computed results, so they do not use these marks. A 0 in a file without a mark is trusted as a computed value (a total of 0
    is normal in an empty form).
    """
    names = set(archive.namelist())
    workbook_path = _workbook_path(archive, names)
    try:
        workbook = etree.fromstring(archive.read(workbook_path), parser=_XML_PARSER)
    except (KeyError, etree.XMLSyntaxError):
        return False
    calculation = workbook.find(f"{{{_SPREADSHEET_MAIN}}}calcPr")
    if calculation is not None and calculation.get("fullCalcOnLoad", "").strip().lower() in ("1", "true"):
        return True

    path = "docProps/app.xml"
    if "_rels/.rels" in names:
        try:
            package = etree.fromstring(archive.read("_rels/.rels"), parser=_XML_PARSER)
        except etree.XMLSyntaxError:
            return False
        path = next((relationship.get("Target", "").lstrip("/") for relationship in package.iter(_RELATIONSHIPS)
                     if relationship.get("Type") == _EXTENDED_PROPERTIES), path)
    try:
        properties = etree.fromstring(archive.read(path), parser=_XML_PARSER)
    except (KeyError, etree.XMLSyntaxError):
        return False
    return "apache poi" in (properties.findtext("{*}Application") or "").lower()


def _only_zero_results(archive: zipfile.ZipFile, sheet_names: list[str]) -> bool:
    """Whether there are formula cells and every stored result is the number 0 (cells without a result and empty-text results are
    not counted). A single non-zero result, or a text, boolean or error result, means the file was calculated — stop there. Sheet
    XML is scanned by streaming (even a large sheet uses no memory)."""
    zeros = 0
    for name in sheet_names:
        if not _has_formula(archive, name):
            continue  # do not XML-parse large export sheets without formulas (a 111MB sheet without formulas: 3.4s → 0.3s)
        with archive.open(name) as stream:
            for _, cell in etree.iterparse(stream, tag="{*}c", resolve_entities=False, no_network=True):
                value = cell.findtext("{*}v") if cell.find("{*}f") is not None else None
                kind = cell.get("t", "n")
                cell.clear()
                if value is None or not value.strip():
                    continue
                if kind != "n" or _float_or_none(value) != 0:
                    return False
                zeros += 1
    return zeros > 0


def _has_formula(archive: zipfile.ZipFile, name: str) -> bool:
    """Whether the sheet XML has a formula cell (`<f`). It is decompressed and searched in chunks (for a tag straddling a chunk
    boundary, the end of the previous chunk is prepended)."""
    tail = b""
    with archive.open(name) as stream:
        while chunk := stream.read(1 << 20):
            if _FORMULA.search(tail + chunk):
                return True
            tail = chunk[-16:]
    return False


def _float_or_none(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def _unresolved_formulas(xml: bytes, max_cols: int) -> dict[int, dict[int, str]]:
    """Formula cells stored without a computed result in a sheet whose values are read: row → column → formula text
    (`=SUM(A1:A3)`). Only cells within the column limit are collected.

    If there is even one result only values are read (`data_only=True`), so formula cells without a result became blank without a
    warning. The workbook is not loaded a second time as formulas; just those cells are looked up in the sheet XML. A formula
    computed to an empty string (`t="str"` with an empty `<v>`) does have a result. A shared formula has its text only in the first
    cell, so for the other cells it is rewritten for their own position (as when openpyxl reads formulas)
    (Pinned by: test_spreadsheet.py::TestFormulasPartlyWithoutResults).
    """
    shared: dict[str | None, tuple[str, str]] = {}  # shared formula index (si) → (formula, address of the first cell)
    formulas: dict[int, dict[int, str]] = {}

    for _, cell in etree.iterparse(BytesIO(xml), tag="{*}c", resolve_entities=False, no_network=True):
        formula, value, reference = cell.find("{*}f"), cell.find("{*}v"), cell.get("r", "")
        if formula is not None and reference:
            text = formula.text or ""
            try:
                if formula.get("t") == "shared" and text:
                    shared[formula.get("si")] = (text, reference)
                elif formula.get("t") == "shared" and formula.get("si") in shared:
                    first, origin = shared[formula.get("si")]
                    text = Translator(f"={first}", origin=origin).translate_formula(reference)[1:]
                row, column = coordinate_to_tuple(reference)
            except (ValueError, TypeError, IndexError):
                text = ""  # the cell address or the formula cannot be read. Left blank
            computed = cell.get("t") == "str" or (value is not None and value.text)
            if text and not computed and column <= max_cols:
                formulas.setdefault(row, {})[column] = f"={text}"
        cell.clear()
    return formulas


def _with_formulas(rows: Iterator[tuple[int, tuple[str, ...]]], formulas: dict[int, dict[int, str]], sheet_name: str,
                   warnings: list[str]) -> Iterator[tuple[int, tuple[str, ...]]]:
    """Fill the blanks of (row number, row) that are formula cells without a result with the formula text, and warn. Formulas of a
    row that did not arrive because it has no cells are inserted as a row at that position."""
    count = sum(len(columns) for columns in formulas.values())
    if count:
        warnings.append(f"Sheet '{sheet_name}': {count:,} formula(s) with no stored result were copied as written "
                        "(opening and re-saving the file in Excel converts them to values).")

    pending = deque(sorted(formulas))

    for number, row in rows:
        while pending and pending[0] < number:
            missing = pending.popleft()
            yield missing, _filled_with_formulas((), formulas[missing])
        if pending and pending[0] == number:
            row = _filled_with_formulas(row, formulas[pending.popleft()])
        yield number, row
    for missing in pending:
        yield missing, _filled_with_formulas((), formulas[missing])


def _filled_with_formulas(row: tuple[str, ...], columns: dict[int, str]) -> tuple[str, ...]:
    values = [*row, *[""] * (max(columns) - len(row))]
    for column, formula in columns.items():
        values[column - 1] = values[column - 1] or formula
    return tuple(values)


def _noting_numbers(rows: Iterator[tuple[int, tuple[str, ...]]], numbers: list[int]) -> Iterator[tuple[str, ...]]:
    """From (row number, row) yield only the row and record the number in `numbers`, in order. Even if reading stops midway, the
    number of rows yielded equals the number of numbers recorded."""
    for number, row in rows:
        numbers.append(number)
        yield row


def _with_blank_gaps(rows: list[list[str]], numbers: list[int]) -> list[list[str]]:
    """In fully loaded rows (only rows that have cells), insert one empty row wherever the row number skips. An empty row is where
    tables within a sheet are split (`_sheet_text`). It does not insert as many as the rows skipped — so that a sheet with one cell
    on the very last row does not create a million empty rows."""
    spaced: list[list[str]] = []
    previous = 0

    for number, row in zip(numbers, rows, strict=False):
        if previous and number > previous + 1:
            spaced.append([])
        spaced.append(row)
        previous = number
    return spaced


def _first_used_column(sheet: object) -> int:
    """Number of columns before the leftmost column holding a value in a fully loaded sheet (0-based). 0 when there is no value.

    Counting the column limit (`max_sheet_cols`) from column A made the last 4 columns of a 10-column table starting at column CQ
    disappear with a warning. The limit guards the width of a table, so it counts from the first used column
    (Pinned by: test_core_sheet_layouts.py::TestColumnLimitFromTheFirstUsedColumn). The columns to its left are all empty, so they
    are removed from the rows — so that rows do not grow by the number of leading empty cells.
    """
    return min((column for (_, column), cell in sheet._cells.items() if cell.value is not None), default=1) - 1


def _loaded_rows(sheet: object, max_cols: int, offset: int = 0) -> Iterator[tuple[int, tuple[str, ...]]]:
    """Collect only the cells present in the file of a fully loaded sheet, as (row number, row). Rows without cells are not yielded —
    merge filling matches by row number.

    `iter_rows()` creates a new cell object for every empty position up to the last row and column and attaches it to the sheet.
    One far column holding only formatting produced rows × columns of them, so a 4.9KB file used 17s and 3.3GB at 2000 columns.
    Values beyond the column limit will be discarded, so only the first such value is placed at position limit+1, just so that the
    exceeded warning fires (Pinned by: test_office.py::TestXlsx::test_full_load_does_not_create_cells_for_empty_positions,
    test_rows_are_never_wider_than_the_column_limit). `offset` is the number of leading empty columns to drop (`_first_used_column`).
    """
    by_row: dict[int, dict[int, object]] = {}
    for (row, column), cell in sheet._cells.items():
        if column <= offset:
            continue
        if column - offset <= max_cols:
            by_row.setdefault(row, {})[column - offset] = cell
        elif cell.value is not None:
            by_row.setdefault(row, {}).setdefault(max_cols + 1, cell)

    # 50 sheets (28KB) each with one cell on the very last row (1,048,576) created empty rows one by one and took 28s. Only rows
    # that exist are yielded
    # (Pinned by: test_spreadsheet.py::TestXlsxRowsFarApart).
    for row in sorted(by_row):
        cells = by_row[row]
        ordered = [cells.get(column) for column in range(1, max(cells) + 1)]
        yield row, _with_xlsx_level_mark(ordered, [_cell_text(cell) for cell in ordered])


def _streamed_rows(sheet: object, max_cols: int) -> Iterator[tuple[int, tuple[str, ...]]]:
    """Rows of a streamed sheet. Like `_loaded_rows`, it builds only up to column limit + 1 cells.

    A streamed sheet reads only up to the <dimension> range in the sheet head, and some generators write this value wrongly (A1:A1),
    so the rest vanished without a warning. The range is discarded and the actual rows are read to the end
    (Pinned by: test_office.py::TestXlsx::test_streamed_sheet_ignores_a_wrong_dimension).
    Without a range, openpyxl pads every row with empty cells up to that row's last cell (16,384 cells per row from a single XFD
    column). So before parsed cells are made into a row (`_get_row`, on this sheet object only) those beyond the limit are filtered
    out, and only the first such value is moved to position limit+1, just so that the exceeded warning fires.
    openpyxl sets the row width from the last cell written, so the cells are passed on sorted by column
    (Pinned by: test_office.py::TestXlsx::test_streamed_row_keeps_cells_written_out_of_order).
    """
    build_row = sheet._get_row

    def bounded_row(cells: list[dict], min_col: int = 1, max_col: int | None = None, values_only: bool = False) -> tuple:
        kept = sorted((cell for cell in cells if cell["column"] <= max_cols), key=lambda cell: cell["column"])
        overflow = next((cell for cell in cells if cell["column"] > max_cols and cell["value"] is not None), None)
        if overflow is not None:
            kept.append({**overflow, "column": max_cols + 1})
        return build_row(kept, min_col, max_col, values_only)

    sheet.reset_dimensions()
    sheet._get_row = bounded_row
    # Rows arrive under the number written in the file (`_consecutive`). Empty rows written in the file arrive too; row numbers
    # the file skips are not made into rows here (`_until_long_empty_gap` counts them and puts one empty row in their place).
    reading = _READING.get()
    numbers = None
    if reading is not None:
        numbers = reading.row_numbers = []
    try:
        for index, row in enumerate(sheet.iter_rows()):
            number = index + 1 if numbers is None else numbers[index]
            yield number, _with_xlsx_level_mark(row, [_cell_text(cell) for cell in row])
    finally:
        if reading is not None:
            reading.row_numbers = None


def _with_xlsx_level_mark(cells: list[object] | tuple[object, ...], texts: list[str]) -> tuple[str, ...]:
    """Add the indent-level mark to the first text cell of a row (`_level_marked`). Cell indent is `alignment.indent` (both full
    load and streaming)."""
    first = next((index for index, text in enumerate(texts) if text), None)
    if first is not None:
        value = _plain_value(cells[first].value)
        if isinstance(value, str):
            alignment = getattr(cells[first], "alignment", None)
            texts[first] = _level_marked(texts[first], value, getattr(alignment, "indent", 0) or 0)
    return tuple(texts)


# Built-in date/time format ids of East Asian locales. openpyxl does not know the format strings of these ids, treats them as General
# and returns the serial number (45365) as is. What an id actually looks like differs per locale (Korean, Chinese, Japanese), so it
# is not imitated; the value decides between date, time, and date and time
# (Pinned by: test_spreadsheet.py::TestEastAsianBuiltinDateFormats).
_EAST_ASIAN_DATE_FORMAT_IDS = frozenset((*range(27, 37), *range(50, 59)))


# Wrong strings in openpyxl's built-in format table → the strings of ECMA-376 Part 1 §18.8.30. 44 (the accounting format of Excel's
# 'Currency' cell style) lacked the section separators (;) and came out as `$1,234,567.5000000000@`. The other ids such as 37–43
# are the same as the table or look the same. The replacement is by string, not by id, so it applies to both fully loaded and
# streamed cells, and files that redefine the same id as a numFmt are untouched. Not applicable to XLS, where xlrd's table is
# correct (Pinned by: test_spreadsheet.py::TestBuiltinAccountingFormat).
_BUILTIN_FORMAT_FIXES = {
    BUILTIN_FORMATS[44]: '_("$"* #,##0.00_);_("$"* \\(#,##0.00\\);_("$"* "-"??_);_(@_)',
}


def _cell_text(cell: object) -> str:
    if cell is None:
        return ""
    value = _plain_value(cell.value)
    if isinstance(value, _CellPicture):
        reading = _READING.get()
        return reading.picture_text(value) if reading is not None and reading.picture_text is not None else ""
    number_format = _number_format(cell)
    number_format = _BUILTIN_FORMAT_FIXES.get(number_format, number_format)
    if _is_serial_zero_shown_as_date(value, number_format):
        return "0"
    if isinstance(value, (int, float)) and not isinstance(value, bool) \
            and (_format_id(cell) in _EAST_ASIAN_DATE_FORMAT_IDS or _is_weekday_only(number_format)):
        return _serial_date_text(value, cell.parent.parent.epoch)
    return format_cell(value, number_format)


def _number_format(cell: object) -> str | None:
    """The display format of a cell. An undefined format id (164 missing from numFmts, an id outside the built-in range) is None
    (General format).

    openpyxl rejected such an id with IndexError when reading the cell format, making the whole document 'corrupted'. Excel opens
    it with the General format
    (Pinned by: test_core_spreadsheet.py::TestUndefinedNumberFormatId).
    """
    try:
        return _format_within_length(getattr(cell, "number_format", None))
    except IndexError:
        return None


def _is_serial_zero_shown_as_date(value: object, number_format: str | None) -> bool:
    """Whether this is serial 0 under a format that shows a date (openpyxl gives 0 as midnight `time(0)`).

    Excel shows this cell as '1900-01-00' (a nonexistent day). Emitting '00:00:00' would read as a time value and change the
    meaning. The original number 0 is shown — these are mostly date cells of empty forms and formula cells that returned 0. A 0
    under a time-only format is midnight and stays as is. XLS (`_xls_value`) is the same. Known accepted gap: 0 in the 1904 date
    system (1904-01-01) is also shown as 0 (Pinned by: test_spreadsheet.py::TestDateCellsOutsideTheDateRange).
    """
    return value == time(0) and isinstance(value, time) and bool(number_format) and _shows_date(number_format)


def _format_id(cell: object) -> int | None:
    """The number-format id of the cell style. It is in `_style` for fully loaded cells and in `style_array` for streamed cells."""
    try:
        style = cell._style if hasattr(cell, "_style") else cell.style_array
        return style.numFmtId
    except (AttributeError, IndexError, KeyError):
        return None


def _serial_date_text(value: float, epoch: datetime) -> str:
    """Excel serial → a time below 1, a date for an integer, otherwise date and time. A negative stays the original number (it would
    be a nonexistent day, `_negative_serial_text`)."""
    if value < 0:
        return _format_value(value)
    try:
        converted = from_excel(value, epoch)
    except (OverflowError, ValueError):
        return _format_value(value)
    if isinstance(converted, datetime) and float(value).is_integer():
        return converted.date().isoformat()
    return _format_value(converted)


def _plain_value(value: object) -> object:
    """When formulas are read as written, an array formula arrives as an object. Turn it into the formula text. Rich text (text
    whose font changes within the cell) is joined into one text with only the superscript/subscript pieces marked — joined as plain
    text, m² became m2
    (Pinned by: test_spreadsheet.py::TestSuperscriptInCells)."""
    if isinstance(value, CellRichText):
        return resolve_scripts("".join(_rich_piece(piece) for piece in value))
    if isinstance(value, str):
        return _unescape_xstring(value)
    return value.text if isinstance(value, ArrayFormula) else value


_VERTICAL_ALIGN_SCRIPTS = {"superscript": SUPERSCRIPT, "subscript": SUBSCRIPT}


def _rich_piece(piece: object) -> str:
    if not isinstance(piece, TextBlock):
        return _unescape_xstring(str(piece))
    mark = _VERTICAL_ALIGN_SCRIPTS.get(getattr(piece.font, "vertAlign", None))
    text = _unescape_xstring(piece.text)
    return mark_script(text, mark) if mark else text


# OOXML strings (ST_Xstring) write characters that cannot appear in XML as `_xHHHH_`. Excel only writes control characters
# (0000–001F) and `_x005F_`, the underscore put in front of a literal `_xHHHH_`.
_XSTRING_ESCAPE = re.compile(r"_x(005[Ff]|00[01][0-9A-Fa-f])_")


def _unescape_xstring(text: str) -> str:
    """Turn the `_xHHHH_` notation in cell text back into characters (shared strings, inlineStr and formula result text alike).
    openpyxl does not decode it when reading.

    Excel writes the CR of an in-cell line break (CR LF) as `_x000D_`, giving `1차 출고_x000D_<br>2차 출고`. The CR is merged into a
    single line break. A vertical tab (`_x000B_`, a line break pasted from another program) is a line break too — removed as a
    control character, it glued words together as in `제시1)PACS`
    (same rule as PPTX. Pinned by: test_spreadsheet.py::test_vertical_tab_escape_in_a_cell_becomes_a_line_break).
    Notation that is not a control character (`_x0041_`) is not written by Excel, so it is left literally.
    Known accepted gap: openpyxl removes `_x005F_` beforehand in shared strings and rich text (`Text.content`). On that path a
    literal `_x000D_` (`_x005F_x000D_` in the file) cannot be told from a real CR, so it is read as a line break. Only inlineStr
    cells read by streaming still have `_x005F_`, so they are restored literally
    (Pinned by: test_spreadsheet.py::TestEscapedCharactersInStrings).
    """
    if "_x" not in text:
        return text
    unescaped = _XSTRING_ESCAPE.sub(lambda match: chr(int(match.group(1), 16)), text)
    return unescaped.replace("\r\n", "\n").replace("\r", "\n").replace("\v", "\n")


_DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
_DRAWING_MAIN = "http://schemas.openxmlformats.org/drawingml/2006/main"
_ANCHORS = frozenset(f"{{{_DRAWING}}}{name}" for name in ("twoCellAnchor", "oneCellAnchor", "absoluteAnchor"))
_TEXT_BODY = f"{{{_DRAWING}}}txBody"
_ALTERNATE_CONTENT = "{http://schemas.openxmlformats.org/markup-compatibility/2006}AlternateContent"
_CHART_RENDERERS = {"{http://schemas.openxmlformats.org/drawingml/2006/chart}chart": render_chart,
                    "{http://schemas.microsoft.com/office/drawing/2014/chartex}chart": render_chartex}
# Prefix `_drawing_items` adds when it puts a chart kind and relationship id in the place of a picture relationship id
_CHART_REFERENCE = "\0chart:"
# SmartArt frame of a drawing part (dgm:relIds inside a graphicFrame). The data model part is the r:dm relationship. Passed on in the
# same slot as charts (`_chart_text`).
_DIAGRAM_RELATIONS = "{http://schemas.openxmlformats.org/drawingml/2006/diagram}relIds"
_DIAGRAM_DATA = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}dm"


def _drawing_blocks(archive: zipfile.ZipFile, names: set[str], sheet_path: str, pictures: dict[str, bytes],
                    extraction: Extraction, options: ConvertOptions, row_at: _SheetRows | None = None) -> list[str]:
    """Turn the pictures (chart images, screenshots), text boxes, shape text, charts and SmartArt of the sheet's drawing part, and
    the form controls of the VML drawing (`_form_controls`), into blocks ordered by where they are placed (top→bottom, left→right).
    They are not put inside the cell grid but after the table.

    Deliberate: even on a full load the openpyxl picture list (`sheet._images`) is not used; as when streaming, they are read
    directly from the ZIP (sheet → drawing → picture file relationships). openpyxl does not read text boxes and discards EMF/WMF
    pictures, so notes in a text box next to a table were dropped without a warning and vector pictures disappeared, on a full load
    only, with neither a placeholder nor a warning (Pinned by: test_spreadsheet.py::TestSheetDrawings,
    test_image.py::test_xlsx_pictures_are_extracted_even_when_streamed). The same picture file is kept in `pictures` and
    decompressed only once.
    Text box text counts toward the character limit, and processing stops there when it is exceeded. Cell comments
    (`_xlsx_comments`) are handled separately, and hyperlink addresses are not read.

    In a sheet with two or more pictures, each picture is preceded by the cell it is placed on and the text of that row
    (`(cell C2: 제품A)`) — the pictures of a list with a photo per row followed the table as nothing but `[Image]`, so it was
    impossible to tell which row a picture belonged to. A single picture (logo, stamp) gets none because its position carries no
    meaning, and when the same picture file is placed as many times as the repeated-image threshold
    (`repeated_image_threshold`) it gets none because it is removed as a repeated image (only the cell address remains). Known
    accepted gap: the cell mark in front of a picture removed for being small or decorative remains
    (Pinned by: test_spreadsheet_notes_pictures_and_times.py::TestFloatingPictureCells).
    """
    # (position, text, picture/chart relationship id, its relationships)
    items: list[tuple[tuple[int, int] | None, str, str, dict[str, str]]] = []
    for drawing in _related(archive, names, sheet_path, kind="/drawing").values():
        if drawing not in names:
            continue
        try:
            root = etree.fromstring(archive.read(drawing), parser=_XML_PARSER)
        except etree.XMLSyntaxError:
            continue  # openpyxl cannot read it either
        media = _part_relations(archive, names, drawing)
        items.extend((position, text, embed, media) for position, text, embed in _drawing_items(root, None))
    items.extend((position, text, "", {}) for position, text in _form_controls(archive, names, sheet_path))

    # Picture path → number of times placed on the sheet. Recounting the list for every picture took time quadratic in the number
    # of anchors (200,000 anchors: 40 seconds; Pinned by:
    # test_spreadsheet_notes_pictures_and_times.py::TestFloatingPictureCells::test_many_anchors_take_linear_time)
    placed = Counter(media[embed] for position, text, embed, media in items
                     if not text and not embed.startswith(_CHART_REFERENCE) and media.get(embed) in names)
    blocks: list[str] = []
    for position, text, embed, media in sorted(items, key=lambda item: item[0] or (0, 0)):
        target = embed
        if embed.startswith(_CHART_REFERENCE):
            kind, _, target = embed.removeprefix(_CHART_REFERENCE).partition("\0")
            path = media.get(target, "")
            text = _once_per_part(("chart", kind, path), lambda kind=kind, path=path: _chart_text(archive, names, kind, path))

        # Pictures and charts whose target part is missing are left out and reported. openpyxl does not follow drawing
        # relationships while reading (`_RELATIONS_LEFT_UNREAD`, Pinned by: test_spreadsheet.py::TestMissingPartRelationships)
        if target and not text and media.get(target) not in names and _DETACHED_PARTS_WARNING not in extraction.warnings:
            extraction.warnings.append(_DETACHED_PARTS_WARNING)

        if text:
            blocks.append(text)
            if extraction.count(solid_length(text), options):
                return blocks
        elif options.extract_images and media.get(embed) in names:
            path = media[embed]
            if path not in pictures:
                pictures[path] = archive.read(path)
            marker = extraction.add_image(pictures[path])
            labeled = (row_at is not None and position is not None and placed.total() > 1
                       and placed[path] < options.repeated_image_threshold)
            blocks.append(f"({row_at.place(position[0] + 1, position[1] + 1)}) {marker}" if labeled else marker)
    return blocks


_THREADED_COMMENTS = "http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments"


def _xlsx_people(archive: zipfile.ZipFile, names: set[str]) -> dict[str, str]:
    """Threaded comment author id → name. Found in the workbook's person list (persons). Read once per workbook — reading it per
    sheet re-parsed the workbook relationships file, which has as many entries as there are sheets, and was quadratic in the number
    of sheets (3,200 sheets: 8.9 seconds; Pinned by:
    test_office_round43.py::TestManySheets::test_workbook_relationships_are_not_read_again_for_every_sheet)."""
    people = {}
    for path in _related(archive, names, _workbook_path(archive, names), kind="/person").values():
        root = parse_part(archive.read(path)) if path in names else None
        for person in root.iter(f"{{{_THREADED_COMMENTS}}}person") if root is not None else ():
            people[person.get("id", "")] = person.get("displayName", "")
    return people


def _xlsx_comments(archive: zipfile.ZipFile, names: set[str], sheet_path: str,
                   people: dict[str, str]) -> tuple[list[_CellComment], bool]:
    """Cell comments of a sheet. Excel 365 threaded comments (threadedComments + the workbook's persons, `_xlsx_people`) are read
    together with their replies, and the legacy-format copy on the same cell (author `tc={id}`, body a '[Threaded comment] …'
    notice) is left out. Legacy comments (comments.xml) are an author list + cell addresses.
    A corrupted part only has its own comments skipped and is reported through the second value (whether there was a corrupted
    part) — comments used to be dropped without a warning (Pinned by: test_office_round44.py::TestBrokenCommentPart)."""
    found: list[_CellComment] = []
    threaded_cells: set[tuple[int, int]] = set()
    damaged = False

    # Threaded comments: one with a parent is a reply to that comment
    for path in _related(archive, names, sheet_path, kind="/threadedComment").values():
        root = parse_part(archive.read(path)) if path in names else None
        damaged = damaged or (path in names and root is None)
        threads: dict[str, _CellComment] = {}
        for comment in root.iter(f"{{{_THREADED_COMMENTS}}}threadedComment") if root is not None else ():
            cell = _cell_address(comment.get("ref", ""))
            text = comment.findtext(f"{{{_THREADED_COMMENTS}}}text") or ""
            author = people.get(comment.get("personId", ""), "")
            parent = threads.get(comment.get("parentId") or "")
            if parent is not None:
                parent[4].append((author, text))
            elif cell is not None:
                threads[comment.get("id", "")] = (*cell, author, text, [])
                threaded_cells.add(cell)
        found.extend(threads.values())

    # Legacy comments. Copies on cells that have a threaded comment are left out
    for path in _related(archive, names, sheet_path, kind="/comments").values():
        root = parse_part(archive.read(path)) if path in names else None
        if root is None:
            damaged = damaged or path in names
            continue
        authors = [" ".join("".join(author.itertext()).split()) for author in root.iter(f"{{{_SPREADSHEET_MAIN}}}author")]
        for comment in root.iter(f"{{{_SPREADSHEET_MAIN}}}comment"):
            cell = _cell_address(comment.get("ref", ""))
            author_id = decimal_int(comment.get("authorId", ""), -1)
            author = authors[author_id] if 0 <= author_id < len(authors) else ""
            if cell is None or cell in threaded_cells or author.startswith("tc="):
                continue
            body = comment.find(f"{{{_SPREADSHEET_MAIN}}}text")
            text = "".join(node.text or "" for node in body.iter(f"{{{_SPREADSHEET_MAIN}}}t")) if body is not None else ""
            found.append((*cell, author, text, []))

    return found, damaged


def _cell_address(reference: str) -> tuple[int, int] | None:
    """`E5` → (5, 5). A range (`E5:F6`) gives its top-left cell. None if it is not an address."""
    try:
        row, column = coordinate_to_tuple(reference.split(":")[0].replace("$", "").strip().upper())
    except (ValueError, TypeError, IndexError):
        return None
    return row, column


# Form controls placed in the sheet's VML drawing (legacyDrawing) that have a selected state (check boxes, option buttons). Cell
# comments (Note) are handled separately through comments.xml (`_xlsx_comments`); buttons and lists are not read.
_VML = "urn:schemas-microsoft-com:vml"
_VML_OFFICE = "urn:schemas-microsoft-com:office:office"
_VML_EXCEL = "urn:schemas-microsoft-com:office:excel"
_FORM_CONTROL_TYPES = frozenset(("checkbox", "radio"))


# Excel's VML sometimes uses unclosed tags (<br>) as HTML does. Broken parts are skipped while reading.
_VML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, recover=True)
# Unclosed void HTML tags. For every unmatched closing tag the parser pushed elements one level, so x:ClientData ended up inside
# v:textbox and the check box was lost.
# `<` is not allowed in the attribute part — each `<br` continuing without a `>` was scanned to the end, and a 6KB document took a
# minute
_VML_VOID_TAG = re.compile(rb"<((?:[\w.-]+:)?(?:br|hr|img|wbr))\b([^<>]*?)(?<!/)>", re.IGNORECASE)
# The inside of a label text box (v:textbox) is an HTML fragment (`<br></br>`, unclosed `<p>`, `&nbsp;`), so reading it as XML shifts
# the shape structure. The tags are stripped and only the text is kept
_VML_TEXT_BOX_OPEN = re.compile(rb"<(?:[\w.-]+:)?textbox\b[^<>]*>", re.IGNORECASE)
_VML_TEXT_BOX_CLOSE = re.compile(rb"</(?:[\w.-]+:)?textbox\s*>", re.IGNORECASE)
_HTML_TAG = re.compile(rb"<[^<>]*>")
_HTML_NAMED_ENTITY = re.compile(rb"&([A-Za-z][A-Za-z0-9]{0,31});")
_XML_ENTITY_NAMES = frozenset((b"amp", b"lt", b"gt", b"quot", b"apos"))
_VML_SHAPE_NUMBER = re.compile(r"_s(\d+)$")
_CONTROL_ELEMENT = re.compile(rb"<(?:[\w.-]+:)?control\b[^<>]*>")
_CONTROL_SHAPE_ID = re.compile(rb"\sshapeId\s*=\s*[\"']\s*(\d+)")
_CONTROL_RELATIONSHIP = re.compile(rb"\s[\w.-]+:id\s*=\s*[\"']([^\"']*)")


def _form_controls(archive: zipfile.ZipFile, names: set[str], sheet_path: str) -> list[tuple[tuple[int, int], str]]:
    """Form control check boxes and option buttons → (row/column where placed, '☒ label' / '☐ label').

    The label and the state are in the sheet's VML drawing (x:ClientData ObjectType="Checkbox"/"Radio", state x:Checked). Excel 2010
    and later also write the same state in the control properties part (checked="Checked" in ctrlProps/ctrlProp*.xml) and link it
    to the VML shape through the sheet's `<control shapeId=…>`. If either of the two says selected, it is ☒. Both the label and the
    state were dropped, so it was impossible to tell which of '동의함' (agree) / '동의하지 않음' (do not agree) had been chosen on a
    consent form (Pinned by: test_spreadsheet_cell_values_and_controls.py::TestFormControlCheckBoxes).
    """
    controls: list[tuple[tuple[int, int], str]] = []
    checked_shapes = _checked_control_shapes(archive, names, sheet_path)
    for vml in _related(archive, names, sheet_path, kind="/vmlDrawing").values():
        root = _vml_root(archive.read(vml)) if vml in names else None
        for shape in root.iter(f"{{{_VML}}}shape") if root is not None else ():
            data = shape.find(f"{{{_VML_EXCEL}}}ClientData")
            if data is None or (data.get("ObjectType") or "").lower() not in _FORM_CONTROL_TYPES:
                continue
            box = shape.find(f"{{{_VML}}}textbox")
            label = " ".join("".join(box.itertext()).split()) if box is not None else ""
            number = next((match.group(1) for name in (f"{{{_VML_OFFICE}}}spid", "id")
                           if (match := _VML_SHAPE_NUMBER.search(shape.get(name) or ""))), None)
            checked = (data.findtext(f"{{{_VML_EXCEL}}}Checked") or "").strip() == "1" or number in checked_shapes
            anchor = [part.strip() for part in (data.findtext(f"{{{_VML_EXCEL}}}Anchor") or "").split(",")]
            position = (decimal_int(anchor[2], 0), decimal_int(anchor[0], 0)) if len(anchor) > 2 else (0, 0)
            controls.append((position, f"{_CHECKED if checked else _UNCHECKED} {label}".strip()))
    return controls


def _vml_root(xml: bytes) -> etree._Element | None:
    """Root of the VML drawing part. Unclosed `<br>` and the like are closed before reading. None if it cannot be read (empty part,
    garbage bytes) — check boxes are an extra, so the document is not rejected because of them
    (Pinned by: test_spreadsheet_damaged_parts.py::TestDamagedFormControlDrawing)."""
    try:
        return etree.fromstring(_VML_VOID_TAG.sub(rb"<\1\2/>", _plain_text_boxes(xml)), parser=_VML_PARSER)
    except etree.XMLSyntaxError:
        return None


def _plain_text_boxes(xml: bytes) -> bytes:
    """Strip the HTML tags inside VML label text boxes and turn HTML named character references (`&nbsp;`) into numeric references.
    If a closing text box tag is missing, everything after that point is left as is
    (Pinned by: test_spreadsheet_damaged_parts.py::TestDamagedFormControlDrawing::test_html_shaped_label_keeps_both_check_boxes)."""
    parts, position = [], 0

    for opening in _VML_TEXT_BOX_OPEN.finditer(xml):
        if opening.start() < position:
            continue
        closing = _VML_TEXT_BOX_CLOSE.search(xml, opening.end())
        if closing is None:
            break
        inner = _HTML_TAG.sub(b"", xml[opening.end():closing.start()])
        parts += [xml[position:opening.end()], _HTML_NAMED_ENTITY.sub(_numeric_entity, inner)]
        position = closing.start()
    parts.append(xml[position:])
    return b"".join(parts)


def _numeric_entity(match: re.Match[bytes]) -> bytes:
    name = match.group(1)
    if name in _XML_ENTITY_NAMES:
        return match.group()
    text = html.unescape(match.group().decode("ascii"))
    return match.group() if text.startswith("&") else "".join(f"&#{ord(character)};" for character in text).encode()


def _checked_control_shapes(archive: zipfile.ZipFile, names: set[str], sheet_path: str) -> set[str]:
    """VML shape numbers (shapeId) of the controls whose control properties part says selected (checked="Checked"). The sheet XML
    is scanned only for sheets that have control properties relationships (`<control>` sits near the end of the sheet, but the
    sheet is not parsed as XML again; only the elements are searched for)."""
    properties = _related(archive, names, sheet_path, kind="/ctrlProp")
    if not properties or sheet_path not in names:
        return set()

    def says_checked(path: str) -> bool:
        root = parse_part(archive.read(path))
        return root is not None and root.get("checked") == "Checked"

    checked = set()
    for element in _CONTROL_ELEMENT.finditer(archive.read(sheet_path)):
        shape, relationship = _CONTROL_SHAPE_ID.search(element.group()), _CONTROL_RELATIONSHIP.search(element.group())
        path = properties.get(relationship.group(1).decode("utf-8", errors="replace")) if relationship else None
        if shape and path in names and _once_per_part(("checked", path), lambda path=path: says_checked(path)):
            checked.add(shape.group(1).decode())
    return checked


def _once_per_part[T](key: tuple[str, ...], read: Callable[[], T]) -> T:
    """What `read` works out from a part, worked out once per workbook (`key`: what is read, and the part path). A part was read
    and parsed again for every reference to it: 1,000 `<control>` elements pointing at one 2 MB control properties part (a 7 KB
    file) took 2.2 s, and so did a chart placed many times. The results live in the reading state of this conversion
    (`_CellReading`); outside one, nothing is kept (Pinned by: test_audit_spreadsheet.py::TestPartsReferencedManyTimes)."""
    reading = _READING.get()
    if reading is None:
        return read()
    if key not in reading.part_results:
        reading.part_results[key] = read()
    return reading.part_results[key]


def _drawing_items(element: etree._Element,
                   position: tuple[int, int] | None) -> Iterator[tuple[tuple[int, int] | None, str, str]]:
    """Turn the text box/shape text and picture references of drawing XML into (row/column where placed, text, picture relationship
    id). Group shapes are descended into as well.

    For `mc:AlternateContent`, mc:Choice is read first and mc:Fallback only when that yields nothing (they are alternative
    representations of the same shape, so reading both would emit it twice — same rule as PPTX).
    """
    for child in element:
        if child.tag in _ANCHORS:
            yield from _drawing_items(child, _anchor_position(child))
        elif child.tag == _ALTERNATE_CONTENT:
            for branch in child:
                items = list(_drawing_items(branch, position))
                if items:
                    yield from items
                    break
        elif child.tag == _TEXT_BODY:
            text = _text_body(child)
            if text:
                yield position, text, ""
        elif child.tag == _BLIP:
            yield position, "", child.get(_EMBED, "")
        elif child.tag in _CHART_RENDERERS:
            yield position, "", f"{_CHART_REFERENCE}{child.tag}\0{child.get(_RELATIONSHIP_ID, '')}"
        elif child.tag == _DIAGRAM_RELATIONS:
            yield position, "", f"{_CHART_REFERENCE}{child.tag}\0{child.get(_DIAGRAM_DATA, '')}"
        elif isinstance(child.tag, str):
            yield from _drawing_items(child, position)


def _chart_text(archive: zipfile.ZipFile, names: set[str], kind: str, path: str) -> str:
    """A chart placed on a sheet → "[Chart] title" + a table of the stored values (the same `_charts.render_chart` as PPTX/DOCX).
    For a chart that stores no values (one made by openpyxl), the header-only table is dropped and only the title is kept.

    Charts disappeared without a trace, so a dashboard sheet consisting only of charts was left with just the sheet title. The
    source table of a chart is often on a hidden sheet, so the values stored in the chart are carried over
    (Pinned by: test_spreadsheet.py::TestSheetCharts). Text boxes inside a chart (c:userShapes — '(단위: 억원)') are found through
    the chart part's relationships (same as DOCX/PPTX/HWPX. Pinned by: test_core_spreadsheet.py::TestSheetChartUserShapes).
    """
    root = parse_part(archive.read(path)) if path in names else None
    if root is None:
        return ""
    if kind == _DIAGRAM_RELATIONS:
        # SmartArt placed on a sheet (organisation chart, process diagram) becomes the same nested list as in PPTX/DOCX (Pinned by:
        # test_spreadsheet_cell_values_and_controls.py::TestSmartArtOnASheet)
        return render_smartart(root)
    if _CHART_RENDERERS[kind] is render_chart:
        rendered = render_chart(root, load_related=package_loader(lambda name: archive.read(name) if name in names else None,
                                                                  path))
    else:
        rendered = _CHART_RENDERERS[kind](root)
    parts = rendered.split("\n\n")
    return "\n\n".join(part for part in parts if not (part.startswith("|") and part.count("\n") < 2))


def _anchor_position(anchor: etree._Element) -> tuple[int, int] | None:
    """Top-left cell of a drawing object (row, column, 0-based). An object not tied to a cell (absoluteAnchor) gives None — it is
    treated as being at the very top."""
    try:
        return int(anchor.findtext(f"{{{_DRAWING}}}from/{{{_DRAWING}}}row")), int(anchor.findtext(f"{{{_DRAWING}}}from/{{{_DRAWING}}}col"))
    except (TypeError, ValueError):
        return None


def _text_body(body: etree._Element) -> str:
    """Text of a text box or shape. One line per paragraph; a line break inside a paragraph (a:br) is a line break too."""
    lines = []
    for paragraph in body.iter(f"{{{_DRAWING_MAIN}}}p"):
        pieces = [(node.text or "") if node.tag == f"{{{_DRAWING_MAIN}}}t" else "\n"
                  for node in paragraph.iter(f"{{{_DRAWING_MAIN}}}t", f"{{{_DRAWING_MAIN}}}br")]
        lines.append("".join(pieces).strip())
    return "\n".join(line for line in lines if line)


_RICH_DATA = "http://schemas.microsoft.com/office/spreadsheetml/2017/richdata"
_RICH_VALUE_RELS = "http://schemas.microsoft.com/office/spreadsheetml/2022/richvaluerel"
_RICH_PICTURE_KEY = "_rvRel:LocalImageIdentifier"


def _rich_values(archive: zipfile.ZipFile, names: set[str]) -> dict[str, object]:
    """A cell's vm (value metadata index, 1-based) → the picture of a 'Place in Cell' picture cell (`_CellPicture`) or the displayed
    text of a data type cell (_DisplayString).

    Excel 365 writes these cells in the sheet XML as `t="e" vm="N"` + `#VALUE!` and keeps the actual value in parts linked through
    the workbook's relationships: value metadata (metadata.xml: vm → rvb index of XLRICHVALUE) → rich value (rv in
    rdrichvalue.xml, key order of the structure in rdrichvaluestructure.xml) → picture number → relationship in richValueRel.xml →
    picture file. If a part is missing or corrupted the result is an empty dict (the cell is blank + a warning,
    `_tolerant_parse_cell`).
    Known accepted gap: web pictures (_webimage) and other structures are not read
    (Pinned by: test_spreadsheet_cell_values_and_controls.py::TestPicturesAndDataTypesPlacedInCells).
    """
    workbook_path = _workbook_path(archive, names)
    parts = {}
    for kind in ("/sheetMetadata", "/rdRichValue", "/rdRichValueStructure", "/richValueRel"):
        targets = [path for path in _related(archive, names, workbook_path, kind=kind).values() if path in names]
        parts[kind] = targets[0] if targets else None
    if parts["/sheetMetadata"] is None or parts["/rdRichValue"] is None:
        return {}

    try:
        metadata = etree.fromstring(archive.read(parts["/sheetMetadata"]), parser=_XML_PARSER)
        values = etree.fromstring(archive.read(parts["/rdRichValue"]), parser=_XML_PARSER)
        structures = (etree.fromstring(archive.read(parts["/rdRichValueStructure"]), parser=_XML_PARSER)
                      if parts["/rdRichValueStructure"] else None)
        relations = (etree.fromstring(archive.read(parts["/richValueRel"]), parser=_XML_PARSER)
                     if parts["/richValueRel"] else None)
    except etree.XMLSyntaxError:
        return {}

    # For each rich value, a picture (picture number → relationship → file) or the displayed text. The structure (list of key names)
    # and the values (v) are paired by order
    keys = [[key.get("n", "") for key in structure.iterchildren(f"{{{_RICH_DATA}}}k")]
            for structure in (structures.iterchildren(f"{{{_RICH_DATA}}}s") if structures is not None else ())]
    # Relationships that cannot be read are treated as absent (`_part_relations`): the pictures are left blank with the warning
    # and the data types are kept. They rejected the whole workbook
    # (Pinned by: test_audit_spreadsheet.py::TestDamagedRichValueRelationships).
    targets = _part_relations(archive, names, parts["/richValueRel"]) if relations is not None else {}
    pictures = [targets.get(relation.get(_RELATIONSHIP_ID, ""), "")
                for relation in (relations.iterchildren(f"{{{_RICH_VALUE_RELS}}}rel") if relations is not None else ())]
    rich: list[object] = []
    for value in values.iterchildren(f"{{{_RICH_DATA}}}rv"):
        structure = _list_item(keys, value.get("s")) or []
        fields = dict(zip(structure, (item.text or "" for item in value.iterchildren(f"{{{_RICH_DATA}}}v")), strict=False))
        picture = _list_item(pictures, fields.get(_RICH_PICTURE_KEY))
        rich.append(_CellPicture(picture, fields.get("Text") or None) if picture else fields.get("_DisplayString"))

    # The cell's vm-th value metadata → metadata type (1-based) and the future metadata index of that type → rich value index of rvb
    main = f"{{{_SPREADSHEET_MAIN}}}"
    types = [None, *(kind.get("name") for kind in metadata.iterfind(f"{main}metadataTypes/{main}metadataType"))]
    blocks = {future.get("name"): [next((item.get("i") for item in block.iter(f"{{{_RICH_DATA}}}rvb")), None)
                                   for block in future.iterchildren(f"{main}bk")]
              for future in metadata.iterchildren(f"{main}futureMetadata")}
    found: dict[str, object] = {}
    for number, block in enumerate(metadata.iterfind(f"{main}valueMetadata/{main}bk"), start=1):
        record = block.find(f"{main}rc")
        if record is None:
            continue
        index = _list_item(blocks.get(_list_item(types, record.get("t")), []), record.get("v"))
        value = _list_item(rich, index)
        if value is not None:
            found[str(number)] = value
    return found


def _list_item[T](items: list[T], index: str | None) -> T | None:
    """`items[int(index)]`. None if the number is not decimal (including a very long number) or is out of range."""
    number = decimal_int(index.strip() if index is not None else None, None)
    return items[number] if number is not None and number < len(items) else None


def _cell_picture_text(picture: _CellPicture, archive: zipfile.ZipFile, names: set[str], pictures: dict[str, bytes],
                       extraction: Extraction, options: ConvertOptions) -> str:
    """A picture in a cell → the placeholder to put in that cell. Blank if pictures are not extracted by the options or the picture
    file is missing. The same picture file is decompressed only once."""
    if not options.extract_images or picture.path not in names:
        return ""
    if picture.path not in pictures:
        pictures[picture.path] = archive.read(picture.path)
    return extraction.add_image(pictures[picture.path], alt=picture.alt)


def _picture_reference_bytes(archive: zipfile.ZipFile) -> int:
    """The amount an openpyxl full load actually decompresses because of pictures: for every picture reference (`r:embed`) in the
    drawing XML the sheets point to through relationships, add the uncompressed size of that picture file.

    Counted the way openpyxl does it, by following the sheet relationships and parsing the XML. Counting with regular expressions
    or fixed paths could be evaded with single quotes, whitespace, character references or drawing files at other paths.
    """
    names = set(archive.namelist())
    sizes = {info.filename: info.file_size for info in archive.infolist()}
    sheets = {name for name in names if name.startswith("xl/worksheets/") and name.endswith(".xml")}
    sheets |= {path for path in _related(archive, names, _workbook_path(archive, names)).values() if path.endswith(".xml")}

    total = 0
    for sheet in sheets & names:
        # Parts the workbook points to that are not sheets are looked at too, and openpyxl never reads their relationships: ones
        # that cannot be read count nothing (Pinned by: test_audit_spreadsheet.py::TestDamagedRichValueRelationships)
        for part in _part_relations(archive, names, sheet).values():
            if part == sheet or part not in names or not part.endswith(".xml"):
                continue
            media = _part_relations(archive, names, part)
            try:
                root = etree.fromstring(archive.read(part), parser=_XML_PARSER)
            except etree.XMLSyntaxError:
                continue  # openpyxl cannot read it either
            total += sum(sizes.get(media.get(element.get(_EMBED), ""), 0) for element in root.iter()
                         if element.get(_EMBED) is not None)
    return total


def _merged_cell_count(archive: zipfile.ZipFile, sheet_names: list[str]) -> int:
    """Total number of cells covered by the merge and hyperlink ranges of the sheets (stops once the cap is exceeded)."""
    total = 0
    for name in sheet_names:
        xml = archive.read(name)
        if b"mergeCell" not in xml and b"hyperlink" not in xml:
            continue
        for match in _MERGE_REF.finditer(xml):
            reference = html.unescape((match.group(1) or match.group(2) or b"").decode("utf-8", errors="replace"))
            if not reference.strip():
                # `ref=""` — an empty group is None, so decode blew up (Pinned by: test_spreadsheet.py::TestEmptyRangeReferences)
                continue
            try:
                bounds = range_boundaries(reference.strip())
            except (ValueError, TypeError):
                continue
            if None in bounds:
                # A column name without rows (`ref="abc"`) gave None and the subtraction blew up
                # (Pinned by: test_core_spreadsheet.py::TestMergeReferenceWithoutRows)
                continue
            min_col, min_row, max_col, max_row = bounds
            if min_col > max_col or min_row > max_row:
                # A reversed range was counted as negative and cancelled out a large merge (`_readable_merges` discards it.
                # Pinned by: test_office_round45.py::TestMalformedMergeRangeAndTheMergeLimit)
                continue
            total += (max_col - min_col + 1) * (max_row - min_row + 1)
            if total > _MAX_MERGED_CELLS:
                return total
    return total


def _pivot_cache_bytes(archive: zipfile.ZipFile) -> int:
    """Total uncompressed size of the pivot cache definitions the workbook points to through relationships and of the parts
    (records) those definitions point to.

    Counted by following relationships as openpyxl does. Counting by path name (`xl/pivotCache/`) would miss caches kept at other
    paths.
    """
    names = set(archive.namelist())
    sizes = {info.filename: info.file_size for info in archive.infolist()}
    total = 0
    for definition in _related(archive, names, _workbook_path(archive, names), kind=_PIVOT_CACHE_DEFINITION).values():
        total += sizes.get(definition, 0) + sum(sizes.get(part, 0) for part in _related(archive, names, definition).values())
    return total


def _related(archive: zipfile.ZipFile, names: set[str], source: str, kind: str | None = None) -> dict[str, str]:
    """Relationship id of the source part → target part path. With `kind`, only relationships whose Type ends with it."""
    folder, name = posixpath.split(source)
    rels_path = f"{folder}/_rels/{name}.rels"
    if rels_path not in names:
        return {}

    root = etree.fromstring(archive.read(rels_path), parser=_XML_PARSER)
    targets = {}
    for relationship in root.iter(_RELATIONSHIPS):
        if kind is not None and not relationship.get("Type", "").endswith(kind):
            continue
        target = relationship.get("Target", "")
        path = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(folder, target))
        targets[relationship.get("Id", "")] = path
    return targets


def _part_relations(archive: zipfile.ZipFile, names: set[str], part: str) -> dict[str, str]:
    """Relationships of a part only the converter reads, such as a drawing — if the relationships file is corrupted they are treated
    as absent. Only that part's pictures and charts are lost (`_drawing_blocks` warns; text boxes remain) and the whole workbook is
    not rejected. Pinned by: test_spreadsheet_damaged_parts.py::TestDamagedDrawingRelationships"""
    try:
        return _related(archive, names, part)
    except etree.XMLSyntaxError:
        return {}
