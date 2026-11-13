"""Excel 2003 XML Spreadsheet → one Markdown table per sheet."""

from collections.abc import Iterator
from datetime import date, datetime, timedelta
import re

from lxml import etree
from openpyxl.styles.numbers import is_date_format
from openpyxl.utils.cell import get_column_letter
from openpyxl.utils.datetime import from_excel

from opendockit.exceptions import CorruptedDocumentError
from opendockit.extractors._encoding import decode_text, decoding_warning
from opendockit.extractors._inline import SUBSCRIPT, SUPERSCRIPT, mark_script, resolve_scripts
from opendockit.extractors._package import _XML_PARSER, decimal_int
from opendockit.extractors._sheet_format import _ELAPSED_FORMAT, _MAX_FORMAT_LENGTH, _XML_GENERAL_DATE, _format_value, format_cell
from opendockit.extractors._sheet_layout import _append_sheet, _collect_rows, _level_marked
from opendockit.models import ConvertOptions, Extraction


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


def _ss_int(element: etree._Element, name: str, fallback: int, invalid: set[str], minimum: int = 0) -> int:
    """Integer attribute (`ss:Index`, `ss:Span`, `ss:MergeAcross`, `ss:MergeDown`). Missing or empty gives `fallback`; a value that is
    not a decimal number (`abc`, `²`) also gives `fallback` and records the name in `invalid` — a bare `int()` rejected the whole
    document over a single attribute (Pinned by: test_spreadsheet.py::TestXmlSpreadsheetIntegerAttributes). So does a number
    below `minimum` — a cell at `ss:Index="0"`, a column that does not exist, was dropped without a warning
    (Pinned by: test_audit_spreadsheet.py::TestXmlCellAtPositionZero)."""
    value = (_ss(element, name) or "").strip()
    if not value:
        return fallback
    # None also for more than 4,300 digits, which `int()` refuses although they are decimal
    # (Pinned by: test_audit_spreadsheet.py::TestIntegerAttributesTooLongToRead).
    number = decimal_int(value, None)
    if number is not None and number >= minimum:
        return number
    invalid.add(name)
    return fallback


def _xml_number_format(number_format: str | None) -> str | None:
    """A named format becomes its format string; a name missing from the table is treated as the General format (None) — the
    letters of the name were being read as date codes
    (Pinned by: test_spreadsheet.py::TestXmlSpreadsheetNamedFormats::test_unknown_format_name_is_not_read_as_a_date)."""
    if not number_format or len(number_format) > _MAX_FORMAT_LENGTH:
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
            column = _ss_int(cell, "Index", column + 1, invalid, minimum=1)
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
    (the same `_inline.mark_script` as XLSX rich text). Joining the characters alone turned `m²` into `m2` and `10⁶원` into `106원`
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
