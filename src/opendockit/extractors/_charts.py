"""OOXML charts and SmartArt as data, shared by PPTX, DOCX, HWPX and Excel.

- A chart (`c:chartSpace`) uses the same XML in PPTX, DOCX and HWPX (`Chart/chart*.xml`). It goes in as a table of its
  values, not as a picture: an LLM reads numbers rather than bar shapes. The new chart kinds (`cx:chartSpace`) use the
  same XML in PPTX and DOCX.
- SmartArt uses the same data part (`dgm:dataModel`) in PPTX and DOCX.
"""

from bisect import bisect_right
from collections.abc import Callable
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, Overflow
import posixpath
import re

from lxml import etree

from opendockit.extractors._lists import MAX_LEVEL, cell_level_mark
from opendockit.extractors._package import _PACKAGE_RELATIONSHIPS, _relationships_of, _resolve_target, decimal_int, parse_part
from opendockit.markdown import TableGrid, render_table


_C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_CX = "http://schemas.microsoft.com/office/drawing/2014/chartex"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_DGM = "http://schemas.openxmlformats.org/drawingml/2006/diagram"


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
    # The 1904 system starts 1,462 days later, so its last date has a serial that much smaller. Checked against the 1900 limit only,
    # 9999-12-31 of the 1900 system overflowed and the document was rejected as corrupted
    # (Pinned by: test_audit_core.py::TestChartDateCategoryPastTheLastDate).
    if not (has_date or has_time) or not 0 <= serial <= _LAST_DATE_SERIAL - (1462 if date1904 else 0):
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
    # that is not there made it read as a value known to the day — the same rule as Excel cell dates (`_sheet_format._date_text`)
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
    loss read as a profit — the same rule as Excel cells (`_sheet_format._format_section`)
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
    # a format with dozens of decimal places / a value with too many digits (InvalidOperation), or a value whose exponent is at the
    # edge of what a decimal number can hold (Overflow when multiplied) — use the raw value
    # (Pinned by: test_audit_core.py::TestChartValueTooLargeForItsFormat)
    except (InvalidOperation, Overflow):
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
