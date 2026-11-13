"""XLSX, XLS, XML Spreadsheet and CSV → one Markdown table per sheet."""

from bisect import bisect_left, bisect_right
from collections import Counter, deque
from collections.abc import Callable, Iterator
from contextvars import ContextVar
import copy
import csv
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
import hashlib
import html
from io import BytesIO, StringIO
import math
import posixpath
import re
import struct
import zipfile
from zipfile import BadZipFile
import zlib

from lxml import etree
import olefile
import openpyxl
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.cell.text import Text
from openpyxl.chartsheet import Chartsheet
from openpyxl.formula.translate import Translator
from openpyxl.packaging.relationship import RelationshipList
from openpyxl.reader import excel as _openpyxl_excel_reader
from openpyxl.styles.cell_style import CellStyleList
from openpyxl.styles.numbers import BUILTIN_FORMATS, is_date_format
from openpyxl.utils.cell import coordinate_to_tuple, get_column_letter, range_boundaries
from openpyxl.utils.datetime import from_excel, from_ISO8601
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.worksheet import _reader as _openpyxl_sheet_reader
from openpyxl.worksheet.formula import ArrayFormula
from openpyxl.worksheet.merge import MergeCell, MergeCells
from openpyxl.xml.constants import SHEET_MAIN_NS
from openpyxl.xml.functions import iterparse as _openpyxl_iterparse
import xlrd

from opendockit.exceptions import CorruptedDocumentError, EncryptedDocumentError, UnsupportedFormatError
from opendockit.extractors._shared import (
    SUBSCRIPT,
    SUPERSCRIPT,
    cell_level_mark,
    comment_line,
    decimal_int,
    mark_script,
    package_loader,
    parse_part,
    render_chart,
    render_chartex,
    render_smartart,
    resolve_scripts,
)
from opendockit.extractors.text import decode_text, decoding_warning
from opendockit.markdown import join_blocks, render_table, solid_length
from opendockit.models import ConvertOptions, Extraction, Section, SectionKind, quiet_warnings


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
_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True)
# Upper bound on the number of cells covered by merge and hyperlink ranges in a full load (same as the merged-cell total limit of
# markdown.TableGrid). openpyxl creates a cell object for every covered position of both kinds of range: a 4.8KB file with a single
# merge range (A1:CV20000) took 8.8s and 800MB, and a single hyperlink range A1:XFD1048576 used 5GB in 20s. Above the limit the
# workbook is streamed, which reads neither.
_MAX_MERGED_CELLS = 500_000
# Cells spanned by a horizontal merge: row (`id(row)`) → [(column after the first cell, end column (exclusive), value)].
# Filled in only on data rows, after the table is split (`_fill_across`).
_Across = dict[int, list[tuple[int, int, str]]]
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
# Sheets where hundreds of thousands of empty rows holding only formatting follow one another are common. A streamed sheet is taken
# to have ended after this many consecutive empty rows, with a warning. Full load, XLS and CSV read to the end because their row
# count is tied to the file size — a totals row after 5,000 empty rows used to vanish without a warning
# (Pinned by: test_spreadsheet.py::TestEmptyRowGaps).
_EMPTY_ROW_STOP = 5_000
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
# `_shared.core_properties` for DOCX/PPTX. Pinned by: test_office_round45.py::TestDocumentPropertiesTheConverterDoesNotNeed).
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


# Leading whitespace of the first text cell of a row (used as indentation). A full-width space counts as two.
_LEADING_SPACES = re.compile(r"[ \t\xa0\u3000]*")
# A label with spaced-out letters ('합   계', '구 분', '총　계' — "total", "category", "grand total"): each character is separated by
# spaces. Deliberate: labels with wide gaps between words ('  매출  원가') are not considered — the leading spaces of sub-accounts
# would be discarded too.
_SPACED_LABEL = re.compile(r"\S(?:[ \t\xa0\u3000]+\S)+")
# Maximum level of the indent-level mark (`_level_marked`).
_MAX_INDENT_LEVEL = 5
# Indent-level mark (`_shared.cell_level_mark`) — removed from the first row (header row) of a table (`_header_without_level_mark`).
_LEVEL_MARK = re.compile(r"└─* ")


def _level_marked(text: str, raw: str, indent: float) -> str:
    """Add the indent-level mark (`└ `, `└─ `, `_shared.cell_level_mark` — the same mark as list levels inside table cells) to the
    first text cell of a row (`text`, original text `raw`). Level = cell indent (`indent`) + one per two leading spaces (a single
    space is one too; a full-width space is two).

    Sub-accounts in financial statements and budgets are shown by indentation or leading spaces; both were discarded, so '매출원가'
    (cost of sales) and its sub-item '재료비' (materials) read as the same level. Text that looks like a number (an amount stored as
    text) and the second and later cells of a row are left alone — leading spaces in an amount cell are for alignment
    (common to XLSX, XLS and XML, Pinned by: test_spreadsheet.py::TestIndentedLabels).
    The leading spaces of a label with spaced-out letters (`_SPACED_LABEL` — '   합   계', '  구 분') are not indentation either but
    centering done with spaces. Totals rows and headers became sub-items (`└ 합 계`)
    (Pinned by: test_core_spreadsheet.py::TestLeadingSpacesThatAreNotIndentation).
    """
    leading = _LEADING_SPACES.match(raw).group()
    spaces = 0 if _SPACED_LABEL.fullmatch(raw.strip()) else len(leading) + leading.count("\u3000")
    level = int(indent) + (max(spaces // 2, 1) if spaces else 0)
    if level <= 0 or _NUMBER_LIKE.fullmatch(text.strip()):
        return text
    return cell_level_mark(min(level, _MAX_INDENT_LEVEL)) + text.strip()


# Built-in date/time format ids of East Asian locales. openpyxl does not know the format strings of these ids, treats them as General
# and returns the serial number (45365) as is. What an id actually looks like differs per locale (Korean, Chinese, Japanese), so it
# is not imitated; the value decides between date, time, and date and time
# (Pinned by: test_spreadsheet.py::TestEastAsianBuiltinDateFormats).
_EAST_ASIAN_DATE_FORMAT_IDS = frozenset((*range(27, 37), *range(50, 59)))
# Formats that show only the weekday (`aaa` = 목, `aaaa` = 목요일). openpyxl and xlrd do not treat a format without y, m, d, h, s as a
# date and return the serial number as is. The weekday letters are not imitated; the date is shown
# (Pinned by: test_spreadsheet.py::TestDateFormats).
_WEEKDAY_ONLY_FORMAT = re.compile(r"a{3,4}", re.IGNORECASE)
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
        return getattr(cell, "number_format", None)
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


def extract_xls(data: bytes, options: ConvertOptions) -> Extraction:
    # xlrd writes minor inconsistencies of the file ("WARNING *** OLE2 inconsistency …") to logfile (sys.stdout by default). Mixed
    # into the caller's output, it broke the CLI `--json` result. It is redirected to a sink
    # (Pinned by: test_spreadsheet.py::test_xls_reader_does_not_write_to_stdout).
    # ragged_rows: each row is built only up to its own cells. The default pads every row to the widest row (256 columns from one
    # far cell)
    # (Pinned by: test_spreadsheet.py::TestXlsResourceBounds).
    # The OLE container is not left to xlrd; olefile extracts just the workbook stream and passes it on. xlrd's OLE parsing does
    # not check for chain cycles in streams under 4KB (mini streams), so a 5KB file whose mini FAT points to itself looped forever
    # and used several GB. olefile follows a chain only as far as the stream size
    # (Pinned by: test_spreadsheet.py::TestXlsOleContainer).
    stream = _without_default_password(_xls_workbook_stream(data))
    try:
        book = _open_xls_book(stream)
    except xlrd.biffh.XLRDError as exc:
        if "encrypt" in str(exc).lower():
            raise EncryptedDocumentError("This Excel document is password-protected. Remove the password and try again.") from exc
        raise CorruptedDocumentError("The XLS document structure could not be read.") from exc

    extraction = Extraction()
    drawings = _xls_drawings(stream, options)
    if drawings is None:
        extraction.warnings.append("Text box and shape text and pictures in the XLS could not be read, so only cell contents were converted.")
    for name in _xls_chart_sheets(stream, options.include_hidden_sheets):
        extraction.warnings.append(f"Chart sheet '{name}' was not read "
                                   "(XLS charts are not converted to value tables — saving as XLSX converts them).")
    unresolved, placeholder_zeros = _xls_formulas_without_results(stream)
    if unresolved:
        extraction.warnings.append(f"The results of {unresolved:,} formula(s) are not stored in the file, so those cells were read as blank "
                                   "(opening and re-saving the file in Excel converts them to values).")
    if placeholder_zeros:
        count = sum(len(cells) for cells in placeholder_zeros.values())
        extraction.warnings.append(f"The results of {count:,} formula(s) are stored only as 0 (the file was made by a program that does not "
                                   "calculate), so those cells were read as blank "
                                   "(opening and re-saving the file in Excel converts them to values).")
    holding_charts = _xls_sheets_holding_charts(stream)
    for sheet_index in range(book.nsheets):
        sheet = book.sheet_by_index(sheet_index)
        if sheet.visibility != 0 and not options.include_hidden_sheets:
            extraction.warnings.append(f"Hidden sheet '{sheet.name}' was excluded.")
            continue
        if sheet.nrows == 0 and sheet_index in holding_charts:
            extraction.warnings.append(f"Chart sheet '{sheet.name}' was not read "
                                       "(XLS charts are not converted to value tables — saving as XLSX converts them).")

        blank = placeholder_zeros.get(sheet_index, set())  # formula cells whose 0 in the result slot cannot be trusted

        offset = _xls_first_used_column(sheet)  # the column limit counts from the first used column (`_first_used_column`)
        rows = _collect_rows((_xls_row(book, sheet, row_index, blank, options.max_sheet_cols, offset)
                              for row_index in range(sheet.nrows)), options, sheet.name, extraction)
        across = _fill_xls_merges(sheet, rows, extraction.warnings, options.max_sheet_cols, offset)
        drawn = (_xls_drawing_blocks(drawings[sheet_index], sheet.name, extraction, options)
                 if drawings and sheet_index < len(drawings) and not extraction.exceeded else [])
        if not extraction.exceeded:
            notes = [(row + 1, column + 1, note.author or "", note.text or "", [])
                     for (row, column), note in sheet.cell_note_map.items()]
            row_at = _SheetRows(dict(enumerate(rows, start=1)), offset)
            drawn.append(_comment_block(notes, row_at, extraction, options))
        _append_sheet(extraction, sheet.name, rows, drawn, across)
        if extraction.exceeded:
            break

    extraction.metadata["sheet_count"] = str(len(extraction.sections))
    return extraction


# The default password Excel uses when it encrypts without asking the person opening the file for a password (workbook protection,
# read-only recommended, etc.) ([MS-OFFCRYPTO] 2.3.6.4 etc.).
_DEFAULT_XLS_PASSWORD = "VelvetSweatshop".encode("utf-16-le")
_BIFF_FILEPASS = 0x002F
# Records that are not encrypted: BOF, FILEPASS, USREXCL, FILELOCK, INTERFACEHDR, RRDINFO, RRDHEAD. BOUNDSHEET has only its first 4
# bytes (the sheet position) in plaintext ([MS-XLS] 2.2.10).
_UNENCRYPTED_RECORDS = frozenset((0x0809, 0x002F, 0x0194, 0x0195, 0x00E1, 0x0196, 0x0138))
_RC4_BLOCK_BYTES = 1024
# Record type put in place of FILEPASS after decryption. Both xlrd and this module's scanner skip it as an unknown id (the length is
# kept, so sheet positions are unchanged).
_BIFF_IGNORED = 0x0FFF


def _without_default_password(stream: bytes) -> bytes:
    """Decrypt a workbook stream RC4-encrypted with the default password (VelvetSweatshop). If it is not encrypted, or uses another
    password or method, it is left as is (xlrd rejects it as a 'password-protected document').

    Excel also encrypts files that do not ask the opener for a password, saving them with this default password. Excel and
    LibreOffice open them directly, but they were rejected as a 'password-protected Excel document'. Standard RC4 (version 1.1,
    MD5) and RC4 CryptoAPI (versions 2–4.2, SHA-1, 40/128-bit) are decrypted, and the password is confirmed with the verifier.
    Known accepted gap: XOR obfuscation (the old method) is not decrypted and is rejected
    (Pinned by: test_spreadsheet_cell_values_and_controls.py::TestXlsEncryptedWithTheDefaultPassword).
    """
    records = _biff_records(stream)
    begin, filepass = next(records, None), next(records, None)
    if begin is None or filepass is None or begin[1] != _BIFF_BOF or filepass[1] != _BIFF_FILEPASS:
        return stream
    key_of = _default_password_key(filepass[2])
    if key_of is None:
        return stream

    # The keystream is determined by stream position (a new key for every 1,024-byte block). Plaintext records and record headers
    # take up positions too.
    plain = bytearray(stream)
    keystream: tuple[int, bytes] = (-1, b"")  # current block number and its keystream. Positions only move forward
    for position, record_id, body in records:
        if record_id in _UNENCRYPTED_RECORDS:
            continue
        at, end = position + 4 + (4 if record_id == _BIFF_BOUNDSHEET else 0), position + 4 + len(body)
        while at < end:
            block, offset = divmod(at, _RC4_BLOCK_BYTES)
            if keystream[0] != block:
                keystream = (block, _rc4_keystream(key_of(block), _RC4_BLOCK_BYTES))
            size = min(end - at, _RC4_BLOCK_BYTES - offset)
            mixed = int.from_bytes(plain[at:at + size], "little") ^ int.from_bytes(keystream[1][offset:offset + size], "little")
            plain[at:at + size] = mixed.to_bytes(size, "little")
            at += size
    struct.pack_into("<H", plain, filepass[0], _BIFF_IGNORED)
    return bytes(plain)


def _default_password_key(filepass: bytes) -> Callable[[int], bytes] | None:
    """A function block number → RC4 key, if FILEPASS is RC4 and the verifier of the default password matches. Otherwise None."""
    if len(filepass) < 6 or struct.unpack_from("<H", filepass)[0] != 1:
        return None  # 0 = XOR obfuscation
    major, minor = struct.unpack_from("<HH", filepass, 2)
    if major == 1 and minor == 1 and len(filepass) >= 54:
        salt, sealed, check = filepass[6:22], filepass[22:38], filepass[38:54]
        first = hashlib.md5(_DEFAULT_XLS_PASSWORD).digest()[:5]
        second = hashlib.md5((first + salt) * 16).digest()[:5]

        def key_of(block: int) -> bytes:
            return hashlib.md5(second + struct.pack("<I", block)).digest()

        digest = hashlib.md5
    elif major in (2, 3, 4) and minor == 2 and len(filepass) >= 14:
        header_size = struct.unpack_from("<I", filepass, 10)[0]
        verifier_at = 14 + header_size
        if header_size < 20 or len(filepass) < verifier_at + 60:
            return None
        key_bits = struct.unpack_from("<I", filepass, 14 + 16)[0] or 40
        salt, sealed = filepass[verifier_at + 4:verifier_at + 20], filepass[verifier_at + 20:verifier_at + 36]
        check = filepass[verifier_at + 40:verifier_at + 60]
        base = hashlib.sha1(salt + _DEFAULT_XLS_PASSWORD).digest()

        def key_of(block: int) -> bytes:
            key = hashlib.sha1(base + struct.pack("<I", block)).digest()
            return key[:5] + bytes(11) if key_bits == 40 else key[:key_bits // 8]

        digest = hashlib.sha1
    else:
        return None

    opened = _rc4_keystream(key_of(0), len(sealed) + len(check))
    verifier = bytes(a ^ b for a, b in zip(sealed + check, opened, strict=True))
    return key_of if digest(verifier[:16]).digest() == verifier[16:] else None


def _rc4_keystream(key: bytes, length: int) -> bytes:
    """`length` bytes of RC4 keystream. An empty key gives an empty stream (the caller's verifier will not match)."""
    if not key:
        return bytes(length)
    state = list(range(256))
    j = 0
    for i in range(256):
        j = (j + state[i] + key[i % len(key)]) & 0xFF
        state[i], state[j] = state[j], state[i]
    stream = bytearray(length)
    i = j = 0
    for index in range(length):
        i = (i + 1) & 0xFF
        j = (j + state[i]) & 0xFF
        state[i], state[j] = state[j], state[i]
        stream[index] = state[(state[i] + state[j]) & 0xFF]
    return bytes(stream)


def _open_xls_book(stream: bytes) -> xlrd.book.Book:
    """Open the workbook stream. Without a CODEPAGE record, 8-bit text (LABEL etc. of BIFF5 and below) is decoded as CP949.

    A BIFF 'Excel download' without OLE (PHP xlsBOF style) writes no CODEPAGE and writes text as CP949 bytes. xlrd then decoded it
    as latin-1, so '이름' came out as 'ÀÌ¸§'. If some text does not decode as CP949, the book is reopened with the xlrd default
    (latin-1) — with CP949 a failure is known only after reading all sheets, so only then are sheets read up front
    (on_demand=False). BIFF8 text is Unicode and independent of the code page. Known accepted gap: among Western European text
    without CODEPAGE, byte sequences that also decode as CP949 are read as Hangul (Korean documents take priority)
    (Pinned by: test_spreadsheet.py::TestRawBiffEncoding).
    """
    options = {"formatting_info": True, "on_demand": True, "logfile": StringIO(), "ragged_rows": True}
    if any(record_id == _BIFF_CODEPAGE for _, record_id, _ in _biff_records(stream)):
        return xlrd.open_workbook(file_contents=stream, **options)
    try:
        return xlrd.open_workbook(file_contents=stream, **{**options, "on_demand": False}, encoding_override="cp949")
    except UnicodeDecodeError:
        return xlrd.open_workbook(file_contents=stream, **options)


def _xls_first_used_column(sheet: xlrd.sheet.Sheet) -> int:
    """Index of the leftmost column holding a value in an XLS sheet (0-based, 0 if none). Same rule as `_first_used_column` for XLSX."""
    empty = (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK)
    firsts = (next((column for column, cell in enumerate(sheet.row(row)) if cell.ctype not in empty and cell.value != ""), None)
              for row in range(sheet.nrows))
    return min((column for column in firsts if column is not None), default=0)


def _xls_row(book: xlrd.book.Book, sheet: xlrd.sheet.Sheet, row_index: int, blank: set[tuple[int, int]],
             max_cols: int, offset: int = 0) -> tuple[str, ...]:
    """Cell texts of one XLS row. As for XLSX, it builds only up to the column limit, and the first value beyond the limit is placed
    at position limit+1 just so that the 'columns excluded' warning fires. `blank` is the formula cells emptied because the 0 in
    their result slot cannot be trusted; `offset` is the number of leading empty columns dropped from the row
    (`_xls_first_used_column`)."""
    rich = sheet.rich_text_runlist_map

    def text(col: int) -> str:
        if (row_index, col) in blank:
            return ""
        cell = sheet.cell(row_index, col)
        if (row_index, col) in rich and cell.ctype == xlrd.XL_CELL_TEXT:
            return _xls_rich_text(cell.value, rich[row_index, col], lambda index: _xls_escapement(book, index))
        return _xls_cell_text(cell, book)

    length = sheet.row_len(row_index)
    values = [text(col) for col in range(offset, min(length, offset + max_cols))]
    first = next((col for col, value in enumerate(values) if value), None)
    cell = sheet.cell(row_index, offset + first) if first is not None else None
    if cell is not None and cell.ctype == xlrd.XL_CELL_TEXT:
        values[first] = _level_marked(values[first], cell.value, _xls_indent(book, cell))  # indentation of the first text cell of the row
    overflow = next((value for value in map(text, range(offset + max_cols, length)) if value), None)
    return (*values, overflow) if overflow is not None else tuple(values)


def _xls_indent(book: xlrd.book.Book, cell: xlrd.sheet.Cell) -> int:
    try:
        return book.xf_list[cell.xf_index].alignment.indent_level
    except (AttributeError, IndexError, TypeError):
        return 0


def _fill_xls_merges(sheet: xlrd.sheet.Sheet, rows: list[list[str]], warnings: list[str], width: int,
                     offset: int = 0) -> _Across:
    """Repeat XLS vertical merge values and collect the cells spanned by horizontal merges (`_repeat_merge`). The same range is
    handled only once, and when the total of cells covered by merges exceeds the same limit as XLSX (`_MAX_MERGED_CELLS`) nothing
    is repeated (the value stays in the first cell only), with a warning. The same range 20,000 times was rescanned for every row
    and took 17s (Pinned by: test_spreadsheet.py::TestXlsResourceBounds)."""
    across: _Across = {}
    merges = sorted(set(sheet.merged_cells))
    if sum((bottom - top) * (right - left) for top, bottom, left, right in merges) > _MAX_MERGED_CELLS:
        warnings.append(f"Sheet '{sheet.name}': there are too many merged cells, so vertical merge values were left in the first cell only.")
        return across
    for top, bottom, left, right in merges:
        if left >= offset:  # a merge starting in a dropped leading empty column has an empty first cell, so there is no value to fill
            _repeat_merge(rows, top, bottom, left - offset, min(right - offset, width), across)
    return across


_BIFF_FORMULA = 0x0006
_BIFF_FORMULA_EMPTY_RESULT = (3, b"\xff\xff")  # first byte of the 8-byte result (kind 3 = empty text) and its last two bytes (not a number)
# Build number and year that Apache POI (HSSF) writes in the workbook BOF. Excel (0x3266, 1997, etc.) and LibreOffice (0x0DBB) write
# other values.
_BIFF_POI_BUILD = (0x10D3, 0x07CC)


def _xls_formulas_without_results(stream: bytes) -> tuple[int, dict[int | None, set[tuple[int, int]]]]:
    """(number of formula cells in a workbook where not a single result is stored, formula cells whose 0 in the result slot cannot
    be trusted: worksheet index → (row, column)). 0 and an empty dict when neither applies.

    A library (xlwt) writes 'empty text' in the result slot of every formula. xlrd returns such a cell as empty text, so totals
    cells became blank without a warning. Excel writes a formula computed to empty text (`=IF(A1="","",…)`) in exactly the same
    way, so a single cell cannot tell them apart. As with XLSX, if there is even one result the file is taken as saved by Excel.
    xlrd does not provide formula text, so the formulas cannot be carried over; there is only a warning
    (Pinned by: test_spreadsheet.py::TestXlsFormulasWithoutResults).
    Apache POI (HSSF) does not calculate and writes the number 0 in the result slot, so totals and ratios all came out as 0 (Excel
    recalculates on open). Those cells are emptied only when the workbook BOF is POI's and every formula result is the number 0 —
    the 0 in an empty form saved by Excel or LibreOffice is a computed value. Known accepted gap: a file made by opening an Excel
    template with POI and filling in values only keeps the template's BOF, so its 0s are read as is
    (Pinned by: test_spreadsheet.py::TestXlsFormulasWithPlaceholderZeroResults).
    If the scan fails it moves on without a warning — cell contents are read by xlrd.
    """
    empty = 0
    zeros: dict[int | None, set[tuple[int, int]]] = {}
    sheet_offsets: dict[int, int] = {}  # worksheet BOF position → sheet index (the same mapping as `_scan_xls_drawings`)
    written_by_poi = False
    sheet = None
    kind, marker = _BIFF_FORMULA_EMPTY_RESULT
    for position, record_id, body in _biff_records(stream):
        if record_id == _BIFF_BOF and len(body) >= 8:
            if position == 0:
                written_by_poi = struct.unpack_from("<HH", body, 4) == _BIFF_POI_BUILD
            sheet = sheet_offsets.get(position, sheet)
        elif record_id == _BIFF_BOUNDSHEET and len(body) >= 6 and body[5] == 0:
            sheet_offsets[struct.unpack_from("<I", body)[0]] = len(sheet_offsets)
        elif record_id == _BIFF_FORMULA and len(body) >= 14:
            if body[6] == kind and body[12:14] == marker:
                empty += 1
            elif body[12:14] != b"\xff\xff" and struct.unpack_from("<d", body, 6)[0] == 0 and written_by_poi:
                zeros.setdefault(sheet, set()).add(struct.unpack_from("<HH", body))
            else:
                return 0, {}
    if empty and zeros:
        return 0, {}
    return empty, zeros


# ---- XLS drawing objects (text box / shape text, pictures) --------------------------------------------------------

_BIFF_BOF, _BIFF_EOF, _BIFF_BOUNDSHEET, _BIFF_CONTINUE, _BIFF_CODEPAGE = 0x0809, 0x000A, 0x0085, 0x003C, 0x0042
_BIFF_OBJECT, _BIFF_TEXT_OBJECT, _BIFF_DRAWING, _BIFF_DRAWING_GROUP = 0x005D, 0x01B6, 0x00EC, 0x00EB
_BIFF8 = 0x0600
_OBJECT_PICTURE, _OBJECT_NOTE = 0x08, 0x19
_OBJECT_CHOICES = frozenset((0x0B, 0x0C))  # form control check boxes and option buttons
# Among the OBJ sub-records, the check box state (first 2 bytes: 0 unchecked, 1 checked, 2 mixed). Excel writes it in FtCblsData
# (0x12), LibreOffice in FtCbls (0x0A).
_OBJECT_CHECK_STATES = (0x12, 0x0A)
_ESCHER_STORE_ENTRY, _ESCHER_OPTIONS = 0xF007, 0xF00B
_ESCHER_PICTURE_INDEX = 0x0104  # shape property pib: index into the global picture store (1-based)
_ESCHER_METAFILES = frozenset((0xF01A, 0xF01B, 0xF01C))  # EMF, WMF, PICT
_ESCHER_BITMAPS = frozenset((0xF01D, 0xF01E, 0xF01F, 0xF029, 0xF02A))  # JPEG, PNG, DIB, TIFF, JPEG (CMYK)
_ESCHER_DIB = 0xF01F


def _xls_drawings(stream: bytes, options: ConvertOptions) -> list[list[str | bytes | None]] | None:
    """For each worksheet (BOUNDSHEET order = xlrd's sheet order), collect text box / shape text (str) and pictures (bytes; None for
    a picture that was not found) in the order they sit in the workbook stream. None when it cannot be parsed — the caller only
    leaves a warning and cell contents are converted as usual.

    xlrd does not hand out drawing objects (it reads the text records and discards them unless they are cell comments), so
    annotations in a text box beside a table (unit, basis) and pictures were dropped without a warning. The records of the
    workbook stream are scanned directly (Pinned by: test_spreadsheet.py::TestXlsDrawings).
    """
    try:
        return _scan_xls_drawings(stream, options.max_uncompressed_bytes)
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001 — this content is read as an extra. Whatever broke, the whole document is not rejected
        return None


def _xls_chart_sheets(stream: bytes, include_hidden: bool) -> list[str]:
    """Names of the chart sheets (type 2) among the BOUNDSHEET records of the workbook globals section. xlrd does not count chart
    sheets as sheets, so a workbook where only chart sheets are visible gave an empty result with no reason
    (Pinned by: test_spreadsheet.py::TestChartSheets::test_xls_chart_sheet_is_reported)."""
    names = []
    for _, record_id, body in _biff_records(stream):
        if record_id == _BIFF_EOF:
            break
        if record_id != _BIFF_BOUNDSHEET or len(body) < 8 or body[5] != 2 or (body[4] & 3 and not include_hidden):
            continue
        length, wide = body[6], body[7] & 1
        names.append(body[8:8 + length * 2].decode("utf-16-le", errors="replace") if wide
                     else body[8:8 + length].decode("latin-1"))
    return names


_BIFF_CHART_SUBSTREAM = 0x0020


def _xls_sheets_holding_charts(stream: bytes) -> set[int]:
    """Indexes of the ordinary sheets holding a chart placed on the sheet (BOF type 0x20 inside the sheet substream) (xlrd's sheet
    index — only ordinary sheets are counted). LibreOffice saves a chart sheet as an ordinary sheet + a chart placed on it, so it
    was dropped as an empty sheet without a warning. `extract_xls` warns, as for a chart sheet, only for sheets without cells
    (Pinned by: test_office_round43.py::TestXlsChartSheetSavedAsAWorksheet)."""
    starts = []
    for _, record_id, body in _biff_records(stream):
        if record_id == _BIFF_EOF:
            break
        if record_id == _BIFF_BOUNDSHEET and len(body) >= 6 and body[5] == 0:
            starts.append(struct.unpack_from("<I", body)[0])

    charted = _chart_holders(stream, set(starts))
    return {index for index, start in enumerate(starts) if start in charted}


def _chart_holders(stream: bytes, starts: set[int]) -> set[int]:
    """Those of the sheet positions `starts` whose sheet holds a chart substream: the first BOF at or after the position (a
    position right at an EOF has no sheet) has a chart BOF inside it, before its own EOF.

    The stream is read once for all sheets. Walking it again from every sheet position, to the end when no EOF follows, took
    64 s for 2,000 sheet entries over 200,000 empty records (808 KB). Deliberate: only positions at a record boundary of the
    stream are looked at — a sheet entry pointing into the middle of a record gets no chart warning
    (Pinned by: test_audit_spreadsheet.py::TestXlsChartScan::test_stream_is_walked_a_fixed_number_of_times).
    """
    charted: set[int] = set()
    waiting: list[int] = []  # positions passed since the last BOF or EOF
    open_sheets: list[list[int]] = []  # for every BOF still open, the positions whose sheet it starts
    with_chart = 0  # the first this many open BOFs have a chart BOF inside them

    for position, record_id, body in _biff_records(stream):
        if position in starts:
            waiting.append(position)
        if record_id == _BIFF_BOF:
            if body[2:4] == _BIFF_CHART_SUBSTREAM.to_bytes(2, "little"):
                with_chart = len(open_sheets)
            open_sheets.append(waiting)
            waiting = []
        elif record_id == _BIFF_EOF:
            waiting = []
            if open_sheets:
                closed = open_sheets.pop()
                if with_chart > len(open_sheets):
                    charted.update(closed)
                    with_chart = len(open_sheets)
    for closed in open_sheets[:with_chart]:
        charted.update(closed)
    return charted


def _xls_workbook_stream(data: bytes) -> bytes:
    """Extract the workbook stream (BIFF8 `Workbook`, BIFF5 and below `Book`) from the OLE container. If it cannot be extracted the
    document is rejected as corrupted.

    Stream names are case-insensitive (`ole.exists` is — the same rule as detection `detect._detect_ole`,
    Pinned by: test_detect.py::TestXlsStreamNameCase). When both exist (Excel 97 dual format) `Workbook` comes first.
    """
    if not data.startswith(olefile.MAGIC):
        return data  # a BIFF stream not wrapped in OLE. xlrd reads it as is, too

    try:
        with olefile.OleFileIO(BytesIO(data)) as ole:
            for name in ("Workbook", "Book"):
                if ole.exists(name) and (stream := ole.openstream(name).read()):
                    return stream  # xlrd rejects an empty stream as 'no file name', so it is sent on below
    except (OSError, ValueError, struct.error) as exc:  # olefile: OSError (OleFileError) for structural damage, ValueError for bad header values
        raise CorruptedDocumentError("The XLS document structure could not be read.") from exc
    raise CorruptedDocumentError("The XLS document structure could not be read.")


def _scan_xls_drawings(stream: bytes, max_inflated_bytes: int) -> list[list[str | bytes | None]]:
    """Scan BIFF8 records in order. One shape is written as MSODRAWING (shape properties) → OBJ (object type) → [MSODRAWING → TXO →
    CONTINUE (text) → CONTINUE (character formatting)].

    - Text: read from the CONTINUE records that follow, for as many characters as the TXO says. In each record a first byte of 1
      means UTF-16LE, 0 means single-byte characters.
    - Cell comments (object type 0x19) use TXO too. They are not added as text boxes — comments are placed at the end of the sheet
      with their cell, from xlrd's NOTE (`cell_note_map`).
    - Pictures: looked up in the global picture store (MSODRAWINGGROUP) by the picture index (pib) of the shape properties. The
      same picture is extracted only once.
    - Excel writes the properties of the shape following a shape with text in a CONTINUE, not an MSODRAWING. A CONTINUE after OBJ
      or TXO is read as shape properties (15 pictures of a real Excel file had this shape).
    - Chart sheets and macro sheets are not counted as sheets by xlrd, so they are not counted here either. The inside of a chart
      placed on a sheet (the span where BOFs nest) is skipped. Old non-BIFF8 files (Excel 95 and below) have a different structure
      and are not read.
    """
    sheets: list[list[str | bytes | None]] = []
    sheet_offsets: dict[int, int] = {}  # worksheet BOF position → sheet index
    store = _XlsPictureStore(max_inflated_bytes)
    current: list[str | bytes | None] | None = None
    depth = previous = text_left = runs_left = 0
    drawing, object_type, pieces, mark = bytearray(), None, [], ""

    for position, record_id, body in _biff_records(stream):
        if record_id == _BIFF_BOF:
            if position == 0 and struct.unpack_from("<H", body)[0] != _BIFF8:
                return []
            depth += 1
            if depth == 1:
                current = sheets[sheet_offsets[position]] if position in sheet_offsets else None
        elif record_id == _BIFF_EOF:
            depth = max(depth - 1, 0)
        elif record_id == _BIFF_BOUNDSHEET:
            offset, _, kind = struct.unpack_from("<IBB", body)
            if kind == 0:
                sheet_offsets[offset] = len(sheets)
                sheets.append([])
        elif record_id == _BIFF_DRAWING_GROUP or (record_id == _BIFF_CONTINUE and previous == _BIFF_DRAWING_GROUP):
            store.data += body
            record_id = _BIFF_DRAWING_GROUP  # the CONTINUE records that follow are picture store too
        elif current is None or depth != 1:
            pass
        elif record_id == _BIFF_DRAWING:
            drawing = bytearray(body)
        elif record_id == _BIFF_OBJECT:
            object_type = struct.unpack_from("<H", body, 4)[0]
            if object_type == _OBJECT_PICTURE:
                current.append(store.picture(_escher_picture_index(drawing)))
            mark = _xls_choice_mark(body) if object_type in _OBJECT_CHOICES else ""
            drawing = bytearray()
        elif record_id == _BIFF_TEXT_OBJECT:
            text_left, runs_left = struct.unpack_from("<HH", body, 10)
            pieces = []
        elif record_id == _BIFF_CONTINUE and text_left > 0:
            piece = body[1:].decode("utf-16-le" if body[0] & 1 else "latin-1", errors="replace")
            pieces.append(piece)
            text_left -= len(piece)
            if text_left <= 0:
                text = "".join(pieces).replace("\r\n", "\n").replace("\r", "\n").strip()
                if text and object_type != _OBJECT_NOTE:
                    current.append(f"{mark} {text}" if mark else text)
                object_type, mark = None, ""
            record_id = _BIFF_TEXT_OBJECT
        elif record_id == _BIFF_CONTINUE and runs_left > 0 and previous == _BIFF_TEXT_OBJECT:
            runs_left -= len(body)  # character-formatting runs. Not read
            record_id = _BIFF_TEXT_OBJECT
        elif record_id == _BIFF_CONTINUE and previous in (_BIFF_OBJECT, _BIFF_TEXT_OBJECT, _BIFF_DRAWING):
            # Drawing fragments are appended in place for each CONTINUE — creating new bytes each time made a 16MB chain take 2.2s
            # and 1.1GB
            # (Pinned by: test_spreadsheet_damaged_parts.py::TestLongDrawingContinuation)
            if previous == _BIFF_DRAWING:
                drawing += body
            else:
                drawing = bytearray(body)
            record_id = _BIFF_DRAWING
        previous = record_id
    return sheets


def _xls_choice_mark(body: bytes) -> str:
    """Checked state of a check box / option button OBJ → ☒ or ☐ (the same characters as XLSX `_form_controls`, DOCX and HWPX). The
    label is the text of the TXO that follows.

    xlrd does not hand out drawing objects and this scanner read only the label, so the chosen side was unknown
    (Pinned by: test_spreadsheet_cell_values_and_controls.py::TestFormControlCheckBoxes::test_xls_check_boxes_keep_their_states).
    Sub-records follow one another as (type 2 bytes, length 2 bytes, content) and end at type 0 (ftEnd).
    """
    states: dict[int, int] = {}
    position = 0
    while position + 4 <= len(body):
        kind, length = struct.unpack_from("<HH", body, position)
        if kind == 0:
            break
        if kind in _OBJECT_CHECK_STATES and length >= 2 and position + 6 <= len(body):
            states.setdefault(kind, struct.unpack_from("<H", body, position + 4)[0])
        position += 4 + length
    state = next((states[kind] for kind in _OBJECT_CHECK_STATES if kind in states), 0)
    return _CHECKED if state == 1 else _UNCHECKED


@dataclass(slots=True)
class _XlsPictureStore:
    """The workbook-global picture store (the MSODRAWINGGROUP records joined). A picture is extracted only once, when first looked up.

    `budget` is the remaining bytes that compressed metafiles may inflate to (a per-document total — `max_uncompressed_bytes`).
    Measured per picture, a small file would use the limit once per picture.
    """

    budget: int
    data: bytearray = field(default_factory=bytearray)
    _entries: list[tuple[int, int, int, int] | None] | None = None
    _pictures: dict[int | None, bytes | None] = field(default_factory=dict)

    def picture(self, index: int | None) -> bytes | None:
        """Picture index (1-based) → picture file bytes. None when there is no index or it is not in the store."""
        if index not in self._pictures:
            if self._entries is None:
                self._entries = _escher_store_entries(self.data)
            entry = self._entries[index - 1] if index and index <= len(self._entries) else None
            picture = _escher_picture(self.data, entry, self.budget) if entry else None
            if picture and entry[0] in _ESCHER_METAFILES:
                self.budget -= len(picture)
            self._pictures[index] = picture
        return self._pictures[index]


def _biff_records(stream: bytes, start: int = 0) -> Iterator[tuple[int, int, bytes]]:
    """(record position, type, content). Reads from position `start`. A last record whose length runs past the end of the stream is
    given only as far as it exists."""
    position = start
    while position + 4 <= len(stream):
        record_id, length = struct.unpack_from("<HH", stream, position)
        yield position, record_id, stream[position + 4:position + 4 + length]
        position += 4 + length


def _escher_atoms(data: bytes | bytearray) -> Iterator[tuple[int, int, int, int]]:
    """The non-container drawing (Escher) records as (type, instance, content start, length). Container records (version 0xF) are
    entered.

    A sheet's MSODRAWING fragment holds only the front part of a container (the rest is in later records), so container lengths are
    not trusted and the records are walked in order.
    """
    position = 0
    while position + 8 <= len(data):
        version_instance, record_type, length = struct.unpack_from("<HHI", data, position)
        position += 8
        if version_instance & 0xF != 0xF:
            yield record_type, version_instance >> 4, position, length
            position += length


def _escher_picture_index(drawing: bytes | bytearray) -> int | None:
    """The picture index (pib) from one shape's MSODRAWING fragment."""
    for record_type, count, start, length in _escher_atoms(drawing):
        if record_type != _ESCHER_OPTIONS:
            continue
        for offset in range(start, min(start + 6 * count, start + length, len(drawing) - 5), 6):
            property_id, value = struct.unpack_from("<HI", drawing, offset)
            if property_id & 0x3FFF == _ESCHER_PICTURE_INDEX:
                return value
    return None


def _escher_store_entries(store: bytearray) -> list[tuple[int, int, int, int] | None]:
    """For each entry (BSE) of the global picture store, the picture record inside it (type, instance, content start, length). None
    for an entry without a picture."""
    entries: list[tuple[int, int, int, int] | None] = []
    for record_type, _, start, length in _escher_atoms(store):
        if record_type != _ESCHER_STORE_ENTRY:
            continue
        end = min(start + length, len(store))
        picture_at = start + 36 + (store[start + 33] if start + 33 < end else 0)  # 36-byte header + name
        if picture_at + 8 > end:
            entries.append(None)
            continue
        version_instance, picture_type, picture_length = struct.unpack_from("<HHI", store, picture_at)
        entries.append((picture_type, version_instance >> 4, picture_at + 8, min(picture_length, end - picture_at - 8)))
    return entries


def _escher_picture(store: bytearray, entry: tuple[int, int, int, int], max_bytes: int) -> bytes | None:
    """Picture record → picture file bytes. The leading header (1–2 identifiers + 1 marker byte; a 34-byte header for metafiles) is
    stripped.

    Metafiles (EMF, WMF) may be deflate-compressed. The inflated size is bounded by `max_bytes` (the document's remaining
    decompression limit). A DIB is a bitmap without a file header, so a BMP header is prepended. None when it cannot be read.
    """
    picture_type, instance, start, length = entry
    end = start + length
    start += 16 * (1 + (instance & 1))  # an odd instance value means two identifiers
    if picture_type in _ESCHER_METAFILES:
        if start + 34 > end:
            return None
        payload = bytes(store[start + 34:end])
        if store[start + 32] != 0:  # 0 = deflate, 0xFE = no compression
            return payload or None
        try:
            payload = zlib.decompressobj().decompress(payload, max_bytes + 1)
        except zlib.error:
            return None
        return payload if payload and len(payload) <= max_bytes else None
    if picture_type not in _ESCHER_BITMAPS or start + 1 >= end:
        return None

    payload = bytes(store[start + 1:end])
    if picture_type != _ESCHER_DIB:
        return payload
    if len(payload) < 40:
        return None
    (header_size,), (bit_count, compression), (colors_used,) = (
        struct.unpack_from("<I", payload), struct.unpack_from("<HI", payload, 14), struct.unpack_from("<I", payload, 32))
    palette = 4 * (colors_used or (1 << bit_count if bit_count <= 8 else 0))
    masks = 12 if compression == 3 and header_size == 40 else 0  # color masks of BI_BITFIELDS
    return b"BM" + struct.pack("<IHHI", 14 + len(payload), 0, 0, 14 + header_size + palette + masks) + payload


def _xls_drawing_blocks(items: list[str | bytes | None], sheet_name: str, extraction: Extraction,
                        options: ConvertOptions) -> list[str]:
    """Text box / shape text and pictures of one sheet → blocks to place after the table (same place as `_drawing_blocks` for XLSX).
    Text counts toward the character limit."""
    blocks: list[str] = []
    missing = 0
    for item in items:
        if isinstance(item, str):
            blocks.append(item)
            if extraction.count(solid_length(item), options):
                break
        elif options.extract_images and item:
            blocks.append(extraction.add_image(item))
        elif options.extract_images:
            missing += 1

    if missing:
        extraction.warnings.append(f"Sheet '{sheet_name}': {missing:,} picture(s) could not be read.")
    return blocks


def extract_xml_spreadsheet(data: bytes, options: ConvertOptions) -> Extraction:
    """Excel 2003 XML Spreadsheet (`<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet">`).

    This is the format that the 'Excel download' of Korean web systems serves under a .xls name. `<Table>` was detected as an HTML
    table and the result was an empty string
    (Pinned by: test_office.py::TestXmlSpreadsheet). Entities are not resolved (same parser as XLSX).
    """
    root, encoding = _parse_xml_spreadsheet(data)
    formats = _xml_style_formats(root)
    indents = _xml_style_indents(root)
    unresolved = _xml_formulas_lack_results(root)

    extraction = Extraction()
    warning = decoding_warning(encoding) if encoding else None
    if warning:
        extraction.warnings.append(warning)
    invalid: set[str] = set()  # names of position/merge attributes that are not integers (one warning for the whole document)
    for index, worksheet in enumerate(root.iter(f"{{{_SS}}}Worksheet"), start=1):
        name = _ss(worksheet, "Name") or f"Sheet{index}"
        visible = worksheet.find(f"{{{_X}}}WorksheetOptions/{{{_X}}}Visible")
        if visible is not None and (visible.text or "").strip() != "SheetVisible" and not options.include_hidden_sheets:
            extraction.warnings.append(f"Hidden sheet '{name}' was excluded.")
            continue

        table = worksheet.find(f"{{{_SS}}}Table")
        formulas: list[str] = []
        spans: dict[int, list[tuple[int, int, str]]] = {}  # index of the yielded row → horizontal merges (`_fill_across`)
        cells = _xml_sheet_rows(table, formats, options.max_sheet_cols, formulas if unresolved else None, invalid,
                                indents, spans) if table is not None else iter(())
        rows = _collect_rows(cells, options, name, extraction)
        across = {id(rows[index]): found for index, found in spans.items() if index < len(rows)}
        if formulas:
            extraction.warnings.append(f"Sheet '{name}': {len(formulas):,} formula(s) with no stored result were copied as written "
                                       "(opening and re-saving the file in Excel converts them to values).")
        _append_sheet(extraction, name, rows, across=across)
        if extraction.exceeded:
            break
    if invalid:
        extraction.warnings.append(f"Non-integer cell position and merge attributes ({', '.join(sorted(invalid))}) were ignored. "
                                   "Cells may have been placed in a different column.")

    extraction.metadata["sheet_count"] = str(len(extraction.sections))
    return extraction


_SS = "urn:schemas-microsoft-com:office:spreadsheet"
_X = "urn:schemas-microsoft-com:office:excel"
# Named number formats of XML Spreadsheet. Date names ("Short Date") would be caught by format_cell's time-letter check, so they are
# replaced by format strings. The time names (Long/Medium/Short Time) were missing, so time cells came out as dates. General Date
# is kept as a date format so that numeric cells are also read as dates
# (Deliberate: like Excel, only the date at midnight — `format_cell` tells it apart by `_XML_GENERAL_DATE`)
# (Pinned by: test_spreadsheet.py::TestXmlSpreadsheetDatesAndTimes,
# TestDateTimeAtMidnight::test_xml_general_date_at_midnight_shows_only_the_date).
# Scientific, Euro Currency, Yes/No, True/False and On/Off were missing, so letters in the name (s, y, d) were read as date codes
# and 0.000123 became `00:00:11` and 1 became `1900-01-01`. Exponent formats show the raw value (`format_cell`)
# (Pinned by: test_spreadsheet.py::TestXmlSpreadsheetNamedFormats).
_XML_GENERAL_DATE = "[$-x-general-date]yyyy-mm-dd h:mm:ss"
_XML_NAMED_FORMATS = {"Percent": "0.00%", "Fixed": "0.00", "Standard": "#,##0.00", "Currency": "#,##0.00",
                      "Euro Currency": '"€"#,##0.00', "Scientific": "0.00E+00",
                      "Yes/No": '"Yes";"Yes";"No"', "True/False": '"TRUE";"TRUE";"FALSE"', "On/Off": '"On";"On";"Off"',
                      "Short Date": "yyyy-mm-dd", "Medium Date": "yyyy-mm-dd", "Long Date": "yyyy-mm-dd",
                      "Long Time": "h:mm:ss", "Medium Time": "h:mm", "Short Time": "h:mm",
                      "General Date": _XML_GENERAL_DATE, "General Number": None, "General": None}
# English names not in the table (formats made only of words and spaces or `/`). One made only of date/time code letters
# (y, m, d, h, s, e, g, b, a, AM/PM markers) is a format code (`mmmm`, `dddd`) and is not treated as a name.
_XML_FORMAT_NAME = re.compile(r"[A-Za-z]+(?:[ /][A-Za-z]+)*")
_DATE_CODE_LETTERS = re.compile(r"(?:AM/PM|A/P|[ymdhsegba /])+", re.IGNORECASE)


# The XML declaration at the very start of the document. When re-parsing after decoding to text, the encoding name in the
# declaration differs from the actual one, so it is removed.
_XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>")


def _parse_xml_spreadsheet(data: bytes) -> tuple[etree._Element, str | None]:
    """Tolerate the common cases an XML parser rejects: blank lines before `<?xml` (JSP/ASP output), CP949 without a declaration,
    CP949 declared as UTF-8, an encoding name the parser does not know (`ks_c_5601-1987`). If parsing fails as is, decode with
    decode_text and parse again
    (Pinned by: test_office.py::TestXmlSpreadsheet::test_leading_whitespace_before_declaration_is_tolerated,
    test_korean_encodings_that_xml_parsers_reject_are_decoded). When decoded here, the encoding name is returned as well (for the
    replacement-character warning).
    """
    try:
        return etree.fromstring(data.removeprefix(b"\xef\xbb\xbf").lstrip(b" \t\r\n"), _XML_PARSER), None
    except (etree.XMLSyntaxError, LookupError, ValueError):
        pass

    text, encoding = decode_text(data)
    try:
        root = etree.fromstring(_XML_DECLARATION.sub("", text.lstrip("\ufeff"), count=1).encode("utf-8"), _XML_PARSER)
        return root, encoding
    except (etree.XMLSyntaxError, ValueError) as exc:
        raise CorruptedDocumentError("The XML spreadsheet structure could not be read.") from exc


def _xml_style_formats(root: etree._Element) -> dict[str | None, str | None]:
    """Style ID → number format. A style without its own format inherits from `ss:Parent`, and failing that from the `Default` style."""
    styles = {_ss(style, "ID"): style for style in root.iter(f"{{{_SS}}}Style")}

    def own(style_id: str | None) -> str | None:
        style = styles.get(style_id)
        number_format = style.find(f"{{{_SS}}}NumberFormat") if style is not None else None
        return _ss(number_format, "Format") if number_format is not None else None

    # The format each style inherits is kept, so a chain of parents is followed once and not again from every style on it: 8,000
    # chained styles (310 KB) took 44 s (Pinned by: test_audit_spreadsheet.py::TestXmlSpreadsheetStyleChains).
    formats: dict[str | None, str | None] = {}
    inherited: dict[str, str | None] = {}
    for style_id in [*styles, None]:
        current, seen = style_id, set()
        while current is not None and current not in seen and current not in inherited and own(current) is None:
            seen.add(current)
            current = _ss(styles[current], "Parent") if current in styles else None
        found = inherited[current] if current in inherited else own(current) if current is not None else None
        inherited.update(dict.fromkeys(seen, found))
        formats[style_id] = _xml_number_format(found if found is not None else own("Default"))
    return formats


def _xml_style_indents(root: etree._Element) -> dict[str | None, int]:
    """Style ID → cell indent (`<Alignment ss:Indent>`). Inherited by following `ss:Parent`. Styles without an indent are not included."""
    styles = {_ss(style, "ID"): style for style in root.iter(f"{{{_SS}}}Style")}
    indents: dict[str | None, int] = {}
    inherited: dict[str | None, int | None] = {}  # followed once per style, as in `_xml_style_formats`
    for style_id in styles:
        current, seen = style_id, set()
        found = None
        while current in styles and current not in seen:
            if current in inherited:
                found = inherited[current]
                break
            seen.add(current)
            alignment = styles[current].find(f"{{{_SS}}}Alignment")
            value = (_ss(alignment, "Indent") or "").strip() if alignment is not None else ""
            if value:
                if decimal_int(value, 0) > 0:
                    found = decimal_int(value, 0)
                break
            current = _ss(styles[current], "Parent")
        inherited.update(dict.fromkeys(seen, found))
        if found is not None:
            indents[style_id] = found
    return indents


def _ss(element: etree._Element, name: str) -> str | None:
    return element.get(f"{{{_SS}}}{name}", element.get(name))


def _ss_int(element: etree._Element, name: str, fallback: int, invalid: set[str]) -> int:
    """Integer attribute (`ss:Index`, `ss:Span`, `ss:MergeAcross`, `ss:MergeDown`). Missing or empty gives `fallback`; a value that is
    not a decimal number (`abc`, `²`) also gives `fallback` and records the name in `invalid` — a bare `int()` rejected the whole
    document over a single attribute (Pinned by: test_spreadsheet.py::TestXmlSpreadsheetIntegerAttributes)."""
    value = (_ss(element, name) or "").strip()
    if not value:
        return fallback
    # None also for more than 4,300 digits, which `int()` refuses although they are decimal
    # (Pinned by: test_audit_spreadsheet.py::TestIntegerAttributesTooLongToRead).
    number = decimal_int(value, None)
    if number is not None:
        return number
    invalid.add(name)
    return fallback


def _xml_number_format(number_format: str | None) -> str | None:
    """A named format becomes its format string; a name missing from the table is treated as the General format (None) — the
    letters of the name were being read as date codes
    (Pinned by: test_spreadsheet.py::TestXmlSpreadsheetNamedFormats::test_unknown_format_name_is_not_read_as_a_date)."""
    if not number_format:
        return None
    if number_format in _XML_NAMED_FORMATS:
        return _XML_NAMED_FORMATS[number_format]
    if _XML_FORMAT_NAME.fullmatch(number_format) and not _DATE_CODE_LETTERS.fullmatch(number_format):
        return None
    return number_format


def _xml_formulas_lack_results(root: etree._Element) -> bool:
    """Whether there are formula cells (`ss:Formula`) but not a single calculated value.

    Libraries such as SheetJS write only the formula and leave the value empty (`<Data ss:Type="Number"></Data>` or no Data at all),
    so total cells became blank without any warning. Files saved by Excel write the value as well, so — as with XLS and XLSX — if at
    least one value exists an empty value is taken as the calculated result (empty text)
    (Pinned by: test_spreadsheet.py::TestXmlSpreadsheetFormulasWithoutResults).
    """
    found = False
    for cell in root.iter(f"{{{_SS}}}Cell"):
        if not _ss(cell, "Formula"):
            continue
        data = cell.find(f"{{{_SS}}}Data")
        if data is not None and "".join(data.itertext()).strip():
            return False
        found = True
    return found


# R1C1 reference (`R[-2]C`, `RC[-1]`, `R1C2`). Quoted text is skipped whole (a match without the first group).
_R1C1_REFERENCE = re.compile(r'"[^"]*"|(?<![\w.$])R(\[-?\d+\]|\d+)?C(\[-?\d+\]|\d+)?(?![\w(\[])')


def _a1_formula(formula: str, row: int, column: int) -> str:
    """Turn an Excel 2003 XML R1C1 formula into the A1 formula seen from the cell position (`row`, `column`, 1-based)
    (`=SUM(R[-2]C:R[-1]C)` → `=SUM(B1:B2)`). It has to look like XLSX formula text for an LLM to read which cells are meant.
    A reference pointing outside the sheet leaves the original text as is."""
    def target(part: str | None, current: int) -> tuple[int, str]:
        """(target row/column number, absolute-reference mark). `[n]` is relative, a bare number is absolute, nothing means the
        same row/column."""
        if not part:
            return current, ""
        return (current + int(part[1:-1]), "") if part.startswith("[") else (int(part), "$")

    def a1(match: re.Match[str]) -> str:
        if match.group(0).startswith('"'):
            return match.group(0)
        (target_row, row_mark), (target_column, column_mark) = target(match.group(1), row), target(match.group(2), column)
        if not (1 <= target_row <= 1_048_576 and 1 <= target_column <= 16_384):
            raise ValueError(target_row, target_column)
        return f"{column_mark}{get_column_letter(target_column)}{row_mark}{target_row}"

    try:
        return _R1C1_REFERENCE.sub(a1, formula)
    except ValueError:
        return formula


def _xml_sheet_rows(table: etree._Element, formats: dict[str | None, str | None], max_cols: int,
                    formulas: list[str] | None = None, invalid: set[str] | None = None,
                    indents: dict[str | None, int] | None = None,
                    across: dict[int, list[tuple[int, int, str]]] | None = None) -> Iterator[tuple[str, ...]]:
    """Turn `<Row>`/`<Cell>` into rows. `ss:Index` is a skipped position (1-based); `MergeAcross` is repeated over the spanned cells
    only in rows that have text to the left (a title row keeps it in the first cell only — `_repeat_merge`), and `MergeDown` is
    repeated in the rows below.
    The format is resolved in the order cell → row → column (`<Column>`) → Default style
    (Pinned by: test_office.py::TestXmlSpreadsheet::test_number_format_comes_from_row_column_default_or_parent_style).

    Only cells within the column limit go into the row and have their merges remembered. The first value beyond the limit is put at
    position limit+1 so that only the 'columns excluded' warning is raised. Remembering the merges of cells beyond the limit as well
    and scanning them for every row made a 2.4MB file take 44 seconds
    (Pinned by: test_office.py::TestXmlSpreadsheet::test_merges_and_cells_beyond_the_column_limit_stay_cheap).
    Skipped rows (`ss:Index`) and empty rows are emitted as one empty row — that is where the tables inside a sheet are split
    (`_sheet_text`). When `formulas` is given, formula cells without a value are filled with the A1 formula text and that text is
    collected there (for the warning). Position/merge attributes that are not integers are treated as absent and their names are
    collected in `invalid` (for the warning).
    When `indents` (style ID → cell indent) is given, the first text cell of a row gets an indent-level mark (`_level_marked`).
    When `across` is given, the cells spanned by horizontal merges in rows that have text to the left are collected as
    index of the emitted row (0-based) → [(column, end column, value)] (`_repeat_merge`).
    """
    invalid = set() if invalid is None else invalid
    column_styles: dict[int, str] = {}
    column = 0

    for column_element in table.iterfind(f"{{{_SS}}}Column"):
        column = _ss_int(column_element, "Index", column + 1, invalid)
        span = _ss_int(column_element, "Span", 0, invalid)
        if _ss(column_element, "StyleID"):
            for styled in range(column, min(column + span, max_cols) + 1):
                column_styles[styled] = _ss(column_element, "StyleID")
        column += span

    merged_down: dict[int, tuple[str, int, int]] = {}  # column within the column limit → (value, last row, last column spanned horizontally)
    row_number = yielded = 0
    for row in table.iterfind(f"{{{_SS}}}Row"):
        previous, row_number = row_number, _ss_int(row, "Index", row_number + 1, invalid)
        if row_number > previous + 1:
            yield ()
            yielded += 1
        values: dict[int, str] = {}
        spans: list[tuple[int, int, str]] = []  # horizontal merges of this row (1-based columns)
        column = 0

        for cell in row.iterfind(f"{{{_SS}}}Cell"):
            column = _ss_int(cell, "Index", column + 1, invalid)
            data = cell.find(f"{{{_SS}}}Data")
            style = _ss(cell, "StyleID") or _ss(row, "StyleID") or column_styles.get(column)
            text = _xml_cell_value(data, formats.get(style, formats.get(None))) if data is not None else ""
            if not text and formulas is not None and _ss(cell, "Formula"):
                text = _a1_formula(_ss(cell, "Formula"), row_number, column)
                formulas.append(text)
            if text and not values and data is not None and _ss(data, "Type") == "String":
                text = _level_marked(text, "".join(data.itertext()), (indents or {}).get(style, 0))
            merge_across = _ss_int(cell, "MergeAcross", 0, invalid)
            if text and column <= max_cols:
                # Collect spanned cells only for horizontal merges in rows that have text to the left (same rule as `_repeat_merge`)
                labelled = values or any(left < column and last_row >= row_number
                                         for left, (_, last_row, _) in merged_down.items())
                last = min(column + merge_across, max_cols) if labelled else column
                values[column] = text
                if last > column:
                    spans.append((column + 1, last, text))
                merge_down = _ss_int(cell, "MergeDown", 0, invalid)
                if merge_down > 0:
                    merged_down[column] = (text, row_number + merge_down, last)
            elif text:
                values.setdefault(max_cols + 1, text)
            column += merge_across

        for merged_column, (text, last_row, last) in list(merged_down.items()):
            if last_row < row_number:
                del merged_down[merged_column]
            elif merged_column not in values:
                values[merged_column] = text
                if last > merged_column:
                    spans.append((merged_column + 1, last, text))
        if values and across is not None and spans:
            across[yielded] = [(first - 1, last, text) for first, last, text in spans]  # 0-based columns, end column exclusive
        yield tuple(values.get(index, "") for index in range(1, max(values) + 1)) if values else ()
        yielded += 1


def _xml_cell_value(data: etree._Element, number_format: str | None) -> str:
    text = "".join(data.itertext()).strip()  # rich text (html tags inside ss:Data) too: characters only
    kind = _ss(data, "Type")
    if kind == "String" and len(data):
        return _xml_rich_text(data).strip()
    try:
        if kind == "Number" and number_format and is_date_format(number_format):
            return _xml_serial_text(float(text), number_format)
        if kind == "Number":
            return format_cell(float(text), number_format)
        if kind == "DateTime":
            value = datetime.fromisoformat(text)
            if number_format and _ELAPSED_FORMAT.search(number_format):
                # Elapsed time is a duration measured from Excel's day 0. From 1900-03-01 on, Excel counts the non-existent day
                # (1900-02-29) as one day
                # (Pinned by: test_spreadsheet.py::TestDateFormats::test_xml_spreadsheet_elapsed_time_is_shown_as_total_hours).
                # A value written with a time zone (`…T10:00:00Z`) is measured by the time as written, as the time-only case
                # below reads it — compared with a date without a zone it raised TypeError and rejected the whole document
                # (Pinned by: test_audit_spreadsheet.py::TestXmlDateTimeWithATimeZone).
                value = value.replace(tzinfo=None)
                return format_cell(value - datetime(1899, 12, 31 if value < datetime(1900, 3, 1) else 30), number_format)

            # A time-only value is stored attached to Excel's day 0 (1899-12-31) (Pinned by: test_time_only_value_is_shown_as_a_time).
            return _format_value(value.time()) if value.date() == date(1899, 12, 31) else format_cell(value, number_format)
    except ValueError:
        return text
    if kind == "Boolean":
        return "TRUE" if text == "1" else "FALSE"
    return text


def _xml_rich_text(data: etree._Element) -> str:
    """Characters of rich text (html tags inside `ss:Data`). Characters inside `<Sup>`/`<Sub>` are marked as superscript/subscript
    (the same `_shared.mark_script` as XLSX rich text). Joining the characters alone turned `m²` into `m2` and `10⁶원` into `106원`
    (Pinned by: test_core_spreadsheet.py::TestXmlSpreadsheetScripts). The text of comments and processing instructions is left out
    and only their tail text is joined (same as `itertext`)."""
    pieces: list[str] = []
    # (text, element, mark) — when text is set it is tail text
    stack: list[tuple[str | None, etree._Element, str | None]] = [(None, data, None)]

    while stack:
        tail, element, mark = stack.pop()
        if tail is not None:
            pieces.append(mark_script(tail, mark) if mark else tail)
            continue
        if isinstance(element.tag, str):
            mark = _XML_SCRIPT_TAGS.get(etree.QName(element).localname.lower(), mark)
            if element.text:
                pieces.append(mark_script(element.text, mark) if mark else element.text)
        for child in reversed(element):
            if child.tail:
                stack.append((child.tail, child, mark))
            stack.append((None, child, mark))

    return resolve_scripts("".join(pieces))


# Superscript/subscript tags of Excel 2003 XML rich text (html namespace, case-insensitive).
_XML_SCRIPT_TAGS = {"sup": SUPERSCRIPT, "sub": SUBSCRIPT}


def _xml_serial_text(value: float, number_format: str) -> str:
    """A number cell (`ss:Type="Number"`) with a date/time format is an Excel serial number. It is shown as a date, a time or an
    elapsed time (below 1 it is a time). The serial number (45356) used to be emitted as is
    (Pinned by: test_spreadsheet.py::TestXmlSpreadsheetDatesAndTimes). Known accepted gap: the 1904 date system (`x:Date1904`) is
    not considered."""
    try:
        if _ELAPSED_FORMAT.search(number_format):
            return format_cell(timedelta(days=value), number_format)
        if value < 0:
            return format_cell(value, number_format)  # a negative serial number is not a date (`_negative_serial_text`)
        return format_cell(from_excel(value), number_format)
    except (OverflowError, ValueError):
        return format_cell(value, number_format)


# The delimiter hint on the first line that Excel writes and reads (`sep=;`). Honoured only when the first line is nothing but that.
_SEPARATOR_HINT = re.compile(r"sep=([^\r\n])[ \t]*(?:\r\n|\r|\n|\Z)", re.IGNORECASE)


def extract_csv(data: bytes, options: ConvertOptions) -> Extraction:
    text, encoding = decode_text(data)
    # A first line `sep=;` is Excel's delimiter hint. Use that delimiter and drop the line — it used to become the header row and
    # was not even used to decide the delimiter (Pinned by: test_spreadsheet.py::TestCsvLimitsAndHints).
    hint = _SEPARATOR_HINT.match(text)
    if hint:
        text = text[hint.end():]
    # Using only the delimiter and discarding the quote character and the skip-space-after-delimiter setting split the cell at the
    # comma inside the quotes of `홍길동, 30, "서울시, 강남구"` (Pinned by: test_spreadsheet.py::TestCsvDialect). If sniffing fails
    # the csv module defaults are used.
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=hint.group(1) if hint else ",\t;|")
        style = {"delimiter": dialect.delimiter, "quotechar": dialect.quotechar or '"',
                 "skipinitialspace": dialect.skipinitialspace}
    except csv.Error:
        style = {"delimiter": hint.group(1) if hint else "\t" if sample.count("\t") > sample.count(",") else ","}
    steadier = _steadier_delimiter(sample, len(text) > len(sample)) if style["delimiter"] == "," and not hint else None
    if steadier:
        style["delimiter"], style["skipinitialspace"] = steadier
    if style.get("quotechar") == "'" and not _wraps_whole_fields(sample, "'", style["delimiter"]):
        style["quotechar"] = '"'
    if (style.get("quotechar", '"') == '"'
            and _escapes_quotes_with_backslashes(sample, str(style["delimiter"]), len(text) > len(sample))):
        text = _unescape_backslashes(text)

    extraction = Extraction(metadata={"encoding": encoding})
    warning = decoding_warning(encoding)
    if warning:
        extraction.warnings.append(warning)
    rows = _collect_rows(_csv_rows(text, style, extraction.warnings, options.max_sheet_cols), options, "CSV", extraction)
    extraction.sections.append(Section(SectionKind.DOCUMENT, 1, _sheet_text(rows)))
    return extraction


# A backslash and the one character after it. The only characters MySQL `INTO OUTFILE` (ESCAPED BY '\') escapes are the quote, the
# backslash, NULL (`\N`), NUL (`\0`), the control character names (`\n`, `\t`, `\r`, `\b`, `\Z`), the delimiter and line breaks.
_BACKSLASH_PAIR = re.compile(r"\\(.?)", re.DOTALL)
_MYSQL_ESCAPED = frozenset('"\\N0ntrbZ\r\n')
_BACKSLASH_ESCAPE = re.compile(r'\\(["\\])')


def _escapes_quotes_with_backslashes(sample: str, delimiter: str, cut: bool) -> bool:
    """Whether this is a CSV that writes a double quote inside a cell as `\\"` (the MySQL `SELECT … INTO OUTFILE` default). Only when
    every backslash in the sample stands before a character MySQL escapes, there is a place where `\\"` is followed by a character
    that is not a cell end (delimiter, line end, quote), and the sample read with those escapes resolved has the same number of
    cells on every line with double quotes wrapping whole cells (`_field_counts`; this filters out ordinary CSVs containing a path
    ending like `"c:\\"`).

    Reading with the doubled-quote rule alone broke `"27\\" 모니터"` into `27\\ 모니터"`
    (Pinned by: test_core_spreadsheet.py::TestCsvBackslashEscapes).
    Deliberate: detection is conservative — if there are other backslashes (`C:\\temp`, `\\d+`) the file is not read as escaped.
    Reading it that way would make those characters disappear. In a file detected this way only `\\"` and `\\\\` are resolved, and
    `\\N` (NULL) and the like are left as literal characters. Known accepted gap: if `\\"` occurs only after the sample (the first
    8,192 characters) it is not detected.
    """
    evidence = False

    for match in _BACKSLASH_PAIR.finditer(sample):
        escaped = match.group(1)
        if escaped not in _MYSQL_ESCAPED and escaped != delimiter:
            return False
        if escaped == '"' and sample[match.end():match.end() + 1] not in (delimiter, '"', "\r", "\n", ""):
            evidence = True
    if not evidence:
        return False

    unescaped = _unescape_backslashes(sample)
    _, steady, quoted = _field_counts(unescaped, delimiter, cut)
    return steady and quoted


def _unescape_backslashes(text: str) -> str:
    """`\\"` → `""` (the csv module's doubled quote), `\\\\` → `\\`. Other backslashes are left as is."""
    return _BACKSLASH_ESCAPE.sub(lambda match: '""' if match.group(1) == '"' else "\\", text)


def _steadier_delimiter(sample: str, cut: bool) -> tuple[str, bool] | None:
    """The delimiter to use instead of the comma (tab, semicolon, pipe) and whether to skip spaces after it. None if the comma is right.

    The Sniffer picks the comma whenever every line has the same number of commas. Tab-separated amount tables
    (`서울\t1,234\t5,678`), semicolon CSVs with a decimal comma (`2024-01-01;1,5;2,5`) and cells listing words with commas were
    therefore split in the middle of numbers and sentences. If splitting by a candidate gives the same number of cells on every
    line of the sample (2 or more), that candidate is used. For files whose lines differ in cell count, only TSVs that omit
    trailing empty cells (`_tab_on_every_comma_line`) and files with title/metadata lines in front or short lines mixed in
    (`_delimiter_of_most_lines`) are detected; the rest keep the Sniffer's decision.
    A one-line file accepts only the tab — semicolons and pipes also occur inside text, so one line is weak evidence.
    To tell apart a comma CSV whose quoted cells contain one candidate per line (`가,"확인; 완료"`), the comma is kept when the
    pattern of double quotes wrapping whole cells fits the comma but not the candidate
    (Pinned by: test_spreadsheet.py::TestCsvDialect).
    """
    _, comma_steady, comma_quoted = _field_counts(sample, ",", cut)
    for delimiter in "\t;|":
        counts, steady, quoted = _field_counts(sample, delimiter, cut)
        if steady and (len(counts) > 1 or delimiter == "\t") and (quoted or not (comma_steady and comma_quoted)):
            return delimiter, quoted and f'{delimiter} "' in sample
    if not comma_steady and _field_counts(sample, "\t", cut)[2] and _tab_on_every_comma_line(sample, cut):
        return "\t", False
    if not comma_steady:
        delimiter = _delimiter_of_most_lines(sample, cut)
        if delimiter:
            return delimiter, f'{delimiter} "' in sample
    return None


def _delimiter_of_most_lines(sample: str, cut: bool) -> str | None:
    """The delimiter to use instead of the comma (tab, semicolon, pipe) in a file whose lines differ in cell count. None if none fits.

    With title/metadata lines in front (`Umsatzanzeige;Datei erstellt am …`, `고객 목록`) or lines that omit trailing empty cells
    mixed in, no delimiter gives an even cell count, so the Sniffer's comma remained and the amount `-1.234,56` was split into
    `-1.234 | 56`. If the lines having the maximum cell count for a candidate (3 cells or more) number two or more and are the
    majority, and the comma cell count is uneven from the first such line (the header) on, that candidate is used. If the comma
    cell count is even from the header on, the `;`/`|` are lists inside values and it is a comma CSV
    (Pinned by: test_spreadsheet.py::TestCsvDelimiterOfMostLines).
    """
    commas = _line_field_counts(sample, ",", cut)
    for delimiter in "\t;|":
        counts = _line_field_counts(sample, delimiter, cut)
        width = max(counts, default=0)
        if width < 3 or counts.count(width) < 2 or counts.count(width) * 2 <= len(counts):
            continue
        if len(set(commas[counts.index(width):])) > 1:
            return delimiter
    return None


def _line_field_counts(sample: str, delimiter: str, cut: bool) -> list[int]:
    """Cell count per non-empty line when split by `delimiter` (double-quoted cells within a line are counted as in `_field_counts`)."""
    lines = [line for line in sample.splitlines() if line.strip()]
    if cut and len(lines) > 1 and not sample.endswith(("\n", "\r")):
        lines.pop()  # only the head of the file was sampled, so the last line may be cut off in the middle
    return [max(_field_counts(line, delimiter, False)[0], default=1) for line in lines]


def _tab_on_every_comma_line(sample: str, cut: bool) -> bool:
    """Whether this is a TSV whose lines differ in tab count because trailing empty cells are omitted: the first line (header) has a
    tab, and every line containing a comma has a tab (2 or more lines with tabs).

    TSV exports from Excel and ERP systems omit the empty cells at the end of a line, so the tab cell count is uneven. The comma
    picked by the Sniffer then remained, cells were split at the thousands separators of amounts (`1,100,000`) and columns shifted.
    A comma CSV would instead have a header with tabs but no commas, or a line with commas but no tabs
    (Pinned by: test_spreadsheet.py::TestCsvDialect::test_tab_file_whose_rows_omit_trailing_empty_cells_is_tab_delimited,
    test_comma_file_with_some_tabs_stays_comma_delimited).
    """
    lines = [line for line in sample.splitlines() if line.strip()]
    if cut and len(lines) > 1 and not sample.endswith(("\n", "\r")):
        lines.pop()  # only the head of the file was sampled, so the last line may be cut off in the middle
    return (bool(lines) and "\t" in lines[0] and sum("\t" in line for line in lines) >= 2
            and all("\t" in line for line in lines if "," in line))


def _field_counts(sample: str, delimiter: str, cut: bool) -> tuple[list[int], bool, bool]:
    """(cell count per line when split by `delimiter`, whether all counts are equal and at least 2, whether double quotes are used
    only to wrap whole cells). Empty lines are left out.

    When double quotes wrap a whole cell, the delimiters and line breaks inside count as characters. If a double quote occurs in
    the middle of a cell (it is not a quote for this delimiter), each line is counted by its number of delimiters only. `cut` means
    the sample is only the head of the file — the cut-off last line is not counted.
    """
    separator = re.escape(delimiter)
    token = re.compile(rf' *"(?:[^"]|"")*(?:" *|\Z)({separator}|\r\n|[\r\n]|\Z)|[^"{separator}\r\n]*({separator}|\r\n|[\r\n]|\Z)')
    counts: list[int] = []
    fields = position = 0
    quoted = True

    while position < len(sample):
        match = token.match(sample, position)
        if match is None:
            quoted = False
            break
        fields += 1
        position = match.end()
        end = match.group(1) if match.group(1) is not None else match.group(2)
        if end != delimiter:
            if (fields > 1 or match.group(0).strip()) and (end or not cut):
                counts.append(fields)
            fields = 0
    if quoted and fields and not cut:
        counts.append(fields + 1)  # last line ending in a delimiter: there is one more empty cell

    if not quoted:
        lines = sample.splitlines()
        if cut and len(lines) > 1 and not sample.endswith(("\n", "\r")):
            lines.pop()
        counts = [line.count(delimiter) + 1 for line in lines if line.strip()]
    return counts, bool(counts) and counts[0] >= 2 and len(set(counts)) == 1, quoted


def _wraps_whole_fields(sample: str, quote: str, delimiter: str) -> bool:
    """Whether every cell containing `quote` is wrapped whole in that character (`'홍길동','1,000'`). Only the lines of the head
    sample are checked.

    The Sniffer picks the single quote as the quote character after seeing just one word such as `'목표'`. A cell starting with a
    single quote, like `'24년 상반기`, then swallowed the delimiters up to the next single quote and the cells shifted. Single
    quotes are common inside text (abbreviated years, quotations, English possessives), so they are accepted as the quote character
    only when they wrap whole cells. For double quotes the Sniffer's decision is used as is
    (Pinned by: test_spreadsheet.py::TestCsvDialect::test_apostrophe_inside_a_field_is_not_a_quote_character).
    """
    mark, separator = re.escape(quote), re.escape(delimiter)
    field = rf"(?: *{mark}(?:[^{mark}]|{mark}{mark})*{mark} *|[^{mark}{separator}]*)"
    line_pattern = re.compile(rf"{field}(?:{separator}{field})*")
    lines = sample.splitlines()
    if len(lines) > 1 and not sample.endswith(("\n", "\r")):
        lines.pop()  # only the head of the file was sampled, so the last line may be cut off in the middle
    return all(line_pattern.fullmatch(line) for line in lines)


def _csv_rows(text: str, style: dict[str, object], warnings: list[str], max_cols: int) -> Iterator[tuple[str, ...]]:
    """CSV rows. If the quoting is broken and the csv module cannot read it, lines from that point on are split by the delimiter
    only. Cells are created only up to the column limit + 1 (`_within_columns`).

    A single unclosed quote swallowed everything after it into one cell, and when that cell exceeded the csv module's field size
    limit (131,072 characters) a csv.Error was raised and the whole file was rejected. The limit is a process-wide setting, so it
    is not raised (deliberate). Rows read so far are used as they are; the failed row is read again with a quote-aware split
    (`_quoted_record`), and if the quote closes only that row is taken from it and reading returns to the csv module from the next
    line. If it never closes before the end of the file, each line from that one on is split by the delimiter, preferring not to
    lose characters (Pinned by: test_office.py::TestCsvWithBrokenQuotes).
    """
    lines = StringIO(text, newline="").readlines()
    delimiter, quote = str(style["delimiter"]), str(style.get("quotechar", '"'))
    start = 0

    while True:
        reader = csv.reader(_lines_within_columns((lines[index] for index in range(start, len(lines))), delimiter, quote,
                                                  max_cols), **style)
        read_lines = 0
        try:
            for row in reader:
                yield tuple(map(_unwrap_text_formula, row))
                read_lines = reader.line_num
            return
        except csv.Error:
            failed = start + read_lines

        record = _quoted_record(lines, failed, style) if _plain_quoting(style) else None
        if record is None:
            warnings.append(f"The quoting in the CSV is inconsistent from line {failed + 1} on, so the rest was split by the delimiter only.")
            for line in lines[failed:]:
                yield tuple(map(_unwrap_text_formula,
                                _within_columns(line, delimiter, max_cols).rstrip("\r\n").split(delimiter)))
            return
        fields, start = record
        beyond = "…" if any(field.strip() for field in fields[max_cols:]) else ""
        yield tuple(map(_unwrap_text_formula, fields if len(fields) <= max_cols else [*fields[:max_cols], beyond]))


def _plain_quoting(style: dict[str, object]) -> bool:
    """Whether this is a dialect `_quoted_record` can read the same way as the csv module (doubled-quote rule, no escape character)."""
    return (not style.get("escapechar") and style.get("doublequote", True) is not False
            and style.get("quoting", csv.QUOTE_MINIMAL) != csv.QUOTE_NONE)


def _quoted_record(lines: list[str], first: int, style: dict[str, object]) -> tuple[list[str], int] | None:
    """Split the one row starting at line `first` with quote awareness → (cells, line number of the next row). None if the quote
    never closes before the end of the file.

    A valid row (a long body cell) on which the csv module failed because of the field size limit was split by the delimiter only,
    so the body was broken into a cell at every comma and its tail was dropped at the column limit
    (Pinned by: test_office.py::TestCsvWithBrokenQuotes::test_quoted_field_longer_than_the_csv_module_limit_is_kept_whole).
    Text between a closing quote and the next delimiter is appended to the cell, as the csv module (non-strict) does.
    """
    delimiter, quote = str(style["delimiter"]), str(style.get("quotechar", '"'))
    skip_space = bool(style.get("skipinitialspace"))
    fields: list[str] = []
    parts: list[str] = []
    index, position, at_start, quoted = first, 0, True, False
    line = lines[first]
    end = len(line.rstrip("\r\n"))

    while True:
        if at_start:
            while skip_space and line.startswith(" ", position):
                position += 1
            at_start, quoted = False, line.startswith(quote, position)
            position += quoted
        if quoted:
            close = line.find(quote, position)
            if close < 0:
                parts.append(line[position:])
                index += 1
                if index == len(lines):
                    return None
                line, position = lines[index], 0
                end = len(line.rstrip("\r\n"))
                continue
            doubled = line.startswith(quote, close + 1)  # `""` is one quote character and the cell continues
            parts.append(line[position:close + doubled])
            quoted, position = doubled, close + 1 + doubled
            continue

        cut = line.find(delimiter, position, end)
        parts.append(line[position:cut if cut >= 0 else end])
        fields.append("".join(parts))
        parts = []
        if cut < 0:
            return fields, index + 1
        position, at_start = cut + 1, True


# A formula whose whole cell returns one piece of text (`="00123"`, inner quotes are `""`). An idiom used when exporting for Excel
# to keep leading zeros and long numbers.
_TEXT_FORMULA = re.compile(r'="((?:[^"]|"")*)"')


def _unwrap_text_formula(value: str) -> str:
    """Unwrap a `="00123"` cell to `00123`. Excel shows this cell as the text 00123, but the formula characters were emitted as is.
    Other formulas (`=A1+1`, `="a"&"b"`) are not evaluated, so they are left as is
    (Pinned by: test_spreadsheet.py::TestCsvExcelTextFormula)."""
    match = _TEXT_FORMULA.fullmatch(value) if value.startswith("=") else None
    return match.group(1).replace('""', '"') if match else value


def _lines_within_columns(lines: list[str], delimiter: str, quote: str, max_cols: int) -> Iterator[str]:
    """Cut each line with `_within_columns`. Lines containing the quote character, and lines inside a quoted cell opened on an
    earlier line (between lines with an odd number of quotes), are not cut — the delimiters inside are characters."""
    quoted = False

    for line in lines:
        if quote in line:
            quoted ^= line.count(quote) % 2 == 1
            yield line
        else:
            yield line if quoted else _within_columns(line, delimiter, max_cols)


def _within_columns(line: str, delimiter: str, max_cols: int) -> str:
    """Reduce a line with more delimiters than the column limit to `max_cols` cells + one cell marking that something beyond the
    limit was not empty. What lies beyond the limit is discarded anyway.

    A single line of 2 million commas was turned into 2 million cells, and a 45MB file took 5.2 seconds and 1.55GB (same rule as
    the column limit + 1 cell of XLSX) (Pinned by: test_spreadsheet.py::TestCsvLimitsAndHints).
    """
    if line.count(delimiter) <= max_cols:
        return line
    cut = -1

    for _ in range(max_cols):
        cut = line.find(delimiter, cut + 1)

    body = line.rstrip("\r\n")
    beyond = body[cut + 1:].replace(delimiter, "").strip()
    return f"{body[:cut + 1]}{'…' if beyond else ''}{line[len(body):]}"


def _collect_rows(rows: object, options: ConvertOptions, sheet_name: str, extraction: Extraction) -> list[list[str]]:
    """Collect rows. Stops at the row where the character limit (`max_chars`) is exceeded — the remaining hundreds of thousands of
    rows are not read to the end.

    Deliberate: rows are not cut by row count. The only size limit is the character count (a 2,390-row sheet hit a 2,000-row limit
    and lost 390 rows). Merge filling maps by row number, so empty rows keep their place too.
    """
    width = options.max_sheet_cols
    collected: list[list[str]] = []
    for raw in rows:
        values = [_format_value(value) for value in raw]
        if not any(values):
            collected.append([])
            continue

        if any(values[width:]):
            _warn_once(extraction.warnings, f"Sheet '{sheet_name}': columns beyond column {width} were excluded.")
        collected.append(values[:width])
        if extraction.count(sum(solid_length(value) for value in values[:width]), options):
            break
    return collected


def _until_long_empty_gap(rows: Iterator[tuple[int, tuple[str, ...]]], sheet_name: str,
                          warnings: list[str]) -> Iterator[tuple[int, tuple[str, ...]]]:
    """Yield the (row number, row) pairs of a streamed sheet only up to a run of `_EMPTY_ROW_STOP` empty rows, and warn when
    stopping there. Row numbers the file skips count as empty rows of the run and are yielded as one empty row however many
    they are — a run of empty rows renders the same as a single one (`_sheet_text`), as in a full load (`_with_blank_gaps`)
    (Pinned by: test_audit_spreadsheet.py::TestStreamedRowsFarApart)."""
    empty_streak = previous = 0
    for number, row in rows:
        skipped = max(number - previous - 1, 0)
        run = empty_streak + skipped + (0 if any(row) else 1)  # empty rows up to and including this row
        if run >= _EMPTY_ROW_STOP:
            warnings.append(f"Sheet '{sheet_name}': {_EMPTY_ROW_STOP:,} consecutive empty rows were found, "
                            f"so nothing was read from row {previous - empty_streak + 1:,} on.")
            return
        if skipped:
            yield previous + 1, ()
        empty_streak, previous = (0 if any(row) else run), number
        yield number, row


def _append_sheet(extraction: Extraction, name: str, rows: list[list[str]], drawn: list[str] | None = None,
                  across: _Across | None = None) -> None:
    index = len(extraction.sections) + 1
    text = join_blocks([_sheet_text(rows, across), *(drawn or [])])
    extraction.sections.append(Section(SectionKind.SHEET, index, text, title=name))


# Cell text that looks like a number: sign, currency, thousands separators, decimals, percent, parenthesised negatives, a quantity
# unit appended at the end (`_UNIT_SUFFIX`), dates (2024-01-02) and times (9:30). Used to decide header rows (`_looks_like_header`).
# Units: values of number formats with a unit appended (`#,##0"원"`, `0"명"`) looked like text, so the header of the second table
# was not recognised as a header and was merged into the first table
# (Pinned by: test_core_spreadsheet.py::TestTablesWithUnitFormats). Deliberate: words common in column names ('1월', '2024년',
# '1차') such as 월, 년, 일, 차, 회, 분기 are not included — if a header looks like a number the new table is missed.
# The whitespace after the currency sign belongs to the sign (`(?:[₩$€¥£]\s*)?`): as `\s*[₩$€¥£]?\s*` a sign followed by N spaces
# and no digit was split between the two runs in N² ways (Pinned by: test_audit_spreadsheet.py::TestNumberLikeText).
_UNIT_SUFFIX = r"(?:\s?(?:[천만억조]|백만|십억)?원|\s?(?:명|건|개|대|톤|%p|kg|㎏|천|만|억|백만))?"
_NUMBER_LIKE = re.compile(r"[-+−▲▼△(]?\s*(?:[₩$€¥£]\s*)?\d[\d,]*(?:\.\d+)?\s*%?" + _UNIT_SUFFIX + r"\)?"
                          r"|\d{4}[-./]\d{1,2}[-./]\d{1,2}\b.*|-?\d{1,3}:\d{2}(?::\d{2})?")
# Years, common in headers (`구분 | 2023 | 2024`). Treated as header cells even though they look like numbers.
_YEAR = re.compile(r"(?:19|20)\d{2}")
# Number of rows below a header candidate in which to look for number cells.
_HEADER_PROBE_ROWS = 5


def _sheet_text(rows: list[list[str]], across: _Across | None = None) -> str:
    """Turn the rows of a sheet (or CSV) into Markdown. When a block separated by empty rows is a new table the tables are split,
    and one-cell text blocks between or after tables (titles, notes) are kept as paragraphs (`_sheet_segments`). If there is
    nowhere to split, the whole sheet is one table as before.

    Deliberate: a single empty row alone does not split. A single table with empty rows before the total or between groups is
    common. In a report layout, the '2. 인원' table below the '1. 매출' table became data rows of the first table, so the header
    '부서 | 정원 | 현원' ended up in the columns '사업부 | 1월 | …'
    (Pinned by: test_spreadsheet.py::TestSeveralTablesInOneSheet). `across` holds the horizontal merges to fill into the data rows
    of each table (`_fill_across`).
    """
    left = min((next(column for column, value in enumerate(row) if value) for row in rows if any(row)), default=0)
    segments = _sheet_segments(rows, left)
    if segments is None:
        _fill_across(rows, across)
        return join_blocks(_table_blocks(rows, across))

    parts = []

    for is_table, segment in segments:
        lead = _leading_note_rows(segment, left) if is_table else len(segment)
        parts.extend(_note_text(row) for row in segment[:lead])
        if lead < len(segment):
            _fill_across(segment[lead:], across)
            parts.extend(_table_blocks(segment[lead:], across))
    return join_blocks(parts)


def _table_blocks(rows: list[list[str]], across: _Across | None) -> list[str]:
    """Render one table (a block separated by empty rows). Summary cells to the right of the header row become a paragraph above
    the table (`_header_summary`), and side-by-side tables whose every row is separated by empty columns are rendered separately
    (`_side_by_side`)."""
    summary, rows = _header_summary(rows)
    tables = [render_table(_header_without_level_mark(table)) for table in _side_by_side(rows, across)]
    return [summary, *tables] if summary else tables


def _header_row(rows: list[list[str]]) -> int | None:
    """Index of the table's header row: the first row with two or more filled cells. One-cell rows above it are title rows
    (`render_table` moves them into paragraphs above the table)."""
    return next((index for index, row in enumerate(rows) if sum(1 for value in row if value) >= 2), None)


def _is_number_or_formula(value: str) -> bool:
    text = value.strip()
    return text.startswith("=") or (_NUMBER_LIKE.fullmatch(text) is not None and _YEAR.fullmatch(text) is None)


def _header_summary(rows: list[list[str]]) -> tuple[str, list[list[str]]]:
    """If the right end of the header row — the columns that are empty in every data row below — holds a number or formula (a
    '총원 | 26' placed next to the header), detach those cells and return them as paragraph text. Otherwise return empty text and
    the rows unchanged.

    The summary value became a column name, as in '| 본부 | 팀 | 인원 | 총원 | 26 |'
    (Pinned by: test_core_sheet_layouts.py::TestSummaryCellsRightOfTheHeader). Deliberate: an empty column without a number or
    formula ('비고') and year column names ('2023') stay column names, and this applies only when the remaining header looks like a
    header (`_looks_like_header`) — the heading cells of a quotation ('수신 | … | 견적일 | 2024-03-05') are not a table.
    """
    index = _header_row(rows)
    data = [row for row in rows[index + 1:] if any(row)] if index is not None else []
    if not data:
        return "", rows

    width = _width(data)
    header = rows[index]
    extra = [value.strip() for value in header[width:] if value]
    if not any(map(_is_number_or_formula, extra)) or not _looks_like_header(tuple(header[:width]), data[:_HEADER_PROBE_ROWS]):
        return "", rows
    return " ".join(extra), [*rows[:index], header[:width], *rows[index + 1:]]


def _side_by_side(rows: list[list[str]], across: _Across | None) -> list[list[list[str]]]:
    """If the column groups into which empty columns divide every row (header included) are each a table standing on its own
    (`_is_side_table`), cut the rows per group into several tables; otherwise `[rows]`.

    Two tables placed side by side with an empty column between them were merged into one table and their rows paired up — '김대리'
    was read as the person in charge of the Seoul branch (Pinned by: test_core_sheet_layouts.py::TestSideBySideTables).
    Deliberately narrow: every group must have two or more columns, the group's header row (`_header_row`) must have two or more
    cells that are all non-numeric text, and the data cells of the first column must contain a non-numeric name for it to be a
    table. A group with numbers only (right of a spacer column between '1월 | 2월'), a one-column group and an empty column spanned
    by a horizontal merge belong to the same table. Known accepted gap: a single table with name columns on both sides and a spacer
    column ('성명 | 부서 | | 비상연락 | 관계') is split as well — the row order is unchanged, so the pairing survives by position.
    """
    filled = [row for row in rows if any(row)]
    width = _width(filled)
    covered = {column for row in filled for first, end, _ in (across or {}).get(id(row), []) for column in range(first, end)}
    used = [column in covered or any(column < len(row) and row[column] for row in filled) for column in range(width)]
    groups: list[tuple[int, int]] = []

    for column, is_used in enumerate(used):
        if not is_used:
            continue
        if groups and groups[-1][1] == column:
            groups[-1] = (groups[-1][0], column + 1)
        else:
            groups.append((column, column + 1))
    if len(groups) < 2:
        return [rows]

    tables = [[row[start:end] for row in filled if any(row[start:end])] for start, end in groups]
    return tables if all(map(_is_side_table, tables)) else [rows]


def _is_side_table(rows: list[list[str]]) -> bool:
    """Whether one group of side-by-side tables is a table standing on its own (`_side_by_side`): two or more columns, a header of
    two or more non-numeric text cells, a non-numeric name in the first column."""
    index = _header_row(rows)
    if _width(rows) < 2 or index is None or any(map(_is_number_or_formula, filter(None, rows[index]))):
        return False
    return any(row and row[0] and not _is_number_or_formula(row[0]) for row in rows[index + 1:])


def _header_without_level_mark(rows: list[list[str]]) -> list[list[str]]:
    """Remove the indent-level mark (`_level_marked`) from the first text cell of the table's first non-empty row (the header).

    A header is not a sub-item. A header cell centred with spaces ('  구분') or given an indent format became '└ 구분'
    (Pinned by: test_core_spreadsheet.py::TestLeadingSpacesThatAreNotIndentation). The rows are not modified; a changed list is
    returned.
    """
    index = next((index for index, row in enumerate(rows) if any(row)), None)
    if index is None:
        return rows
    header = list(rows[index])
    first = next(column for column, value in enumerate(header) if value)
    mark = _LEVEL_MARK.match(header[first])
    if mark is None:
        return rows
    header[first] = header[first][mark.end():]
    return [*rows[:index], header, *rows[index + 1:]]


def _sheet_segments(rows: list[list[str]], left: int) -> list[tuple[bool, list[list[str]]]] | None:
    """Group the blocks separated by empty rows into (is a table, rows) segments. None if there is only one segment (nowhere to
    split).

    A one-cell text block (a block of `_is_note_row` rows only) is a paragraph when a new table follows or the sheet ends, and rows
    of the table when the same table continues. A table block is a new table only when `_starts_new_table` holds.
    """
    blocks: list[list[list[str]]] = [[]]

    for row in rows:
        if any(row):
            blocks[-1].append(row)
        elif blocks[-1]:
            blocks.append([])
    blocks = [block for block in blocks if block]
    if len(blocks) < 2:
        return None

    segments: list[tuple[bool, list[list[str]]]] = []
    table: list[list[str]] | None = None
    notes: list[list[str]] = []

    for block in blocks:
        if all(_is_note_row(row, left) for row in block):
            notes.extend(block)
            continue
        if table is not None and not _starts_new_table(table, block, left, titled=bool(notes)):
            table.extend([*notes, *block])
            notes = []
            continue
        if table is not None:
            segments.append((True, table))
        if notes:
            segments.append((False, notes))
        table, notes = list(block), []

    if table is None:
        return None
    segments.append((True, table))
    if notes:
        segments.append((False, notes))
    return segments if len(segments) > 1 else None


def _is_note_row(row: list[str], left: int) -> bool:
    """A row in which only the cell in the sheet's leftmost column (`left`) is filled and its text is not a number or formula (a
    table title, or a note such as '※ 단위: 백만원'). A total number standing alone, or a single cell in another column (a value of
    that column), is a row of the table."""
    filled = [(column, value) for column, value in enumerate(row) if value]
    if len(filled) != 1 or filled[0][0] != left:
        return False
    text = filled[0][1].strip()
    return not text.startswith("=") and _NUMBER_LIKE.fullmatch(text) is None


def _leading_note_rows(rows: list[list[str]], left: int) -> int:
    count = 0
    while count < len(rows) and _is_note_row(rows[count], left):
        count += 1
    return count


def _note_text(row: list[str]) -> str:
    return next(value for value in row if value).strip()


def _starts_new_table(table: list[list[str]], block: list[list[str]], left: int, titled: bool) -> bool:
    """Whether the block `block` after an empty row is a new table different from the preceding one (`table`). True when the
    block's first table row (after its leading title rows) equals the preceding table's header, or when it looks like a header
    (`_looks_like_header`) and either a title precedes it (inside the block or as the one-cell block right before, `titled`) or
    the column count differs.

    Deliberate: even if it looks like a header, no split happens without a title when the column count is the same — a data row
    with text ('결시') in a number column that comes after an empty row cannot be told apart from a header. Known accepted gap: a
    text-only table (one without number columns) is split only when the headers are equal.
    """
    lead = _leading_note_rows(block, left)
    body = block[lead:]
    first = _trimmed(body[0])
    table_body = table[_leading_note_rows(table, left):]
    if first == _trimmed(table_body[0]):
        return True
    if not _looks_like_header(first, body[1:1 + _HEADER_PROBE_ROWS]):
        return False
    return titled or lead > 0 or _width(body) != _width(table_body)


def _trimmed(row: list[str]) -> tuple[str, ...]:
    values = list(row)
    while values and not values[-1]:
        values.pop()
    return tuple(values)


def _width(rows: list[list[str]]) -> int:
    return max((len(_trimmed(row)) for row in rows), default=0)


def _looks_like_header(row: tuple[str, ...], below: list[list[str]]) -> bool:
    """A row with two or more filled cells, all non-numeric text (years count as text), where at least one of those text cells has
    a number in its column in the rows below."""
    filled = [(column, value.strip()) for column, value in enumerate(row) if value]
    if len(filled) < 2 or any(_NUMBER_LIKE.fullmatch(value) and not _YEAR.fullmatch(value) for _, value in filled):
        return False
    return any(column < len(other) and _NUMBER_LIKE.fullmatch(other[column].strip()) is not None
               for column, _ in filled for other in below)


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


@dataclass
class _SheetRows:
    """Sheet row number (1-based) → cell texts of the row as read. `offset` is the number of leading empty columns removed from
    the rows (`_first_used_column`)."""

    rows: dict[int, list[str]]
    offset: int = 0

    def values(self, row: int) -> list[str]:
        return self.rows.get(row) or []

    @staticmethod
    def address(row: int, column: int) -> str:
        return f"{get_column_letter(min(max(column, 1), 16384))}{row}"

    def place(self, row: int, column: int) -> str:
        """`cell C2: 제품A` — the cell address and the first text cell of that row other than that cell (one that does not look like
        a number). Tables carry no row/column numbers, so the address alone does not tell which row it is. Just the address if the
        row was not read or has no text cell."""
        values, index = self.values(row), column - 1 - self.offset
        label = next((value for position, value in enumerate(values)
                      if value and position != index and not _NUMBER_LIKE.fullmatch(value)), "")
        return f"cell {self.address(row, column)}: {label}" if label else f"cell {self.address(row, column)}"

    def target(self, row: int, column: int) -> str:
        """The text of the commented cell and the first text cell of its row: `2월: 120`. Just the cell text if that cell is the
        first text cell."""
        values, index = self.values(row), column - 1 - self.offset
        value = values[index] if 0 <= index < len(values) else ""
        label = next((cell for position, cell in enumerate(values)
                      if cell and position != index and not _NUMBER_LIKE.fullmatch(cell)), "")
        if label and next((position for position, cell in enumerate(values) if cell), index) >= index:
            label = ""  # the commented cell is the first text cell of the row
        return f"{label}: {value}" if label and value else value or label


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
            text = _chart_text(archive, names, kind, media.get(target, ""))

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


# (row, column (1-based), author, text, replies [(author, text)])
_CellComment = tuple[int, int, str, str, list[tuple[str, str]]]


def _comment_block(comments: list[_CellComment], row_at: _SheetRows, extraction: Extraction, options: ConvertOptions) -> str:
    """Turn cell comments, in cell order (top→bottom, left→right), into `> Comment(author, cell E5, on "row text: cell text"): text`
    lines (`_shared.comment_line`).

    Comments were dropped without a warning, so review remarks could not be seen (same direction as DOCX/PPTX/HWPX comments; they
    are placed at the end of the sheet. Pinned by:
    test_spreadsheet_notes_pictures_and_times.py::TestXlsxCellComments, TestXlsCellComments). Excel writes the author as an
    `author:` line in front of the comment text, so that is removed. The text counts toward the character limit (whether it was
    exceeded is reported by `extraction.count`).
    """
    lines = []

    for row, column, author, text, replies in sorted(comments, key=lambda comment: comment[:2]):
        text = text.strip()
        if author and text.startswith(f"{author}:"):
            text = text[len(author) + 1:]
        if not text.strip():
            continue
        lines.append(comment_line(author, text, row_at.target(row, column), cell=row_at.address(row, column)))
        lines.extend(comment_line(name, reply, reply=True) for name, reply in replies if reply.strip())

    block = "\n".join(lines)
    if block:
        extraction.count(solid_length(block), options)
    return block


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
_CHECKED, _UNCHECKED = "☒", "☐"  # same characters as DOCX form fields, HWPX check boxes and HTML
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

    checked = set()
    for element in _CONTROL_ELEMENT.finditer(archive.read(sheet_path)):
        shape, relationship = _CONTROL_SHAPE_ID.search(element.group()), _CONTROL_RELATIONSHIP.search(element.group())
        path = properties.get(relationship.group(1).decode("utf-8", errors="replace")) if relationship else None
        root = parse_part(archive.read(path)) if shape and path in names else None
        if root is not None and root.get("checked") == "Checked":
            checked.add(shape.group(1).decode())
    return checked


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
    """A chart placed on a sheet → "[Chart] title" + a table of the stored values (the same `_shared.render_chart` as PPTX/DOCX).
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
    targets = _related(archive, names, parts["/richValueRel"]) if relations is not None else {}
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
        for part in _related(archive, names, sheet).values():
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


def _fill_merges(sheet: object, rows: list[list[str]], numbers: list[int], width: int, offset: int = 0) -> _Across:
    """`numbers` holds the sheet row number of each row in rows (ascending). If the top row of a merge is not in rows there is no
    value either. `offset` is the number of leading empty columns removed from the rows (`_first_used_column`) — a merge starting
    in those columns has an empty first cell and no value to fill."""
    across: _Across = {}
    # Merges of upper rows first — a left cell filled earlier by a vertical merge also serves as the label cell of a horizontal
    # merge (`_repeat_merge`)
    for merged in sorted(sheet.merged_cells.ranges, key=lambda merged: (merged.min_row, merged.min_col)):
        top = bisect_left(numbers, merged.min_row)
        if top < len(numbers) and numbers[top] == merged.min_row and merged.min_col > offset:
            _repeat_merge(rows, top, bisect_right(numbers, merged.max_row), merged.min_col - 1 - offset,
                          min(merged.max_col - offset, width), across)
    return across


def _repeat_merge(rows: list[list[str]], top: int, bottom: int, left: int, right: int, across: _Across) -> None:
    """Repeat a vertical merge value in the rows below (`_repeat_down`), and for a horizontal merge with text to its left in the
    same row collect the spanned cells in `across`.

    `top`/`left` are 0-based; `bottom`/`right` are exclusive row/column numbers (`right` is already cut at the column limit).
    Horizontally spanned cells are not filled here — table splitting and title-row detection (`_sheet_text`) see the rows as
    before, and filling happens afterwards in data rows only (`_fill_across`).
    """
    _repeat_down(rows, top, bottom, left)
    if right <= left + 1 or top >= len(rows) or left >= len(rows[top]) or not rows[top][left] or not any(rows[top][:left]):
        return
    for row_index in range(top, min(bottom, len(rows))):
        if rows[row_index]:
            across.setdefault(id(rows[row_index]), []).append((left + 1, right, rows[top][left]))


def _repeat_down(rows: list[list[str]], top: int, bottom: int, left: int) -> None:
    """Repeat a vertical merge value in the rows below (same rule as markdown.TableGrid).

    `top`/`left` are 0-based; `bottom` is an exclusive row number. A short row that has no cells up to the merge column (XLS reads
    each row only up to that row's cells) is extended to that column — the top row has a value there, so it is within the column
    limit.
    """
    if top >= len(rows) or left >= len(rows[top]) or not rows[top][left]:
        return
    value = rows[top][left]
    for row_index in range(top + 1, min(bottom, len(rows))):
        row = rows[row_index]
        if not row:
            continue
        if left >= len(row):
            row.extend([""] * (left + 1 - len(row)))
        if not row[left]:
            row[left] = value


def _fill_across(table: list[list[str]], across: _Across | None) -> None:
    """Repeat the horizontal merge values of a table (the rows of one table split out of a sheet) over the spanned cells. Only when
    every spanned column is a column that has text in a row above (header and so on).

    A merge spanning March–April with '육아휴직' in a data row was left in the first cell only, so the other month read as blank
    (Pinned by: test_core_spreadsheet.py::TestHorizontalMergesInDataRows). Deliberately narrow: the first row of the table (header
    or title row) has no row above it and is not filled; merges that start in the leftmost column or have nothing to their left
    (table title, group name '[개발팀]') are not collected (`_repeat_merge`); and merges spanning into unnamed columns (a
    quotation's '합계금액 | 일금 … 원정' spanning into empty columns) are not filled — repeating a value in cells without a column
    name is noise. This is done after table splitting so that the filled cells do not change the shape of the table.
    """
    if not across:
        return
    named: set[int] = set()

    for row in table:
        filled = {column for column, value in enumerate(row) if value}
        for first, end, value in across.get(id(row), []):
            if not all(column in named for column in range(first, end)):
                continue
            if end > len(row):
                row.extend([""] * (end - len(row)))
            for column in range(first, end):
                row[column] = row[column] or value
        named |= filled


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
    kept by `_clock_text`."""
    pattern = (number_format or "[h]:mm:ss").lower()
    seconds = round(value.total_seconds())
    sign, seconds = ("-" if seconds < 0 else ""), abs(seconds)
    if number_format and _FORMAT_QUOTED.search(_first_section(number_format)):
        rounded = seconds if _shows_seconds(number_format) else (seconds + 30) // 60 * 60
        clock = _clock_text(rounded, number_format, elapsed=True)
        if clock is not None:
            return sign + clock
    units = {match.group(0)[1] for match in _ELAPSED_FORMAT.finditer(pattern)}  # a colour name (`[Magenta]`) is not an elapsed section
    if units == {"s"}:
        return f"{sign}{seconds}"
    if "h" not in units and "m" in units:
        return f"{sign}{seconds // 60}:{seconds % 60:02d}"
    if "s" not in _FORMAT_QUOTED.sub("", pattern):
        minutes = (seconds + 30) // 60
        return f"{sign}{minutes // 60}:{minutes % 60:02d}"
    return f"{sign}{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _shows_seconds(number_format: str) -> bool:
    """Whether the format shows seconds (s). Quoted text, backslash-escaped characters and bracket sections (elapsed seconds `[ss]`
    do count as seconds) are not format codes."""
    codes = _FORMAT_ESCAPED.sub("", _FORMAT_BRACKET.sub(
        lambda match: match.group(0) if _ELAPSED_FORMAT.fullmatch(match.group(0)) else "", _FORMAT_QUOTED.sub("", number_format)))
    return "s" in codes.lower()


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
    decimals, optional = len(places), len(places) - len(places.rstrip("#"))  # trailing `#` decimal places are not shown when they are 0
    try:
        number = _rounded(value, percent, scale, decimals)
    except InvalidOperation:
        return None  # a format with hundreds of decimal places — the original value is used
    text = f"{number:,.{decimals}f}" if "," in whole else f"{number:.{decimals}f}"
    # The sign goes before the leading literals (currency symbol included) (Excel: -₩1,200). For a value that used the negative
    # section the sign is shown by the format's literals.
    sign = "-" if text.startswith("-") and not signed_by_format else ""
    integer, dot, rest = text.removeprefix("-").partition(".")
    if optional:
        rest = rest[:decimals - optional] + rest[decimals - optional:].rstrip("0")
        dot = dot if rest else ""  # 12 under `0.###` → 12 (same as LibreOffice)
    if any(char not in "0#?," for char in whole) and "," not in whole:
        # A format with literals between digit places (phone number `0##\-####\-####`, date shape `0000\-00\-00`) fills the places
        # from the right and inserts the literals. Dropping the literals turned 010-1234-5678 into 1012345678
        # (Pinned by: test_quality.py::TestSpreadsheetNumberFormats).
        integer = _fill_digits(whole, literals[literals_before:], integer)
    elif whole.count("0") > 1 and "," not in whole:
        # The 0s of the integer part are the minimum number of digits (employee number / postal code 001234 written as `000000`)
        integer = integer.zfill(whole.count("0"))
    elif not whole and integer == "0":
        integer = ""  # for a format without integer places Excel does not show the 0 either (0.5 under `.00` → .50)
    return f"{sign}{prefix}{integer}{dot}{rest}{'%' if percent else ''}{suffix}"


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


def _xls_rich_text(text: str, runs: list[tuple[int, int]], escapement: Callable[[int], int | None]) -> str:
    """Rich text of an XLS cell. `runs` are (character position, font index) pairs; that font applies from that position to the
    next one (before the first position it is the cell's font). A font escapement of 1 is superscript and 2 is subscript
    (Pinned by: test_spreadsheet.py::TestSuperscriptInCells)."""
    runs = sorted(runs)
    pieces = [text[:runs[0][0]]] if runs else [text]
    for (start, font), (end, _) in zip(runs, [*runs[1:], (len(text), 0)], strict=True):
        mark = {1: SUPERSCRIPT, 2: SUBSCRIPT}.get(escapement(font))
        pieces.append(mark_script(text[start:end], mark) if mark else text[start:end])
    return resolve_scripts("".join(pieces))


def _xls_escapement(book: xlrd.book.Book, font_index: int) -> int | None:
    try:
        return book.font_list[font_index].escapement
    except (AttributeError, IndexError, TypeError):
        return None


def _xls_number_format(book: xlrd.book.Book, cell: xlrd.sheet.Cell) -> str | None:
    try:
        return book.format_map[book.xf_list[cell.xf_index].format_key].format_str
    except (AttributeError, IndexError, KeyError, TypeError):
        return None


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


def _xls_cell_text(cell: xlrd.sheet.Cell, book: xlrd.book.Book) -> str:
    number_format = _xls_number_format(book, cell)
    return format_cell(_xls_value(cell, book.datemode, number_format), number_format)


def _xls_value(cell: xlrd.sheet.Cell, datemode: int, number_format: str | None) -> object:
    """XLS cell value. Among date cells, an elapsed-time format (`[h]:mm`) becomes a duration and a value below 1 a time only (same
    rule as the XML spreadsheet); error cells become the error text (#REF! and so on)
    (Pinned by: test_spreadsheet.py::TestXlsValues). A number cell with a weekday-only format is read as a date as well. So is a
    format consisting only of an elapsed section (`[ss]`, `[mm]`, `[h]`) — xlrd decides after removing bracket sections and hands
    it over as a number cell, so the serial number was emitted (XLSX gives the cumulative value; Pinned by:
    test_spreadsheet.py::TestXlsValues::test_number_cell_with_only_an_elapsed_section_is_a_duration)."""
    if cell.ctype == xlrd.XL_CELL_DATE and cell.value == 0 and number_format and _shows_date(number_format):
        # 0 under a date format is '1900-01-00' in Excel. Returned as a number, not the time '00:00:00'
        # (`_is_serial_zero_shown_as_date`)
        return cell.value
    elapsed = bool(number_format and _ELAPSED_FORMAT.search(number_format))
    if cell.ctype == xlrd.XL_CELL_DATE or cell.ctype == xlrd.XL_CELL_NUMBER and (
            elapsed and is_date_format(number_format) or _is_weekday_only(number_format)):
        try:
            # A value that does not fit in a duration (a billion days or more) raises OverflowError. The whole document was rejected
            # as 'corrupted'. The original value is shown
            # (Pinned by: test_spreadsheet.py::TestXlsValues::test_elapsed_time_value_too_large_for_a_duration_is_shown_as_the_number).
            if elapsed:
                return timedelta(days=cell.value)
            if cell.value < 0:
                return cell.value  # a negative serial number is not a date. Its displayed shape: `format_cell` (`_negative_serial_text`)
            value = xlrd.xldate.xldate_as_datetime(cell.value, datemode)
        except (xlrd.xldate.XLDateError, OverflowError, ValueError):
            return cell.value
        return value.time() if 0 <= cell.value < 1 else value
    if cell.ctype == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    if cell.ctype == xlrd.XL_CELL_ERROR:
        return xlrd.error_text_from_code.get(cell.value, "#ERR!")
    if cell.ctype in (xlrd.XL_CELL_BLANK, xlrd.XL_CELL_EMPTY):
        return None
    return cell.value


def _warn_once(warnings: list[str], message: str) -> None:
    if message not in warnings:
        warnings.append(message)
