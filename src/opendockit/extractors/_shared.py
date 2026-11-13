"""Format pieces shared by several extractors: OOXML chart and SmartArt rendering, and auto-numbering strings.

- OOXML charts (`c:chartSpace`) use the same XML in PPTX, DOCX and HWPX (`Chart/chart*.xml`). A chart goes in as data, not as a
  picture. An LLM reads numbers rather than bar shapes. The new chart kinds (`cx:chartSpace`) use the same XML in PPTX and DOCX.
- SmartArt uses the same data part (`dgm:dataModel`) in PPTX and DOCX.
- Auto-numbering (1. 가. ① …) uses the same number shapes in DOCX, HWP and HWPX.
- Superscripts and subscripts come out in the same notation (`10^6`, `H_2O`) in every format.
- Struck-through text comes out in DOCX and PPTX in the same notation as HTML (`~~text~~`).
- Dutmal (annotation text) comes out in the same shape ("main(annotation)") in HWP and HWPX.
- Office equations (OMML) use the same XML in DOCX and PPTX and come out in the same notation (` $LaTeX$ `).
- Symbol-font (Symbol, Wingdings) characters are converted to Unicode with the same per-font tables in DOCX and PPTX.
- A table inside a footnote comes out in the same one-line shape ('cell / cell; cell / cell') in DOCX, HWP and HWPX.
- Hyperlinks and bold / italic come out by the same rules (`[text](url)`, `**bold**`, `*italic*`) in DOCX, PPTX and HTML.

The per-format extractors do not import each other. Only the pieces that must be shared, so that the same rule does not drift
between formats, live here.
"""

from bisect import bisect_right
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from io import BytesIO
import posixpath
import re
import shutil
import zipfile
import zlib

from lxml import etree

from opendockit.markdown import TableGrid, render_table, solid_length
from opendockit.models import IMAGE_MARKER, ConvertOptions, Extraction


_C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_CX = "http://schemas.microsoft.com/office/drawing/2014/chartex"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_DGM = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True)
# The 'off' values of an OOXML on/off value (ST_OnOff)
_OFF_VALUES = frozenset(("0", "off", "false"))
# Upper bound for list indent levels (PowerPoint and Word use 0-8)
# (Pinned by: test_office.py::TestPptx::test_absurd_paragraph_level_does_not_build_a_huge_indent).
MAX_LEVEL = 8
_SMARTART_LAYOUT_POINTS = {"pres", "parTrans", "sibTrans", "doc"}
# Charts drawn from pairs of horizontal and vertical values. They have no category axis.
_XY_CHARTS = {"scatterChart", "bubbleChart"}
# Name put in front of an axis title. Charts with value axes only (c:valAx — scatter, bubble) tell X from Y by the axis position
# (c:axPos).
_AXIS_LABELS = {"catAx": "Category axis", "dateAx": "Category axis", "valAx": "Value axis", "serAx": "Series axis"}
_HORIZONTAL_AXIS_POSITIONS = {"b", "t"}
# Text erased before looking for date/time letters in a display format: quoted text, bracketed sections (color, locale, condition;
# elapsed time [h] is kept), and the one character after a backslash, fill or repeat.
# Deliberate: a bracketed section holds no `[` (here and in `_FORMAT_TOKEN`). A `[` with no `]` was scanned to the end of the format
# from every `[`, for every chart point (a 20,000-character format on 40 points took 6 seconds)
# (Pinned by: test_audit_core.py::TestChartFormatWithManyOpenBrackets).
_FORMAT_LITERALS = re.compile(r'"[^"]*"|\[(?![hms]+\])[^\[\]]*\]|[\\_*].')
_LAST_DATE_SERIAL = 2_958_465  # 9999-12-31
# Longest display format that is applied (the longest Excel accepts). A format is read again for every point of a chart, so a
# 20,000-character format on 8,000 points (a 40 KB file) took 30 seconds. With a longer format the value stays as written, as with
# any format that is not handled (Pinned by: test_audit_core.py::TestChartFormatWithManyOpenBrackets).
_MAX_FORMAT_CHARS = 255
# Tokens of one section of a value display format (`_unit_format`): quoted text, the one character after a backslash, a currency
# symbol ([$₩-412]), other brackets (color, condition), fill/spacing (`*x`, `_x`), digit placeholders (0 # ? , .), any other character.
_FORMAT_TOKEN = re.compile(r'"([^"]*)"|\\(.)|\[\$([^\[\]-]*)(?:-[^\[\]]*)?\]|(\[[^\[\]]*\])|[_*].|([0#?,.])|(.)', re.DOTALL)
_FORMAT_COLORS = re.compile(r"\[(?:black|blue|cyan|green|magenta|red|white|yellow|color\s*\d{1,2})\]", re.IGNORECASE)
# Symbols Excel shows as literal text even without quotes. Formats with other characters (date y/m/d, exponent E, text @, % …)
# are not handled.
_FORMAT_PLAIN_LITERALS = frozenset("$-+()/: ")
# Upper bound on the magnitude of a value a format is applied to. Beyond it, exponent notation as in `_number` is better.
_UNIT_FORMAT_LIMIT = Decimal("1e15")
# Text of a text box inside a chart (c:userShapes → chart drawing part). Used to lay a unit ('(단위: 억원)', "unit: 100M KRW") or a
# source over the chart.
_CHART_DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/chartDrawing"
_RELATIONSHIP_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
# Namespaces of OOXML package relationships (.rels) and content types ([Content_Types].xml), and the main-part relationship type
# (`repair_package`).
_PACKAGE_RELATIONSHIPS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
_OFFICE_DOCUMENT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument"
_STRICT_OFFICE_DOCUMENT = "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument"
_XML_CONTENT_TYPES = {"application/xml", "text/xml"}
_CORE_PROPERTIES = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties"


def decimal_int[T](value: str | None, default: T, *, signed: bool = False) -> int | T:
    """Integer attribute value written in a file. `default` if it is not decimal digits (with `signed`, one leading '-' is accepted).

    `isdigit()` is also true for '²', so `int()` raised ValueError; and a number past 4,300 digits passes `isdecimal()` but `int()`
    raises ValueError (Python's integer string length limit), so one value got the whole document rejected — both count as odd values
    (Pinned by: test_office_robustness.py::TestHugeNumbersInOfficeDocuments, test_hwp_robustness.py::TestHugeNumbersInHangulDocuments).
    """
    if value is None:
        return default
    digits = value.removeprefix("-") if signed else value
    if not digits.isdecimal():
        return default
    try:
        return int(value)
    except ValueError:
        return default


def omitted_rows_warning(count: int) -> str:
    """Warning for rows dropped by the table grid cell limit (`markdown.TableGrid.omitted_rows`), shared by DOCX, PPTX, HWP and HWPX.
    The bottom rows used to vanish without a warning
    (Pinned by: test_office_robustness.py::TestSpansPastTheTable::test_docx_rows_left_out_by_the_grid_limit_are_reported)."""
    return f"The table is too large; the bottom {count} row(s) could not be converted."


class TableBudget:
    """Watches the character limit (`max_chars`) while a top-level table is read cell by cell (shared by DOCX, HWP and HWPX). Counting
    only per top-level block, the overrun was noticed only after a table of hundreds of thousands of rows had been rendered to the end
    (a 200,000-row DOCX: 33 s, 2.3 GB. Pinned by: test_office_table_char_limit.py).

    `Extraction.count` is not called — after the table is rendered the extractor counts again per top-level block, so it would be
    counted twice. Here we only report whether 'characters already counted + cell text read from this table (`solid_length`)' exceeded
    the limit, so that cell reading stops. The text rendered from a stopped table holds all the cell text read, so the later per-block
    count also exceeds the limit (it never counts more than the real amount — Pinned by: TestLimitIsExactAroundTables).

    Only cell text that actually lands in the table is counted. A cell placed where an earlier cell or an earlier cell's span already
    sits is not counted — DOCX: `TableGrid` drops it; HWP/HWPX: it moves to the next free slot of that row
    (`TableGrid.place_or_next`), but that slot is unknown here, so this errs toward undercounting. A span is taken to cover exactly what
    the file says — `TableGrid` only ever shrinks spans, so this errs toward undercounting. Once the memory of covered slots exceeds
    `_MAX_MARKS`, or a cell with an unknown position (`forget`) arrives, the remaining cells of that table are not counted (reading
    just does not stop; the result is the same).
    Known accepted gap: a table whose bottom rows are dropped by the grid cell limit (5 million cells) may stop early by counting rows
    that will be dropped — dropped rows require filling 5 million cells with characters below the limit, which real documents do not.
    """

    _MAX_MARKS = 1_000_000

    def __init__(self, extraction: Extraction, options: ConvertOptions) -> None:
        self._extraction = extraction
        self._limit = options.max_chars
        self._counted = 0
        self._taken: set[tuple[int, int]] = set()
        self._marks = self._MAX_MARKS

    def add(self, row: int, col: int, text: str, *, row_span: int = 1, col_span: int = 1) -> bool:
        """One cell placed at (row, column). True once the limit is exceeded — the signal not to read the remaining cells."""
        if self._limit is None:
            return False
        if self._marks >= 0 and (row, col) not in self._taken:
            row_span, col_span = max(row_span, 1), max(col_span, 1)
            self._marks -= row_span * col_span
            if self._marks >= 0:
                self._taken.update((row + dr, col + dc) for dr in range(row_span) for dc in range(col_span))
                self._counted += solid_length(text)
        return self._extraction.chars + self._counted > self._limit

    def forget(self) -> None:
        """A cell with an unknown position (`TableGrid.place_next`) arrived. Where it will sit is unknown, so the remaining cells of
        this table are not counted."""
        self._marks = -1


def parse_part(data: bytes) -> etree._Element | None:
    """Read part XML without entities or network access. A broken part gives None (only that object is skipped)."""
    try:
        return etree.fromstring(data, parser=_XML_PARSER)
    except etree.XMLSyntaxError:
        return None


# ---- Package -----------------------------------------------------------------------------------------------------


def open_package[T](opener: Callable[[BytesIO], T], data: bytes, warnings: list[str]) -> T:
    """Open an OOXML package with `opener` (`Package.open` of python-docx / python-pptx). If opening fails because of one missing or
    non-XML part (KeyError, XMLSyntaxError), open once more from a copy with that part and its relationship detached
    (`repair_package`) and leave a warning.

    The libraries read every part the relationships point to when opening, so a document with a sound body was rejected outright when
    one picture or one numbering definition was missing or docProps was not XML. No copy is made for a healthy document — inspection
    and rebuilding happen only when opening fails, so performance and output are unchanged
    (Pinned by: test_office.py::TestOneBrokenPartDoesNotRejectTheDocument).
    """
    try:
        return opener(BytesIO(data))
    except (KeyError, etree.XMLSyntaxError):
        repaired = repair_package(data)
        if repaired is None:
            raise
        warnings.append(repaired[1])
        return opener(BytesIO(repaired[0]))


def is_strict_package(data: bytes) -> bool:
    """Is the main-part relationship type in the package relationships (_rels/.rels) the Strict Open XML one (purl.oclc.org)? False
    if unreadable.

    A DOCX/PPTX saved as a 'Strict Open XML document' differs in every relationship type and namespace, so python-docx / python-pptx
    cannot find the main part. It is not corruption (the 'document structure cannot be read' error) but an unsupported format, so, to
    advise saving again, we look only when opening fails (no cost for healthy documents.
    Pinned by: test_office_revisions_and_numbering.py::TestStrictOpenXmlOfficeDocuments).
    """
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            root = _parse_member(archive, "_rels/.rels") if "_rels/.rels" in archive.namelist() else None
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError, EOFError, zlib.error):
        return False

    relationships = root.iter(f"{{{_PACKAGE_RELATIONSHIPS}}}Relationship") if root is not None else []
    return any(relationship.get("Type") == _STRICT_OFFICE_DOCUMENT for relationship in relationships)


def core_properties(package: object) -> dict[str, str]:
    """Title (`title`) and author (`author`) from the package's document properties (docProps/core.xml). Only what is written is
    included.

    `core_properties` of python-docx / python-pptx builds default properties (title 'Word Document' / 'PowerPoint Presentation') when
    the part is missing, so a title the document does not have came out as metadata (Pinned by: test_office.py::
    TestOfficeLineBreaksNestedFieldsAndMetadata::test_package_without_document_properties_reports_no_library_default_title). Document
    properties are an extra, not the body — for a core.xml whose root element has the wrong namespace the library raised
    AttributeError and the whole document was rejected. If unreadable, they are left empty
    (Pinned by: test_office_robustness.py::TestDocumentPropertiesAndSlideRoots).
    """
    try:
        package.part_related_by(_CORE_PROPERTIES)
    except KeyError:
        return {}
    try:
        properties = package.core_properties
        values = {"title": properties.title, "author": properties.author}
    except (AttributeError, KeyError, ValueError, TypeError):
        return {}
    return {name: value for name, value in values.items() if isinstance(value, str) and value}


def repair_package(data: bytes) -> tuple[bytes, str] | None:
    """A copy of the package with the relationships that point to missing parts or non-XML XML parts removed and those parts left
    out, plus the warning text. None if there is nothing to detach.

    Relationship targets are resolved against the folder of the part that owns the `.rels` (an empty target or a `../..` target
    outside the package is a missing part). A part with no content type also makes the library raise KeyError, so it counts as
    missing. A broken main part (officeDocument) is not repaired — that is real corruption. The copy stream-copies the parts (large
    pictures and videos are not loaded whole into memory) and recompresses them.
    """
    with zipfile.ZipFile(BytesIO(data)) as archive:
        names = set(archive.namelist())
        content_type_of = _content_type_lookup(archive, names)
        main_part = _main_part(archive, names)
        if content_type_of is None or main_part is None:
            return None

        # Check the target of every relationship and remove relationships whose target cannot be read. Each XML part is parsed only once
        readable: dict[str, bool] = {}
        rewritten: dict[str, bytes] = {}
        dropped: set[str] = set()
        for rels_name in sorted(name for name in names if _is_relationships(name)):
            root = _parse_member(archive, rels_name)
            if root is None:
                continue
            base = rels_name[: rels_name.index("_rels/")]
            removed = False
            for relationship in list(root):
                if relationship.tag != f"{{{_PACKAGE_RELATIONSHIPS}}}Relationship" or relationship.get("TargetMode") == "External":
                    continue
                target = _resolve_target(base, relationship.get("Target", ""))
                if target == main_part or _is_readable(archive, names, content_type_of, readable, target):
                    continue
                root.remove(relationship)
                dropped.add(target)
                removed = True
            if removed:
                rewritten[rels_name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        if not dropped:
            return None

        # Broken parts are left out of the copy together with their .rels; the rest is carried over as is (with duplicate names, the
        # last one, as when reading)
        excluded = {name for name in dropped if name in names}
        excluded |= {_relationships_of(name) for name in excluded} & names
        members = {info.filename: info for info in archive.infolist()}
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target_archive:
            for info in members.values():
                if info.filename in excluded:
                    continue
                if info.filename in rewritten:
                    target_archive.writestr(info.filename, rewritten[info.filename])
                    continue
                entry = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                entry.compress_type = zipfile.ZIP_DEFLATED
                with archive.open(info) as source, target_archive.open(entry, "w") as sink:
                    shutil.copyfileobj(source, sink)

    labels = sorted({posixpath.basename(name) or name for name in dropped})
    shown = ", ".join(labels[:4]) + (f" and {len(labels) - 4} more" if len(labels) > 4 else "")
    return buffer.getvalue(), (f"Read after detaching {len(dropped)} missing or broken part(s) ({shown}). "
                               "The contents of those parts (pictures, numbering, styles, etc.) may be missing.")


def _is_relationships(name: str) -> bool:
    return name.endswith(".rels") and posixpath.basename(posixpath.dirname(name)) == "_rels"


def _relationships_of(name: str) -> str:
    """Name of a part's relationships file: `word/document.xml` → `word/_rels/document.xml.rels`."""
    return posixpath.join(posixpath.dirname(name), "_rels", posixpath.basename(name) + ".rels")


def _resolve_target(base: str, target: str) -> str:
    """Relationship target (a path relative to the folder of the part that owns the .rels, or an absolute path starting with `/`)
    → name inside the package."""
    return posixpath.normpath(posixpath.join("/" + base, target)).lstrip("/")


def _parse_member(archive: zipfile.ZipFile, name: str) -> etree._Element | None:
    try:
        return etree.fromstring(archive.read(name), parser=_XML_PARSER)
    except etree.XMLSyntaxError:
        return None


def _content_type_lookup(archive: zipfile.ZipFile, names: set[str]) -> Callable[[str], str | None] | None:
    """Part name → content type (an Override first, otherwise the extension's Default). None if the content types part is missing
    or broken."""
    root = _parse_member(archive, "[Content_Types].xml") if "[Content_Types].xml" in names else None
    if root is None:
        return None
    defaults = {e.get("Extension", "").lower(): e.get("ContentType", "") for e in root.iter(f"{{{_CONTENT_TYPES}}}Default")}
    overrides = {e.get("PartName", "").lstrip("/").lower(): e.get("ContentType", "")
                 for e in root.iter(f"{{{_CONTENT_TYPES}}}Override")}

    def lookup(name: str) -> str | None:
        basename = name.rsplit("/", 1)[-1]
        extension = basename.rsplit(".", 1)[-1].lower() if "." in basename else ""
        return overrides.get(name.lower()) or defaults.get(extension) or None

    return lookup


def _main_part(archive: zipfile.ZipFile, names: set[str]) -> str | None:
    """The officeDocument target of the package relationships (_rels/.rels). None if missing or broken."""
    root = _parse_member(archive, "_rels/.rels") if "_rels/.rels" in names else None
    for relationship in root.iter(f"{{{_PACKAGE_RELATIONSHIPS}}}Relationship") if root is not None else []:
        if relationship.get("Type") == _OFFICE_DOCUMENT and relationship.get("TargetMode") != "External":
            return _resolve_target("", relationship.get("Target", ""))
    return None


def _is_readable(archive: zipfile.ZipFile, names: set[str], content_type_of: Callable[[str], str | None],
                 readable: dict[str, bool], name: str) -> bool:
    """Does the part exist, have a content type and, if it is an XML type, parse?"""
    content_type = content_type_of(name) if name in names else None
    if content_type is None:
        return False
    if not (content_type.endswith("+xml") or content_type in _XML_CONTENT_TYPES):
        return True
    if name not in readable:
        readable[name] = _parse_member(archive, name) is not None
    return readable[name]


# ---- Charts ------------------------------------------------------------------------------------------------------

def render_chart(chart_space: etree._Element, table: Callable[[list[list[str]]], str] = render_table,
                 load_related: Callable[[str], bytes | None] | None = None) -> str:
    """`c:chartSpace` → "[Chart] title" + axis title line + lines of the text boxes inside the chart + value table. `table` turns the
    value table's rows into text (`embedded_table` inside a table cell). `load_related` maps a relationship id of the chart part to
    that part's bytes (None if missing or unreadable).

    Category-axis charts (bar, line, pie …) merge every chart in the plot area (the bars + lines of a combo chart) into one row per
    category. Scatter and bubble charts have no categories, so each series puts its X/Y(/size) value pairs in rows
    (Pinned by: test_office.py::TestPptx::test_scatter_chart_values_are_kept, test_combo_chart_keeps_series_of_every_plot).
    Axis titles go on one line right under the title. Charts whose unit ("단위: 억원") is only in an axis title are common
    (Pinned by: test_office.py::TestChartAxes::test_axis_titles_are_kept_under_the_chart_title).
    Text boxes laid over the chart (c:userShapes) go one per line under the axis titles. The unit was lost in charts where
    '(단위: 억원)' is only in a text box
    (Pinned by: test_office_chart_units_and_ole.py::TestChartUserShapes).
    """
    chart = chart_space.find(f"{{{_C}}}chart")
    if chart is None:
        return ""
    title_element = chart.find(f"{{{_C}}}title")
    title = _title_text(title_element) if title_element is not None else ""

    category_series, xy_series = [], []
    plot_area = chart.find(f"{{{_C}}}plotArea")
    for plot in plot_area if plot_area is not None else []:
        name = etree.QName(plot).localname if isinstance(plot.tag, str) else ""
        if not name.endswith("Chart"):
            continue
        for series in plot.iterchildren(f"{{{_C}}}ser"):
            if name in _XY_CHARTS:
                xy_series.append((_series_order(series), series))
            else:
                category_series.append((_series_order(series), series, _shown_value_format(plot, series, plot_area)))

    # Series order is c:order regardless of the chart kind (in a combo chart bars and lines may alternate).
    category_series.sort(key=lambda item: item[0])
    xy_series = [series for _, series in sorted(xy_series, key=lambda item: item[0])]

    head = "\n".join(line for line in (f"[Chart] {title}".strip(), _axis_titles(plot_area),
                                       *_user_shape_lines(chart_space, load_related)) if line)
    tables = [_category_table([series for _, series, _ in category_series], table,
                              _is_flagged(chart_space.find(f"{{{_C}}}date1904")), _date_axis_format(plot_area),
                              [shown for _, _, shown in category_series]),
              _xy_table(xy_series, table)]
    return "\n\n".join(part for part in (head, *tables) if part)


def related_loader(part: object) -> Callable[[str], bytes | None]:
    """Relationship id of a python-docx / python-pptx part → bytes of the target part (`load_related` of `render_chart`). None for
    broken or external relationships."""
    def load(relationship: str) -> bytes | None:
        try:
            return getattr(part.rels[relationship].target_part, "blob", None)
        except (KeyError, ValueError, AttributeError):
            return None

    return load


def package_loader(load_part: Callable[[str], bytes | None], name: str) -> Callable[[str], bytes | None]:
    """Relationship id of the archive part `name` (`<folder>/_rels/<name>.rels`) → bytes of the target part (`load_related` for HWPX
    charts). `load_part` maps a name inside the archive to bytes. None if the relationships part is missing or broken, or the
    relationship is external."""
    def load(relationship: str) -> bytes | None:
        data = load_part(_relationships_of(name))
        root = parse_part(data) if data else None
        for element in root.iter(f"{{{_PACKAGE_RELATIONSHIPS}}}Relationship") if root is not None else []:
            if element.get("Id") == relationship and element.get("TargetMode") != "External":
                return load_part(_resolve_target(posixpath.dirname(name), element.get("Target", "")))
        return None

    return load


def _user_shape_lines(chart_space: etree._Element, load_related: Callable[[str], bytes | None] | None) -> list[str]:
    """Paragraph text of the text boxes in the chart drawing part (r:id of c:userShapes). The same text only once. An empty list if
    the relationship is missing or broken."""
    reference = chart_space.find(f"{{{_C}}}userShapes")
    relationship = reference.get(_RELATIONSHIP_ID) if reference is not None else None
    data = load_related(relationship) if load_related is not None and relationship else None
    drawing = parse_part(data) if data else None
    if drawing is None:
        return []
    lines = (line for body in drawing.iter(f"{{{_CHART_DRAWING}}}txBody") for paragraph in body.iter(f"{{{_A}}}p")
             for line in drawing_paragraph_lines(paragraph))
    return list(dict.fromkeys(lines))


def embedded_table(outer: TableGrid) -> Callable[[list[list[str]]], str]:
    """A `table` (`render_chart`, `render_chartex`) that hands the value table of a chart inside a table cell to the outer table:
    rows → inner `TableGrid` → `outer.embed` placeholder. Putting Markdown table text in the cell made a mess of `\\|` and `<br>` —
    this is the same path as a table inside a table (DOCX, HWPX.
    Pinned by: test_office_cells_and_notes.py::TestDocxObjectsInsideTableCells, test_hwp_heads_and_shapes.py::TestChartInsideATableCell)."""
    def table(rows: list[list[str]]) -> str:
        grid = TableGrid()
        for row_index, row in enumerate(rows):
            for col_index, text in enumerate(row):
                grid.place(row_index, col_index, text)
        return outer.embed(grid)

    return table


def render_chartex(chart_space: etree._Element, table: Callable[[list[list[str]]], str] = render_table) -> str:
    """`cx:chartSpace` (the new chart kinds: waterfall, treemap, sunburst, histogram, funnel …) → "[Chart] title" + value table
    (`table`, same as in `render_chart`).

    The category names (cx:strDim) and first numeric dimension (cx:numDim) of the data (cx:data) a series (cx:series) points to go
    one row per category. Multi-level categories use only the innermost level (the first cx:lvl; unlike `_points`, outer levels are
    not joined). If no value is found at all the result is an empty string — the caller uses the fallback picture (mc:Fallback)
    (Pinned by: test_office.py::TestPptxLayoutAndEmbeddedObjects).
    """
    data = {element.get("id", ""): element for element in chart_space.iter(f"{{{_CX}}}data")}
    names, values = [], []
    categories: dict[int, str] = {}
    for series in chart_space.iter(f"{{{_CX}}}series"):
        reference = series.find(f"{{{_CX}}}dataId")
        source = data.get(reference.get("val", "") if reference is not None else "")
        column = _chartex_points(source.find(f"{{{_CX}}}numDim")) if source is not None else {}
        if not column:
            continue
        names.append(_chartex_text(series.find(f"{{{_CX}}}tx")) or f"Series {len(names) + 1}")
        values.append(column)
        for index, label in _chartex_points(source.find(f"{{{_CX}}}strDim")).items():
            categories.setdefault(index, label)

    if not values:
        return ""
    title = _chartex_text(chart_space.find(f"{{{_CX}}}chart/{{{_CX}}}title/{{{_CX}}}tx"))
    return f"[Chart] {title}".strip() + "\n\n" + _value_table(names, categories, values, table)


def _chartex_points(dimension: etree._Element | None) -> dict[int, str]:
    """First-level (cx:lvl) values of cx:strDim / cx:numDim as {point index: text}. Unlike `c:pt`, the text sits directly under cx:pt."""
    level = dimension.find(f"{{{_CX}}}lvl") if dimension is not None else None
    if level is None:
        return {}
    return {decimal_int(point.get("idx"), 0): point.text or ""
            for point in level.iterchildren(f"{{{_CX}}}pt") if point.get("idx", "").isdecimal()}


def _chartex_text(text: etree._Element | None) -> str:
    """Text of cx:tx: the cell reference cache (cx:v) or rich text (a:t; a line break a:br becomes a space)."""
    if text is None:
        return ""
    pieces = [" " if node.tag == f"{{{_A}}}br" else node.text or ""
              for node in text.iter(f"{{{_CX}}}v", f"{{{_A}}}t", f"{{{_A}}}br")]
    return " ".join("".join(pieces).split())


def _axis_titles(plot_area: etree._Element | None) -> str:
    """Titles of the visible axes on one line, "Category axis: 기준월 / Value axis: 단위: 억원". The title of a deleted axis (c:delete)
    is not on screen, so it is left out."""
    if plot_area is None:
        return ""
    axes = [(etree.QName(child).localname, child) for child in plot_area if isinstance(child.tag, str)]
    axes = [(name, axis) for name, axis in axes if name in _AXIS_LABELS]
    has_only_value_axes = all(name == "valAx" for name, _ in axes)

    titles = []
    for name, axis in axes:
        title_element = axis.find(f"{{{_C}}}title")
        title = _title_text(title_element) if title_element is not None else ""
        if not title or _is_flagged(axis.find(f"{{{_C}}}delete")):
            continue
        label = _AXIS_LABELS[name]
        if has_only_value_axes:
            position = axis.find(f"{{{_C}}}axPos")
            is_horizontal = position is not None and position.get("val") in _HORIZONTAL_AXIS_POSITIONS
            label = "X axis" if is_horizontal else "Y axis"
        titles.append(f"{label}: {title}")
    # The X axis is written before the Y axis (in the file the value axes are in creation order). The same title on a secondary axis
    # appears only once.
    return " / ".join(dict.fromkeys(sorted(titles, key=lambda title: title.startswith("Y axis"))))


def _is_flagged(element: etree._Element | None) -> bool:
    """True/false elements such as c:delete and c:date1904. An element that is present without val is true (the schema default)."""
    return element is not None and element.get("val", "1") not in ("0", "false")


def _date_axis_format(plot_area: etree._Element | None) -> str:
    """Display format of the date axis (c:dateAx). Used only when the category cache has no date format."""
    number_format = plot_area.find(f"{{{_C}}}dateAx/{{{_C}}}numFmt") if plot_area is not None else None
    return number_format.get("formatCode", "") if number_format is not None else ""


def _category_table(series_list: list[etree._Element], table: Callable[[list[list[str]]], str], date1904: bool,
                    axis_format: str, shown_formats: list[str]) -> str:
    if not series_list:
        return ""
    categories: dict[int, str] = {}
    for series in series_list:
        for index, label in _category_labels(series.find(f"{{{_C}}}cat"), date1904, axis_format).items():
            categories.setdefault(index, label)
    values = [_value_points(series.find(f"{{{_C}}}val"), shown)
              for series, shown in zip(series_list, shown_formats, strict=True)]
    names = [_series_name(series, number) for number, series in enumerate(series_list, start=1)]
    return _value_table(names, categories, values, table)


def _value_table(names: list[str], categories: dict[int, str], values: list[dict[int, str]],
                 table: Callable[[list[list[str]]], str]) -> str:
    """One row per category, one column per series. Rows are built only from the indexes of points that really exist — trusting
    ptCount, a single number would make a huge table."""
    indexes = sorted(set(categories).union(*values))
    rows = [[categories.get(index, str(index + 1)), *(_number(column.get(index, "")) for column in values)]
            for index in indexes]
    return table([["Category", *names], *rows])


def _category_labels(element: etree._Element | None, date1904: bool, axis_format: str) -> dict[int, str]:
    """Category names {point index: text}. Text (c:strRef) is left as is; numbers (c:numRef, c:numLit) become a date when the display
    format is a date, and are otherwise tidied like values. Date categories came out as Excel serial numbers (46023.0)
    (Pinned by: test_office.py::TestChartAxes::test_date_categories_are_shown_as_dates)."""
    points = _points(element)
    cache = next(element.iter(f"{{{_C}}}numCache", f"{{{_C}}}numLit"), None) if element is not None else None
    if cache is None:
        return points
    cache_format = cache.findtext(f"{{{_C}}}formatCode") or ""
    return {index: _format_date(value, cache_format, date1904) or _format_date(value, axis_format, date1904)
            or _number(value) for index, value in points.items()}


def _format_date(value: str, format_code: str, date1904: bool) -> str | None:
    """Excel serial number → `2026-01-01` (`2026-01-01 09:30` when the format shows a time, `09:30` when it shows only a time). None
    if the format is not a date format or the value cannot be a date. A format without a day gives `2026-01`, year only `2026`. The
    literal look of the format ('년', '월', weekday names) is not imitated — it is the same ISO notation as the spreadsheet
    extractor's dates."""
    if len(format_code) > _MAX_FORMAT_CHARS:
        return None
    letters = set(_FORMAT_LITERALS.sub("", format_code).lower())
    has_time = bool(letters & {"h", "s"})
    has_date = bool(letters & {"y", "d"}) or ("m" in letters and not has_time)
    try:
        serial = float(value)
    except ValueError:
        return None
    if not (has_date or has_time) or not 0 <= serial <= _LAST_DATE_SERIAL:
        return None

    # The base date of the 1900 system is 1899-12-30 (Excel counts the nonexistent 1900-02-29, so dates before it are one day late).
    epoch = datetime(1904, 1, 1) if date1904 else datetime(1899, 12, 30 if serial >= 60 else 31)
    moment = epoch + timedelta(seconds=round(serial * 86_400))
    time = moment.strftime("%H:%M:%S" if moment.second else "%H:%M")
    if not has_date:
        return time
    if has_time:
        return f"{moment.date().isoformat()} {time}"

    # A format that shows no day (d) or weekday (aaa) gives `YYYY-MM`, and `YYYY` when there is no month either. Appending a day 1
    # that is not there made it read as a value known to the day — the same rule as Excel cell dates (`spreadsheet._date_text`)
    # (Pinned by: test_office.py::TestChartAxes::test_date_categories_are_shown_as_dates).
    if letters & {"d", "a"}:
        return moment.date().isoformat()
    return f"{moment.year:04d}-{moment.month:02d}" if "m" in letters else f"{moment.year:04d}"


def _xy_table(series_list: list[etree._Element], table: Callable[[list[list[str]]], str]) -> str:
    if not series_list:
        return ""
    has_size = any(series.find(f"{{{_C}}}bubbleSize") is not None for series in series_list)
    header = ["Series", "X", "Y", *(["Size"] if has_size else [])]
    rows = []
    for number, series in enumerate(series_list, start=1):
        name = _series_name(series, number)
        x_values, y_values = _value_points(series.find(f"{{{_C}}}xVal")), _value_points(series.find(f"{{{_C}}}yVal"))
        sizes = _value_points(series.find(f"{{{_C}}}bubbleSize"))
        for index in sorted(set(x_values) | set(y_values)):
            row = [name, _number(x_values.get(index, str(index + 1))), _number(y_values.get(index, ""))]
            if has_size:
                row.append(_number(sizes.get(index, "")))
            rows.append(row)
    return table([header, *rows])


def _points(element: etree._Element | None) -> dict[int, str]:
    """Cached values of c:cat / c:val / c:xVal / c:yVal as {point index: text}.

    Multi-level categories (c:multiLvlStrRef) list their levels (c:lvl) innermost first, and an outer level has a value only at the
    first point of a group (it carries on until the next value). They are joined outer → inner with spaces, giving "2024 상반기". With
    only the innermost level there was no telling which year '상반기' (first half) / '하반기' (second half) belonged to
    (Pinned by: test_office.py::TestChartAxes::test_multi_level_categories_join_the_outer_levels).
    """
    if element is None:
        return {}
    levels = [_level_points(level) for level in element.iter(f"{{{_C}}}lvl")]
    if len(levels) < 2:
        return levels[0] if levels else _level_points(element)

    points = {}
    outer_levels = [(sorted(level), level) for level in reversed(levels[1:])]
    for index in sorted(set().union(*levels)):
        labels = []
        for starts, level in outer_levels:
            position = bisect_right(starts, index)
            labels.append(level[starts[position - 1]] if position else "")
        labels.append(levels[0].get(index, ""))
        points[index] = " ".join(label for label in labels if label)
    return points


def _level_points(container: etree._Element) -> dict[int, str]:
    points = {}
    for point in container.iter(f"{{{_C}}}pt"):
        index = point.get("idx", "")
        value = point.find(f"{{{_C}}}v")
        if index.isdecimal() and value is not None:
            points.setdefault(decimal_int(index, 0), value.text or "")
    return points


def _value_points(element: etree._Element | None, shown_format: str = "") -> dict[int, str]:
    """Values of c:val / c:xVal / c:yVal / c:bubbleSize as {point index: text}. When the display format (c:formatCode of the cache,
    or the formatCode of the c:pt when it differs per point) is a percentage, `25.3%` is written (decimal places as in the format).
    It went out as 0.253 and was not read as 25.3% (Pinned by:
    test_office.py::TestChartAxes::test_values_with_a_percent_format_are_shown_as_percentages). A simple number format with a text
    unit gives `1,200억원` (`_format_units`); other formats leave the value as is. When the cache format is General (or absent),
    `shown_format` (the format of the data labels / value axis, `_shown_value_format`) is used instead.
    """
    points = _points(element)
    cache = next(element.iter(f"{{{_C}}}numCache", f"{{{_C}}}numLit"), None) if element is not None else None
    if cache is None:
        return points
    cache_format = cache.findtext(f"{{{_C}}}formatCode") or ""
    if cache_format.strip().lower() in ("", "general"):
        cache_format = shown_format or cache_format
    formats = {decimal_int(point.get("idx"), 0): point.get("formatCode") for point in cache.iterchildren(f"{{{_C}}}pt")
               if point.get("formatCode") and point.get("idx", "").isdecimal()}
    if not cache_format and not formats:
        return points
    return {index: _format_value(value, formats.get(index, cache_format)) for index, value in points.items()}


def _shown_value_format(plot: etree._Element, series: etree._Element, plot_area: etree._Element) -> str:
    """Format in which the values of a category-axis chart series are shown on screen: the format of the visible data labels
    (c:dLbls — the series' own, otherwise the chart's), otherwise the format of the visible value axis (c:valAx — found through the
    chart's c:axId). A format that follows the source cell format (sourceLinked) and deleted labels/axes give an empty string.

    In charts whose unit is only in the label/axis format and not in the value cache (the cache is General), 1200 came out without a
    unit. Deliberate: used only when the cache format is General (`_value_points`) — the cache format is the source cell's format
    (Pinned by: test_office_chart_units_and_ole.py::TestChartUnitsOnLabelsAndValueAxis).
    """
    labels = series.find(f"{{{_C}}}dLbls")
    labels = plot.find(f"{{{_C}}}dLbls") if labels is None else labels
    if labels is not None and not _is_flagged(labels.find(f"{{{_C}}}delete")) \
            and _is_flagged(labels.find(f"{{{_C}}}showVal")):
        own = _own_number_format(labels)
        if own:
            return own

    # The value axis format (`0%`) of a 100% stacked chart is the scale of the cumulative share, not the format of the values — 1200
    # would become 120000%.
    grouping = plot.find(f"{{{_C}}}grouping")
    if grouping is not None and grouping.get("val") == "percentStacked":
        return ""
    axis_ids = {axis.get("val") for axis in plot.iterchildren(f"{{{_C}}}axId")}
    for axis in plot_area.iterchildren(f"{{{_C}}}valAx"):
        axis_id = axis.find(f"{{{_C}}}axId")
        if axis_id is not None and axis_id.get("val") in axis_ids and not _is_flagged(axis.find(f"{{{_C}}}delete")):
            return _own_number_format(axis)
    return ""


def _own_number_format(element: etree._Element) -> str:
    """Format of c:numFmt (an empty string for a sourceLinked format, which follows the source cell format)."""
    number_format = element.find(f"{{{_C}}}numFmt")
    if number_format is None or number_format.get("sourceLinked", "0").strip().lower() in ("1", "true"):
        return ""
    return number_format.get("formatCode", "")


def _format_value(value: str, format_code: str) -> str:
    """`25.3%` when the display format is a percentage, `1,200억원` for a simple number format with a text unit, otherwise the value
    as is."""
    return _format_percent(value, format_code) or _format_units(value, format_code) or value


def _format_units(value: str, format_code: str) -> str | None:
    """Value in a number format with a text unit (`#,##0"억원"`, `"₩"#,##0`, `#,##0,"천원"`) → `1,200억원`. None for unhandled formats
    and non-numeric values.

    Charts whose unit is only in the format are common, so 1200 came out without a unit (Pinned by:
    test_office_chart_units_and_ole.py::TestChartValueUnits). Excel formats are not imitated in full: only sections whose literals
    (quotes, backslash, currency symbol [$₩-412], `$-+()/: `) sit only before and after the digit placeholders (0 # ? , .) are
    followed, applying digit grouping (,), division by thousands (trailing ,) and decimal places (0 pads, # only when needed). A
    format with no text unit (`#,##0`) leaves the value as is, as before — rounding to the placeholders would only lose value. A
    negative number puts its absolute value in the second section if there is one, otherwise gets a leading `-`. Conditions
    ([>=1000]), dates, exponents, text (@) and a zero under three or more sections give None. Rounding is half away from zero, as
    in Excel.
    Deliberate: when the second section has no literal showing that the value is negative (its literals equal the first section's
    or the section is empty — `#,##0"억원";[Red]#,##0"억원"`), `-` is added. Colors are discarded, so -300 went out as `300억원` and a
    loss read as a profit — the same rule as Excel cells (`spreadsheet._format_section`)
    (Pinned by: test_office_chart_units_and_ole.py::TestChartNegativeSectionWithoutSign).
    """
    if len(format_code) > _MAX_FORMAT_CHARS:
        return None
    try:
        number = Decimal(value.strip())
    except InvalidOperation:
        return None
    if not number.is_finite() or abs(number) >= _UNIT_FORMAT_LIMIT:
        return None

    # Split the format into tokens and collect them per section (;). A ; inside quotes is a literal.
    sections: list[list[tuple[str, str]]] = [[]]

    for literal, escaped, currency, bracket, digit, other in _FORMAT_TOKEN.findall(format_code):
        if other == ";":
            sections.append([])
        elif bracket:
            sections[-1].append(("skip" if _FORMAT_COLORS.fullmatch(bracket) else "bad", bracket))
        elif digit:
            sections[-1].append(("digit", digit))
        elif other and other not in _FORMAT_PLAIN_LITERALS:
            sections[-1].append(("bad", other))
        else:
            sections[-1].append(("text", literal or escaped or currency or other))

    if number < 0 and len(sections) > 1 and sections[1]:
        marked = _section_literals(sections[1]) not in ("", _section_literals(sections[0]))
        section, sign = sections[1], "" if marked else "-"
    elif number == 0 and len(sections) > 2:
        return None
    else:
        section, sign = sections[0], "-" if number < 0 else ""

    parsed = _unit_section(section)
    if parsed is None:
        return None
    prefix, placeholders, suffix = parsed
    shown = _digits_text(abs(number), placeholders)
    return f"{sign if shown.strip('0.,') else ''}{prefix}{shown}{suffix}" if shown is not None else None


def _section_literals(section: list[tuple[str, str]]) -> str:
    """Literals a section shows around the value (spaces excluded). The `()` of `(#,##0)`, the `△` of `"△"#,##0`, the `-` of `-#,##0`."""
    return "".join("".join(text for kind, text in section if kind == "text").split())


def _unit_section(section: list[tuple[str, str]]) -> tuple[str, str, str] | None:
    """One format section → (leading literals, digit placeholders, trailing literals). None if the digit placeholders are not one
    run, there is an unhandled token, or there is no text unit."""
    parts: list[list[str]] = [[], [], []]  # leading literals, digit placeholders, trailing literals
    position = 0

    for kind, text in section:
        if kind == "bad":
            return None
        if kind == "digit":
            if position == 2:
                return None  # digits appear again after a literal (`0" "0`)
            position = 1
        elif kind == "text" and position == 1:
            position = 2
        if kind != "skip":
            parts[position].append(text)

    prefix, placeholders, suffix = ("".join(part) for part in parts)
    if not any(char in "0#?" for char in placeholders) or not (prefix + suffix).strip("-+() "):
        return None  # a section with only signs/parentheses (`-#,##0`) is not a unit — the value stays as is, like a positive one
    return prefix, placeholders, suffix


def _digits_text(number: Decimal, placeholders: str) -> str | None:
    """Write a value of 0 or more according to the digit placeholders (`#,##0.0#`; each trailing `,` divides by 1000). None if there
    are two decimal points or a `,` in the fraction."""
    scaled = placeholders.rstrip(",")
    whole, _, fraction = scaled.partition(".")
    scale = len(placeholders) - len(scaled) + len(whole) - len(whole.rstrip(","))
    whole = whole.rstrip(",")
    if "." in fraction or "," in fraction:
        return None
    least = sum(1 for char in fraction if char == "0")
    most = sum(1 for char in fraction if char in "0#?")

    try:
        rounded = (number / Decimal(1000) ** scale).quantize(Decimal(1).scaleb(-most), rounding=ROUND_HALF_UP)
    except InvalidOperation:  # a format with dozens of decimal places — use the raw value
        return None

    # In the fraction, drop the zeros of the # places and pad up to the number of 0 places.
    text = f"{rounded:,f}" if "," in whole else f"{rounded:f}"
    if "." in text:
        head, _, tail = text.partition(".")
        tail = tail.rstrip("0").ljust(least, "0")
        text = f"{head}.{tail}" if tail else head
    return text


def _format_percent(value: str, format_code: str) -> str | None:
    """Value in a percent format (`0.0%`) → `25.3%`. None if it is not a percent format or not a number.

    Only the first section of the format is looked at (negative sections, colors and conditions are not imitated; `-` is added). A
    `%` blocked by quotes or a backslash is a literal, so it is not a percentage. Rounding is half away from zero, as in Excel.
    """
    if len(format_code) > _MAX_FORMAT_CHARS:
        return None
    section = _FORMAT_LITERALS.sub("", format_code).split(";")[0]
    if "%" not in section:
        return None
    try:
        number = Decimal(value.strip())
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None

    whole, _, fraction = section.partition(".")
    decimals = sum(1 for char in fraction if char in "0#?")
    try:
        percent = (number * 100).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
    except InvalidOperation:  # a format with dozens of decimal places / a value with too many digits — use the raw value
        return None
    return f"{percent:,f}%" if "," in whole else f"{percent:f}%"


def _series_name(series: etree._Element, number: int) -> str:
    text = series.find(f"{{{_C}}}tx")
    if text is not None:
        name = " ".join("".join(node.text or "" for node in text.iter(f"{{{_C}}}v", f"{{{_A}}}t")).split())
        if name:
            return name
    return f"Series {number}"


def _series_order(series: etree._Element) -> int:
    order = series.find(f"{{{_C}}}order")
    return decimal_int(order.get("val") if order is not None else None, 0)


def _title_text(title: etree._Element) -> str:
    """The title text is only in c:tx (rich text or a cell reference cache). c:txPr is formatting only. Paragraphs and line breaks
    are separated by a space."""
    text = title.find(f"{{{_C}}}tx")
    if text is None:
        return ""
    paragraphs = ["".join(" " if node.tag == f"{{{_A}}}br" else node.text or ""
                          for node in paragraph.iter(f"{{{_A}}}t", f"{{{_A}}}br"))
                  for paragraph in text.iter(f"{{{_A}}}p")]
    paragraphs.extend(node.text or "" for node in text.iter(f"{{{_C}}}v"))
    return " ".join(" ".join(paragraphs).split())


def _number(value: str) -> str:
    """Cached values are strings. A number is written with all its digits. `:g` gives 6 significant digits, so 1234567890 became
    1.23457e+09 (Pinned by: test_office.py::TestPptx::test_chart_values_keep_all_their_digits)."""
    try:
        number = float(value)
    except ValueError:
        return value
    if number != number or number in (float("inf"), float("-inf")):
        return value
    return str(int(number)) if number.is_integer() and abs(number) < 1e15 else format(number, ".15g")


def drawing_paragraph_lines(paragraph: etree._Element) -> list[str]:
    """Lines of a DrawingML paragraph (a:p): split at in-paragraph line breaks (a:br, Shift+Enter), with whitespace collapsed to one
    space per line. Empty lines are dropped.

    Joining only the a:t made '2023' + a:br + '1,500' stick together as '20231,500'. Places written on one line (SmartArt items,
    chart titles) join the lines with a space; places where each line becomes a block (text boxes inside a chart, DOCX shape text)
    use the lines as they are — the same rule as XLSX text boxes (`spreadsheet._text_body`) and PPTX text boxes
    (Pinned by: test_office_drawing_line_breaks.py::TestLineBreaksInsideDrawingParagraphs).
    """
    pieces = ["\n" if node.tag == f"{{{_A}}}br" else node.text or ""
              for node in paragraph.iter(f"{{{_A}}}t", f"{{{_A}}}br")]
    lines = (" ".join(line.split()) for line in "".join(pieces).split("\n"))
    return [line for line in lines if line]


# ---- SmartArt ----------------------------------------------------------------------------------------------------

def render_smartart(data_model: etree._Element, *, in_cell: bool = False) -> str:
    """Carry the text of a SmartArt (diagram) over as a nested list. Inside a table cell (`in_cell`) lower levels use
    `cell_level_mark` instead of indentation — leading spaces of a line in a cell are erased and the levels went flat
    (Pinned by: test_office_cells_and_notes.py::TestDocxObjectsInsideTableCells).

    The text is not in the body XML but in the data part (diagrams/data*.xml). It consists of points (dgm:pt) and parent→child
    connections (dgm:cxn), so we walk down from the document root (type="doc") in connection order (srcOrd). Layout-only points
    (pres) and connector points are left out
    (Pinned by: test_office.py::test_smartart_text_is_extracted_as_nested_list, test_chart_and_smartart_are_kept).
    With a chain of 1,500 connections the recursion exceeded its limit and the whole document was rejected. A stack follows the same
    order (everything under an earlier sibling is written before the next sibling)
    (Pinned by: test_office.py::TestOneOddValueDoesNotRejectTheDocument::test_smartart_with_a_very_deep_chain_is_read_without_recursion).
    """
    kinds: dict[str, str] = {}
    texts: dict[str, list[str]] = {}
    for point in data_model.iter(f"{{{_DGM}}}pt"):
        model_id = point.get("modelId", "")
        kinds[model_id] = point.get("type", "node")
        paragraphs = (" ".join(drawing_paragraph_lines(paragraph)) for paragraph in point.iter(f"{{{_A}}}p"))
        texts[model_id] = [paragraph for paragraph in paragraphs if paragraph]
    children: dict[str, list[tuple[int, str]]] = {}
    for connection in data_model.iter(f"{{{_DGM}}}cxn"):
        if connection.get("type", "parOf") == "parOf":
            order = connection.get("srcOrd", "0")
            children.setdefault(connection.get("srcId", ""), []).append((decimal_int(order, 0),
                                                                         connection.get("destId", "")))

    # Walk down from the document root in connection order. (point, depth) pairs are pushed on the stack in reverse so they pop in
    # the same order as recursion, and whether a point was already written is checked on pop (the same moment as with recursion).
    roots = [model_id for model_id, kind in kinds.items() if kind == "doc"]
    stack = [(child, 0) for root in reversed(roots) for _, child in sorted(children.get(root, []), reverse=True)]
    lines: list[str] = []
    visited: set[str] = set()
    while stack:
        child, depth = stack.pop()
        if child in visited or kinds.get(child) in _SMARTART_LAYOUT_POINTS:
            continue
        visited.add(child)
        indent = cell_level_mark(min(depth, MAX_LEVEL)) if in_cell else "  " * min(depth, MAX_LEVEL)
        lines.extend(f"{indent}- {text}" for text in texts.get(child, []))
        next_depth = depth + 1 if texts.get(child) else depth
        stack.extend((grandchild, next_depth) for _, grandchild in sorted(children.get(child, []), reverse=True))

    # Points with no or broken connection info are appended in order of appearance (the side that loses no text).
    lines.extend(f"- {text}" for model_id, kind in kinds.items()
                 if kind == "node" and model_id not in visited for text in texts[model_id])
    return "\n".join(lines)


# ---- Superscripts and subscripts ---------------------------------------------------------------------------------

SUPERSCRIPT, SUBSCRIPT = "^", "_"
# The temporary marks of links and emphasis (see "Links and emphasis" below). They are still in the text when scripts and
# strikethrough are resolved and are skipped where those look at the neighbouring text.
_INLINE_MARK_CHARS = "\x0e\x0f\x10\x11\x12"
_WITHOUT_INLINE_MARKS = dict.fromkeys(map(ord, _INLINE_MARK_CHARS))
# Script text longer than this is not an exponent or chemical formula but a sentence written small (trademark notices etc.), so it
# is left unmarked.
_SCRIPT_MAX_CHARS = 10
# Temporary marks that the formats read run by run (DOCX, PPTX) put around a script span. Control characters that cannot occur in
# XML text.
_SCRIPT_OPEN = {SUPERSCRIPT: "\x02", SUBSCRIPT: "\x04"}
_SCRIPT_CLOSE = "\x03"
_SCRIPT_SPLIT = re.compile(r"([\x02\x04])([^\x02-\x04]*)\x03\1")
_SCRIPT_SPAN = re.compile(r"([\x02\x04])([^\x02-\x04]*)\x03")


# Hangul (jamo, compatibility jamo, syllables), Han characters, kana. A script containing these is not an exponent but a raised unit
# or footnote mark ('1,000원', '주1)').
_CJK_SCRIPT = re.compile("[\u1100-\u11ff\u3040-\u30ff\u3130-\u318f\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7a3\uf900-\ufaff]")
# Ordinal suffixes written small and raised after a number (21st, 2nd, 3ème). Not exponents; plain text is right for them.
_ORDINAL_SUFFIXES = frozenset({"st", "nd", "rd", "th", "er", "re", "ère", "ere", "ème", "eme", "ième", "ieme", "nde"})


def format_script(text: str, mark: str, before: str = "") -> str:
    """Prefix `^` to a superscript and `_` to a subscript (`10^6` for one character, `10^{-3}` for several). Every format uses the
    same shape.

    Joined as plain text, 10⁶ read as 106 and km² as km2. Only short text starting with a letter, digit or sign is marked. Footnote
    marks (`[1]`, `[^1]`), symbols (†), page marks (`: 125`) and long text are left as is
    (Pinned by: test_text_html.py::test_html_superscript_and_subscript_are_marked).
    """
    inner = " ".join(text.split())
    if not inner or not (inner[0].isalnum() or inner[0] in "+-−") or len(inner) > _SCRIPT_MAX_CHARS:
        return text
    # Scripts containing Hangul or Han characters are not marked (the same rule as the PDF script guess). They became '1,000^원' and
    # '각주^{주1)}' (Pinned by: test_text_html.py::test_html_superscript_with_hangul_or_hanja_is_left_as_plain_text,
    # test_office.py::TestSuperscriptAndTextBoxes::test_docx_superscript_with_hangul_is_left_as_plain_text).
    if _CJK_SCRIPT.search(inner):
        return text
    # `before` is the text right before the script. An ordinal suffix after a number is not marked — 21st became 21^{st}
    # (Pinned by: test_text_html.py::test_html_ordinal_suffix_after_a_number_is_not_marked_as_superscript).
    if mark == SUPERSCRIPT and before[-1:].isdigit() and inner.lower() in _ORDINAL_SUFFIXES:
        return text
    lead, trail = text[:len(text) - len(text.lstrip())], text[len(text.rstrip()):]
    return f"{lead}{mark}{inner if len(inner) == 1 else '{' + inner + '}'}{trail}"


def mark_script(text: str, mark: str) -> str:
    """Put temporary marks around the text of a script run. `resolve_scripts` converts them once the paragraph text is collected."""
    return f"{_SCRIPT_OPEN[mark]}{text}{_SCRIPT_CLOSE}" if text else text


def resolve_scripts(text: str) -> str:
    """Turn the temporary marks into script notation. Consecutive runs of the same kind ('-' and '3') are merged into one first."""
    if _SCRIPT_CLOSE not in text:
        return text
    while _SCRIPT_SPLIT.search(text):
        text = _SCRIPT_SPLIT.sub(lambda match: match.group(1) + match.group(2), text)
    marks = {opened: mark for mark, opened in _SCRIPT_OPEN.items()}
    return _SCRIPT_SPAN.sub(
        lambda match: format_script(match.group(2), marks[match.group(1)], _character_before(match.string, match.start())),
        text)


def _character_before(text: str, position: int) -> str:
    """The character before `position`, not counting the temporary link and emphasis marks (the script marks are resolved while
    those are still in the text: '21' in bold followed by a raised 'st' is still an ordinal)."""
    while position > 0 and text[position - 1] in _INLINE_MARK_CHARS:
        position -= 1
    return text[position - 1:position] if position else ""


# ---- Strikethrough -----------------------------------------------------------------------------------------------

# Temporary marks that the formats read run by run (DOCX, PPTX, HWP, HWPX) put around a struck-through run. Control characters that
# do not clash with the script marks.
_STRIKE_OPEN, _STRIKE_CLOSE = "\x05", "\x06"
_STRIKE_JOINT = re.compile(r"\x06(\s*)\x05")
_STRIKE_SPAN = re.compile(r"\x05([^\x05\x06]*)\x06")
# Line-leading bullets kept outside the strikethrough marks: Markdown list markers and hand-typed bullet symbols (only when
# followed by whitespace — '-5%' is a negative number).
_STRIKE_LEAD = re.compile(r"[-*+·•●○◦▪◆◇▶►➢]\s+")


def mark_strike(text: str) -> str:
    """Put temporary marks around the text of a struck-through run. `resolve_strikes` converts them once the paragraph text is
    collected."""
    return f"{_STRIKE_OPEN}{text}{_STRIKE_CLOSE}" if text else text


def resolve_strikes(text: str) -> str:
    """Turn the temporary marks into `~~text~~`. The same shape as `<s>` / `<del>` in the HTML extractor — the marks go before the
    first character and after the last (whitespace stays outside), consecutive struck-through runs are merged into one even when
    only whitespace lies between them, and whitespace-only spans are not marked. When the leading characters are struck through it
    is `~~가나~~`, not `~~가~~~~나~~`, even if the runs are split per character. Resolved before the script marks (`resolve_scripts`)
    — with a strikethrough mark in between, consecutive script runs do not merge (Pinned by:
    test_office.py::TestDocxStruckDeletedAndSymbolRuns, TestPptxStrikethroughTitlesAndHiddenSlides, test_hwp.py::TestStrikethrough).

    A strikethrough spanning a line break is closed and reopened on each line. A bullet at the start of a line ('- ', '•' etc.,
    `_STRIKE_LEAD`) stays outside the marks. Wrapped in one mark, a line starting with '~~- 1차 …' was not read as a sub-item (the
    w:br lines of a DOCX list item. Pinned by: test_office.py::
    TestOfficeLineBreaksNestedFieldsAndMetadata::test_docx_strikethrough_across_a_line_break_is_marked_per_line)."""
    if _STRIKE_CLOSE not in text:
        return text
    text = _STRIKE_JOINT.sub(r"\1", text)

    # Whether a span is at the start of a line is carried on from the end of the previous span. Scanning back to the line start for
    # every mark was quadratic when one line had many marks (40,000 pairs on one line: 2 s. Pinned by:
    # test_office_symbols_fields_notes.py::TestStrikeResolutionIsLinear). The closing mark of the previous span is not whitespace, so
    # the next span on the same line is not at the line start.
    pieces = []
    position = 0
    line_start = True

    for match in _STRIKE_SPAN.finditer(text):
        gap = text[position:match.start()]
        newline = gap.rfind("\n")
        # The temporary link and emphasis marks (resolved later) are not text: a struck-through bullet line in bold is still at
        # the start of its line
        line_start = _is_blank(gap[newline + 1:]) if newline >= 0 else line_start and _is_blank(gap)
        lines = match.group(1).split("\n")
        pieces.append(gap)
        pieces.append("\n".join(_strike_line(line, index > 0 or line_start) for index, line in enumerate(lines)))
        position = match.end()
        line_start = False

    pieces.append(text[position:])
    return "".join(pieces)


def _is_blank(text: str) -> bool:
    return not text.translate(_WITHOUT_INLINE_MARKS).strip()


def _strike_line(inner: str, at_line_start: bool) -> str:
    """A struck-through span within one line → `~~text~~`. Surrounding whitespace and, at the start of a line, the bullet stay
    outside the marks."""
    body = inner.strip()
    if not body:
        return inner
    lead = _STRIKE_LEAD.match(body) if at_line_start else None
    head = lead.group(0) if lead and lead.end() < len(body) else ""
    return f"{inner[:len(inner) - len(inner.lstrip())]}{head}~~{body[len(head):]}~~{inner[len(inner.rstrip()):]}"


# ---- Links and emphasis ------------------------------------------------------------------------------------------
#
# DOCX, PPTX and HTML keep hyperlinks as `[text](url)` and bold / italic as `**bold**`, `*italic*`, `***both***`
# (`ConvertOptions.keep_links`, `keep_emphasis`). The decisions are recorded in docs/decisions/inline-formatting.md.
#
# The extractors only say where a span starts and ends; one place (`resolve_inline`) turns the spans into Markdown, so that every
# format merges runs, places whitespace and escapes text by the same rules. A span is announced either with temporary marks inside
# the text (DOCX and PPTX, which read run by run; control characters that cannot occur in XML text and that are not whitespace for
# `str.split`) or as `InlineMark` items between the pieces of text (HTML, whose text may hold any character).

_BOLD_OPEN, _BOLD_CLOSE, _ITALIC_OPEN, _ITALIC_CLOSE = "\x0e", "\x0f", "\x10", "\x11"
LINK_OPEN, _LINK_TARGET, _LINK_CLOSE = "\x12", "\x13", "\x14"
_INLINE_TOKEN = re.compile(r"[\x0e-\x12]|\x13([^\x13\x14]*)\x14")
_HAS_INLINE_MARK = re.compile(r"[\x0e-\x14]")
# Runs with the same emphasis that touch (or have only whitespace between them) are one span. Bold is always the outer mark and
# italic the inner one (`mark_emphasis`), so two replacements are enough.
_BOLD_JOINT = re.compile(r"\x0f(\s*)\x0e")
_ITALIC_JOINT = re.compile(r"\x11(\s*)\x10")
# Link targets that are kept. Everything else (relative addresses, `#fragment`, `javascript:`, `file:`, `data:` …) stays text.
_LINK_SCHEMES = ("http://", "https://", "mailto:")
# Characters of an address that would end the link early, split a table row, or be changed by `clean_text` (whitespace, control
# characters, `( ) < > | \`, the brackets of a picture placeholder). They are percent-encoded.
_LINK_UNSAFE = re.compile(r"[\s\x00-\x1f\x7f-\x9f()<>|\\⟦⟧​-‏‪-‮⁠-⁤﻿­]")
# Text that stands for an object, not words: picture placeholders, `[Image: alt]` of pictures that are not extracted, OLE objects,
# charts, videos and footnote references. Emphasis is not put around them, and at the edge of a link they stay outside the brackets
# (the converter turns a picture placeholder into a block below its line).
# Deliberate: no `[` inside `[Image: …]` or `[^…]`. An opening with no `]` was scanned to the end of the line from every `[`
# (10,000 `[Image: ` next to a bold word took 5 seconds); with a `[` inside, the text is no longer taken for a placeholder
# (Pinned by: test_audit_core.py::TestUnclosedImagePlaceholderText).
_INLINE_PLACEHOLDER = re.compile(
    rf"({IMAGE_MARKER.pattern}|\[Image(?:: [^\[\]\n]*)?\]|\[OLE object\]|\[Chart\]|\[Video\]|\[\^[^\[\]\s]+\])")
_EQUATION_ONLY = re.compile(r"\$[^$\n]+\$")
# A bullet or number typed at the start of a line. It stays outside the marks (the same idea as `_STRIKE_LEAD`): `**- 항목**` is
# not read as a list item, and the extractors look for these at the start of a line (`HAND_TYPED_BULLET`).
_INLINE_LEAD = re.compile(r"[ \t]*(?:[-*+][ \t]+|[·•●○◦▪■□◆◇▶►➢✓☐☒☑][ \t]*|\d{1,3}[.)][ \t]+|#{1,6}[ \t]+)")
_LEAD_PROBE = 16  # a lead is a few characters: only this much of the start of a line is looked at
_BLOCK_GAP = re.compile(r"(\n[ \t]*\n\s*)")
_STARTS_WITH_LINE_BREAK = re.compile(r"[ \t]*\n")
_ENDS_WITH_LINE_BREAK = re.compile(r"\n[ \t]*\Z")
_BRACKET_OR_ESCAPE = re.compile(r"\\.?|[\[\]]", re.DOTALL)
_STRIKE_MARK = re.compile("~~")
_LINE_ENDS = ("break", "gap")


@dataclass(frozen=True, slots=True)
class InlineMark:
    """Start or end of a span, placed between pieces of text (`resolve_inline`). `kind` is "b" (bold), "i" (italic) or "a" (link);
    the end of a link carries the vetted address (`link_target`)."""

    kind: str
    opens: bool
    url: str | None = None


BOLD_ON, BOLD_OFF = InlineMark("b", True), InlineMark("b", False)
ITALIC_ON, ITALIC_OFF = InlineMark("i", True), InlineMark("i", False)
LINK_ON = InlineMark("a", True)
# A table, list, code block or quote block written between the pieces (HTML). No link or emphasis span holds it.
BLOCK_INSIDE = InlineMark("x", True)
_CHAR_MARKS = {_BOLD_OPEN: BOLD_ON, _BOLD_CLOSE: BOLD_OFF, _ITALIC_OPEN: ITALIC_ON, _ITALIC_CLOSE: ITALIC_OFF, LINK_OPEN: LINK_ON}


def link_target(address: str | None) -> str | None:
    """The address to write into `[text](address)`, or None when the link is not kept.

    Only `http`, `https` and `mailto` addresses are kept — a relative address or a `#fragment` points nowhere outside the original
    page, and `javascript:`, `file:` or `data:` must not become something a reader of the Markdown can follow. Nothing is resolved or
    fetched. Characters that would end the link early or break a table row are percent-encoded, so the text of a document cannot
    close the link and open another one (Pinned by: test_inline_formatting.py::TestLinkTargets).
    """
    if not address:
        return None
    url = address.strip()
    scheme = next((scheme for scheme in _LINK_SCHEMES if url[:len(scheme)].lower() == scheme), None)
    if scheme is None or len(url) == len(scheme):
        return None
    return _LINK_UNSAFE.sub(lambda match: "".join(f"%{byte:02X}" for byte in match.group().encode("utf-8", "replace")), url)


def link_off(url: str) -> InlineMark:
    return InlineMark("a", False, url)


def link_close(url: str) -> str:
    """The temporary mark that ends a link started with `LINK_OPEN`. `url` must come from `link_target`."""
    return f"{_LINK_TARGET}{url}{_LINK_CLOSE}"


def mark_emphasis(text: str, bold: bool, italic: bool) -> str:
    """Put temporary marks around the text of a bold and/or italic run. Whitespace-only text is left unmarked — it joins the runs
    around it (`resolve_inline`)."""
    if not (bold or italic) or not text.strip():
        return text
    if italic:
        text = f"{_ITALIC_OPEN}{text}{_ITALIC_CLOSE}"
    return f"{_BOLD_OPEN}{text}{_BOLD_CLOSE}" if bold else text


def merge_emphasis_marks(text: str) -> str:
    """Join the temporary marks of neighbouring runs that have the same emphasis. Done before the strikethrough and script marks
    are resolved: with an emphasis mark between them, struck-through runs that Word split per character would each get their own
    `~~` pair."""
    if _BOLD_CLOSE not in text and _ITALIC_CLOSE not in text:
        return text
    return _ITALIC_JOINT.sub(r"\1", _BOLD_JOINT.sub(r"\1", text))


def plain_inline(text: str) -> str:
    """The text without the temporary link and emphasis marks (and without the link addresses)."""
    return _INLINE_TOKEN.sub("", text) if _HAS_INLINE_MARK.search(text) else text


@dataclass(slots=True)
class _Atom:
    """A piece of text with the formatting that applies to it. `kind`: "text", "space" (whitespace only: takes the formatting both
    neighbours share), "plain" (a placeholder or a bullet at the start of a line: never marked), "break" (ends a line: a line
    break, or unformatted lines from one line break to another kept in one piece) or "gap" (a blank line: ends a block)."""

    text: str
    bold: bool
    italic: bool
    url: str | None
    kind: str


def resolve_inline(source: str | list[str | InlineMark], *, emphasis: bool = True) -> str:
    """Turn link and emphasis spans into Markdown: `[text](url)`, `**bold**`, `*italic*`, `***both***`.

    `source` is text holding temporary marks (`mark_emphasis`, `LINK_OPEN` … `link_close`) or a list of text pieces and
    `InlineMark`s. With `emphasis` off only links are written (headings). Rules, each recorded in
    docs/decisions/inline-formatting.md and pinned in test_inline_formatting.py:

    - Neighbouring pieces with the same formatting are one span, also across whitespace (Word splits a sentence into many runs).
    - Whitespace goes outside the marks and whitespace-only spans are not marked. A span is closed and reopened at a line break,
      and a bullet or number at the start of a line stays in front of the marks.
    - A span whose words are in different blocks (a blank line between them, or a table, list or code block inside it —
      `BLOCK_INSIDE`) is not written: Markdown emphasis and links do not cross paragraphs.
    - Placeholders (pictures, footnote references …) are never emphasized and stay outside a link at its edges; a link with no
      text left is not written. A link whose text is its own address stays the bare address.
    - Bold or italic that covers a whole block (paragraph, table cell line) from its first to its last word is not written:
      it is how the block is styled, not an emphasis inside it.
    - A `*` at the edge of an emphasized span and unbalanced or link-forming brackets inside link text are escaped with `\\`.

    Everything is done in one pass over the pieces (Pinned by: test_inline_formatting.py::TestLinearTime).
    """
    if isinstance(source, str):
        if not _HAS_INLINE_MARK.search(source):
            return source
        tokens: list[str | InlineMark] = []
        position = 0
        for match in _INLINE_TOKEN.finditer(source):
            if match.start() > position:
                tokens.append(source[position:match.start()])
            mark = match.group()
            tokens.append(_CHAR_MARKS[mark] if len(mark) == 1 else link_off(match.group(1)))
            position = match.end()
        tokens.append(source[position:])
        # A stray half of a link mark (the extractors do not produce one) goes the way of the other control characters
        tokens = [_HAS_INLINE_MARK.sub("", token) if isinstance(token, str) else token for token in tokens]
    else:
        tokens = source
        if not any(isinstance(token, InlineMark) and token.kind != "x" for token in tokens):
            return "".join(token for token in tokens if isinstance(token, str))

    return _write_atoms(_settle_lines(_inline_atoms(tokens, emphasis)))


def _inline_atoms(tokens: list[str | InlineMark], emphasis: bool) -> list[_Atom]:
    """Pieces and marks → atoms carrying their formatting. A span is valid when it was closed and all its words are in one block;
    a piece is bold / italic when any valid span of that kind encloses it, and linked to the address of the innermost valid link."""
    # Per span, in the order they open: kind, the enclosing span of the same kind (-1 when none), the number of worded pieces
    # seen when it opened, whether it is valid, and (links) its address
    kinds: list[str] = []
    parents: list[int] = []
    first_word: list[int] = []
    valid: list[bool] = []
    urls: list[str | None] = []
    open_spans: dict[str, list[int]] = {"b": [], "i": [], "a": []}
    # text, innermost bold span, italic span, link span (-1 when none), whether the piece is a blank line between blocks
    pieces: list[tuple[str, int, int, int, bool]] = []
    word_blocks: list[int] = []  # for every worded piece so far, the number of the block it is in
    block = 0
    after_line_break = False

    for token in tokens:
        if isinstance(token, InlineMark):
            if token.kind == "x":  # a table, list, code block or quote: a block of its own that no span can hold
                word_blocks.append(block + 1)
                block += 2
                continue
            stack = open_spans[token.kind]
            if token.opens:
                stack.append(len(kinds))
                kinds.append(token.kind)
                parents.append(stack[-2] if len(stack) > 1 else -1)
                first_word.append(len(word_blocks))
                valid.append(False)
                urls.append(None)
            elif stack:
                span = stack.pop()
                # No words, or the first and the last word in different blocks: not written
                valid[span] = len(word_blocks) > first_word[span] and word_blocks[first_word[span]] == word_blocks[-1]
                urls[span] = token.url if valid[span] else None
            continue
        for position, part in enumerate(_BLOCK_GAP.split(token)):
            if not part:
                continue
            if position % 2:
                block += 1
                after_line_break = True
            elif part.strip(" \t"):
                if after_line_break and _STARTS_WITH_LINE_BREAK.match(part):
                    block += 1
                after_line_break = _ENDS_WITH_LINE_BREAK.search(part) is not None
                if part.strip():
                    word_blocks.append(block)
            pieces.append((part, open_spans["b"][-1] if open_spans["b"] else -1, open_spans["i"][-1] if open_spans["i"] else -1,
                           open_spans["a"][-1] if open_spans["a"] else -1, position % 2 == 1))

    # The enclosing span opened earlier, so it is settled before the spans inside it
    for span, parent in enumerate(parents):
        if parent < 0:
            continue
        if kinds[span] == "a":
            urls[span] = urls[span] or urls[parent]
        else:
            valid[span] = valid[span] or valid[parent]

    atoms: list[_Atom] = []
    for text, bold_span, italic_span, link_span, is_gap in pieces:
        bold = emphasis and bold_span >= 0 and valid[bold_span]
        italic = emphasis and italic_span >= 0 and valid[italic_span]
        url = urls[link_span] if link_span >= 0 else None
        if is_gap:
            atoms.append(_Atom(text, False, False, url, "gap"))
            continue
        lines = text.split("\n")
        # Unformatted lines (a table or list rendered on its own) stay one piece from the first line break to the last: only
        # the first and the last line can touch a span. A page with a 100,000-row table is not cut into as many pieces.
        whole = len(lines) > 2 and not (bold or italic or url)
        if whole:
            lines = [lines[0], lines[-1]]
        for index, line in enumerate(lines):
            if index:
                between = text[len(lines[0]):len(text) - len(lines[-1])] if whole else "\n"
                atoms.append(_Atom(between, False, False, url, "break"))
            if not line:
                continue
            body = line.strip()
            if not body:
                atoms.append(_Atom(line, bold, italic, url, "space"))
            elif _is_placeholder_text(body):
                atoms.append(_Atom(line, False, False, url, "plain"))
            else:
                atoms.append(_Atom(line, bold, italic, url, "text"))
    return atoms


def _is_placeholder_text(body: str) -> bool:
    """Whether the (stripped) piece is nothing but object placeholders, or one equation (`$…$`)."""
    if body[0] not in "⟦[$":
        return False
    if _EQUATION_ONLY.fullmatch(body):
        return True
    return not "".join(_INLINE_PLACEHOLDER.split(body)[::2]).strip()


def _settle_lines(atoms: list[_Atom]) -> list[_Atom]:
    """Per line: take a bullet or number at its start out of the spans (`_without_lead`). Per block (the lines up to a blank
    line): drop bold, or italic, that every word of the block carries (`_drop_uniform_emphasis`). Then whitespace between two
    pieces takes the formatting both share."""
    settled: list[_Atom] = []
    block_start = 0
    line: list[_Atom] = []
    for atom in (*atoms, None):
        if atom is not None and atom.kind not in _LINE_ENDS:
            line.append(atom)
            continue
        blank = all(piece.kind == "space" for piece in line)
        settled.extend(_without_lead(line))
        if blank or atom is None or atom.kind == "gap":
            _drop_uniform_emphasis(settled, block_start)
            block_start = len(settled) + 1
        if atom is not None:
            settled.append(atom)
        line = []

    before: _Atom | None = None
    waiting: list[_Atom] = []
    for atom in (*settled, None):
        if atom is not None and atom.kind == "space":
            waiting.append(atom)
            continue
        same_line = before is not None and atom is not None and atom.kind not in _LINE_ENDS
        for space in waiting:
            space.bold = same_line and before.bold and atom.bold
            space.italic = same_line and before.italic and atom.italic
            space.url = before.url if same_line and before.url == atom.url else None
        waiting.clear()
        before = None if atom is None or atom.kind in _LINE_ENDS else atom
    _drop_emphasis_inside_words(settled)
    return settled


def _drop_emphasis_inside_words(atoms: list[_Atom]) -> None:
    """Clear bold (or italic) that styles part of a word instead of stressing words: a span that starts in the middle of a word
    (`1<b>2</b>,345`, `인사<em>발령</em>`, a letter in another colour), and a single character glued to the rest of its word
    (`<b>부</b>분`, a decorated initial). Marked, they came out as `1**2**,345` and `**부**분`: noise inside a word, and marks
    Markdown readers do not agree on.

    Deliberate: a span that starts a word and stops before its ending is kept — `**매출**은`, `**신규 요금제**를`. In Korean the
    particle is written onto the stressed word without a space, and most emphasis in real documents has this shape. Known accepted
    gap: an emphasized one-syllable word followed by a particle (`<b>값</b>이`) and a stressed initial (`<b>S</b>trength`) are left
    unmarked too (Pinned by: test_inline_formatting.py::TestPartOfAWord).
    """
    count = len(atoms)
    for name in ("bold", "italic"):
        index = 0
        while index < count:
            if not getattr(atoms[index], name):
                index += 1
                continue
            end = index
            while end < count and getattr(atoms[end], name):
                end += 1
            first, last = atoms[index].text, atoms[end - 1].text
            before = atoms[index - 1].text[-1:] if index else ""
            after = atoms[end].text[:1] if end < count else ""
            starts_inside = before.isalnum() and first[:1].isalnum()
            is_glued_character = (end - index == 1 and len(first.strip()) == 1 and last[-1:].isalnum() and after.isalnum())
            if starts_inside or is_glued_character:
                for position in range(index, end):
                    setattr(atoms[position], name, False)
            index = end


def _without_lead(line: list[_Atom]) -> list[_Atom]:
    """The line with the bullet or number at its start (`_INLINE_LEAD`) made a plain piece of its own, when formatted text
    follows it. The words after a heading mark (`#`) lose their emphasis: a heading is not emphasized."""
    if not any(atom.bold or atom.italic or atom.url for atom in line):
        return line
    head = ""
    for atom in line:
        head += atom.text
        if len(head) >= _LEAD_PROBE:
            break
    lead = _INLINE_LEAD.match(head)
    if lead is None or not lead.group().strip():
        return line
    if lead.group().lstrip().startswith("#"):
        for atom in line:
            atom.bold = atom.italic = False
    left = lead.end()
    result: list[_Atom] = []
    for index, atom in enumerate(line):
        if not left:
            result.extend(line[index:])
            break
        if len(atom.text) <= left:
            left -= len(atom.text)
            result.append(_Atom(atom.text, False, False, None, "plain"))
            continue
        if not atom.text[left:].strip() and all(piece.kind == "space" for piece in line[index + 1:]):
            return line  # nothing but the bullet on this line
        result.append(_Atom(atom.text[:left], False, False, None, "plain"))
        result.append(_Atom(atom.text[left:], atom.bold, atom.italic, atom.url, atom.kind if atom.text[left:].strip() else "space"))
        left = 0
    return result


def _drop_uniform_emphasis(atoms: list[_Atom], start: int) -> None:
    """Clear bold (and italic) in the block `atoms[start:]` when every word of the block carries it.

    Bold over a whole paragraph, list item or table cell line is how that block is styled (a label, a header cell, a slide
    caption), not a stress inside it. Marked, 5,939 of the 7,777 marked lines of 143 real DOCX / PPTX files were whole lines in
    `**…**` — in slide decks a quarter of all lines (Pinned by: test_inline_formatting.py::TestWholeBlocks).
    """
    block = range(start, len(atoms))  # indices, not a slice: a document of many short blocks stays linear
    words = [atoms[index] for index in block if atoms[index].kind == "text"]
    if not words:
        return
    for name in ("bold", "italic"):
        if all(getattr(atom, name) for atom in words):
            for index in block:
                setattr(atoms[index], name, False)


def _write_atoms(atoms: list[_Atom]) -> str:
    out: list[str] = []
    index = 0
    while index < len(atoms):
        url = atoms[index].url
        end = index + 1
        while end < len(atoms) and atoms[end].url == url:
            end += 1
        group = atoms[index:end]
        text = _write_emphasis(group)
        out.append(_write_link(text, url, group) if url else text)
        index = end
    return "".join(out)


def _write_link(markup: str, url: str, group: list[_Atom]) -> str:
    """`[text](url)` with whitespace and placeholders at the edges left outside. Text that is the address itself stays as it is —
    `[https://a.example](https://a.example)` says the same thing twice."""
    parts = _INLINE_PLACEHOLDER.split(markup)
    worded = [position for position in range(0, len(parts), 2) if parts[position].strip()]
    if not worded:
        return markup
    first, last = worded[0], worded[-1]
    head, tail = parts[first], parts[last]
    lead = "".join(parts[:first]) + head[:len(head) - len(head.lstrip())]
    trail = tail[len(tail.rstrip()):] + "".join(parts[last + 1:])
    core = markup[len(lead):len(markup) - len(trail)]
    edges = _without_edge_strike(core)
    if edges is not None and edges[1]:
        lead, core, trail = lead + edges[0], edges[1], edges[2] + trail

    label = "".join(atom.text for atom in group if atom.kind != "plain").strip()
    addresses = {url.rstrip("/"), url.removeprefix("mailto:").rstrip("/")}
    if label.rstrip("/") in addresses or (link_target(label) or "").rstrip("/") in addresses:
        return markup
    return f"{lead}[{_escape_link_text(core)}]({url}){trail}"


def _escape_link_text(text: str) -> str:
    """Escape the brackets that would end the link text early or start another link inside it: a `]` without its `[`, a `[`
    without its `]`, and a bracket pair followed by `(` or `[` (`[a](b)` and `[a][b]` are links). Other balanced brackets stay
    (`[Image: logo]`, `[1]`). A lone backslash at the very end is doubled so that it does not escape the closing bracket."""
    if "[" not in text and "]" not in text and not text.endswith("\\"):
        return text
    escaped: list[int] = []
    opened: list[int] = []
    lone_backslash = False
    for match in _BRACKET_OR_ESCAPE.finditer(text):
        token = match.group()
        if token[0] == "\\":
            lone_backslash = len(token) == 1
        elif token == "[":
            opened.append(match.start())
        elif not opened:
            escaped.append(match.start())
        else:
            opening = opened.pop()
            if text[match.end():match.end() + 1] in ("(", "["):
                escaped.extend((opening, match.start()))
    escaped.extend(opened)
    pieces = []
    position = 0
    for place in sorted(escaped):
        pieces.append(text[position:place])
        pieces.append("\\")
        position = place
    pieces.append(text[position:])
    return "".join(pieces) + ("\\" if lone_backslash else "")


def _write_emphasis(atoms: list[_Atom]) -> str:
    """The atoms of one link (or of the text between links) with their emphasis marks. Bold is the outer mark: `**a *b* c**`."""
    if not any(atom.bold or atom.italic for atom in atoms):
        return "".join(atom.text for atom in atoms)
    out: list[str] = []
    index = 0
    while index < len(atoms):
        atom = atoms[index]
        end = index + 1
        if not atom.bold:
            while end < len(atoms) and not atoms[end].bold and atoms[end].italic == atom.italic:
                end += 1
            text = "".join(piece.text for piece in atoms[index:end])
            out.append(_wrap(_guard(text), "*") if atom.italic else text)
            index = end
            continue
        while end < len(atoms) and atoms[end].bold:
            end += 1
        group = atoms[index:end]
        if all(piece.italic for piece in group):
            out.append(_wrap(_guard("".join(piece.text for piece in group)), "***"))
            index = end
            continue
        inner: list[str] = []
        start = 0
        while start < len(group):
            stop = start + 1
            while stop < len(group) and group[stop].italic == group[start].italic:
                stop += 1
            text = "".join(piece.text for piece in group[start:stop])
            if group[start].italic:
                inner.append(_wrap(_guard(text), "*"))
            else:
                inner.append(_guard(text, leading=start == 0, trailing=stop == len(group)))
            start = stop
        out.append(_wrap("".join(inner), "**"))
        index = end
    return "".join(out)


def _guard(text: str, *, leading: bool = True, trailing: bool = True) -> str:
    """Escape what would change the meaning of the mark written next to `text`: a literal `*` at the edge (it would lengthen the
    mark) and a lone backslash at the end (it would escape the mark)."""
    body = text.strip()
    if not body:
        return text
    lead, trail = text[:len(text) - len(text.lstrip())], text[len(text.rstrip()):]
    is_single = len(body) == 1
    if leading and body[0] == "*":
        body = "\\" + body
    if trailing and not (is_single and len(body) == 2):  # a lone `*` is escaped once
        if body[-1] == "*":
            body = body[:-1] + "\\*"
        elif body[-1] == "\\" and (len(body) - len(body.rstrip("\\"))) % 2:
            body += "\\"
    return f"{lead}{body}{trail}"


def _wrap(text: str, mark: str) -> str:
    """`mark` before the first and after the last character of `text`; whitespace stays outside, and so does half of a
    strikethrough pair (`_without_edge_strike`). A span that strikethrough marks cross in its middle is left unmarked."""
    edges = _without_edge_strike(text.strip())
    if edges is None or not edges[1]:
        return text
    before, body, after = edges
    return f"{text[:len(text) - len(text.lstrip())]}{before}{mark}{body}{mark}{after}{text[len(text.rstrip()):]}"


def _without_edge_strike(body: str) -> tuple[str, str, str] | None:
    """(`~~` moved in front, body, `~~` moved behind), or None when the strikethrough marks in `body` cannot be made to nest with
    a mark put around it.

    The HTML extractor writes `~~` into the text before the spans are resolved, so a span can hold only one half of a pair. At
    the edge of the span that half goes outside: `<s>구 <b>판</b></s>` is `~~구 **판**~~`, not `~~구 **판~~**`. A half in the
    middle (two bold words in two different strikethroughs, joined across the space between them) cannot be moved
    (Pinned by: test_inline_formatting.py::TestHtmlEmphasis::test_strikethrough_and_emphasis_nest).
    """
    if "~~" not in body:
        return "", body, ""
    depth = 0
    places = [match.start() for match in _STRIKE_MARK.finditer(body)]
    lone_closing = lone_opening = -1
    for place in places:
        opens = body[place + 2:place + 3].strip() != ""
        closes = place > 0 and not body[place - 1].isspace()
        if opens and (not closes or depth == 0):
            depth += 1
            if depth == 1:
                lone_opening = place
        elif depth:
            depth -= 1
        elif lone_closing < 0:
            lone_closing = place
        else:
            return None
    if depth > 1 or (depth and lone_closing >= 0):
        return None
    if depth:
        if lone_opening != 0:
            return None
        inner = body[2:].lstrip()
        return body[:len(body) - len(inner)], inner, ""
    if lone_closing >= 0:
        if lone_closing != len(body) - 2:
            return None
        inner = body[:-2].rstrip()
        return "", inner, body[len(inner):]
    return "", body, ""


# ---- Office equations (OMML) -------------------------------------------------------------------------------------

# Equation accent character (m:chr of m:acc) → LaTeX command. A character not listed here is appended after the letter as is.
_ACCENTS = {"\u0302": "hat", "\u0303": "tilde", "\u0304": "bar", "\u0305": "bar", "\u0307": "dot", "\u0308": "ddot",
            "\u20d7": "vec"}


def render_omml(equation: etree._Element) -> str:
    """One m:oMath → ` $LaTeX$ ` to insert in the paragraph text. DOCX and PPTX use the same notation."""
    return f" ${omml_to_latex(equation).strip()}$ "


def _m(tag: str) -> str:
    return f"{{{_M}}}{tag}"


def _group(text: str) -> str:
    return text if len(text) == 1 else f"{{{text}}}"


# Text ending in a one-character script (`x^k`, `a_n`). When a letter follows directly, the script is braced (`_join_math`).
_BARE_SCRIPT_END = re.compile(r"[_^]([^{}\\\s])$")
# Grouping character (m:chr of m:groupChr) → LaTeX command. A character not listed here goes above/below with `\overset` / `\underset`.
_GROUP_BRACES = {"\u23df": "underbrace", "\u23de": "overbrace"}
# The m:d delimiters that LaTeX reads as grouping characters.
_ESCAPED_DELIMITERS = {"{": "\\{", "}": "\\}"}
# Equation font style (m:rPr/m:scr) → LaTeX command. Only double-struck (ℝ), script (ℒ) and fraktur, whose meaning differs, are
# written; sans-serif, monospace etc. are left as is.
_MATH_ALPHABETS = {"double-struck": "mathbb", "script": "mathcal", "fraktur": "mathfrak"}
_MATH_ALPHABET_LETTERS = re.compile(r"[A-Za-z0-9]+")
# The removed side of tracked changes (deleted text, the original place of moved text). It wraps m:r inside equations too. Left out
# as in body paragraphs (`docx._paragraph_text`).
_TRACKED_REMOVALS = (f"{{{_W}}}del", f"{{{_W}}}moveFrom")


def _math_content(element: etree._Element) -> list[etree._Element]:
    """Content children of an equation element (properties m:*Pr / w:rPr and comments / processing instructions are left out)."""
    return [child for child in element if isinstance(child.tag, str) and not child.tag.endswith("Pr")]


def _join_math(element: etree._Element) -> str:
    """Join the child equations in order. When a letter or digit directly follows a one-character script, the script is wrapped in
    `{}` (`x^{k}a^{n-k}`), and a function (m:func) name is spaced from a preceding letter, digit or closing bracket (`a_n cos x`;
    after an operator it stays `α+cos β`). Joined as is, the script and the following letter read as one lump, like `x^ka^{n-k}` and
    `a_ncos x` (Pinned by: test_office_equations.py::TestOmmlScriptsFollowedByText).
    The script is looked for in the last three characters only (the pattern ends at the end of the text, or before a line break
    there): searched from the start for every child, an equation of 80,000 runs took 10 seconds
    (Pinned by: test_audit_core.py::TestEquationWithManyRuns)."""
    text = ""
    for child in element:
        piece = omml_to_latex(child)
        if piece and text and child.tag == _m("func") and (text[-1].isalnum() or text[-1] in ")]}"):
            text += " "
        elif piece[:1].isalnum() and (bare := _BARE_SCRIPT_END.search(text, max(len(text) - 3, 0))):
            text = f"{text[:bare.start(1)]}{{{bare.group(1)}}}"
        text += piece
    return text


# A base that already carries a script on the same side. Script element → the script elements that clash when put on top of it
_CLASHING_SCRIPTS = {"sSup": ("sSup", "sSubSup"), "sSub": ("sSub", "sSubSup"), "sSubSup": ("sSup", "sSub", "sSubSup")}


def _script_base(element: etree._Element, kind: str) -> str:
    """Base of a script (m:e). If the last element of the base is an expression carrying a script on the same side, it is braced —
    `(x²)³` became `x^2^3` (a LaTeX error, confusable with `x^{2^3}`) (Pinned by:
    test_office_equations.py::TestOmmlScriptOnAScriptedBase). A superscript on top of a subscript (`a_i^2`) is left as is."""
    base = element.find(_m("e"))
    if base is None:
        return ""
    text = omml_to_latex(base)
    last = next((child for child in reversed(base) if isinstance(child.tag, str) and not child.tag.endswith("Pr")), None)
    clashes = last is not None and any(last.tag == _m(name) for name in _CLASHING_SCRIPTS[kind])
    return f"{{{text}}}" if clashes else text


def omml_to_latex(element: etree._Element) -> str:
    """Carry an Office equation (OMML; Word and PowerPoint use the same XML) over as a one-line LaTeX-style string.

    The aim is not a complete LaTeX conversion but to let an LLM read the structure (fractions, scripts, radicals, delimiters, sums).
    For unknown elements the child text is joined in order.
    """
    def part(name: str) -> str:
        child = element.find(_m(name))
        return omml_to_latex(child) if child is not None else ""

    def prop(container: str, name: str, default: str) -> str:
        node = element.find(f"{_m(container)}/{_m(name)}")
        return node.get(_m("val"), default) if node is not None else default

    tag = element.tag
    if tag == _m("t"):
        return element.text or ""
    if not isinstance(tag, str) or tag.endswith("Pr") or tag in _TRACKED_REMOVALS:
        # Reading the removed side of tracked changes made an equation edited from 0.3 to 0.5 into `E=A×0.30.5` (Pinned by:
        # test_office_equations.py::TestOmmlTrackedChanges). The inserted side (w:ins, w:moveTo) is read as is by `_join_math` below.
        return ""
    if tag == _m("r"):
        # Dropping double-struck / script letters (m:scr) turned the set of reals ℝ into an ordinary variable R. Only letters and
        # digits are wrapped in the command (Pinned by: test_office_equations.py::TestOmmlMathAlphabets).
        text = _join_math(element)
        command = _MATH_ALPHABETS.get(prop("rPr", "scr", "roman"))
        return _MATH_ALPHABET_LETTERS.sub(lambda match: f"\\{command}{{{match.group()}}}", text) if command else text
    if tag == _m("f"):
        # A fraction without a bar (noBar) is stacked text such as a binomial coefficient (inside parentheses m:d turns it into
        # `\binom`). Linear and skewed fractions (lin, skw) have the `a/b` shape. Emitting them all as `\frac` made a binomial
        # coefficient read as a division (Pinned by: test_office_equations.py::TestOmmlFractionsWithoutABar).
        kind = prop("fPr", "type", "bar")
        if kind == "noBar":
            return f"{{{part('num')} \\atop {part('den')}}}"
        if kind in ("lin", "skw"):
            return f"{_group(part('num'))}/{_group(part('den'))}"
        return f"\\frac{{{part('num')}}}{{{part('den')}}}"
    if tag == _m("sSup"):
        return f"{_script_base(element, 'sSup')}^{_group(part('sup'))}"
    if tag == _m("sSub"):
        return f"{_script_base(element, 'sSub')}_{_group(part('sub'))}"
    if tag == _m("sSubSup"):
        return f"{_script_base(element, 'sSubSup')}_{_group(part('sub'))}^{_group(part('sup'))}"
    if tag == _m("sPre"):
        return f"{{}}_{_group(part('sub'))}^{_group(part('sup'))}{part('e')}"
    if tag == _m("rad"):
        degree = part("deg")
        return f"\\sqrt[{degree}]{{{part('e')}}}" if degree else f"\\sqrt{{{part('e')}}}"
    if tag == _m("d"):
        return _delimited(element, prop("dPr", "begChr", "("), prop("dPr", "endChr", ")"), prop("dPr", "sepChr", "|"))
    if tag == _m("nary"):
        operator = prop("naryPr", "chr", "∫")
        sub, sup = part("sub"), part("sup")
        limits = (f"_{_group(sub)}" if sub else "") + (f"^{_group(sup)}" if sup else "")
        return f"{operator}{limits} {part('e')}"
    if tag == _m("groupChr"):
        # The grouping character (default: bottom brace ⏟) is a property, so it was dropped and what the note under the group
        # referred to was lost (Pinned by: test_office_equations.py::TestOmmlGroupCharactersAndBraces::test_group_character_is_kept).
        symbol, position = prop("groupChrPr", "chr", "\u23df"), prop("groupChrPr", "pos", "bot")
        if symbol in _GROUP_BRACES:
            return f"\\{_GROUP_BRACES[symbol]}{{{part('e')}}}"
        return f"\\{'overset' if position == 'top' else 'underset'}{{{symbol}}}{{{part('e')}}}"
    if tag == _m("func"):
        return f"{part('fName')} {part('e')}"
    if tag == _m("limLow"):
        return f"{part('e')}_{_group(part('lim'))}"
    if tag == _m("limUpp"):
        return f"{part('e')}^{_group(part('lim'))}"
    if tag == _m("acc"):
        # The accent (the overbar of x̄ etc.) was dropped, leaving only x. The default character is ^
        # (Pinned by: test_office.py::TestDocxFieldsAndRuns::test_equation_array_rows_and_accents_are_kept).
        accent = prop("accPr", "chr", "\u0302")
        return f"\\{_ACCENTS[accent]}{{{part('e')}}}" if accent in _ACCENTS else f"{part('e')}{accent}"
    if tag == _m("bar"):
        return f"\\{'overline' if prop('barPr', 'pos', 'bot') == 'top' else 'underline'}{{{part('e')}}}"
    if tag == _m("phant"):
        # A phantom only takes up space. The text of a hidden one (m:show 0) was joined too, giving `hiddenx` (Pinned by:
        # test_office_equations.py::TestOmmlPhantom). Removing the width/height (zeroWid etc.) of a visible phantom (the default)
        # does not affect the text.
        return "" if prop("phantPr", "show", "on") in _OFF_VALUES else part("e")
    if tag == _m("eqArr"):
        # A multi-line equation. Joining the rows with no separator gave `a+b=1a-b=3`. The same row separator as a matrix is used.
        return " \\\\ ".join(omml_to_latex(child) for child in element.iterchildren(_m("e")))
    if tag == _m("m"):
        rows = [" & ".join(omml_to_latex(cell) for cell in row.iterchildren(_m("e")))
                for row in element.iterchildren(_m("mr"))]
        return "\\begin{matrix}" + " \\\\ ".join(rows) + "\\end{matrix}"
    return _join_math(element)


def _delimited(element: etree._Element, begin: str, end: str, separator: str) -> str:
    """Delimiters (m:d). A single bar-less fraction inside parentheses is a binomial coefficient (`\\binom`, the parentheses are
    absorbed); a single equation array inside a delimiter that has only an opening `{` is a system of equations (`cases`). Brace
    delimiters are written `\\{` / `\\}` — left as is they are LaTeX grouping and the braces of `{a,b}` vanish
    (Pinned by: test_office_equations.py::TestOmmlFractionsWithoutABar, TestOmmlGroupCharactersAndBraces)."""
    parts = list(element.iterchildren(_m("e")))
    only = _math_content(parts[0]) if len(parts) == 1 else []
    if len(only) == 1 and only[0].tag == _m("f") and (begin, end) == ("(", ")"):
        kind = only[0].find(f"{_m('fPr')}/{_m('type')}")
        if kind is not None and kind.get(_m("val")) == "noBar":
            numerator, denominator = (only[0].find(_m(name)) for name in ("num", "den"))
            return (f"\\binom{{{omml_to_latex(numerator) if numerator is not None else ''}}}"
                    f"{{{omml_to_latex(denominator) if denominator is not None else ''}}}")
    if len(only) == 1 and only[0].tag == _m("eqArr") and begin == "{" and not end:
        return f"\\begin{{cases}}{omml_to_latex(only[0])}\\end{{cases}}"
    opening, closing = _ESCAPED_DELIMITERS.get(begin, begin), _ESCAPED_DELIMITERS.get(end, end)
    return opening + separator.join(omml_to_latex(part) for part in parts) + closing


# ---- Dutmal ------------------------------------------------------------------------------------------------------

def format_dutmal(main: str, annotation: str = "") -> str:
    """Hangul (HWP) dutmal (a small annotation attached above or below the main text) → "main(annotation)". HWP and HWPX use the
    same shape."""
    return f"{main}({annotation})" if main and annotation else main or annotation


# ---- Hand-typed bullets ------------------------------------------------------------------------------------------

# Bullet symbols other than '- ' / '* ' that make a line joined by a line break (DOCX w:br, PPTX a:br) inside a list item paragraph
# count as an item one level down (DOCX `_DocxRenderer._list_item_lines`, PPTX `pptx._item_lines`).
HAND_TYPED_BULLET = re.compile(r"[·•●○◦▪■□◆◇▶►➢✓]")


# ---- Symbol fonts ------------------------------------------------------------------------------------------------

# A symbol-font character is a glyph number inside the font (00FE, or F0FE in the private use area). The same number means different
# things per font (Symbol B3 is ≥, Wingdings A8 is □), so where the font is known it is converted straight to Unicode with the
# per-font table (`SYMBOL_GLYPHS`). Used by DOCX w:sym, characters of symbol-font runs (w:rFonts) and characters of PPTX runs
# (a:sym, a:latin typeface). Characters not in the table are left to the shared mapping of `markdown.clean_text` (erased if not
# there either). The shared mapping holds only numbers with one meaning regardless of font, so Wingdings ☒ (78) and □ (6F) were erased
# (Pinned by: test_office.py::TestDocxStruckDeletedAndSymbolRuns, test_office_symbols_fields_notes.py::TestSymbolFontCharactersInText).
SYMBOL_FONTS = ("symbol", "wingdings", "webdings")


def _glyph_run(start: int, glyphs: str) -> dict[int, str]:
    """Glyphs laid out in order from glyph number `start`. A space marks a number with no corresponding Unicode character."""
    return {start + offset: glyph for offset, glyph in enumerate(glyphs) if glyph != " "}


# Symbol is the standard mapping of the Adobe Symbol encoding (bracket pieces and extender pieces are left out). The digits and
# punctuation at 0x20-0x3F are the same as ASCII. Wingdings / Wingdings 2 hold only what carries meaning in documents (checks, boxes,
# bullets, circled digits, arrows, pointing hands). Where the exact Unicode character lies outside the Basic Multilingual Plane, as
# for arrows and checks, a common character of the same meaning is used (the same characters as the existing clean_text mapping).
SYMBOL_GLYPHS: dict[str, dict[int, str]] = {
    "symbol": {
        **{code: chr(code) for code in range(0x20, 0x40)},
        **_glyph_run(0x41, "ΑΒΧΔΕΦΓΗΙϑΚΛΜΝΟΠΘΡΣΤΥςΩΞΨΖ[∴]⊥_"),
        **_glyph_run(0x61, "αβχδεφγηιϕκλμνοπθρστυϖωξψζ{|}∼"),
        **_glyph_run(0xA0, "€ϒ′≤⁄∞ƒ♣♦♥♠↔←↑→↓°±″≥×∝∂•÷≠≡≈…  ↵ℵℑℜ℘⊗⊕∅∩∪⊃⊇⊄⊂⊆∈∉∠∇®©™∏√⋅¬∧∨⇔⇐⇑⇒⇓◊⟨®©™∑"),
        0x22: "∀", 0x24: "∃", 0x27: "∋", 0x2A: "∗", 0x2D: "−", 0x40: "≅", 0xF1: "⟩", 0xF2: "∫",
    },
    "wingdings": {
        **_glyph_run(0x45, "☜☞☝☟"), **_glyph_run(0x81, "①②③④⑤⑥⑦⑧⑨⑩"), **_glyph_run(0x8C, "❶❷❸❹❺❻❼❽❾❿"),
        **_glyph_run(0xDF, "←→↑↓↖↗↙↘"), **_glyph_run(0xEF, "⇦⇨⇧⇩"), **_glyph_run(0xFB, "✗✓☒☑"),
        0x6C: "●", 0x6E: "■", 0x6F: "□", 0x71: "❑", 0x72: "❒", 0x75: "◆", 0x76: "❖", 0x78: "☒", 0x9F: "•", 0xA1: "○",
        0xA7: "▪", 0xA8: "□", 0xAB: "★", 0xD8: "➢", 0xE8: "➔",
    },
    "wingdings 2": {0x4F: "✗", 0x50: "✓", 0x51: "☒", 0x52: "☑", 0x53: "☒", 0x54: "☒", 0xA3: "☐"},
}
# Characters to convert in the text of a symbol-font run: U+F020-F0FF, font numbers moved into the private use area.
_SYMBOL_PRIVATE_USE = re.compile("[-]")


def symbol_glyph(font: str, code: int) -> str | None:
    """Glyph number `code` (00xx or F0xx) of symbol font `font` (case-insensitive) → Unicode character. None if not in the table."""
    table = SYMBOL_GLYPHS.get(font.strip().lower())
    if table is None:
        return None
    return table.get(code - 0xF000 if 0xF000 <= code <= 0xF0FF else code)


def symbol_font_text(font: str | None, text: str, *, ascii_font: str | None = None, latin_font: str | None = None) -> str:
    """Convert private use area characters (U+F0xx) in the text of a symbol-font run with that font's table. A font not in the
    tables or a character not in the table is left as is (`clean_text` handles it with the shared mapping).

    `ascii_font` / `latin_font` are the fonts that draw ordinary characters (ASCII, the rest of Latin-1). If that is Symbol, those
    characters are also taken as Adobe Symbol encoding glyph numbers and converted — m, a and ³, shown on screen as μ, α and ≥, came
    out unchanged as '10 mm, a ³ 30' (Pinned by:
    test_office_symbols_fields_notes.py::TestSymbolFontCharactersInText::
    test_plain_letters_in_a_symbol_font_run_in_docx_become_the_symbols_they_show). Ordinary characters in Wingdings are not
    converted (the table holds only some meaningful symbols). Characters outside Latin-1 such as Hangul are drawn with the East
    Asian font, so they are not touched."""
    plain_ascii, plain_latin = (bool(name) and name.strip().lower() == "symbol" for name in (ascii_font, latin_font))
    private = bool(font) and font.strip().lower() in SYMBOL_GLYPHS and _SYMBOL_PRIVATE_USE.search(text) is not None
    if not (private or plain_ascii or plain_latin):
        return text

    plain = SYMBOL_GLYPHS["symbol"]

    def glyph(character: str) -> str:
        code = ord(character)
        if 0xF000 <= code <= 0xF0FF:
            return (symbol_glyph(font, code) if private else None) or character
        if (plain_ascii and code < 0x80) or (plain_latin and 0x80 <= code <= 0xFF):
            return plain.get(code, character)
        return character

    return "".join(map(glyph, text))


# ---- Tables inside footnotes -------------------------------------------------------------------------------------

# A footnote is one line (`[^1]: …`), so a table inside it joins cells with ' / ' (same as `TableGrid.flatten`) and rows with '; '.
NOTE_ROW_SEPARATOR = "; "


def note_table(rows: Iterable[Iterable[str]]) -> str:
    """Rows of a table inside a footnote → one line. Line breaks in cell text are merged into spaces; empty cells and empty rows are
    dropped. DOCX, HWP and HWPX use the same shape. Rendering a Markdown table and then merging with spaces gave
    '| 연도 | 값 | | --- |', and joining the cell paragraphs gave '구분 값 A 10'
    (Pinned by: test_office_symbols_fields_notes.py::TestDocxTableInsideFootnote, test_hwp.py::TestTableInsideFootnote)."""
    lines = (" / ".join(text for text in (" ".join(cell.split()) for cell in row) if text) for row in rows)
    return NOTE_ROW_SEPARATOR.join(line for line in lines if line)


# ---- Comments (review remarks) -----------------------------------------------------------------------------------

COMMENT_TARGET_MAX = 40  # show only this much of the body span the comment is attached to ('…' beyond it)


def comment_line(author: str, text: str, target: str = "", *, reply: bool = False, cell: str = "") -> str:
    """One comment line: `> Comment(author, on "body span"): text`; a reply is `> Reply(author): text`. DOCX, PPTX and HWPX put them
    at the end of the document/slide, Excel at the end of the sheet with `cell E5` added. Comments were dropped wholesale without a
    warning, so the LLM could not see review remarks (Pinned by: test_office_comments.py, test_hwp.py::TestMemo,
    test_spreadsheet_notes_pictures_and_times.py). Author, cell and span are left out when absent."""
    target = " ".join(target.split())
    if len(target) > COMMENT_TARGET_MAX:
        target = target[:COMMENT_TARGET_MAX] + "…"
    details = ", ".join(part for part in (" ".join(author.split()), f"cell {cell}" if cell else "",
                                          f'on "{target}"' if target else "") if part)
    label = "Reply" if reply else "Comment"
    return f"> {label}({details}): {' '.join(text.split())}" if details else f"> {label}: {' '.join(text.split())}"


# ---- List levels inside table cells ------------------------------------------------------------------------------

def cell_level_mark(level: int) -> str:
    """Level mark of a list line inside a table cell: none for level 1, "└ " for level 2, "└─ " for level 3 …. Shared by HWP, HWPX
    and DOCX (also SmartArt in a cell and PPTX speaker-note quote blocks, where leading spaces are erased).

    Leading spaces of a line in a cell are erased when the table is built (`markdown._clean_cell`), so indentation cannot convey the
    level. Doubling the bullet ("-- ") cannot be used for numbered lists ("가.") and also reads as a long dash, so a box-drawing
    character was chosen: it does not clash with paragraph heads, is widely used in tree notation and is read by LLMs as a sub-item.
    It is one token per level, so it adds little noise to search and embeddings
    (Pinned by: test_hwp.py::TestListLevelsInsideTableCells).
    """
    return f"└{'─' * (level - 1)} " if level > 0 else ""


# ---- Hangul (HWP) paragraph heads --------------------------------------------------------------------------------

# Placeholder for an OLE object whose contents were not read (a pasted Excel sheet or chart). HWP/HWPX always put it; DOCX/PPTX put
# it when the preview picture could not be registered (picture extraction is off or there is no preview)
# (Pinned by: test_hwp.py::TestEmbeddedObjects, test_office_chart_units_and_ole.py::TestOfficeOleObjectPlaceholders).
OLE_PLACEHOLDER = "[OLE object]"


def unread_ole_warning(count: int) -> str:
    return f"The contents of {count} OLE object(s) (pasted tables, charts, etc.) could not be converted."


# Placeholders for non-text objects (pictures, and OLE objects / videos in HWP/HWPX). A paragraph piece holding only these is not
# text that takes a paragraph head.
_OBJECT_PLACEHOLDERS = re.compile(rf"{IMAGE_MARKER.pattern}|{re.escape(OLE_PLACEHOLDER)}|\[Video\]")


def is_placeholder_only(text: str) -> bool:
    """Is the paragraph piece nothing but object placeholders (HWP, HWPX)? In a numbered/outline paragraph where a picture comes
    before a table or text box, the head was attached to the first piece holding only the picture, so '2.' / '#' was left alone and
    the real text lost its number (Pinned by: test_hwp_heads_and_shapes.py::
    TestHeadOfAParagraphThatStartsWithAPicture)."""
    return not _OBJECT_PLACEHOLDERS.sub("", text).strip()


def paragraph_head(mark: str, level: int, *, in_cell: bool, heading: bool) -> str:
    """Paragraph head text for HWP/HWPX. `mark` is a number ('가.'), a bullet ('-') or nothing (''); `level` is the level, from 0.

    Lower-level numbers in the body take the same `list_item_prefix` form as DOCX/PPTX (`  - 가. `) — with only indentation added,
    `clean_text` erased it and a blank line appeared at every item (Pinned by:
    test_hwp_heads_and_shapes.py::TestLowerLevelNumbers). Inside a cell it is `cell_level_mark`; an outline paragraph that becomes a
    heading gets the number only (a bullet is dropped — the same as a numbered heading in DOCX).
    """
    if not mark:
        return ""
    if heading:
        return "" if mark == "-" else f"{mark} "
    if in_cell:
        return f"{cell_level_mark(level)}{mark} "
    return f"{'  ' * level}- " if mark == "-" else list_item_prefix(mark, level)


# Hangul (HWP) paragraph left margins (HWPUNIT, the HWP binary value) that are equal within this much are the same level.
_MARGIN_TOLERANCE = 100


@dataclass(slots=True)
class BulletNesting:
    """Aligns the output level of HWP/HWPX bullet paragraphs by left margin. Hangul reports that make □, ○ and - separate bullet
    definitions, all at level 0, and express the hierarchy by paragraph margin alone became a flat `- ` list, with no telling which
    item something was under.

    Conservatively: one level down only when the level equals that of the immediately preceding unclosed bullet item and the margin
    is larger. With an equal or smaller margin that item is closed (sibling / upper level). A non-bullet paragraph closes only items
    indented more than itself, and with no margin (0) closes them all (the same direction as DOCX `_list_depth` /
    `_close_list_items`). Numbered paragraphs are not touched. Table cells, footnotes and text boxes are counted separately (the
    caller creates a new one. Pinned by: test_hwp_heads_and_shapes.py::TestBulletNestingByMargin).
    """

    _open: list[tuple[int, int, int]] = field(default_factory=list)  # unclosed earlier items: (level, left margin, emitted depth)

    def bullet(self, level: int, margin: int) -> int:
        """Output level of one bullet paragraph."""
        items = self._open
        while items and items[-1][1] >= margin - _MARGIN_TOLERANCE:
            items.pop()
        depth = min(items[-1][2] + 1, 8) if items and items[-1][0] == level else level
        items.append((level, margin, depth))
        return depth

    def other(self, margin: int) -> None:
        """Earlier items closed by a non-bullet paragraph (numbered, heading, body)."""
        if margin <= _MARGIN_TOLERANCE:
            self._open.clear()
        while self._open and self._open[-1][1] > margin + _MARGIN_TOLERANCE:
            self._open.pop()


# ---- Auto-numbering ----------------------------------------------------------------------------------------------

# Hangul (HWP) number shapes. HWP writes the number in this order (bits 5-8 of the paragraph head info attribute), HWPX this name
# (numFormat).
NUMBER_SHAPES = (
    "DIGIT", "CIRCLED_DIGIT", "ROMAN_CAPITAL", "ROMAN_SMALL", "LATIN_CAPITAL", "LATIN_SMALL", "CIRCLED_LATIN_CAPITAL",
    "CIRCLED_LATIN_SMALL", "HANGUL_SYLLABLE", "CIRCLED_HANGUL_SYLLABLE", "HANGUL_JAMO", "CIRCLED_HANGUL_JAMO",
    "HANGUL_PHONETIC", "IDEOGRAPH", "CIRCLED_IDEOGRAPH",
)
_HANGUL = "가나다라마바사아자차카타파하"
_JAMO = "ㄱㄴㄷㄹㅁㅂㅅㅇㅈㅊㅋㅌㅍㅎ"
# Counting numbers (일, 一): digit characters, place characters (십, 백, 천), empty-place character. Numbers after 10 are also written
# as counting words — shapes checked on screen in LibreOffice (십일, 이십, 백일, 백십일, 十一, 二十, 一百, 一百零一, 一百一十一). Korean
# does not write '일' before a place and does not write empty places
# (Pinned by: test_office_round45.py::TestNumbersPastTheTable).
_COUNTING = {"HANGUL_PHONETIC": ("일이삼사오육칠팔구", "십백천", ""), "IDEOGRAPH": ("一二三四五六七八九", "十百千", "零")}
# Word's koreanDigital writes a digit per place (10 → 일영, same as the LibreOffice screen)
_HANGUL_DIGIT_LETTERS = "영일이삼사오육칠팔구"
# Circled characters only as far as Unicode has them (beyond that, written without the circle). The same for parenthesized digits
# and digits with a full stop (Word-only shapes).
_CIRCLED = {
    "CIRCLED_DIGIT": "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳",
    "PARENTHESIZED_DIGIT": "⑴⑵⑶⑷⑸⑹⑺⑻⑼⑽⑾⑿⒀⒁⒂⒃⒄⒅⒆⒇",
    "DIGIT_FULL_STOP": "⒈⒉⒊⒋⒌⒍⒎⒏⒐⒑⒒⒓⒔⒕⒖⒗⒘⒙⒚⒛",
    "CIRCLED_LATIN_CAPITAL": "ⒶⒷⒸⒹⒺⒻⒼⒽⒾⒿⓀⓁⓂⓃⓄⓅⓆⓇⓈⓉⓊⓋⓌⓍⓎⓏ",
    "CIRCLED_LATIN_SMALL": "ⓐⓑⓒⓓⓔⓕⓖⓗⓘⓙⓚⓛⓜⓝⓞⓟⓠⓡⓢⓣⓤⓥⓦⓧⓨⓩ",
    "CIRCLED_HANGUL_SYLLABLE": "㉮㉯㉰㉱㉲㉳㉴㉵㉶㉷㉸㉹㉺㉻",
    "CIRCLED_HANGUL_JAMO": "㉠㉡㉢㉣㉤㉥㉦㉧㉨㉩㉪㉫㉬㉭",
    "CIRCLED_IDEOGRAPH": "㊀㊁㊂㊃㊄㊅㊆㊇㊈㊉",
}
_UNCIRCLED = {"CIRCLED_DIGIT": "DIGIT", "PARENTHESIZED_DIGIT": "DIGIT", "DIGIT_FULL_STOP": "DIGIT",
              "CIRCLED_LATIN_CAPITAL": "LATIN_CAPITAL", "CIRCLED_LATIN_SMALL": "LATIN_SMALL",
              "CIRCLED_HANGUL_SYLLABLE": "HANGUL_SYLLABLE", "CIRCLED_HANGUL_JAMO": "HANGUL_JAMO",
              "CIRCLED_IDEOGRAPH": "IDEOGRAPH"}
# Word number format (w:numFmt) → Hangul (HWP) number shape name. A format not listed here is written in Arabic digits. 01., ⑴, ⒈
# and １ are Word-only shapes that Hangul lacks, so they get names of their own (Pinned by: test_office.py::
# TestOfficeReadingOrderAndListMarks::test_zero_padded_parenthesized_full_stop_and_full_width_numbers).
DOCX_NUMBER_SHAPES = {
    "decimal": "DIGIT", "decimalZero": "ZERO_PADDED_DIGIT", "decimalEnclosedParen": "PARENTHESIZED_DIGIT",
    "decimalEnclosedFullstop": "DIGIT_FULL_STOP", "decimalFullWidth": "FULLWIDTH_DIGIT",
    "decimalEnclosedCircle": "CIRCLED_DIGIT", "decimalEnclosedCircleChinese": "CIRCLED_DIGIT",
    "upperRoman": "ROMAN_CAPITAL", "lowerRoman": "ROMAN_SMALL", "upperLetter": "LATIN_CAPITAL",
    "lowerLetter": "LATIN_SMALL", "ganada": "HANGUL_SYLLABLE", "chosung": "HANGUL_JAMO",
    "koreanDigital": "HANGUL_DIGIT", "koreanCounting": "HANGUL_PHONETIC", "ideographDigital": "IDEOGRAPH",
    "chineseCounting": "IDEOGRAPH", "japaneseCounting": "IDEOGRAPH", "ideographEnclosedCircle": "CIRCLED_IDEOGRAPH",
    "none": "NONE",
}
_FULLWIDTH_DIGITS = str.maketrans("0123456789", "０１２３４５６７８９")
_ROMAN = ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"), (100, "C"), (90, "XC"), (50, "L"), (40, "XL"), (10, "X"),
          (9, "IX"), (5, "V"), (4, "IV"), (1, "I"))


def format_number(value: int, shape: str) -> str:
    """Write one number in a number shape (a `NUMBER_SHAPES` name). Unknown shapes and out-of-range values give Arabic digits. "NONE"
    (DOCX numFmt none) is empty text — Word does not write that level's number and shows only the rest of the number text (lvlText)."""
    if shape == "NONE":
        return ""
    if value < 1:
        return str(value)
    if shape in _CIRCLED:
        circled = _CIRCLED[shape]
        return circled[value - 1] if value <= len(circled) else format_number(value, _UNCIRCLED[shape])
    if shape in ("ROMAN_CAPITAL", "ROMAN_SMALL") and value < 4000:
        roman = _roman(value)
        return roman if shape == "ROMAN_CAPITAL" else roman.lower()
    if shape in ("LATIN_CAPITAL", "LATIN_SMALL") and value <= 26 * 3:
        # After A…Z come AA, BB … (both Word and Hangul repeat the same letter). The start number is a file value, so the length is bounded
        letter = chr(ord("A") + (value - 1) % 26) * ((value - 1) // 26 + 1)
        return letter if shape == "LATIN_CAPITAL" else letter.lower()
    if shape in ("HANGUL_SYLLABLE", "HANGUL_JAMO"):
        letters = _HANGUL if shape == "HANGUL_SYLLABLE" else _JAMO
        return letters[(value - 1) % len(letters)]
    if shape in _COUNTING and value < 10_000:
        return _counting_number(value, *_COUNTING[shape])
    if shape == "HANGUL_DIGIT":
        return "".join(_HANGUL_DIGIT_LETTERS[int(digit)] for digit in str(value))
    if shape == "ZERO_PADDED_DIGIT":
        return f"{value:02d}"
    if shape == "FULLWIDTH_DIGIT":
        return str(value).translate(_FULLWIDTH_DIGITS)
    return str(value)


def list_item_prefix(number: str, depth: int) -> str:
    """Head of a numbered list item (DOCX, PPTX): indentation + number. If the number of a lower level (depth > 0) is not of the
    '1.' form, a bullet is added, as in `- a. `.

    `markdown.clean_text` / `join_blocks` recognize only lines starting with `-`, `*` or `1.` as list items, keeping their
    indentation and joining the items. Sub-items like 'a.', '1)', '①', '1.1.' lost their indentation and gained blank lines around
    them, so there was no telling they were sub-items. It is the same form as letter numbers in HTML. The top level keeps the number
    as is — adding a bullet to '제1조' / '가.' paragraphs is noise
    (Pinned by: test_office.py::TestDocxNumbering::test_sub_items_with_numbers_other_than_digit_period_keep_their_level).
    """
    indent = "  " * depth
    return f"{indent}{number} " if depth == 0 or re.fullmatch(r"\d+\.", number) else f"{indent}- {number} "


def _counting_number(value: int, digits: str, units: str, zero: str) -> str:
    """1-9999 as counting words: 'digit + place' from the highest place. If there is an empty-place character (零), one is inserted
    for an empty place between digits. The 1 before a place is never written in Korean, and in Han characters is omitted only at the
    very front of 10-19 (十一, 一百一十一)."""
    parts = []
    gap = False
    for place in range(3, -1, -1):
        digit = value // 10 ** place % 10
        if digit == 0:
            gap = gap or bool(parts)
            continue
        if gap and zero:
            parts.append(zero)
        gap = False
        unit = units[place - 1] if place else ""
        parts.append(unit if digit == 1 and unit and not zero else digits[digit - 1] + unit)
    text = "".join(parts)
    return text[1:] if zero and 10 <= value < 20 else text


def _roman(value: int) -> str:
    parts = []
    for amount, symbol in _ROMAN:
        count, value = divmod(value, amount)
        parts.append(symbol * count)
    return "".join(parts)


@dataclass(frozen=True, slots=True)
class NumberLevel:
    """One level of a numbered list. `<mark><level>` in `template` (HWP/HWPX `^1`, DOCX `%1`) is replaced by that level's number."""

    template: str
    shape: str = "DIGIT"
    start: int = 1
    # Legal-style numbering (DOCX w:isLgl): in this level's number text, the numbers of the upper levels are also written in Arabic
    # digits ('1.1.' under 'I.')
    # (Pinned by: test_office.py::TestOfficeLineBreaksNestedFieldsAndMetadata::test_docx_legal_numbering_shows_every_level_in_arabic_digits).
    legal: bool = False
    # The deepest upper level that makes this level count from 1 again (1-based, DOCX w:lvlRestart — ECMA-376 17.9.11). None restarts
    # whenever any shallower level, the one right above included, appears (the default; HWP/HWPX always use this). 0 never restarts —
    # '제N조' (Article N) continuing across chapters
    # (Pinned by: test_office_revisions_and_numbering.py::TestDocxLevelRestart).
    restart_after: int | None = None


@dataclass(slots=True)
class Numbering:
    """Per-level counters of one numbered list (HWP/HWPX paragraph numbering, DOCX w:num).

    When a level's number appears, deeper levels count from the start again (except levels held back by DOCX w:lvlRestart), and
    shallower levels that have not appeared yet are taken at their start number
    (the "4. → 4.1.1.1." behavior of ParaHead in the public hwpxlib sample. Pinned by: test_hwp.py::TestParagraphNumbering,
    TestHwpxObjects::test_paragraph_numbers_and_bullets_are_kept).
    """

    levels: list[NumberLevel]
    mark: str = "^"
    counters: dict[int, int] = field(default_factory=dict)

    def advance(self, level: int) -> str:
        """Number string of one paragraph at `level` (from 0). A level not in the definition uses the shape of the last level."""
        if not self.levels:
            return ""
        level = min(max(level, 0), len(self.levels) - 1)
        for shallower in range(level):
            self.counters.setdefault(shallower, self._level(shallower).start)
        self.counters[level] = self.counters[level] + 1 if level in self.counters else self._level(level).start
        for deeper in [key for key in self.counters if key > level and self._restarts_after(key, level)]:
            del self.counters[deeper]

        def number(match: re.Match[str]) -> str:
            token = match.group(1)
            target = level if token in ("N", "n") else int(token) - 1
            if not 0 <= target <= level:
                return ""
            shape = "DIGIT" if self.levels[level].legal else self._level(target).shape
            return format_number(self.counters.get(target, self._level(target).start), shape)

        return re.sub(rf"{re.escape(self.mark)}(\d+|[Nn])", number, self.levels[level].template).strip()

    def _level(self, level: int) -> NumberLevel:
        return self.levels[min(level, len(self.levels) - 1)]

    def _restarts_after(self, deeper: int, level: int) -> bool:
        """When a number at `level` (from 0) appears, is the deeper `deeper` level counted again (`NumberLevel.restart_after`)?"""
        restart_after = self._level(deeper).restart_after
        return restart_after is None or level < restart_after
