"""What the PDF modules share: the line, table, page and paragraph records, the thresholds used by more than one stage, and
small helpers for boxes and fill colours.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
import math
import re
import threading

from pdfminer.utils import decode_text
from pdfplumber.page import CroppedPage
from pdfplumber.utils import clip_obj, get_bbox_overlap, obj_to_bbox


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
# Minimum font size ratio (relative to the body) for a paragraph to be a heading by its size alone (`_is_heading`). Lower than
# `_HEADING_MIN_RATIO`, which the reading-order rules use to tell a title from the column body: a subheading set 12.5pt over a
# 10.5pt body (1.19) is a heading too. Pinned by: test_pdf_headings.py::TestHeadingSizes.
_HEADING_SIZE_RATIO = 1.15
# A heading set in a bold face at about the body size (`_is_bold_heading`): its size is at least this ratio of the body size (a
# smaller bold line is a caption or a table label, not a heading).
_BOLD_HEADING_MIN_RATIO = 0.97
# Share of a line's letters and digits drawn in a bold face for the line to count as bold (`_Line.bold`).
_BOLD_LINE_RATIO = 0.8
# A document whose body lines are bold this often (by characters) sets its body in a bold face, so a bold line tells nothing there.
_BOLD_BODY_MAX_RATIO = 0.3
# Font names of a bold face ('Brill-Bold', 'Arial,BoldItalic', 'NanumGothicExtraBold', 'Helvetica-Black', 'FrutigerLT-Heavy',
# 'MinionPro-Semibold', 'Garamond-Demi'), with the short names of TeX fonts ('NimbusRomNo9L-Medi', 'CMBX12', 'CMSSBX10').
# 'Medium' and 'Book' are body weights.
# 'Demi' counts only as a word of the name and not as 'DemiLight', a light weight common in Korean fonts
# ('NotoSansCJKkr-DemiLight'), nor inside a word ('Academica').
_BOLD_FONT = re.compile(r"(?i:bold|black|heavy)|Demi(?!(?i:light))|[-,_ ](?i:demi)(?!(?i:light))"
                        r"|(?i:[-,]bd\b|[-,]bd[a-z]|-medi\b|-mediita\b|^cmbx|^cmssbx|^cmb\d)")
# A figure label alone ('Diagram 4', 'Figure 6', 'Table 3.1', '그림 2'), the badge of a caption set in a box (`_is_caption_box`)
# The number is digits, one capital letter ('Figure A.1') or a Roman number ('Table IV'): a word like 'Tablet', 'Maps' or 'Images' is no label.
_CAPTION_LABEL = re.compile(r"(?:(?i:fig(?:ure)?\.?|diagram|chart|graph|table|exhibit|photo|image|map)|그림|표|도표|사진|차트)\s*"
                            r"(?:[0-9]{1,3}|[A-Z]\b|[IVX]{1,4}\b)(?:[.\-][0-9]{1,3})*[.:]?")
# Text of a caption or a note label: a bold 'Figure 3.' or 'Source:' at the body size is not a heading.
# '주' only as the label of a note ('주:', '주1)', '주)'): '주 52시간 근무제' (a week of 52 hours) is a heading. A Roman number
# is in capitals ('Table IV'), so 'Box i…' is no caption.
_CAPTION_START = re.compile(r"^(?:(?i:fig(?:ure)?|table|tab|chart|graph|diagram|map|source|sources|note|notes|exhibit|box|photo|image)"
                            r"|그림|표|도표|사진|출처|자료)\s*(?:[\d.:)\-]|[ⅠⅡⅢⅣⅤ]|[IVX]+\b)|^주\s*\d*\s*[):]")
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
    # A line of the notes under a footnote rule, and the line that starts one note (`[^n]: …`), on a page whose footnotes were
    # linked (`_pdf_footnotes._link_footnotes`). The notes are paragraphs of their own, one per note (`_continues`).
    footnote: bool = False
    footnote_start: bool = False
    # At least `_BOLD_LINE_RATIO` of the line's letters are drawn in a bold face (`_BOLD_FONT`). A heading set in bold at about
    # the body size is told from the body only by this (`_is_bold_heading`); lines built from other lines leave it False,
    # but a number or bullet joined to its item, which has the weight of the item (`_pdf_row_joins._join_row_fragments`).
    bold: bool = False
    # Whether the last glyph drawn on the line is a space: True when it is, False when the line draws its spaces as glyphs and
    # ends without one, None when it draws none or was built from other lines. A generator that writes the space a line was
    # broken at leaves none where it broke the line inside a word (`_pdf_paragraphs._breaks_inside_a_word`).
    space_at_end: bool | None = None

    @property
    def height(self) -> float:
        return max(self.bottom - self.top, 1.0)


def _space_at_end(chars: list[dict]) -> bool | None:
    """`_Line.space_at_end` of a line drawn with the characters `chars`."""
    if not chars:
        return None
    if max(chars, key=lambda char: char["x0"]).get("text") == " ":
        return True
    return False if any(char.get("text") == " " for char in chars) else None


def _is_bold_line(chars: list[dict]) -> bool:
    """Whether at least `_BOLD_LINE_RATIO` of the letters and digits (`_WORD_CHAR`) of a line's characters are drawn in a bold
    face (`_BOLD_FONT`, the subset prefix 'ABCDEF+' of the font name left out)."""
    letters = [char for char in chars if _WORD_CHAR.match(char.get("text", ""))]
    if not letters:
        return False
    bold = sum(1 for char in letters if _BOLD_FONT.search(str(char.get("fontname", "")).rpartition("+")[2]))
    return bold >= len(letters) * _BOLD_LINE_RATIO


def _original_lines_of(line: _Line) -> tuple[_Line, ...]:
    """The line, the line it was split from (`source`), the lines joined into it (`joined`) and the lines those were split
    from: every line of the body flow it stands for.

    A cell joined from pieces split from one line ('Dept' and '45,896' of a right-aligned cell beside a left-aligned one,
    which pdfminer read as one line) lists the pieces in `joined`, not their line: only the pieces were removed, so the
    line stayed in the body flow and its text came out twice (Pinned by:
    test_audit_pdf_tail.py::TestJoinedPiecesOfASplitLine)."""
    found = [line, *line.joined]
    return tuple(found + [original.source for original in found if original.source is not None])


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
    # pdfium's number of the page (`_PdfiumPages`): None for pdfminer's number less one, -1 when pdfium must not open the page.
    pdfium_index: int | None = None
    # ids of the lines of columns of prose that were found as a borderless table and dropped (`_reads_as_prose_columns`). They
    # are not joined into one line per row as table-like rows (`_table_like_rows`), which would merge the columns line by line.
    prose_lines: set[int] = field(default_factory=set)
    # Thin horizontal ruling lines of the page (height, left end, right end; top → bottom, `_thin_rules`). Used to join the
    # multi-line header of a three-line table (`_join_ruled_header_lines`) and to cut a borderless table at its bottom border
    # (`_cut_at_closing_rule`).
    rules: list[tuple[float, float, float]] = field(default_factory=list)
    # Filled shapes of the size of a letter standing in the lines of text where no character stands
    # (`_pdf_artifacts._letter_shapes`): text the page draws as outlines, which is missing from what was read.
    letter_shapes: int = 0
    # The tables the structure tree of a tagged PDF states on this page (`_pdf_tagged_tables._TaggedTable`), with the text and
    # the box of each cell. Made into tables where the page agrees with them (`_extract_tagged_tables`).
    tagged: list = field(default_factory=list)
    # Filled rectangles (top, bottom, left end, right end, `_shade_bands`). Used to join the wrapped line of a striped table's last
    # row to that row (`_table_runs`).
    bands: list[tuple[float, float, float, float]] = field(default_factory=list)
    # Vertical lines of the page (x, top, bottom; `_pdf_banded_tables._upright_rules`). A region between horizontal rules
    # that one of them stands in or beside is a box or a grid, not a table drawn with horizontal rules only.
    uprights: list[tuple[float, float, float]] = field(default_factory=list)
    # A '^' character is drawn on the page. The raised numbers of the line text are written with '^' (`_scripted_text`), so on
    # such a page a typed '^1' cannot be told from a raised 1 and footnotes are not linked (`_pdf_footnotes._link_footnotes`).
    literal_carets: bool = False
    # A raised number that the line texts do not show as raised: a raised or small digit `_scripted_text` left as plain text
    # (`_raised_digits_unshown`), or a digit clearly smaller than the other characters of the table cells or of the sideways text
    # (`_small_digits`). It could be the mark of a note that the body does not show, so footnotes are not linked
    # (`_pdf_footnotes._link_footnotes`).
    unshown_marks: bool = False


@dataclass(slots=True)
class _Paragraph:
    lines: list[_Line] = field(default_factory=list)

    @property
    def top(self) -> float:
        return self.lines[0].top


def _fill_colour(colour: object) -> tuple[float, ...] | None:
    """A numeric fill colour as grey or RGB components (0–1; CMYK converted to RGB), or None for an unknown colour such as a
    pattern. pdfminer gives a DeviceGray colour ('0.85 g', '/DeviceGray cs 0.85 sc') as a bare number, the others as tuples:
    a grey fill tested as a tuple only was no shading, and a zebra table filled in grey fell apart into one table per shaded
    row (Pinned by: test_pdf_shaded_tables.py::TestShadedTables::test_zebra_rows_shaded_in_device_grey_stay_one_table)."""
    if isinstance(colour, (int, float)):
        colour = (colour,)
    if not isinstance(colour, (tuple, list)) or not colour or not all(isinstance(value, (int, float)) for value in colour):
        return None
    if len(colour) == 4:
        cyan, magenta, yellow, black = colour
        return tuple((1 - value) * (1 - black) for value in (cyan, magenta, yellow))
    return tuple(colour)


def _is_white(colour: object) -> bool:
    """Whether the fill colour is white (a numeric colour, `_fill_colour`, that is not `_is_shade`). An unknown colour such as
    a pattern is not white."""
    components = _fill_colour(colour)
    return components is not None and min(components) >= _FILL_WHITE_MIN


def _bbox(obj: dict) -> tuple[float, float, float, float]:
    return obj["x0"], obj["top"], obj["x1"], obj["bottom"]


def _is_shade(colour: object) -> bool:
    """Whether a fill colour counts as cell shading: a numeric grey, RGB or CMYK colour (`_fill_colour`) that is not white
    (`_FILL_WHITE_MIN`)."""
    components = _fill_colour(colour)
    return components is not None and min(components) < _FILL_WHITE_MIN


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


class _PointGrid:
    """Items by the 64pt grid cell of one point of theirs (`point(item)` gives its x and y). `within(x0, top, x1, bottom)`
    gives every item whose point lies in that closed rectangle, and some others besides: the caller applies its own exact
    test, so only whether an item is given matters, not the order. Items whose point is not a finite number are always
    given, and so is every item for a rectangle with a side that is not finite, that is turned inside out or that spans more
    than `_MAX_CELLS` grid cells. Flooring a coordinate divided by the cell size keeps the order of coordinates, so the
    cells searched hold every point in the rectangle.

    A picture in each cell of a ruled grid had every cell scan all the characters of the page, and every check box all the
    characters of its page (Pinned by: test_pdf_images.py::TestManyPicturesInRuledCells,
    test_audit_pdf_head.py::TestManySquaresOnOnePage::test_marks_are_those_of_scanning_every_character_and_line)."""

    _CELL = 64.0
    _MAX_CELLS = 4096

    def __init__(self, items: list, point: Callable[[object], tuple[float, float]]) -> None:
        self._items = items
        self._cells: dict[tuple[int, int], list] = {}
        self._odd: list = []
        for item in items:
            x, y = point(item)
            if math.isfinite(x) and math.isfinite(y):
                self._cells.setdefault((math.floor(x / self._CELL), math.floor(y / self._CELL)), []).append(item)
            else:
                self._odd.append(item)

    def within(self, x0: float, top: float, x1: float, bottom: float) -> list:
        if not all(math.isfinite(value) for value in (x0, top, x1, bottom)) or x1 < x0 or bottom < top:
            return self._items
        first_column, last_column = math.floor(x0 / self._CELL), math.floor(x1 / self._CELL)
        first_row, last_row = math.floor(top / self._CELL), math.floor(bottom / self._CELL)
        # Counted as integers: `len()` of a range of more than `sys.maxsize` cells (a cell 1e21pt wide) raised OverflowError
        # and rejected the document (Pinned by: test_pdf_images.py::TestManyPicturesInRuledCells::test_huge_cell_is_read)
        if (last_column - first_column + 1) * (last_row - first_row + 1) > self._MAX_CELLS:
            return self._items
        columns, rows = range(first_column, last_column + 1), range(first_row, last_row + 1)
        return [*self._odd, *(item for column in columns for row in rows for item in self._cells.get((column, row), ()))]


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


class _ObjectBands:
    """The page's objects of every kind by 64pt height band (`_cropped`). `overlapping(box)` gives, per kind and in page
    order, every object that can overlap the box vertically: those in the bands the box covers, and those taller than
    `_MAX_BANDS` bands, turned inside out or with a top or bottom that is not finite, which are always given. None for a box
    whose top or bottom is not finite, that is turned inside out or that covers more than `_MAX_BOX_BANDS` bands."""

    _BAND = 64.0
    _MAX_BANDS = 128
    _MAX_BOX_BANDS = 4096

    def __init__(self, objects: dict[str, list[dict]]) -> None:
        self._objects = objects
        self._bands: dict[str, dict[int, list[int]]] = {}
        self._always: dict[str, list[int]] = {}
        for kind, items in objects.items():
            bands, always = self._bands.setdefault(kind, {}), self._always.setdefault(kind, [])
            for position, obj in enumerate(items):
                top, bottom = obj["top"], obj["bottom"]
                if not (math.isfinite(top) and math.isfinite(bottom)) or top > bottom or bottom - top > self._BAND * self._MAX_BANDS:
                    always.append(position)
                    continue
                for band in range(math.floor(top / self._BAND), math.floor(bottom / self._BAND) + 1):
                    bands.setdefault(band, []).append(position)

    def overlapping(self, box: tuple[float, float, float, float]) -> dict[str, list[dict]] | None:
        top, bottom = box[1], box[3]
        if not (math.isfinite(top) and math.isfinite(bottom)) or top > bottom:
            return None
        first, last = math.floor(top / self._BAND), math.floor(bottom / self._BAND)
        if last - first + 1 > self._MAX_BOX_BANDS:  # counted as integers, as in `_PointGrid.within`
            return None
        covered = range(first, last + 1)
        found = {}
        for kind, items in self._objects.items():
            bands = self._bands[kind]
            positions = {*self._always[kind], *(position for band in covered for position in bands.get(band, ()))}
            found[kind] = [items[position] for position in sorted(positions)]
        return found


def _region_on_page(page: object, left: float, top: float, right: float,
                    bottom: float) -> tuple[float, float, float, float] | None:
    """The region (left, top, right, bottom) grown by 1pt on every side and cut to the page's box, the box a table is searched
    again in. None when no part of the region with an area lies on the page: its cut box would be turned inside out (or not a
    number), and pdfplumber's crop raises on it. Characters of a page turned with /Rotate are not filtered to the visible area
    (`_text_view`), so rules and text drawn past the page edge make tables that lie wholly off the page; one such table made the
    crop raise and the whole document fail as corrupted, every page lost (Pinned by: test_pdf_rotated_pages.py::
    TestTablesOffThePage)."""
    page_left, page_top, page_right, page_bottom = page.bbox
    box = (max(left - 1, page_left), max(top - 1, page_top), min(right + 1, page_right), min(bottom + 1, page_bottom))
    if not (box[0] < box[2] and box[1] < box[3]):
        return None
    return box


def _cropped(page: object, box: tuple[float, float, float, float]) -> object:
    """The page cropped to `box` for a table to be searched again in it (`_crop_for_table_search`), cropped from a
    page holding only the objects that can overlap the box (`_ObjectBands`, built once per page as one of the page's lists
    for its tables, `_page_index`). The crop keeps only objects overlapping the box, in page order; the objects left out
    overlap it nowhere, so the cropped page holds the same objects in the same order. A table grown from shaded bands
    cropped the whole page once per table, so a page of 400 separate shaded tables took 14 seconds (Pinned by:
    test_pdf_shaded_tables.py::TestManySeparateShadedTables::test_cropped_page_holds_the_objects_of_a_crop_of_the_whole_page,
    test_many_shaded_tables_take_linear_time)."""
    near = _page_index(page, "object_bands", lambda: _ObjectBands(page.objects)).overlapping(box)
    if near is None:
        return _crop_for_table_search(page, box)
    subset = page.filter(lambda obj: True)
    subset._objects = near  # as pdfplumber's own `dedupe_chars` hands a derived page its objects
    return _crop_for_table_search(subset, box)


def _crop_for_table_search(page: object, box: tuple[float, float, float, float]) -> object:
    """`page.crop(box)`, except that a character whose vertical centre lies above or below the box is kept whole instead of
    being clipped to it. Everything else is clipped as pdfplumber clips it: the rules, rectangles and curves the table finder
    needs, and the characters on the rows of the box.

    Clipping a character that crosses the top or bottom of the box moves its centre into the box. A caption set tight above a
    table (its box, not its glyph, across the top rule or the shading) then had its centre inside the top cell of the table
    found again, so the cell took its text, while the body, which tells cell text from body text by the centre of the
    character as it is on the page (`_BoxIndex.holds`), kept it as well: the line came out twice (Pinned by:
    test_pdf.py::TestTextTightAgainstATable). Kept whole, it stays on its own line above the table, as the body sees it.
    A character crossing a side of the box is still clipped (deliberate): it is part of a word whose other characters are in
    the edge cell, which the body leaves out as a fragment of cell text (`_part_outside_cells`), so the cell is where it is
    read. Keeping every character whole, or only those whose centre lies in the box, lost that part of the word ('W' of
    'Word10'; 21 more words lost over 200 random documents of the word fuzz test) (Pinned by:
    test_pdf.py::TestTextTightAgainstATable::test_word_across_the_side_of_an_open_sided_table_stays_whole)."""
    top, bottom = box[1], box[3]

    def crop(objects: list[dict], _: tuple) -> list[dict]:
        kept = []
        for obj in objects:
            if obj.get("object_type") == "char" and not top <= (obj["top"] + obj["bottom"]) / 2 <= bottom:
                if get_bbox_overlap(obj_to_bbox(obj), box) is not None:
                    kept.append(obj)
            elif (clipped := clip_obj(obj, box)) is not None:
                kept.append(clipped)
        return kept

    return CroppedPage(page, box, crop_fn=crop)


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
