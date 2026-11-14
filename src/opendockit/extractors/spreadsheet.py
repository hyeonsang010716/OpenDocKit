"""XLSX → one Markdown table per sheet, with the pictures, charts, text boxes, comments and form controls of the sheet."""

from collections import deque
from collections.abc import Iterator
from datetime import datetime, time
import html
from io import BytesIO
import re
import zipfile
from zipfile import BadZipFile

from lxml import etree
import openpyxl
from openpyxl.cell.rich_text import CellRichText, TextBlock
from openpyxl.chartsheet import Chartsheet
from openpyxl.formula.translate import Translator
from openpyxl.styles.numbers import BUILTIN_FORMATS
from openpyxl.utils.cell import coordinate_to_tuple, range_boundaries
from openpyxl.utils.datetime import from_excel
from openpyxl.utils.exceptions import InvalidFileException
from openpyxl.worksheet.formula import ArrayFormula

from opendockit.exceptions import CorruptedDocumentError, UnsupportedFormatError
from opendockit.extractors._inline import SUBSCRIPT, SUPERSCRIPT, mark_script, resolve_scripts
from opendockit.extractors._openpyxl_reading import _RAW_OUT_OF_RANGE_SERIALS, _READING, _CellPicture, _CellReading, _is_number
from opendockit.extractors._package import _XML_PARSER
from opendockit.extractors._sheet_format import _format_value, _format_within_length, _is_weekday_only, _shows_date, format_cell
from opendockit.extractors._sheet_layout import (
    _MAX_MERGED_CELLS,
    _Across,
    _append_sheet,
    _collect_rows,
    _comment_block,
    _fill_merges,
    _level_marked,
    _SheetRows,
    _until_long_empty_gap,
)
from opendockit.extractors._xlsx_parts import (
    _RELATIONSHIP_ID,
    _RELATIONSHIPS,
    _SPREADSHEET_MAIN,
    _STRICT_SPREADSHEET_MAIN,
    _cell_picture_text,
    _drawing_blocks,
    _picture_reference_bytes,
    _related,
    _rich_values,
    _workbook_path,
    _xlsx_comments,
    _xlsx_people,
)
from opendockit.models import ConvertOptions, Extraction, quiet_warnings


# When the sheet XML (uncompressed) totals at most this size, the workbook is fully loaded. Only a full load carries merged-cell
# information; workbooks with many cells are streamed (read_only) to protect memory (fully loading a 130k-row, 60MB sheet takes
# 3.6s and 570MB).
# Deliberate: the split is by sheet XML size, not file size. A workbook with pictures is large because of the pictures, so splitting
# by file size sent it to streaming even with only a few rows of cells, losing merges and pictures
# (Pinned by: test_image.py::test_xlsx_that_is_large_only_because_of_its_picture_keeps_merges_and_picture).
_FULL_LOAD_MAX_SHEET_BYTES = 20 * 1024 * 1024


# In a full load openpyxl turns the pivot caches the workbook points to (definition + records) entirely into objects. The conversion
# does not use those values, yet a 2.8MB file with 9MB of records took 14s and 1.5GB (its sheet XML was 18MB, so the size threshold
# did not catch it). When the uncompressed sizes total more than this, the workbook is streamed, which does not read pivot caches
# (1MB ≈ 1.5s)
# (Pinned by: test_office.py::TestReportedGaps::test_xlsx_with_a_large_pivot_cache_is_streamed).
_MAX_PIVOT_CACHE_BYTES = 1024 * 1024
_PIVOT_CACHE_DEFINITION = "/pivotCacheDefinition"
# Merge ranges in sheet XML (`<mergeCell ref="A1:B2"/>`). An element name cannot be written another way in XML, but an attribute
# value can vary in quotes, whitespace and character references, so attributes are skipped one at a time to find ref, and the value
# is decoded with html.unescape. Hyperlink ranges are not counted: they are not parsed while reading (`_SHEET_PARTS_LEFT_UNREAD`),
# so they cost nothing, and counting them streamed the sheet and lost its merges
# (Pinned by: test_office.py::TestXlsx::test_huge_hyperlink_range_is_read_without_expanding_it).
_MERGE_REF = re.compile(rb"<(?:[\w.-]+:)?mergeCell\b(?:\s+[\w:.-]+\s*=\s*(?:\"[^\"]*\"|'[^']*'))*?\s+ref\s*=\s*(?:\"([^\"]*)\"|'([^']*)')")


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
        # Only sheet XML counts: relationship parts and other files kept under `xl/worksheets/` are not sheets, yet they were
        # counted toward the size threshold and scanned for formulas and merges
        # (Pinned by: test_audit_spreadsheet_fixes.py::TestOnlySheetPartsAreMeasured).
        sheets = [info for info in archive.infolist()
                  if info.filename in listed or _is_sheet_xml_name(info.filename)]
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


def _load_workbook(source: BytesIO, **options: bool) -> openpyxl.Workbook:
    """openpyxl load. The UserWarning etc. that openpyxl emits for a workbook without a default style (made by Apache POI) is
    suppressed by `extract_xlsx` while reading — openpyxl uses a default style instead, so the conversion is unaffected. The cell
    interpretation counts start from zero on every re-read."""
    reading = _READING.get()
    if reading is not None:
        reading.malformed = reading.unread_rich = 0
    return openpyxl.load_workbook(source, **options)


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


def _is_sheet_xml_name(name: str) -> bool:
    """Whether a ZIP entry is sheet XML at the usual place (`xl/worksheets/sheet1.xml`, not `xl/worksheets/_rels/…`)."""
    return name.startswith("xl/worksheets/") and name.endswith(".xml") and "/" not in name.removeprefix("xl/worksheets/")


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
            for cell in _sheet_cells(stream):
                value = cell.findtext("{*}v") if cell.find("{*}f") is not None else None
                kind = cell.get("t", "n")
                if value is None or not value.strip():
                    continue
                if kind != "n" or _float_or_none(value) != 0:
                    return False
                zeros += 1
    return zeros > 0


def _sheet_cells(source: object) -> Iterator[etree._Element]:
    """The `<c>` elements of sheet XML, one at a time. Once the caller has looked at a cell it is emptied and removed from the tree
    together with the rows before it.

    Emptying the cell alone left an empty element per cell and row in the tree: a 13 MB workbook (223 MB of sheet XML, 600,000 rows)
    grew by 948 MB in the zero-result check, which was meant to read the sheet without keeping it
    (Pinned by: test_audit_spreadsheet_fixes.py::TestSheetScansKeepNoTree).
    """
    for _, cell in etree.iterparse(source, tag="{*}c", resolve_entities=False, no_network=True):
        yield cell
        cell.clear(keep_tail=False)
        row = cell.getparent()
        if row is None:
            continue
        while cell.getprevious() is not None:
            del row[0]
        data = row.getparent()
        while data is not None and row.getprevious() is not None:
            del data[0]


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

    for cell in _sheet_cells(BytesIO(xml)):
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


def _merged_cell_count(archive: zipfile.ZipFile, sheet_names: list[str]) -> int:
    """Total number of cells covered by the merge ranges of the sheets (stops once the cap is exceeded)."""
    total = 0
    for name in sheet_names:
        xml = archive.read(name)
        if b"mergeCell" not in xml:
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
