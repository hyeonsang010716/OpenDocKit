"""What the PDF modules share: the line, table, page and paragraph records, the thresholds used by more than one stage, and
small helpers for boxes and fill colours.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
import math
import re
import threading

from pdfminer.utils import decode_text


# A page with more text pieces than this is read without text-box grouping (boxes_flow). Grouping takes time and memory with the
# square of the number of text boxes, so a single page of 4,700 scattered characters used 54 seconds and 3.2GB (0.4 seconds without
# grouping). Over 993 pages of public PDFs the maximum is 1,900 and the median 92
# (Pinned by: test_pdf.py::test_page_of_thousands_of_scattered_text_pieces_is_read_without_box_grouping). The number is measured by
# joining pdfium's text pieces into line pieces (`_text_line_pieces`) — Chrome places Hangul character by character, so when counted
# as pieces an ordinary two-column paper page (about 2,900 characters) went over the limit and the column order got mixed
# (Pinned by: test_pdf_resource_limits.py::TestGlyphByGlyphText). The limit value is the owner's decision and stays as it is.
_MAX_GROUPED_TEXT_PIECES = 2_000


# Bullets in the Symbol and Wingdings fonts come out as private use area characters (a PDF exported from a Word list). These are
# the ones markdown._SYMBOL_FONT_GLYPHS later converts to Unicode symbols.
_SYMBOL_BULLETS = "\uf0b7\uf06c\uf0a1\uf0a7\uf06e\uf071\uf075\uf076\uf0a8\uf0d8\uf0e0\uf0e8\uf0f0\uf0fc\uf0fe\uf02d"
_LONE_BULLET = re.compile(rf"^[•●○◦▪▫■□◆◇►▶➢✓✔※\-–·*{_SYMBOL_BULLETS}]$")


_BARE_NUMBER = re.compile(r"^\d{1,3}$")


_WORD_CHAR = re.compile(r"[0-9A-Za-z가-힣ㄱ-ㅎ一-鿿]")
# Line starting with a bullet symbol. First-level bullets in a slide PDF have large text and were mistaken for headings.
_BULLET_START = re.compile(rf"^[•●○◦▪▫■□◆◇►▶➢✓✔※\-–―·*{_SYMBOL_BULLETS}]")
# Minimum font size ratio (relative to the body) for a heading. The level (#, ##, ###) is assigned not by the ratio but by the rank
# of the heading size within the document.
_HEADING_MIN_RATIO = 1.2
# Borderless table: taken as a table only when at least 3 consecutive lines each have 3 or more short pieces (at most this ratio of
# the page width) at the same height (tolerance in pt) and the pieces sit on the column boundaries of the first line. A two-column
# layout (2 pieces) and long sentence lines are not tables.
_TABLE_ROW_TOLERANCE = 2.5
_ROW_OVERLAP_RATIO = 0.5  # vertical overlap for pieces with offset tops to count as one row (vs. the lower line height, staggered in `_visual_rows`)
_TABLE_CELL_MAX_RATIO = 0.25


# Cell holding only a numeric value ('1,500', '-3.2%', '(120)', '△45'). When a row without any such cell follows rows with values,
# it is the header row of a new table.
_NUMBER_CELL = re.compile(r"^[(△▲▼+\-−–]?\d[\d,.]*%?\)?$")
# Maximum cell text length of a header row to be repeated on a table continued onto the next page (`_carry_table_headers`). The
# body cell on the previous page of a frame box filling every slide's page (layout caught as a ruled table) was put on as a header.
_HEADER_CELL_MAX_CHARS = 30


_SPACED_CHAR = re.compile(r"[가-힣一-鿿]")


# Right-aligned numeric value joined as '항목 값' (item, value) even beyond the gap bound above (`_right_aligned_numbers`):
# '3,000,000', '₩1,200', '(120)', '15.3%', '2025.03.31', '1,200원', '12건', and the unit values of a product specification table
# '220V', '1.5kW', '60L/일', '52dB', '45kg', '0.5㎡', '6개월' (Pinned by:
# test_pdf_forms_and_amounts.py::TestFarRightAmounts::test_right_aligned_values_with_units_stay_beside_their_items).
_FAR_UNIT = (r"%|원|천원|만원|백만원|억원|건|명|개|회|일|시간|점|년|개월|월|주|분|초|대|매|부|권|층|세|"
             r"[kKmM]?(?:V|W|Wh|A|Hz)|VA|kVA|dB|[mk]?g|t|[mck]?m[23²³]?|[mM]?L|ℓ|℃|°C|"
             r"[㎜㎝㎞㎏㎎㎖㎗㎘㎡㎥㎾㎽㎸㏈]")
_FAR_VALUE = re.compile(rf"^[(△▲▼+\-−–₩$€£¥]{{0,2}}\d[\d,.]*(?:(?:{_FAR_UNIT})(?:/(?:{_FAR_UNIT}|h|s))?)?\)?$")


# Re-reading two columns mixed paragraph by paragraph (_read_paragraph_columns_in_turn): the minimum number of wide lines for a
# column (on the side with more), and the gap (multiple of the line height) within which a line that came back counts as continuing
# directly below the column. A blank line between paragraphs or the space above a subheading does not exceed it; a picture or table
# separating the two columns does.
_COLUMN_LINES_MIN = 5


# Maximum gap between a separately placed list number and the first line of its item (relative to the number's line height,
# `_marker_rows`). The hanging indent of a Word list (0.25 inch = 18pt) fits within it at a 10pt font.
_MARKER_GAP_RATIO = 3.0


# Side length (pt) of a rectangle taken as a check box, and the maximum distance between the box and its label (a line starting to
# the right of the box or ending to its left) (relative to the larger of the box width and the label's character size,
# `_mark_checkboxes`). 14pt between an 8pt box and a 10pt label is common.
_CHECKBOX_SIDE = (5.0, 16.0)


_DIGITS = re.compile(r"\d+")


# Candidate area for headers and footers (ratio of the page height) and the share of pages needed to count as repeated.
_MARGIN_RATIO = 0.1
_REPEAT_RATIO = 0.5
# Ratio when odd and even pages are counted separately. At one half, the title (always on odd pages) and signature block (always on
# even pages) of a batch of four-page forms were removed. The left/right page headers of books and journals are on 78–100% of those
# pages (Pinned by: test_pdf.py::TestRepeatedMarginsAndBorderlessTables::
# test_batched_four_page_forms_keep_their_titles_and_signature_lines).
_SIDE_REPEAT_RATIO = 2 / 3
# Maximum gap (relative to the line height) for the line above or below a ruled table to count as belonging to that table (title,
# unit, note). Narrower than one skipped line.
_CAPTION_GAP_RATIO = 1.5


_FILL_WHITE_MIN = 0.95  # white if all fill colour components (0–1) are at least this (`_is_mark_fill`)


# Shaded table (`_with_shaded_rows`): only a filled rectangle with both sides longer than this (pt) is cell shading (thinner ones
# are ruling lines drawn as fills).
_SHADE_MIN_SIDE = 2.0


# Minimum size to crop out as a picture (pt, about 1.4cm). A smaller image is an icon or bullet.
_MIN_FIGURE_POINTS = 40


# pdfium (the PDFium C library) is not thread-safe. Every pdfium call in the process (opening a document, rendering, text, closing)
# is serialized with this lock. Without it the process died with SIGSEGV/SIGTRAP when several PDFs were converted at once in a
# thread pool (Pinned by: test_pdf.py::test_concurrent_pdf_conversion_in_threads_does_not_crash). Objects are closed inside the
# lock rather than left to garbage collection.
_PDFIUM_LOCK = threading.RLock()


@dataclass(slots=True)
class _Line:
    top: float
    bottom: float
    x0: float
    x1: float
    text: str
    size: float
    # Pieces of the line split where it is at least `_CELL_GAP_RATIO` × the character size apart ((x0, x1, text), left → right).
    # Filled only when the text read from the pieces equals the line text. Used only to split a piece in which pdfminer merged
    # neighbouring cells inside a borderless table (`_split_merged_cells`).
    parts: tuple[tuple[float, float, str], ...] = ()
    # In addition to `parts`, pieces also split where it is at least `_UNSPACED_GAP_RATIO` × the character size apart without a
    # space character. Used only when splitting lines in which no table was found and searching again (`_split_line_table_runs`).
    fine_parts: tuple[tuple[float, float, str], ...] = ()
    # The original lines merged into this line (cell text wrapped onto two lines, `_with_centred_wrapped_cells`). They are removed
    # from the body flow together when it moves into a table.
    joined: tuple["_Line", ...] = ()
    # If this line is a piece (`parts`) of another line, that original line. Used to remove the original line of a piece moved into
    # a table from the body flow.
    source: "_Line | None" = None

    @property
    def height(self) -> float:
        return max(self.bottom - self.top, 1.0)


@dataclass(slots=True)
class _Table:
    top: float
    text: str
    bottom: float
    x0: float
    x1: float
    # The cell text the table was built from (first row is the header row) and each column's horizontal range (x0, x1). Consulted
    # when repeating the header row on a table continued onto the next page (`_carry_table_headers`). An empty tuple for a table
    # whose column ranges could not be determined.
    rows: list[list[str]] = field(default_factory=list)
    spans: tuple[tuple[float, float], ...] = ()
    # Line pieces of each row of a borderless table. Used to re-fit a continued table to the columns of the previous table (an
    # empty list for a ruled table).
    lines: list[list[_Line]] = field(default_factory=list)
    # Cell boxes of a ruled table (x0, top, x1, bottom) and each row's vertical range (top, bottom). Consulted when placing a
    # picture inside a cell in that cell (`_place_images_in_cells`). An empty tuple if the number of row ranges does not match the
    # number of rows.
    cells: tuple[tuple[float, float, float, float], ...] = ()
    bands: tuple[tuple[float, float], ...] = ()
    # Top coordinate of each text line of a cell holding a picture (index into `cells`). If it matches the number of lines of the
    # cell text, the picture is placed between the lines at that height.
    cell_lines: dict[int, tuple[float, ...]] = field(default_factory=dict)
    # Ruled table continued from a ruled table on the previous page. The top row is a title only when it is a horizontally merged
    # cell (`_carry_table_headers`, `_table_text`).
    continued: bool = False
    # Columns of a ruled table whose bottom cell has no bottom border (a vertically merged cell split at the page end,
    # `_open_below_columns`). Its text is continued in the empty top cell of the next page's table (`_carry_open_cells`).
    open_below: tuple[int, ...] = ()


@dataclass(slots=True)
class _Page:
    number: int
    width: float
    height: float
    lines: list[_Line]
    tables: list[_Table]
    has_images: bool
    # Visible area (x0, top, x1, bottom) = MediaBox ∩ CropBox. Character and picture coordinates are relative to the MediaBox, but
    # pdfium draws only this area.
    view: tuple[float, float, float, float]
    # (x0, top, x1, bottom, original width in pixels / displayed width in pt)
    figures: list[tuple[float, float, float, float, float]] = field(default_factory=list)
    # Has many curves that look like characters converted to outlines (_OUTLINE_MIN_POINTS).
    has_outlines: bool = False
    # Page read with pdfium's simple extraction. It has no line positions or font sizes, so it is left out of header removal and
    # line gap statistics.
    simple: bool = False
    # Separately placed text (_repair_placed_runs): the (real character width, space width) determined on this page. If they could
    # not be determined but a font looks like such text, pending — the page is read again once another page of the same document
    # yields the values.
    placement_fit: tuple[float, float] | None = None
    placement_pending: bool = False
    # Paragraphs read by turning text drawn rotated by 90 degrees within the page (an identifier in the margin, a table set
    # sideways). Left out of the body flow and the table and header checks and placed at the end of the page (_sideways_paragraphs).
    sideways: list[str] = field(default_factory=list)
    # Page of a document whose text layer does not hold the body (_untrusted_text_layer_pages). The whole page is handed over as
    # an image even though it has characters.
    text_untrusted: bool = False
    # Values entered into form fields ('field name: value' lines, _form_values). They are in the widget annotations, not the page
    # content, so they are placed separately at the end of the page.
    form_values: list[str] = field(default_factory=list)
    # Visible area against which the margins of repeated lines are measured (_text_view). None means the whole page (rotated
    # pages, simple-extraction pages).
    text_view: tuple[float, float, float, float] | None = None
    # Number of characters removed as hidden text (outside the visible area, size 0, copies, watermarks) on a page without a single
    # line. Decides whether to swap in pdfium's simple extraction.
    hidden_chars: int = 0
    # Page where most visible characters have zero width, so pdfminer cannot tell character order or spacing (_unmeasured).
    unmeasured: bool = False
    # Page with no text, pictures or vectors at all that has a content stream which failed to decode (_has_undecodable_contents).
    undecodable: bool = False
    # Thin horizontal ruling lines of the page (height, left end, right end; top → bottom, `_thin_rules`). Used to join the
    # multi-line header of a three-line table (`_join_ruled_header_lines`) and to cut a borderless table at its bottom border
    # (`_cut_at_closing_rule`).
    rules: list[tuple[float, float, float]] = field(default_factory=list)
    # Filled rectangles (top, bottom, left end, right end, `_shade_bands`). Used to join the wrapped line of a striped table's last
    # row to that row (`_table_runs`).
    bands: list[tuple[float, float, float, float]] = field(default_factory=list)


@dataclass(slots=True)
class _Paragraph:
    lines: list[_Line] = field(default_factory=list)

    @property
    def top(self) -> float:
        return self.lines[0].top


def _is_white(colour: object) -> bool:
    """Whether the fill colour is white (a numeric colour that is not `_is_shade`). A single grey value (0) is a numeric colour
    too. An unknown colour such as a pattern is not white."""
    colour = (colour,) if isinstance(colour, (int, float)) else colour
    numeric = isinstance(colour, (tuple, list)) and bool(colour) and all(isinstance(value, (int, float)) for value in colour)
    return numeric and not _is_shade(colour)


def _bbox(obj: dict) -> tuple[float, float, float, float]:
    return obj["x0"], obj["top"], obj["x1"], obj["bottom"]


def _is_shade(colour: object) -> bool:
    """Whether a fill colour counts as cell shading: a numeric grey, RGB or CMYK colour that is not white (`_FILL_WHITE_MIN`)."""
    if not isinstance(colour, (tuple, list)) or not colour or not all(isinstance(value, (int, float)) for value in colour):
        return False
    if len(colour) == 4:
        cyan, magenta, yellow, black = colour
        colour = tuple((1 - value) * (1 - black) for value in (cyan, magenta, yellow))
    return min(colour) < _FILL_WHITE_MIN


def _box_area(box: tuple[float, float, float, float]) -> float:
    return (box[2] - box[0]) * (box[3] - box[1])


def _encloses(outer: tuple[float, ...], inner: tuple[float, ...]) -> bool:
    return (outer[0] - 1 <= inner[0] and outer[1] - 1 <= inner[1]
            and inner[2] <= outer[2] + 1 and inner[3] <= outer[3] + 1)


def _visual_rows(lines: list[_Line], staggered: bool = False) -> list[list[_Line]]:
    """Group line fragments at the same height into one row (top to bottom, left to right within a row). Same height =
    top coordinates within `_TABLE_ROW_TOLERANCE`.

    With `staggered`, a fragment whose top is out of step is also grouped if it has the same font size as the topmost
    fragment of the row and overlaps it vertically by at least `_ROW_OVERLAP_RATIO` of the smaller line height — only
    between a fragment containing Hangul/Hanja and one that does not. A generator that top-aligns table cells
    (LibreOffice) has a different ascent per font, so the baselines and glyph boxes of the Hangul cells and number
    cells of the same row were 4.5–5pt out of step (11pt font). Looking at the top alone, '사원' and '3,200,000'
    became different rows and the borderless table scattered column by column. Two consecutive lines do not overlap by
    as much as half even at 100% line spacing. Used only for the second search for borderless tables
    (`_aligned_table_runs`); without the character-type condition, staggered fragments of equations in a paper became
    a table (Pinned by: test_pdf.py::TestRepeatedMarginsAndBorderlessTables::
    test_borderless_table_row_is_grouped_when_fonts_put_the_cells_at_different_heights).
    """
    def same_row(first: _Line, line: _Line) -> bool:
        if abs(line.top - first.top) <= _TABLE_ROW_TOLERANCE:
            return True
        overlap = min(first.bottom, line.bottom) - line.top
        return (staggered and bool(first.size) and abs(first.size - line.size) <= 0.5
                and bool(_SPACED_CHAR.search(first.text)) != bool(_SPACED_CHAR.search(line.text))
                and overlap >= min(first.height, line.height) * _ROW_OVERLAP_RATIO)

    rows: list[list[_Line]] = []
    for line in sorted(lines, key=lambda line: (line.top, line.x0)):
        if rows and same_row(rows[-1][0], line):
            rows[-1].append(line)
        else:
            rows.append([line])
    return [sorted(row, key=lambda line: line.x0) for row in rows]


def _in_margin(line: _Line, page: _Page) -> bool:
    """Whether the line is in the top or bottom margin area of the page (`_MARGIN_RATIO` of the height of the visible
    area). A print PDF has more paper outside the trim line (CropBox), so measured by the paper height a footer inside
    the trim line fell outside the margin. When the visible area equals the paper, the result is the same as before."""
    _, top, _, bottom = page.text_view or (0.0, 0.0, page.width, page.height)
    band = (bottom - top) * _MARGIN_RATIO
    return line.bottom <= top + band or line.top >= bottom - band


class _BoxIndex:
    """An index of boxes (x0, top, x1, bottom) split into height bands. `holds(obj)` is equivalent to
    `_inside_any(obj, boxes)`.

    On a page with 7,500 cells every character scanned all the cells and one page took tens of seconds (Pinned by:
    test_pdf_resource_limits.py::TestManyCellsOnOnePage). Boxes taller than `_MAX_BANDS` bands (cells reaching far off
    the page) and boxes with non-finite coordinates are not put into bands and are always scanned — making bands for
    the full height of every cell made a 1.6KB PDF use 13GB (Pinned by: TestCellsReachingFarOffThePage)."""

    _BAND = 16.0
    _MAX_BANDS = 128  # 2,048pt — every cell inside a page fits into bands

    def __init__(self, boxes: list[tuple[float, float, float, float]]) -> None:
        self._bands: dict[int, list[tuple[float, float, float, float]]] = {}
        self._tall: list[tuple[float, float, float, float]] = []
        for box in boxes:
            if not (math.isfinite(box[1]) and math.isfinite(box[3])) or box[3] - box[1] > self._BAND * self._MAX_BANDS:
                self._tall.append(box)
                continue
            for band in range(math.floor(box[1] / self._BAND), math.floor(box[3] / self._BAND) + 1):
                self._bands.setdefault(band, []).append(box)

    def holds(self, obj: dict) -> bool:
        center_y = (obj["top"] + obj["bottom"]) / 2
        band = math.floor(center_y / self._BAND) if math.isfinite(center_y) else None
        return _inside_any(obj, self._bands.get(band, [])) or _inside_any(obj, self._tall)

    def near(self, box: tuple[float, float, float, float]) -> list[tuple[float, float, float, float]]:
        """The boxes that may overlap `box` vertically: those in the bands it covers and the tall ones, each once. Every box
        is returned when `box` itself is too tall for bands or not finite."""
        if not (math.isfinite(box[1]) and math.isfinite(box[3])) or box[3] - box[1] > self._BAND * self._MAX_BANDS:
            found = [other for boxes in self._bands.values() for other in boxes]
        else:
            found = [other for band in range(math.floor(box[1] / self._BAND), math.floor(box[3] / self._BAND) + 1)
                     for other in self._bands.get(band, ())]
        return list({id(other): other for other in [*found, *self._tall]}.values())


def _inside_any(obj: dict, boxes: list[tuple[float, float, float, float]]) -> bool:
    center_x = (obj["x0"] + obj["x1"]) / 2
    center_y = (obj["top"] + obj["bottom"]) / 2
    return any(x0 <= center_x <= x1 and top <= center_y <= bottom for x0, top, x1, bottom in boxes)


def _info_text(value: bytes) -> str:
    """Decode the bytes of a document information string. The standard is UTF-16BE (BOM) or PDFDocEncoding, but some
    generators write a Korean title as raw UTF-8 or CP949 bytes, which read as PDFDocEncoding became 'íŁœê¸• ì€œëª©'
    (Pinned by: test_pdf.py::TestInfoStrings). If there are high bytes, strict UTF-8 and then strict EUC-KR yielding
    Hangul are tried in that order, and if neither works it is PDFDocEncoding. Latin PDFDocEncoding text almost always
    fails both decodings (a space after 'é', etc.). Accepting the CP949 extension characters as well turned 'Opiełka'
    (ł = 0x9B) into 'Opie쌽a'."""
    if value.startswith((b"\xfe\xff", b"\xff\xfe")):
        return value.decode("utf-16", errors="replace")
    if value.startswith(b"\xef\xbb\xbf"):
        return value[3:].decode("utf-8", errors="replace")  # UTF-8 of PDF 2.0
    if any(byte >= 0x80 for byte in value):
        for encoding in ("utf-8", "euc_kr"):
            try:
                text = value.decode(encoding)
            except UnicodeDecodeError:
                continue
            if encoding == "utf-8" or any("가" <= char <= "힣" for char in text):
                return text
    return decode_text(value)


# Table finder settings. By default pdfplumber discards lines shorter than 1pt before joining them. A dotted cell border is drawn
# as hundreds of short 0.4pt lines, so left as is the border disappeared and cells merged (23 names crowded into one cell, and
# values and rows went out of step). If they are not discarded, joining (default 3pt) turns the dotted line into one line.
_TABLE_SETTINGS = {"edge_min_length_prefilter": 0}


def _text_by_middle(page: object) -> tuple[list[float], list[dict]]:
    """The page's non-blank characters in order of their vertical middle (page order where the middles are equal), as (list of
    middles, list of characters). Built once per page (`_closed_at_open_ends`, `_join_soft_wraps`). A character whose middle
    is not a number is inside no box and is left out."""
    def build() -> tuple[list[float], list[dict]]:
        placed = [((char["top"] + char["bottom"]) / 2, char) for char in page.chars if char["text"].strip()]
        ordered = sorted((item for item in placed if item[0] == item[0]), key=lambda item: item[0])
        return [middle for middle, _ in ordered], [char for _, char in ordered]

    return _page_index(page, "text", build)


def _page_index(page: object, name: str, build: Callable[[], object]) -> object:
    """The list `name` of the page, built with `build` the first time it is asked for (`_verticals_by_x`, `_text_by_middle`,
    `_sideways_by_middle`). The lists are kept on the page object together with the page's objects they were built from: a
    page that has been read again (`page.close()` releases its objects) gets new lists. `_real_tables` drops them when it is
    done (`_dropping_page_indexes`)."""
    held = page.__dict__.get("_page_indexes")
    if held is None or held[0] is not page.objects:
        held = page.__dict__["_page_indexes"] = (page.objects, {})
    if name not in held[1]:
        held[1][name] = build()
    return held[1][name]


_TABLE_MIN_ROWS = 3
_TABLE_MIN_COLUMNS = 3


# Gap for a line to count as wrapped cell text in a borderless table (_continues_cell): at most this multiple of this table's gap
# between rows, and narrower than the row gap by at least this multiple of the line height. Line spacing inside a cell is clearly
# narrower than row spacing. The staggered lines of a list with tightly packed rows (2pt gap) are not wrapped lines.
_WRAP_GAP_RATIO = 0.6
# The 'narrower by at least this multiple of the line height' side of the condition above. In a Chrome table (4px cell padding) the
# gap between rows is 12.7pt at a 12pt line height and the gap of a wrapped line 6pt, so the difference (6.7pt) fell short of 0.6×
# (7.2pt) and the wrapped line became a row of its own (Pinned by: test_pdf_table_columns.py::TestWideWrappedFirstColumn).
# For a list with a 2pt gap the difference is far below even 0.5×, so it is still filtered out.
_WRAP_GAP_MIN_RATIO = 0.5
