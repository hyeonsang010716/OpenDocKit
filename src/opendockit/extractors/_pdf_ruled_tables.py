"""Tables drawn with ruling lines or shading: which found tables are real, cells and merged cells, shaded rows without
rules, tables open at the sides, text wrapped inside a cell, and the highlights and glyph outlines that look like rules.
"""

from bisect import bisect_left, bisect_right, insort
from collections.abc import Callable
from itertools import accumulate, pairwise
import math
import re
import statistics

from opendockit.extractors._pdf_model import (
    _BULLET_START,
    _CHECKBOX_SIDE,
    _FILL_WHITE_MIN,
    _MIN_FIGURE_POINTS,
    _NUMBER_CELL,
    _SHADE_MIN_SIDE,
    _SPACED_CHAR,
    _TABLE_ROW_TOLERANCE,
    _box_area,
    _BoxIndex,
    _encloses,
    _inside_any,
    _is_shade,
    _is_white,
    _Table,
)
from opendockit.extractors._pdf_pdfminer import _LAPARAMS
from opendockit.extractors._pdf_text import _drawn_text, _is_sideways, _sideways_paragraphs, _text_from_chars
from opendockit.markdown import render_table


# Tolerance for grouping the characters of a table cell into words. The pdfplumber default (a fixed 3pt) is larger than the word
# gap of a 10–11pt font (0.2 × the character size), so spaces disappeared in table cells only. It is made proportional to the
# character size, on the same basis as body lines (pdfminer word_margin 0.1)
# (Pinned by: test_quality.py::test_pdf_table_cell_keeps_spaces_between_words_in_small_fonts).
_CELL_TEXT = {"x_tolerance_ratio": _LAPARAMS["word_margin"]}
# Table finder settings. By default pdfplumber discards lines shorter than 1pt before joining them. A dotted cell border is drawn
# as hundreds of short 0.4pt lines, so left as is the border disappeared and cells merged (23 names crowded into one cell, and
# values and rows went out of step). If they are not discarded, joining (default 3pt) turns the dotted line into one line.
_TABLE_SETTINGS = {"edge_min_length_prefilter": 0}
# Highlighter rectangle (`_without_highlights`): its height is at most this multiple of the size of the covered characters, its
# left edge is within this distance of the first covered character (ratio of the character size, capped in pt), and its right edge
# reaches at most `_HIGHLIGHT_TAIL_RATIO` × the character size past the last character (the width of a painted trailing space).
# Cell shading is wider than the text by the cell's left padding (LibreOffice 2.8pt, Word 5.4pt), so it is not caught
_HIGHLIGHT_MAX_HEIGHT = 2.0
_HIGHLIGHT_EDGE_RATIO, _HIGHLIGHT_EDGE_MAX = 0.15, 1.5
_HIGHLIGHT_TAIL_RATIO = 0.4
# Filled table row (`_without_cell_rows`): it is a cell if it lines up, within this distance (pt), with a column of words within
# this many lines (multiple of the character size) above or below
_CELL_ROW_REACH, _CELL_ALIGN = 6.0, 1.0


# List number at the start of a cell text line ('1)', '(2)', '가.', '①'). The break before such a line is not a wrap caused by the
# cell width (`_soft_wrapped`).
_BARE_LIST_NUMBER = re.compile(r"^(?:\(?(?:\d{1,3}|[가나다라마바사아자차카타파하])[.)]|\((?:\d{1,3}|[가나다라마바사아자차카타파하])\)|[①-⑳])")


# Share of the page area for a table to count as a page frame. In slide PDFs the whole page is often a frame box. A table this
# large is dropped as a frame (layout box) only when it has at most _FRAME_MAX_ROWS rows or a single column. When only the area was
# considered, a requirements table filling the page (29 rows × 6 columns) was dropped and its cell text scattered column by column
# (Pinned by: test_pdf.py::TestRuledTables::
# test_grid_filling_the_whole_page_is_a_table_not_a_page_frame, test_page_frame_split_into_two_boxes_is_still_layout).
_PAGE_FRAME_RATIO = 0.8
# In an outer table taken as a form that contains tables within a table, the maximum text length of the label cell in the same row
# as the cell holding the inner table (`_with_inner_tables`).
_NESTED_LABEL_MAX = 20


_FRAME_MAX_ROWS = 2
_FILL_MARK = "■"  # ruled table cell marked only by its fill colour (`_mark_filled_cells`). Same character as DOCX's filled box
_FILL_MIN_COVER = 0.6  # share of the cell area the fill rectangle has to cover


_FILL_HUE_MIN = 0.1  # chromatic if the components differ by at least this
_FILL_GREY_MAX = 0.6  # an achromatic fill marks the cell only if all components are this dark or darker


_SHADED_ROW_REACH = 1.5  # maximum height of a band accepted as continuing a shaded table fragment (vs. the fragment's median row height)
_BAND_SLACK_RATIO = 0.3  # share of the character height that may stick out of a band and still count as the band's text (font box slack)
# Text reaching deeper than this share of the character height onto both sides of a row boundary is the text of a vertically merged
# cell (`_regridded_fragments`). Chrome placed the text of a cell merged over an even number of rows 25% across the row boundary.
# This tells it from a character box grazing the neighbouring row (0.06pt).
_MERGED_TEXT_CROSSING = 0.15
_CELL_GAP_MIN_RATIO = 0.5  # characters either side of a cell boundary closer than this (vs. character size) are text crossing it (a word gap)
# Gap and overlap (pt) allowed between the end of one segment and the start of the next when joining horizontal line segments into
# one line (_joined_rules). Lines drawn per cell have the same coordinate (gap 0) or overlap by the line width.
_RULE_JOIN_GAP = 0.1
_RULE_JOIN_OVERLAP = 1.0
# Vertical gap (pt) allowed when taking vertical line segments drawn broken at each row as one line (_chained_verticals). Real
# generators break them by 0.25pt
_VERTICAL_JOIN_GAP = 1.5
# Glyph outline curve (_without_glyph_outlines): a curve smaller than this size (pt) both horizontally and vertically, and thicker
# than this on both sides (not a straight line)
_GLYPH_OUTLINE_MAX, _STRAIGHT_MAX = 20.0, 0.1


# Number of rows that must continue with the same boundaries in each group when splitting touching ruled tables at column
# boundaries (`_split_column_sections`).
_SECTION_MIN_ROWS = 2


# Width (pt, `_place_images_in_cells`) a picture may spill outside the cell box and still count as inside a ruled table cell. About
# the cell border thickness plus rounding.
_CELL_IMAGE_SLACK = 2.0


def _column_spans(cells: list[tuple], columns: int) -> tuple[tuple[float, float], ...]:
    """Horizontal range of each column, split at the vertical boundaries of a ruled table's cell boxes (`cells`). An empty tuple if
    the number of boundaries does not match the number of columns (`columns`)."""
    edges: list[float] = []
    for x in sorted(edge for cell in cells if cell for edge in (cell[0], cell[2])):
        if not edges or x - edges[-1] > 1.0:
            edges.append(x)
    spans = tuple(zip(edges, edges[1:], strict=False))
    return spans if len(spans) == columns else ()


def _ruled_table(bbox: tuple[float, float, float, float], rows: list[list[str | None]], boxes: list[tuple], chars: list[dict],
                 figures: list[tuple[float, float, float, float, float]]) -> _Table:
    """Turns one ruled table (a result of `_real_tables`) into a `_Table`. For a cell holding a picture the line heights of the cell
    text are stored as well (`_place_images_in_cells`)."""
    cells = [[cell and _drawn_text(cell) for cell in row] for row in rows]
    boxes = tuple(box for box in boxes if box)
    cell_lines = {}

    for index, box in enumerate(boxes):
        if any(_in_cell(figure[:4], box) for figure in figures):
            tops: list[float] = []

            for top in sorted(char["top"] for char in chars if char["text"].strip() and _inside_any(char, [box])):
                if not tops or top - tops[-1] > _TABLE_ROW_TOLERANCE:
                    tops.append(top)

            cell_lines[index] = tuple(tops)
    return _Table(top=bbox[1], text=render_table(cells), bottom=bbox[3], x0=bbox[0], x1=bbox[2], rows=cells,
                  spans=_column_spans(boxes, len(cells[0])), cells=boxes, bands=_row_bands(boxes, len(cells)),
                  cell_lines=cell_lines)


def _with_open_below(page: object, table: _Table) -> _Table:
    """The table with those of its columns put in `open_below` whose bottom cell has, at its bottom edge, a horizontal ruling line
    over less than 80% of the cell width (modified in place). Measuring at each column's own bottom cell keeps the other columns
    from being seen as open in a table that `_closed_at_the_bottom` closed by extending only the merged-cell column down to the end
    of its text (if every column is open it is a split row, `_carry_open_cells`)."""
    open_columns = []

    for column, (x0, x1) in enumerate(table.spans):
        bottom = max((box[3] for box in table.cells if box[0] < (x0 + x1) / 2 < box[2]), default=table.bottom)
        edges = [edge for edge in page.horizontal_edges if abs(edge["top"] - bottom) <= 1.5]
        covered = sum(max(0.0, min(edge["x1"], x1) - max(edge["x0"], x0)) for edge in edges)
        if x1 > x0 and covered < (x1 - x0) * 0.8:
            open_columns.append(column)
    table.open_below = tuple(open_columns)
    return table


def _in_cell(box: tuple[float, float, float, float], cell: tuple[float, float, float, float]) -> bool:
    """Whether the box (x0, top, x1, bottom) lies inside the cell box (it may spill out by `_CELL_IMAGE_SLACK`)."""
    return (cell[0] - _CELL_IMAGE_SLACK <= box[0] and box[2] <= cell[2] + _CELL_IMAGE_SLACK
            and cell[1] - _CELL_IMAGE_SLACK <= box[1] and box[3] <= cell[3] + _CELL_IMAGE_SLACK)


def _row_bands(cells: list[tuple], rows: int) -> tuple[tuple[float, float], ...]:
    """Vertical range of each row, split at the horizontal boundaries of a ruled table's cell boxes (`cells`). An empty tuple if the
    number of boundaries does not match the number of rows (`rows`)."""
    edges: list[float] = []
    for y in sorted(edge for cell in cells if cell for edge in (cell[1], cell[3])):
        if not edges or y - edges[-1] > 1.0:
            edges.append(y)
    bands = tuple(zip(edges, edges[1:], strict=False))
    return bands if len(bands) == rows else ()


def _without_glyph_outlines(page: object) -> object:
    """Page without the small curves that draw characters as outlines (for finding ruled tables). `page` itself when there are none.

    A document made with a design tool draws the same text once more as filled
    polygons over the text layer. The vertical edges of the strokes line up across the whole table width a little over 1pt apart, so
    when pdfplumber chains nearby vertical lines and snaps them to one position, all cell borders were pulled to one place in the
    middle of the page and the table became a single cell ('표1. 기준점(Baseline) 유형' scattered over every row, Pinned by:
    test_pdf.py::TestRuledTables::test_ruled_table_overdrawn_with_glyph_outlines_keeps_its_columns). Only curves that are below
    `_GLYPH_OUTLINE_MAX` in both width and height and are not straight lines (both sides thicker than `_STRAIGHT_MAX`) are removed —
    cell borders drawn as curves broken per row (width 0, height 9pt) are straight lines and remain. Checkbox detection still looks
    at the curves of the original page.
    """
    outlines = {id(curve) for curve in page.curves
                if max(curve["width"], curve["height"]) < _GLYPH_OUTLINE_MAX
                and min(curve["width"], curve["height"]) > _STRAIGHT_MAX}
    if not outlines:
        return page
    return page.filter(lambda obj: id(obj) not in outlines)


def _without_unadvanced_spaces(page: object) -> object:
    """Page without the space characters lying under the next character (for ruled-table cell text). `page` itself when there are
    none.

    A document with tightened character spacing draws a space character and then draws the next character before the space's start
    point — the space does not advance, so there is no visible gap. pdfplumber lays out cell characters in x order, so that space
    ended up after the first character of the next word, giving '•새 로운' and '측 정원리' (25 places in the tables of one
    manual, Pinned by: test_quality.py::
    test_pdf_table_cell_space_drawn_under_the_next_letter_does_not_split_the_word). It is removed only when the very next character
    in drawing order (character list order) starts on the same line before the space's start point and covers that start point.
    Measuring by overlapping width removed the normal spaces of a Latin font whose character boxes are wider than the advance (every
    character overlapping by 6pt), turning 'CI / CD' into 'CI /CD', and in right-to-left text, where the next character sits
    separately to the left of the space, the start point is not covered and the space remains. The spacing at a removed position is
    decided again by the cell character gaps (`_CELL_TEXT`). Body lines are read in drawing order and do not have this problem, so
    this is used only for finding tables.
    """
    chars = page.chars
    hidden = {id(char) for char, after in zip(chars, chars[1:], strict=False)
              if char["text"].isspace() and after["text"].strip() and abs(after["top"] - char["top"]) <= 1
              and after["x0"] < char["x0"] < after["x1"]}
    if not hidden:
        return page
    return page.filter(lambda obj: obj.get("object_type") != "char" or id(obj) not in hidden)


def _without_highlights(page: object) -> object:
    """Page without highlight rectangles (for finding tables). `page` itself when there are none.

    LibreOffice draws a highlight as borderless filled rectangles per run / font span, and Hangul spans and digit/Latin spans differ
    in height. pdfplumber used their edges as rules and a highlighted paragraph became a table split into a cell per word, like
    '| 재생용기는 일반 | HDPE 80% + | … | 1 | k | g | 당 |' (Pinned by: test_pdf_highlights.py::TestHighlightedParagraphs). A highlight
    is a rectangle that is filled without a border, covers the characters of one line, is at most `_HIGHLIGHT_MAX_HEIGHT` times that
    character size tall, and whose left edge matches the first covered character and whose right edge is right after the last
    character (up to a filled trailing space). A rectangle covering only the space between filled spans (or no characters) is removed
    together with them when it is on the same line as a same-coloured highlight touching it on the left or right and keeps the height
    condition. Cell shading and striped rows are wider than the characters by the cell's left padding and remain, and a rectangle
    for which another row has a rectangle with the same left and right edges but different covered text (cell shading aligned to a
    column — the widest cell can fit its text exactly) and the cells of a filled table row (`_without_cell_rows`) are kept as well.
    Known accepted gap: the full lines of a justified paragraph highlighted as one run per line remain, but they form a one-column
    box and do not become a table.
    """
    chars = sorted((char for char in page.chars if char["text"]), key=lambda char: char["top"] + char["bottom"])
    if not chars:
        return page
    middles = [(char["top"] + char["bottom"]) / 2 for char in chars]
    tallest = max(char["size"] for char in chars) * _HIGHLIGHT_MAX_HEIGHT
    # A rectangle takes its characters from those of its line or from those between its left and right edges, whichever are
    # fewer (in the order of the line either way). Going through the whole line for every rectangle took 8 seconds on a line
    # of 4,000 characters with a filled rectangle under each (a 15 KB file) (Pinned by:
    # test_audit_pdf_mid.py::TestManyFilledRectanglesOnOneLine::test_each_rectangle_looks_only_at_the_characters_it_covers)
    centres = sorted((centre, position) for position, char in enumerate(chars)
                     if (centre := (char["x0"] + char["x1"]) / 2) == centre)
    covered = []
    for rect in page.rects:
        if not rect.get("fill") or rect.get("stroke") or not 0 < rect["bottom"] - rect["top"] <= tallest:
            continue
        low, high = bisect_left(middles, rect["top"]), bisect_right(middles, rect["bottom"])
        left, right = bisect_left(centres, (rect["x0"],)), bisect_right(centres, (rect["x1"], len(chars)))
        if right - left < high - low:
            inside = [chars[position] for position in sorted(position for centre, position in centres[left:right]
                                                             if low <= position < high and rect["x0"] <= centre <= rect["x1"])]
        else:
            inside = [char for char in chars[low:high] if rect["x0"] <= (char["x0"] + char["x1"]) / 2 <= rect["x1"]]
        covered.append((rect, inside, "".join(char["text"] for char in inside).strip()))

    # The only evidence for column shading is a cell that fills its row without gaps (touching the neighbouring cell's shading, with
    # no unfilled text on the same line). Hangul characters all have the same width, so short highlights at the start of paragraph
    # lines ('고 ', '다 ') or highlights filled per ruled-table cell ('장비', '홍보') had the same left and right edges on two lines and
    # remained as column shading, producing a fake table '| 고 | 31 |' or shifting columns because the highlight edges became
    # vertical rules
    shaded = {id(char) for _, inside, _ in covered for char in inside}
    by_right = sorted((rect for rect, _, _ in covered), key=lambda rect: rect["x1"])
    by_left = sorted((rect for rect, _, _ in covered), key=lambda rect: rect["x0"])
    rights, lefts = [rect["x1"] for rect in by_right], [rect["x0"] for rect in by_left]
    columns: dict[tuple[int, int], set[str]] = {}
    # unfilled[n]: how many of the first n characters are visible and under no fill (counted once, not once per rectangle)
    unfilled = list(accumulate((bool(char["text"].strip()) and id(char) not in shaded for char in chars), initial=0))
    for rect, _, text in covered:
        touching = (*by_right[bisect_left(rights, rect["x0"] - _HIGHLIGHT_EDGE_MAX):bisect_right(rights, rect["x0"] + _HIGHLIGHT_EDGE_MAX)],
                    *by_left[bisect_left(lefts, rect["x1"] - _HIGHLIGHT_EDGE_MAX):bisect_right(lefts, rect["x1"] + _HIGHLIGHT_EDGE_MAX)])
        if (text and unfilled[bisect_right(middles, rect["bottom"])] <= unfilled[bisect_left(middles, rect["top"])]
                and any(other is not rect and abs(other["top"] - rect["top"]) <= _CELL_ALIGN
                        and abs(other["bottom"] - rect["bottom"]) <= _CELL_ALIGN for other in touching)):
            columns.setdefault((round(rect["x0"]), round(rect["x1"])), set()).add(text)
    marks, empty = [], []
    for rect, inside, text in covered:
        if not text:
            empty.append(rect)
            continue
        size = max(char["size"] for char in inside)
        edge = min(size * _HIGHLIGHT_EDGE_RATIO, _HIGHLIGHT_EDGE_MAX)
        tail = rect["x1"] - max(char["x1"] for char in inside)
        if (len(columns.get((round(rect["x0"]), round(rect["x1"])), ())) <= 1
                and rect["bottom"] - rect["top"] <= size * _HIGHLIGHT_MAX_HEIGHT
                and max(char["top"] for char in inside) - min(char["top"] for char in inside) <= size / 2
                and abs(min(char["x0"] for char in inside) - rect["x0"]) <= edge
                and -edge <= tail <= size * _HIGHLIGHT_TAIL_RATIO):
            marks.append((rect, inside))
    marks = _without_cell_rows(page, marks)
    if not marks:
        return page

    # A rectangle without characters (other than spaces) is a highlight only when it overlaps vertically with a same-coloured
    # highlight touching it on the left or right. Without the colour check, the neighbouring vertical rule that Chrome draws as a
    # filled rectangle (cell padding is 1px, so within 1.5pt of the highlight) was removed as well
    by_right = sorted((rect for rect, _ in marks), key=lambda mark: mark["x1"])
    by_left = sorted((rect for rect, _ in marks), key=lambda mark: mark["x0"])
    rights, lefts = [mark["x1"] for mark in by_right], [mark["x0"] for mark in by_left]
    dropped = {id(rect) for rect, _ in marks}
    for rect in empty:
        touching = (*by_right[bisect_left(rights, rect["x0"] - _HIGHLIGHT_EDGE_MAX):bisect_right(rights, rect["x0"] + _HIGHLIGHT_EDGE_MAX)],
                    *by_left[bisect_left(lefts, rect["x1"] - _HIGHLIGHT_EDGE_MAX):bisect_right(lefts, rect["x1"] + _HIGHLIGHT_EDGE_MAX)])
        if any(rect["top"] < mark["bottom"] and mark["top"] < rect["bottom"]
               and rect["bottom"] - rect["top"] <= (mark["bottom"] - mark["top"]) * 1.5
               and rect.get("non_stroking_color") == mark.get("non_stroking_color") for mark in touching):
            dropped.add(id(rect))
    return page.filter(lambda obj: id(obj) not in dropped)


def _without_cell_rows(page: object, marks: list[tuple[dict, list[dict]]]) -> list[tuple[dict, list[dict]]]:
    """Remove the cells of a filled table row from `marks` (highlight candidate rectangle, covered characters)
    (`_without_highlights`).

    In a Chrome table whose column widths fit the text, filling only the header row gives cell rectangles that fit the text exactly
    and look the same as highlights (Pinned by: test_pdf_highlights.py::TestChromeTables). They are cells when a line has two or more
    candidates, the line has no unfilled word, and the text of every candidate aligns in a column — left, right or centre — with an
    unfilled word within `_CELL_ROW_REACH` lines above or below. Consecutive lines filling the same text are not compared with each
    other (a highlighted paragraph repeating the same line looked like cell rows and became a table). A line mixed with unfilled
    words is a paragraph — judging by column alignment alone, the first candidate of a line coincided with the left margin and the
    second with one of dozens of surrounding words within 1pt, and a highlighted paragraph became a fake table. A line whose top and
    bottom edges differ or whose candidates are apart from each other (LibreOffice's Hangul and digit spans, highlights that fill
    only the text width in each ruled-table cell) is not a cell row either — the highlight edges became vertical rules and shifted
    the columns (Pinned by: test_pdf_highlights.py::TestScatteredHighlightsInAParagraph). Known accepted gap: a filled row whose
    columns do not align with the text of other rows (a one-row table, empty cells below the columns) is still removed as highlights.
    """
    # Lines are grouped by the middle height of the rectangles. Grouping by the top of the characters put Hangul and digit spans on
    # different lines and bypassed the height comparison
    lines: dict[float, list[tuple[dict, list[dict]]]] = {}
    first_middle = 0.0
    for mark in sorted(marks, key=lambda mark: mark[0]["top"] + mark[0]["bottom"]):
        middle = (mark[0]["top"] + mark[0]["bottom"]) / 2
        if not lines or middle - first_middle > max(char["size"] for char in mark[1]) / 2:
            first_middle, top = middle, min(char["top"] for char in mark[1])
            lines[top] = []
        lines[top].append(mark)
    if all(len(line) < 2 for line in lines.values()):
        return marks

    painted = {(round(char["x0"], 1), round(char["top"], 1)) for _, inside in marks for char in inside}
    words = [word for word in page.extract_words(return_chars=True)
             if (round(word["chars"][0]["x0"], 1), round(word["chars"][0]["top"], 1)) not in painted]
    cells = set()
    for top, line in lines.items():
        size = max(char["size"] for _, inside in line for char in inside)
        reach = size * _CELL_ROW_REACH
        near = [word for word in words if size / 2 < abs(word["top"] - top) <= reach]
        if (len(line) >= 2 and _tiled_row(line) and not any(abs(word["top"] - top) <= size / 2 for word in words)
                and all(_aligned_with_any(inside, near) for _, inside in line)):
            cells.update(id(rect) for rect, _ in line)
    return [mark for mark in marks if id(mark[0]) not in cells]


def _tiled_row(line: list[tuple[dict, list[dict]]]) -> bool:
    """The cell shadings of one row have the same top and bottom edges and touch the neighbouring cell. LibreOffice highlights differ
    in height between Hangul spans and digit/Latin spans, and highlights filled per ruled-table cell cover only the text width, so
    there are gaps between the cells."""
    first = line[0][0]
    rects = sorted((rect for rect, _ in line), key=lambda rect: rect["x0"])
    return (all(abs(rect["top"] - first["top"]) <= _CELL_ALIGN and abs(rect["bottom"] - first["bottom"]) <= _CELL_ALIGN
                for rect in rects)
            and all(right["x0"] - left["x1"] <= _HIGHLIGHT_EDGE_MAX for left, right in pairwise(rects)))


def _aligned_with_any(inside: list[dict], words: list[dict]) -> bool:
    left, right = min(char["x0"] for char in inside), max(char["x1"] for char in inside)
    return any(abs(word["x0"] - left) <= _CELL_ALIGN or abs(word["x1"] - right) <= _CELL_ALIGN
               or abs(word["x0"] + word["x1"] - left - right) <= _CELL_ALIGN * 2 for word in words)


def _dropping_page_indexes(read: Callable[[object], list]) -> Callable[[object], list]:
    """Decorator of `_real_tables`: the lists built once per page for its tables (`_page_index`) are dropped when the tables
    have been read, also when reading them fails.

    They are kept on the page object, and pdfplumber keeps the page objects of the whole document after it has released
    their characters (`page.close()`), so without this every page read would stay in memory until the end of the document
    (Pinned by: test_audit_pdf_mid.py::TestManyTablesOnOnePage::test_lists_built_for_the_tables_are_dropped_with_them).
    """
    def reading(page: object) -> list:
        try:
            return read(page)
        finally:
            page.__dict__.pop("_page_indexes", None)

    return reading


@_dropping_page_indexes
def _real_tables(page: object) -> list[tuple[tuple[float, float, float, float], list[list[str]], list[tuple]]]:
    """Filter layout boxes out of the ruled tables: page frames (a box that covers the page but has hardly any cells), outer boxes
    that enclose another table, and boxes whose content is a single column.

    The characters of a filtered-out box are read as body flow. Returns (bbox, cell matrix, list of cell boxes).
    """
    page_area = max(float(page.width) * float(page.height), 1.0)
    regular = [_closed_at_open_ends(page, _closed_at_the_bottom(page, table)) for table in page.find_tables(_TABLE_SETTINGS)]
    open_sided = [table for table in _open_sided_tables(page)
                  if not any(_is_inner_part(table.bbox, closed.bbox) for closed in regular)]
    found = _with_shaded_rows(page, open_sided + [table for table in regular
                                                  if not any(_encloses(other.bbox, table.bbox) for other in open_sided)])
    candidates = []
    for table in found:
        x0, top, x1, bottom = table.bbox
        is_grid = len(table.rows) > _FRAME_MAX_ROWS and len(table.columns) >= 2
        if (x1 - x0) * (bottom - top) >= page_area * _PAGE_FRAME_RATIO and not is_grid:
            continue
        cells = _cell_rows(table)
        merged = _merge_split_header(cells)
        filled_columns = {index for row in merged for index, cell in enumerate(row) if cell and cell.strip()}
        if len(filled_columns) >= 2:
            header_rows = len(cells) - len(merged) + 1  # 2 if a two-level header was merged
            _mark_filled_cells(table, cells, header_rows)
            rows = [[cell or "" for cell in row] for row in (*merged[:1], *cells[header_rows:])]
            candidates.append((table.bbox, rows, list(table.cells), table))

    kept, absorbed = [], set()
    for bbox, rows, boxes, table in candidates:
        inner = [other for other in candidates if other[0] is not bbox and _encloses(bbox, other[0])]
        if inner:
            rows = _with_inner_tables(table, rows, inner)
            if rows is None:
                continue
            absorbed.update(id(other[0]) for other in inner)
            kept.append((bbox, [(bbox, rows, boxes)]))
        elif len(rows) == len(table.rows):
            kept.append((bbox, _split_column_sections(table, bbox, rows)))
        else:
            kept.append((bbox, [(bbox, rows, boxes)]))
    return [piece for bbox, pieces in kept if id(bbox) not in absorbed for piece in pieces]


def _closed_at_the_bottom(page: object, table: object) -> object:
    """If the bottom row has a place that did not become a cell and text inside the table lies in no cell, the table found again
    after adding a horizontal line at the bottom of the table. `table` itself when there is nothing to change.

    A vertically merged cell split at the page end (rowspan in Chrome printing) has no bottom border on that page and did not become
    a cell. Its text ('그룹5 담당: 팀5') fell out as a paragraph outside the table and the first cell of those rows was empty (Pinned by:
    test_pdf_table_columns.py::TestMergedCellOpenAtThePageEnd). When the cell text is longer than the last row and continues beyond
    the bottom of the table (text of the same column continuing within the line spacing), only that column is extended down to the
    end of the text and closed there — closing at the table bottom split the text so that only the first part was in the cell and
    the rest ('담당: 팀10') became a paragraph outside the table. The table is replaced only when exactly one table is found again,
    the row and column counts are the same, and all the text that fell out is now in cells. For the open-topped empty cell on the
    next page see `_carry_open_cells`.
    """
    if not table.rows or all(cell is not None for cell in table.rows[-1].cells):
        return table
    x0, top, x1, bottom = table.bbox
    in_cells = _BoxIndex([cell for cell in table.cells if cell]).holds
    stray = [char for char in page.chars if char["text"].strip() and x0 < (char["x0"] + char["x1"]) / 2 < x1
             and top < (char["top"] + char["bottom"]) / 2 < bottom and not in_cells(char)]
    if not stray:
        return table

    # When the cell text continues beyond the bottom of the table (merged-cell text longer than the last row), only that column is
    # extended down to the end of the continuing lines and closed there
    edges = _column_edges(table)
    middle = (min(char["x0"] for char in stray) + max(char["x1"] for char in stray)) / 2
    index = max(0, min(bisect_right(edges, middle) - 1, len(edges) - 2))
    left, right = edges[index], edges[index + 1]
    end = max(char["bottom"] for char in stray)
    for char in sorted((char for char in page.chars if char["text"].strip() and (char["top"] + char["bottom"]) / 2 >= bottom
                        and left < (char["x0"] + char["x1"]) / 2 < right), key=lambda char: char["top"]):
        if char["top"] - end > (char["bottom"] - char["top"]) * 0.5:
            break
        stray.append(char)
        end = max(end, char["bottom"])
    lowest = max(bottom, end + 1)
    closing = [_rule_line(a, bottom, b, bottom) for a, b in ((x0, left), (right, x1)) if b > a]  # close while leaving that column open
    lines = {"explicit_horizontal_lines": [*closing, _rule_line(left, lowest, right, lowest)],
             "explicit_vertical_lines": [_rule_line(x, bottom, x, lowest) for x in (left, right)] if lowest > bottom else []}

    page_left, page_top, page_right, page_bottom = page.bbox
    box = (max(x0 - 1, page_left), max(top - 1, page_top), min(x1 + 1, page_right), min(lowest + 1, page_bottom))
    found = page.crop(box).find_tables({**_TABLE_SETTINGS, **lines})
    if (len(found) != 1 or len(found[0].rows) != len(table.rows) or len(found[0].columns) != len(table.columns)
            or not all(map(_BoxIndex([cell for cell in found[0].cells if cell]).holds, stray))):
        return table
    return found[0]


def _closed_at_open_ends(page: object, table: object) -> object:
    """If the vertical rules continue past the bottom (top) of the table at every column boundary and there is text in between, the
    table found again after adding a horizontal line at the end of the continuing vertical lines (a row split at the page end).
    `table` itself when there is nothing to change.

    Chrome printing draws every cell of a row split at the page end without a bottom border, and the remainder on the next page
    without a top border. That row did not become cells, so its first part fell out as a paragraph outside the table, and the
    remainder on the next page also became a paragraph above the table, so the header was not carried to the next page's table
    (Pinned by:
    test_pdf_table_columns.py::TestRowSplitAcrossPages::test_row_whose_cells_are_all_open_at_the_page_end_stays_in_the_table).
    For a vertically merged cell open in one column only see `_closed_at_the_bottom`. The table is replaced only when exactly one
    table is found again, the column count is the same, the rows grow by the number of ends added, and all the text in between is in
    cells. How this row is continued on the next page is in `_carry_table_headers`.
    """
    if not table.rows:
        return table
    x0, top, x1, bottom = table.bbox
    edges = _column_edges(table)
    # The page's vertical rules and characters are sorted once per page and each table takes only those of its own range.
    # Listing every rule and every character of the page again for every table took 31 seconds on a page of 800 small tables
    # (a 51 KB file) (Pinned by: test_audit_pdf_mid.py::TestManyTablesOnOnePage::test_each_table_takes_only_its_own_part_of_the_page)
    positions, verticals = _verticals_by_x(page)
    verticals = verticals[bisect_left(positions, min(edges) - 2):bisect_right(positions, max(edges) + 2)] if edges else []
    lowest = _common_reach(verticals, edges, bottom, 1)
    highest = _common_reach(verticals, edges, top, -1)
    bands = [band for band in ((bottom, lowest), (highest, top)) if band[1] - band[0] >= _CHECKBOX_SIDE[0]]
    if not bands:
        return table
    middles, ordered = _text_by_middle(page)
    stray = [char for low, high in bands for char in ordered[bisect_right(middles, low):bisect_left(middles, high)]
             if x0 < (char["x0"] + char["x1"]) / 2 < x1 and low < (char["top"] + char["bottom"]) / 2 < high]
    if not stray:
        return table

    lines = [_rule_line(x0, y, x1, y) for y in (lowest, highest) if any(y in band for band in bands)]
    page_left, page_top, page_right, page_bottom = page.bbox
    box = (max(x0 - 1, page_left), max(min(top, highest) - 1, page_top), min(x1 + 1, page_right),
           min(max(bottom, lowest) + 1, page_bottom))
    found = page.crop(box).find_tables({**_TABLE_SETTINGS, "explicit_horizontal_lines": lines})
    if (len(found) != 1 or len(found[0].rows) != len(table.rows) + len(lines) or len(found[0].columns) != len(table.columns)
            or not all(map(_BoxIndex([cell for cell in found[0].cells if cell]).holds, stray))):
        return table
    return found[0]


def _verticals_by_x(page: object) -> tuple[list[float], list[dict]]:
    """The page's vertical rules longer than 1pt in order of their x position, as (list of x positions, list of rules). Built
    once per page (`_closed_at_open_ends`). A rule whose position is not a number is at no column boundary and is left out."""
    def build() -> tuple[list[float], list[dict]]:
        ordered = sorted((edge for edge in page.vertical_edges if edge["bottom"] - edge["top"] > 1 and edge["x0"] == edge["x0"]),
                         key=lambda edge: edge["x0"])
        return [edge["x0"] for edge in ordered], ordered

    return _page_index(page, "verticals", build)


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


def _common_reach(verticals: list[dict], edges: list[float], at: float, direction: int) -> float:
    """The end reached by all the vertical rules that start at height `at` at each column boundary (`edges`) and continue downwards
    (`direction` 1) or upwards (-1). `at` if any boundary has no continuing line."""
    reach = math.inf if direction > 0 else -math.inf

    for x in edges:
        ends = [edge["bottom"] if direction > 0 else edge["top"] for edge in verticals if abs(edge["x0"] - x) <= 1.5
                and abs((edge["top"] if direction > 0 else edge["bottom"]) - at) <= 1.5]
        if not ends:
            return at
        reach = min(reach, max(ends)) if direction > 0 else max(reach, min(ends))

    return reach if edges else at


def _split_column_sections(table: object, bbox: tuple[float, float, float, float],
                           rows: list[list[str]]) -> list[tuple[tuple[float, float, float, float], list[list[str]], list[tuple]]]:
    """Split a ruled table (`table`, cell text `rows`) into groups of rows with different column boundaries. It is split only where
    two groups, each keeping the same column boundaries for `_SECTION_MIN_ROWS` rows or more, touch and neither side's boundaries are
    all contained in the other's; in a split table the columns that have no cell in that group (columns covered by merging) are
    removed. Without a split the single table is returned as is (one (bbox, rows, list of cell boxes)).

    pdfplumber read two touching tables (a 6-column '경비 내역' table right below a 4-column '인적 사항' grid) as one grid combining
    the column boundaries of both, so empty columns were inserted and the lower table's header became a data row (Pinned by:
    test_pdf_table_columns.py::TestAbuttingTablesWithDifferentColumns).
    Header rows and total rows that span columns are part of rows with different boundaries and are not split off.
    """
    table_rows = table.rows  # a property that regroups all the cells on every access
    if any(len(row.cells) != len(rows[0]) for row in table_rows):
        return [(bbox, rows, list(table.cells))]
    edges = [tuple(round(edge) for edge in sorted({edge for cell in row.cells if cell is not None for edge in (cell[0], cell[2])}))
             for row in table_rows]
    runs: list[list[int]] = []  # consecutive row numbers with the same boundaries

    for index, edge in enumerate(edges):
        if runs and _same_edges(edges[runs[-1][0]], edge):
            runs[-1].append(index)
        else:
            runs.append([index])

    sections = [list(runs[0])]

    for previous, run in zip(runs, runs[1:], strict=False):
        apart = (len(previous) >= _SECTION_MIN_ROWS and len(run) >= _SECTION_MIN_ROWS
                 and not _edges_within(edges[previous[0]], edges[run[0]])
                 and not _edges_within(edges[run[0]], edges[previous[0]]))
        if apart:
            sections.append(list(run))
        else:
            sections[-1].extend(run)
    if len(sections) == 1:
        return [(bbox, rows, list(table.cells))]

    split = []

    for section in sections:
        cells = [[table_rows[index].cells[column] for column in range(len(rows[0]))] for index in section]
        columns = [column for column in range(len(rows[0])) if any(line[column] is not None for line in cells)]
        boxes = [cell for line in cells for cell in line if cell is not None]
        area = (min(box[0] for box in boxes), min(box[1] for box in boxes), max(box[2] for box in boxes), max(box[3] for box in boxes))
        split.append((area, [[rows[index][column] for column in columns] for index in section], boxes))
    return split


def _same_edges(edges: tuple[int, ...], others: tuple[int, ...]) -> bool:
    return len(edges) == len(others) and all(abs(edge - other) <= 1 for edge, other in zip(edges, others, strict=True))


def _edges_within(edges: tuple[int, ...], others: tuple[int, ...]) -> bool:
    """Whether all column boundaries in `edges` are boundaries of `others` (within 1pt)."""
    return all(any(abs(edge - other) <= 1 for other in others) for edge in edges)


def _with_inner_tables(table: object, rows: list[list[str]], inner: list[tuple]) -> list[list[str]] | None:
    """If the ruled table `table` (cell text `rows`) that encloses other ruled tables (`inner` = candidates of `_real_tables`) is a
    form containing tables within a table, the cell text with the inner tables spread out in place of the outer cell; otherwise None
    (the outer table is a layout box and is discarded).

    It counts as a form only when every inner table has an outer cell containing it, the row of that cell has other cell text
    (labels) and all of it is `_NESTED_LABEL_MAX` characters or less (deliberate, conservative — one cell holding inner tables per
    row). When the inner table holds all the text of that cell (lines above and below the table become rows before and after), the
    row is expanded to the inner table's row count and the label is repeated on every row ('학력 | 기간 | 학교명 | …'); otherwise (the
    inner table was found for only some rows and text remains beside it, or several tables in one cell) the outer cell's text is
    kept as is. Either way the inner table is not emitted separately. In a résumé printed by Chrome the outer table was discarded as
    'a box enclosing another table', so '성명' and '홍길동' were scattered into paragraphs and '학력' was separated from its table
    (Pinned by: test_pdf_nested_tables.py::TestNestedTables::test_inner_table_found_apart_keeps_the_outer_label_on_its_rows,
    test_inner_table_found_only_in_part_is_kept_as_the_outer_cell_text, test_unit_line_under_the_inner_table_becomes_a_row_after_it).
    """
    if len(rows) != len(table.rows):
        return None  # a table whose header was merged or whose inner-table cell rows were removed
    holders: dict[int, tuple[int, list[tuple]]] = {}  # row → (column, inner tables in that cell)

    for other in inner:
        place = next(((row, column) for row, line in enumerate(table.rows) for column, cell in enumerate(line.cells)
                      if cell is not None and _encloses(cell, other[0])), None)
        if place is None:
            return None
        column, held = holders.setdefault(place[0], (place[1], []))
        if column != place[1]:
            return None
        held.append(other)

    for row, (column, _) in holders.items():
        labels = [text for index, text in enumerate(rows[row]) if index != column and text.strip()]
        if not labels or any(len(text) > _NESTED_LABEL_MAX for text in labels):
            return None

    spread: list[list[str]] = []

    for row, cells in enumerate(rows):
        column, held = holders.get(row, (0, []))
        around = _text_around(table.page.chars, table.rows[row].cells[column], held[0]) if len(held) == 1 else None
        if around is None:
            spread.append(cells)
            continue
        width = 1 + next((count for count, cell in enumerate(table.rows[row].cells[column + 1:]) if cell is not None),
                         len(cells) - column - 1)
        above, below = around
        inner_rows = [*([[above]] if above else []), *held[0][1], *([[below]] if below else [])]
        spread.extend([*cells[:column], *inner_row, *cells[column + width:]] for inner_row in inner_rows)

    width = max(len(cells) for cells in spread)
    return [[*cells, *[""] * (width - len(cells))] for cells in spread]


def _text_around(chars: list[dict], cell: tuple, inner: tuple) -> tuple[str, str] | None:
    """Text inside the outer cell `cell` that lies outside the cells of the inner table (`inner` = a candidate of `_real_tables`):
    (text above the table, text below the table). None if there are characters beside the table (the inner table was found only in
    part). A unit line ('(단위: 천원)') or caption becomes a row before or after the inner table."""
    bbox, _, inner_cells, _ = inner
    outside = [char for char in chars
               if char["text"].strip() and _inside_any(char, [cell]) and not _inside_any(char, inner_cells)]
    if any(bbox[1] <= (char["top"] + char["bottom"]) / 2 <= bbox[3] for char in outside):
        return None
    lines: dict[int, set[float]] = {}  # line → the inner-table columns (left edge) the line's characters lie in

    # A line lying across two or more columns of the inner table is not a unit line or caption but a row that was not found as table
    for char in outside:
        middle = (char["x0"] + char["x1"]) / 2
        columns = lines.setdefault(round(char["top"] / _TABLE_ROW_TOLERANCE), set())
        columns.update(x0 for x0, _, x1, _ in inner_cells if x0 <= middle <= x1)
    if any(len(columns) >= 2 for columns in lines.values()):
        return None

    above = _text_outside([char for char in outside if char["bottom"] < bbox[1] + 1], cell, [])
    below = _text_outside([char for char in outside if char["top"] > bbox[3] - 1], cell, [])
    return above, below


def _mark_filled_cells(table: object, rows: list[list[str | None]], header_rows: int) -> None:
    """Put `_FILL_MARK` into the empty cells of a ruled table that are marked only by a fill colour (`rows` is the cell text from
    `_cell_rows`, modified in place).

    In a Gantt-chart schedule (the January–February cells of the '요구사항 분석' row filled blue) the cells have no text, so the whole
    schedule disappeared (Pinned by: test_pdf.py::
    TestCellsMarkedOnlyByFill::test_empty_cells_filled_with_a_colour_are_marked). Only cells that satisfy all of the following are
    marked (deliberate, conservative): an empty cell below the header rows (`header_rows`) ("", not a None hidden by merging), a
    fill rectangle in a mark colour (`_is_mark_fill`) covers at least `_FILL_MIN_COVER` of the cell area, the same row has an
    unfilled cell (not a whole-row background, stripes or a whole-table background), the cells below the header in the same column
    are not all filled (not whole-column shading), and somewhere in the table two neighbouring cells of the same row are filled in
    a run (a bar — not scattered cell emphasis). Header-row shading is excluded because header rows are not examined (Pinned by:
    test_shading_that_is_not_a_mark_leaves_empty_cells_empty). Known accepted gap: a column filled in every data row (a schedule
    over the whole period), a schedule that fills a whole row, pale grey bars and tables whose bars are all one cell wide are not
    marked, while pale coloured 'do not fill in' cells in a table that has cells filled in a run are marked. Cells filled with a
    picture (image) are not examined.
    """
    x0, top, x1, bottom = table.bbox
    fills = [rect for rect in table.page.rects
             if rect["x0"] < x1 and rect["x1"] > x0 and rect["top"] < bottom and rect["bottom"] > top  # only rectangles touching the table
             and rect.get("fill") and _is_mark_fill(rect.get("non_stroking_color"))]
    data = table.rows[header_rows:]
    if not fills or len(data) < 2 or len(rows) != len(table.rows):
        return

    covered = [[cell is not None and _fill_cover(cell, near) >= _FILL_MIN_COVER for cell in row.cells]
               for row, near in zip(data, _fills_by_row(data, fills), strict=True)]
    shaded_columns = {column for column in range(len(covered[0])) if all(row[column] for row in covered)}
    marks: set[tuple[int, int]] = set()
    for index, row in enumerate(data):
        if all(covered[index][column] for column, cell in enumerate(row.cells) if cell is not None):
            continue
        marks.update((header_rows + index, column) for column, cell in enumerate(rows[header_rows + index])
                     if cell == "" and covered[index][column] and column not in shaded_columns)

    # Without a single bar of two neighbouring cells filled in a run it is not a schedule (scattered cell emphasis)
    if not any((row, column + 1) in marks for row, column in marks):
        return
    for row, column in marks:
        rows[row][column] = _FILL_MARK


def _is_mark_fill(colour: object) -> bool:
    """Whether a fill colour counts as a cell mark: a chromatic colour (RGB components differ by at least `_FILL_HUE_MIN`, colours
    close to white excluded) or a colour as dark as `_FILL_GREY_MAX` or darker. Only numeric grey, RGB and CMYK colours are
    considered (unknown colour spaces and patterns are False).

    Light grey is not considered (deliberate): forms commonly paint 'do not fill in' cells light grey, and ■ would read as a
    'selected cell' (pinned by: test_pdf.py::TestCellsMarkedOnlyByFill::test_shading_that_is_not_a_mark_leaves_empty_cells_empty,
    the light-grey case).
    """
    if not isinstance(colour, (tuple, list)) or not colour or not all(isinstance(value, (int, float)) for value in colour):
        return False
    if len(colour) == 4:
        cyan, magenta, yellow, black = colour
        colour = tuple((1 - value) * (1 - black) for value in (cyan, magenta, yellow))
    if min(colour) >= _FILL_WHITE_MIN:
        return False
    return max(colour) - min(colour) >= _FILL_HUE_MIN or max(colour) <= _FILL_GREY_MAX


def _fills_by_row(rows: list[object], fills: list[dict]) -> list[list[dict]]:
    """For each table row, the fill rectangles that reach that row's height. Sweeps top to bottom and keeps only the ones that
    reach — looking at every rectangle for every cell was quadratic time on a table with thousands of filled cells
    (pinned by: test_pdf_resource_limits.py::TestManyCellsOnOnePage)."""
    ordered = sorted(fills, key=lambda rect: rect["top"])
    result, active, position = [], [], 0
    for row in sorted(range(len(rows)), key=lambda index: rows[index].bbox[1]):
        _, top, _, bottom = rows[row].bbox
        while position < len(ordered) and ordered[position]["top"] < bottom:
            active.append(ordered[position])
            position += 1
        active = [rect for rect in active if rect["bottom"] > top]
        result.append((row, _by_area(active)))
    return [near for _, near in sorted(result, key=lambda item: item[0])]


def _fill_cover(cell: tuple[float, float, float, float], fills: list[dict]) -> float:
    """The share of the cell box's (x0, top, x1, bottom) area covered by the fill rectangles (overlapping rectangles are summed
    and clamped at 1).

    Once fully covered, the remaining rectangles are not added — 90,000 cells under thousands of rectangles covering the whole
    table summed all of them per cell and took 78 seconds (fuzzing, pinned by:
    test_pdf_resource_limits.py::TestCellsReachingFarOffThePage). The caller passes the largest rectangles first (`_by_area`)."""
    x0, top, x1, bottom = cell
    area = max((x1 - x0) * (bottom - top), 1e-6)
    inside = 0.0

    for rect in fills:
        inside += max(0.0, min(x1, rect["x1"]) - max(x0, rect["x0"])) * max(0.0, min(bottom, rect["bottom"]) - max(top, rect["top"]))
        if inside >= area:
            return 1.0
    return min(inside / area, 1.0)


def _by_area(rects: list[dict]) -> list[dict]:
    """The rectangles ordered largest first (so `_fill_cover` stops early)."""
    return sorted(rects, key=lambda rect: (rect["x1"] - rect["x0"]) * (rect["bottom"] - rect["top"]), reverse=True)


def _with_shaded_rows(page: object, tables: list[object]) -> list[object]:
    """The table list found again after adding, to ruled tables whose cell boundaries came from cell shading (rectangles that
    are filled but have no border), the unshaded rows that continue on the same column boundaries. `tables` as is when
    there is nothing to change.

    pdfplumber also uses the sides of filled rectangles as rules. In a striped table filled cell by cell (Chrome
    `tr:nth-child(even)`, Word 'Light Shading') only the shaded rows became a ruled table and the unshaded rows became
    separate paragraphs, so the row order was shuffled and the numbers scattered. A table that shades only the header cells
    and draws only a bottom border under the data rows (Chrome th background) became a table of the header alone. A boxed
    table whose vertical lines exist only on the header row (Word 'Medium Shading 1') had its unshaded rows merged into one
    cell (pinned by: test_pdf_shaded_tables.py::TestShadedTables). Only for tables with cell shading, the page is split into
    bands by that table's column boundaries and vertical lines are added only to bands whose cell text fits the cell
    boundaries (`_grown_shaded_table`).
    """
    shades = _by_area([rect for rect in page.rects
                       if rect.get("fill") and not rect.get("stroke") and _is_shade(rect.get("non_stroking_color"))
                       and min(rect["x1"] - rect["x0"], rect["bottom"] - rect["top"]) > _SHADE_MIN_SIDE])
    if not shades:
        return tables
    rules: list[tuple[float, float, float]] | None = None  # collected the first time they are needed
    result = list(tables)

    for table in sorted(tables, key=lambda table: table.bbox[1]):
        if not any(other is table for other in result):
            continue  # merged into an earlier table
        if not any(cell and _fill_cover(cell, shades) >= _FILL_MIN_COVER for cell in table.cells):
            continue
        rules = _horizontal_rules(page) if rules is None else rules
        grown = _grown_shaded_table(page, table, result, rules)
        if grown is None:
            continue
        new, replaced = grown
        position = min(index for index, other in enumerate(result) if any(other is old for old in replaced))
        result = [other for other in result if not any(other is old for old in replaced)]
        result.insert(position, new)
    return _with_merged_column_fragments(page, result, shades)


def _with_merged_column_fragments(page: object, tables: list[object], shades: list[dict]) -> list[object]:
    """The table list found again after joining, among shaded table fragments, those whose column boundaries are a subset of
    another fragment's (a fragment missing its leading column because of a vertically merged cell) with that wider fragment
    into one table. `tables` as is when there is nothing to change.

    A vertically merged cell in a striped table takes the fill of the row it starts on (rowspan under Chrome
    `tr:nth-child(even)`). A fragment starting at a shaded merged cell has 4 columns and a fragment beside an unshaded merged
    cell has 3 columns with the first one missing, so the growing step (`_grown_shaded_table`), which joins only fragments
    with the same column boundaries, could not join the two. The table grown from the 3-column fragment overlapped the
    4-column fragment and rows came out twice, or it could not be grown and the '2팀' and '개발팀' rows fell out of the
    table (pinned by: test_pdf_shaded_tables.py::TestShadedTablesWithMergedCells). Only when the wide fragment (the one with
    the most column boundaries) and a fragment having only some of those boundaries are within `_SHADED_ROW_REACH` row
    heights, the region covering both is split again by the wide fragment's column boundaries (`_regridded_fragments`).
    Fragments with the same column boundaries are left alone (so the in-between bands the growing step rejected are not
    revived).
    """
    shaded = [table for table in tables if any(cell and _fill_cover(cell, shades) >= _FILL_MIN_COVER for cell in table.cells)]
    if len(shaded) < 2 and not any(_has_holes(table) for table in shaded):
        return tables
    result = list(tables)
    row_height = _row_pitch(shaded)

    for widest in sorted(shaded, key=lambda table: -len(_column_edges(table))):
        edges = _column_edges(widest)
        if not any(other is widest for other in result) or len(edges) < 3:
            continue
        members = [widest]
        grew = True

        while grew:
            top, bottom = min(member.bbox[1] for member in members), max(member.bbox[3] for member in members)
            near = [other for other in shaded
                    if any(other is kept for kept in result) and not any(other is member for member in members)
                    and _edges_inside(_column_edges(other), edges) and other.bbox[1] - bottom <= row_height * _SHADED_ROW_REACH
                    and top - other.bbox[3] <= row_height * _SHADED_ROW_REACH]
            members += near
            grew = bool(near)
        if not any(len(_column_edges(member)) < len(edges) or _has_holes(member) for member in members):
            continue

        merged = _regridded_fragments(page, members, edges, shades, row_height)
        if merged is None:
            continue
        position = min(index for index, other in enumerate(result) if any(other is member for member in members))
        result = [other for other in result if not any(other is member for member in members)]
        result.insert(position, merged)
    return result


def _row_pitch(tables: list[object]) -> float:
    """The row height of the table fragments: the median gap between the top and bottom sides of the cell boxes (sides within
    1pt count as one). Rows of merged cells are tall.

    Using the median of pdfplumber's row heights gave two row heights on a table where a vertically merged cell spans every
    row, so the header band right above also covered the caption above the table and the header was not accepted
    (pinned by: test_pdf_round44.py::TestZebraTableWhoseMergedGroupsStartOnShadedRows)."""
    heights: list[float] = []
    for y in sorted({y for table in tables for cell in table.cells if cell for y in (cell[1], cell[3])}):
        if not heights or y - heights[-1] > 1:
            heights.append(y)
    gaps = [lower - upper for upper, lower in pairwise(heights)]
    return statistics.median(gaps) if gaps else statistics.median(row.bbox[3] - row.bbox[1] for table in tables for row in table.rows)


def _has_holes(table: object) -> bool:
    """Whether the cell boxes of a ruled table fail to cover the whole table box (there are gaps with no cell).

    When every vertically merged group of a striped table starts on a shaded row, the single table found from the sides of
    the filled rectangles alone has gaps from the second column onward on the unshaded rows. There was no separate wide and
    narrow fragment, so nothing was split again and those rows scattered outside the table one number at a time
    (pinned by: test_pdf_round44.py::TestZebraTableWhoseMergedGroupsStartOnShadedRows)."""
    x0, top, x1, bottom = table.bbox
    covered = sum((cell[2] - cell[0]) * (cell[3] - cell[1]) for cell in table.cells if cell)
    return covered < (x1 - x0) * (bottom - top) * 0.98


def _edges_inside(edges: list[float], others: list[float]) -> bool:
    """Whether there are two or more column boundaries in `edges` and each matches some boundary of `others` within 1pt."""
    return len(edges) >= 2 and all(any(abs(edge - other) <= 1 for other in others) for edge in edges)


def _regridded_fragments(page: object, members: list[object], edges: list[float], shades: list[dict],
                         row_height: float) -> object | None:
    """The single table found again over the region covering the table fragments (`members`) using the column boundaries
    `edges`. None when it cannot be found.

    The row boundaries are the fragments' row boundaries plus the top and bottom sides of the filled rectangles inside that
    region. The header band right above (`_is_header_band`) and the closing band right below (`_band_cells` +
    `_ends_the_text`, the last unshaded row) are added by the same criteria as the growing step. Vertical lines are drawn at
    every column boundary over the full height of the region; horizontal lines are drawn, per column, only at row boundaries
    that the column's text does not straddle and that are not inside a filled rectangle covering that column — a merged cell
    ('영업본부' shaded over 3 rows, '기술본부' whose text straddles a row boundary by more than `_MERGED_TEXT_CROSSING`)
    stays one cell and its value is repeated on the rows below (`_cell_rows`).
    If any line of the region crosses a column boundary at word spacing (`_band_chars`), it is not a table.
    """
    left, right = edges[0], edges[-1]
    top, bottom = min(member.bbox[1] for member in members), max(member.bbox[3] for member in members)
    above, below = (top - row_height, top), (bottom, bottom + row_height)
    if _is_header_band(page, above, edges) or (_band_cells(page, above, edges) is not None
                                               and _ends_the_text(page, above, -1, left, right, row_height)):
        top = above[0]  # a year header ('2024') is a number cell, so the header-shape test misses it
    if _band_cells(page, below, edges) is not None and _ends_the_text(page, below, 1, left, right, row_height):
        bottom = below[1]
    if _band_chars(page, (top, bottom), edges) is None:
        return None

    covers = [shade for shade in shades if shade["x0"] >= left - 1 and shade["x1"] <= right + 1
              and shade["top"] >= top - 1 and shade["bottom"] <= bottom + 1]
    heights = sorted({top, bottom, *(y for member in members for row in member.rows for y in (row.bbox[1], row.bbox[3])),
                      *(y for shade in covers for y in (shade["top"], shade["bottom"]))})
    boundaries = [y for index, y in enumerate(heights) if index == 0 or y - heights[index - 1] > _TABLE_ROW_TOLERANCE]
    chars = [char for char in page.chars if char["text"].strip() and left < (char["x0"] + char["x1"]) / 2 < right]

    def crossed(y: float, x0: float, x1: float) -> bool:  # whether boundary y lies inside a single cell in this column
        return (any(x0 < (char["x0"] + char["x1"]) / 2 < x1
                    and min(y - char["top"], char["bottom"] - y) > (char["bottom"] - char["top"]) * _MERGED_TEXT_CROSSING
                    for char in chars)
                or any(shade["x0"] <= x0 + 1 and shade["x1"] >= x1 - 1 and shade["top"] + 1 < y < shade["bottom"] - 1
                       for shade in covers))

    verticals = [_rule_line(x, top, x, bottom) for x in edges]
    horizontals = [_rule_line(x0, y, x1, y) for y in boundaries for x0, x1 in zip(edges, edges[1:], strict=False)
                   if y in (top, bottom) or not crossed(y, x0, x1)]

    page_left, page_top, page_right, page_bottom = page.bbox
    box = (max(left - 1, page_left), max(top - 1, page_top), min(right + 1, page_right), min(bottom + 1, page_bottom))
    found = page.crop(box).find_tables({**_TABLE_SETTINGS, "explicit_vertical_lines": verticals,
                                        "explicit_horizontal_lines": horizontals})
    if len(found) != 1 or len(_column_edges(found[0])) != len(edges):
        return None
    return found[0]


def _horizontal_rules(page: object, edges: list[dict] | None = None) -> list[tuple[float, float, float]]:
    """The page's horizontal rules (lines and rectangle sides, or `edges`) as a list of (height, left end, right end), with
    pieces that continue at the same height (within 1pt) joined."""
    rules: list[tuple[float, float, float]] = []
    edges = [edge for edge in page.edges if edge["orientation"] == "h"] if edges is None else edges
    for edge in sorted(edges, key=lambda edge: (edge["top"], edge["x0"])):
        if rules and abs(edge["top"] - rules[-1][0]) <= 1 and edge["x0"] <= rules[-1][2] + 1:
            rules[-1] = (rules[-1][0], rules[-1][1], max(rules[-1][2], edge["x1"]))
        else:
            rules.append((edge["top"], edge["x0"], edge["x1"]))
    return rules


def _thin_rules(page: object) -> list[tuple[float, float, float]]:
    """The page's thin horizontal rules (horizontal lines, and filled rectangles no taller than `_SHADE_MIN_SIDE` — Chrome
    borders) as a joined list (`_horizontal_rules`). The sides of shaded row rectangles are left out — they would make every
    row of a striped table look ruled."""
    thin = [{"top": (line["top"] + line["bottom"]) / 2, "x0": line["x0"], "x1": line["x1"]} for line in page.lines
            if line["bottom"] - line["top"] <= 1]
    thin += [{"top": (rect["top"] + rect["bottom"]) / 2, "x0": rect["x0"], "x1": rect["x1"]} for rect in page.rects
             if rect["bottom"] - rect["top"] <= _SHADE_MIN_SIDE and rect["x1"] - rect["x0"] > _SHADE_MIN_SIDE]
    return _horizontal_rules(page, thin)


def _shade_bands(page: object) -> list[tuple[float, float, float, float]]:
    """(top, bottom, left end, right end) of the page's filled rectangles (no border, not white, both sides longer than
    `_SHADE_MIN_SIDE`)."""
    return [(rect["top"], rect["bottom"], rect["x0"], rect["x1"]) for rect in page.rects
            if rect.get("fill") and not rect.get("stroke") and _is_shade(rect.get("non_stroking_color"))
            and min(rect["x1"] - rect["x0"], rect["bottom"] - rect["top"]) > _SHADE_MIN_SIDE]


def _column_edges(table: object) -> list[float]:
    """The vertical boundaries of a ruled table's cell boxes (left to right, boundaries within 1pt count as one)."""
    edges: list[float] = []
    for x in sorted({x for cell in table.cells if cell for x in (cell[0], cell[2])}):
        if not edges or x - edges[-1] > 1:
            edges.append(x)
    return edges


def _grown_shaded_table(page: object, table: object, tables: list[object],
                        rules: list[tuple[float, float, float]]) -> tuple[object, list[object]] | None:
    """The table found again after growing a ruled table with cell shading (`table`) by bands on the same column boundaries,
    and the tables it replaces. None when there is nothing to change.

    Upward and downward, a band runs up to (1) another table fragment with the same column boundaries (the next shaded row of
    the stripes), (2) a horizontal rule covering the table width (a row's bottom border), or, when neither exists, (3) one
    band as tall as the fragment's row height (the last unshaded row); it continues only while each band's cell text fits the
    column boundaries (`_band_cells`). Rows with merged cells inside the table get the same test. Such bands and rows get
    vertical lines only when, in two or more columns, the cell text matches the same column's text on another row at the left
    end, right end or centre (`_aligned_cells`) — a body paragraph or first-column note under a shaded header, or a centred
    merged title row, does not become cells. As an exception, when the topmost closed band has non-numeric text in every
    column (`_is_header_band`) it is accepted as the header — the left-aligned text of an unshaded header did not match the
    right-aligned number columns and the whole growing step was given up (pinned by: test_pdf_shaded_tables.py::
    TestShadedTables::test_unshaded_header_over_banded_rows_with_right_aligned_numbers_stays_one_table). Vertical lines are
    drawn in each band exactly at the cell boundaries, only that region is searched again, and the table is replaced only
    when exactly one table comes out.
    """
    edges = _column_edges(table)
    if len(edges) < 3:
        return None
    left, right = edges[0], edges[-1]
    fragments = [other for other in tables if _same_column_edges(_column_edges(other), edges)]
    # rows spanned by a merged cell are tall, so the last open band also covered the paragraph below the table ('끝.')
    row_height = _row_pitch(fragments)
    reach = row_height * _SHADED_ROW_REACH
    spanning = sorted(y for y, x0, x1 in rules if x0 <= left + 2 and x1 >= right - 2)
    chain, bands = [table], []  # the table fragments joined so far, (top, bottom, whether it is an open band)
    top, bottom = table.bbox[1], table.bbox[3]
    # The top and bottom of every fragment are measured once and the joined fragments are looked up by identity. Measuring
    # every fragment and searching the whole chain for it at every step grew with the cube of the number of shaded rows: 5
    # of the 7 seconds this function took on a striped table of 2,000 rows (a 53 KB file) (Pinned by:
    # test_audit_pdf_mid.py::TestLongStripedTable::test_fragments_are_measured_once)
    ends = [(fragment.bbox[1], fragment.bbox[3], fragment) for fragment in fragments]
    joined = {id(table)}

    # continue the bands downward (+1) and upward (-1)
    for direction in (1, -1):
        edge = bottom if direction > 0 else top
        while True:
            nearby = [(start if direction > 0 else end, fragment) for start, end, fragment in ends if id(fragment) not in joined]
            nearby = [(y, fragment) for y, fragment in nearby if -1 <= (y - edge) * direction <= reach]
            nearby += [(y, None) for y in spanning if 1 < (y - edge) * direction <= reach]
            if not nearby:
                band = (edge, edge + row_height) if direction > 0 else (edge - row_height, edge)
                if _band_cells(page, band, edges) is not None and _ends_the_text(page, band, direction, left, right, row_height):
                    bands.append((*band, True))  # added to the region only when accepted (below)
                elif direction < 0 and _band_cells(page, band, edges) is not None:
                    bands += _unshaded_head(page, band, edges, row_height)
                break
            y, fragment = min(nearby, key=lambda item: (item[0] - edge) * direction)
            if (y - edge) * direction > 1:
                band = (edge, y) if direction > 0 else (y, edge)
                if _band_cells(page, band, edges) is None:
                    break
                bands.append((*band, False))
            if fragment is None:
                edge = y
            else:
                chain.append(fragment)
                joined.add(id(fragment))
                edge = fragment.bbox[3] if direction > 0 else fragment.bbox[1]
        if direction > 0:
            bottom = edge
        else:
            top = edge

    # rows with every cell present are the reference for column alignment; rows with merged cells are, like the bands,
    # candidates to split again
    columns = len(edges) - 1
    references, merged = [], []  # cell text of the reference rows, (band, cell text, whether it is an open band)
    for fragment in chain:
        for row in fragment.rows:
            band = (row.bbox[1], row.bbox[3])
            cells = _band_cells(page, band, edges)
            if len(row.cells) == columns and all(cell is not None for cell in row.cells):
                references.append(cells)
            elif cells is not None:
                merged.append((band, cells, False))
    between = [((band_top, band_bottom), _band_cells(page, (band_top, band_bottom), edges), is_open)
               for band_top, band_bottom, is_open in bands]
    candidates = [*merged, *between]

    def aligned(entry: tuple) -> bool:
        return _aligned_cells(entry[1], [*references, *(other[1] for other in candidates if other is not entry)])

    # when the topmost closed band is header-shaped it is accepted without column alignment — a left-aligned header does not
    # match right-aligned number columns
    heading = next((entry for entry in between if not entry[2] and abs(entry[0][0] - top) <= 1
                    and _is_header_band(page, entry[0], edges)), None)
    if any(not entry[2] and entry is not heading and not aligned(entry) for entry in between):
        return None  # a band between shaded fragments is not a table row
    accepted = [(entry[0], entry[2]) for entry in candidates if entry is heading or aligned(entry)]
    if not accepted:
        return None

    # draw the column-boundary vertical lines only in the accepted bands (plus the outer horizontal lines for open bands) and
    # search only that region again
    top = min([top, *(band[0] for band, is_open in accepted if is_open)])
    bottom = max([bottom, *(band[1] for band, is_open in accepted if is_open)])
    verticals = [_rule_line(x, band_top, x, band_bottom) for (band_top, band_bottom), _ in accepted for x in edges]
    horizontals = [_rule_line(left, y, right, y) for (band_top, band_bottom), is_open in accepted if is_open
                   for y in (band_top, band_bottom)]
    page_left, page_top, page_right, page_bottom = page.bbox
    box = (max(left - 1, page_left), max(top - 1, page_top), min(right + 1, page_right), min(bottom + 1, page_bottom))
    if box[0] >= box[2] or box[1] >= box[3]:
        return None
    found = page.crop(box).find_tables({**_TABLE_SETTINGS, "explicit_vertical_lines": verticals,
                                        "explicit_horizontal_lines": horizontals})
    if len(found) != 1 or len(_column_edges(found[0])) != len(edges):
        return None
    return found[0], chain


def _unshaded_head(page: object, band: tuple[float, float], edges: list[float],
                   row_height: float) -> list[tuple[float, float, bool]]:
    """The band list when the unshaded row (`band`) above the table's topmost shaded fragment and the header band above it
    are accepted together as open bands. Otherwise [].

    Only one open band was accepted above and one below, so in a striped table whose header and first data row are both
    unshaded (Chrome `tbody tr:nth-child(even)`) the header continued above the first row at line spacing and the first row
    was not accepted. The header scattered into '번호 지점 / 금액 / 상태' and the first row became a jumbled paragraph,
    worse than with no shading at all (pinned by: test_pdf_round44.py::TestZebraTableUnderAnUnshadedHeaderAndFirstRow).
    Accepted only when the band above is header-shaped (`_is_header_band`) and the text above it is set apart."""
    head = (band[0] - row_height, band[0])
    if not _is_header_band(page, head, edges) or not _ends_the_text(page, head, -1, edges[0], edges[-1], row_height):
        return []
    return [(*band, True), (*head, True)]


def _ends_the_text(page: object, band: tuple[float, float], direction: int, left: float, right: float, row_height: float) -> bool:
    """Whether, beyond the text of the open band at the end of a table (`band`, outward direction `direction`), the next text
    within the table width is more than half a row height away. Text that continues past the band at the same line spacing
    (a list under a shaded title box) is not the table's last row."""
    chars = [char for char in page.chars if char["text"].strip() and char["x1"] > left and char["x0"] < right]
    inside = [char for char in chars if band[0] < (char["top"] + char["bottom"]) / 2 < band[1]]
    if direction > 0:
        end = max(char["bottom"] for char in inside)
        return all(char["top"] - end >= row_height / 2 for char in chars if char["top"] >= band[1] - 1)
    end = min(char["top"] for char in inside)
    return all(end - char["bottom"] >= row_height / 2 for char in chars if char["bottom"] <= band[0] + 1)


def _same_column_edges(edges: list[float], others: list[float]) -> bool:
    return len(edges) == len(others) and all(abs(edge - other) <= 1 for edge, other in zip(edges, others, strict=True))


def _rule_line(x0: float, top: float, x1: float, bottom: float) -> dict:
    """A line to add to table finding (a line object for pdfplumber `explicit_*_lines`)."""
    return {"object_type": "line", "x0": x0, "x1": x1, "top": top, "bottom": bottom, "width": x1 - x0, "height": bottom - top}


def _band_cells(page: object, band: tuple[float, float], edges: list[float]) -> list[tuple[float, float] | None] | None:
    """(left end, right end) per column of the text in a horizontal band (`band` = top, bottom) split by the column boundaries
    (`edges`). Only when there is text in two or more columns, all the text lies inside the band and the table width, and no
    line crosses a column boundary at word spacing; otherwise None (`_band_chars`)."""
    cells = _band_chars(page, band, edges)
    if cells is None:
        return None
    return [(min(char["x0"] for char in cell), max(char["x1"] for char in cell)) if cell else None for cell in cells]


def _is_header_band(page: object, band: tuple[float, float], edges: list[float]) -> bool:
    """Whether the band (`band`) is shaped like a header row: text in every column and not a single number cell."""
    cells = _band_chars(page, band, edges)
    if cells is None or not all(cells):
        return False
    texts = ("".join(char["text"] for char in sorted(cell, key=lambda char: char["x0"])) for cell in cells)
    return not any(_NUMBER_CELL.match(text) for text in texts)


def _band_chars(page: object, band: tuple[float, float], edges: list[float]) -> list[list[dict]] | None:
    """The characters per column for `_band_cells` (the column containing each character's centre). None unless the same
    conditions hold.

    A character that reaches into the band by less than `_BAND_SLACK_RATIO` of its height belongs to the neighbouring row and
    is not counted — the character boxes of a Word number column touched the band of the row below by 0.06pt, that band was
    rejected as 'text sticking out of the band', and the unshaded rows merged into one cell (pinned by:
    test_pdf_shaded_tables.py::TestShadedTables::test_numbers_that_barely_touch_the_next_row_do_not_merge_its_cells).
    """
    top, bottom = band
    left, right = edges[0], edges[-1]
    tops, ordered, tallest = _chars_by_top(page)
    chars = [char for char in ordered[bisect_left(tops, top - tallest):bisect_left(tops, bottom)]
             if char["x1"] > left and char["x0"] < right
             and min(char["bottom"], bottom) - max(char["top"], top) >= (char["bottom"] - char["top"]) * _BAND_SLACK_RATIO]
    for char in chars:
        slack = (char["bottom"] - char["top"]) * _BAND_SLACK_RATIO
        if (char["top"] < top - slack or char["bottom"] > bottom + slack or char["x0"] < left - 1 or char["x1"] > right + 1):
            return None

    cells: list[list[dict]] = [[] for _ in edges[1:]]  # characters per column (the column containing the centre)

    for char in chars:
        middle = (char["x0"] + char["x1"]) / 2
        cells[max(0, min(bisect_right(edges, middle) - 1, len(cells) - 1))].append(char)

    # when the text of two neighbouring columns on the same line sits at word spacing on either side of a column boundary, the
    # text crosses that boundary
    for index in range(1, len(cells)):
        for char in cells[index - 1]:
            size = char.get("size") or (char["bottom"] - char["top"])
            if any(abs(other["top"] - char["top"]) <= size / 2 and other["x0"] - char["x1"] < size * _CELL_GAP_MIN_RATIO
                   for other in cells[index]):
                return None
    if sum(1 for cell in cells if cell) < 2:
        return None
    return cells


def _chars_by_top(page: object) -> tuple[list[float], list[dict], float]:
    """The page's non-blank characters in order of their top edge, as (list of top edges, list of characters, tallest character
    height). Built once per page (`_band_chars`).

    Scanning every character of the page for every band took 48 seconds on a large page with many shaded bands and tables
    (1500pt square, 50,000 characters, 196 tables) (pinned by:
    test_pdf_shaded_tables.py::test_many_bands_on_a_page_with_many_letters_take_linear_time).
    """
    index = page.__dict__.get("_chars_by_top")
    if index is None:
        ordered = sorted((char for char in page.chars if char["text"].strip()), key=lambda char: char["top"])
        index = ([char["top"] for char in ordered], ordered, max((char["bottom"] - char["top"] for char in ordered), default=0.0))
        page.__dict__["_chars_by_top"] = index
    return index


def _aligned_cells(cells: list[tuple[float, float] | None] | None,
                   others: list[list[tuple[float, float] | None] | None]) -> bool:
    """Whether, in two or more columns, the band's cell text (`cells`) matches the same column's text on the other rows
    (`others`) at the left end, right end or centre."""
    if cells is None:
        return False
    matched = 0

    for column, cell in enumerate(cells):
        if cell is None:
            continue
        if any(other is not None and other[column] is not None
               and min(abs(other[column][0] - cell[0]), abs(other[column][1] - cell[1]),
                       abs(sum(other[column]) - sum(cell)) / 2) <= _TABLE_ROW_TOLERANCE
               for other in others):
            matched += 1
    return matched >= 2


def _is_inner_part(part: tuple[float, ...], closed: tuple[float, ...]) -> bool:
    """Whether `part`, found as an open-sided table, is an inner part of `closed`, a table found from rules alone: same height
    and sharing exactly one of the left and right ends.

    When the first column of a table closed on all sides is vertically merged and some row merges columns 1-2 horizontally
    (a header '구분' spanning two columns, a '합계' (total) row), the vertical line between columns 1 and 2 does not run the
    full height of the table, so the horizontal lines that stop short of the merged cell look like a 'table open on the
    left' to `_open_sided_tables`. If the partial table found that way from column 2 onward is kept, the whole table that
    encloses it is discarded as a layout box in `_real_tables` and the category column came out as lines outside the table
    (pinned by: test_pdf.py::TestRuledTables::
    test_closed_table_with_merged_first_column_under_a_two_column_header_is_kept_whole).
    The condition is narrowed to match how `_open_sided_tables` filters regions against each other (deliberate): tables
    that share no end or share both ends, and tables of a different height (a table placed inside a box), are left as
    before — an outer box enclosing an inner table may be layout.
    """
    same_height = abs(closed[1] - part[1]) <= 2 and abs(closed[3] - part[3]) <= 2
    return same_height and _encloses(closed, part) and (abs(closed[0] - part[0]) <= 2) != (abs(closed[2] - part[2]) <= 2)


def _cell_rows(table: object) -> list[list[str | None]]:
    """The cell text of a table. The value of a vertically merged cell is repeated on the rows below too (same rule as
    markdown.TableGrid: repeat vertically, first cell only horizontally).

    pdfplumber gives None for positions hidden by a merge. A cell box spanning several rows is a vertical merge, so the None
    values on the lower rows of that column are filled with the first row's value. None values produced by a horizontal merge
    are left as they are (header merging uses them)
    (pinned by: test_quality.py::test_pdf_table_with_dotted_cell_borders_keeps_rows_and_repeats_merged_cell).

    A cell enclosing two or more other cells is not a merge but a cell holding an inner table (the inner table touches the
    outer border and is caught as one table). Only the text outside the inner cells is kept in that cell and it is not
    repeated, and the first row of such a cell is dropped when it has no such text and holds only values repeated on the row
    below (deliberate). The whole text of the inner table was repeated on every inner row and tripled the character count of
    a LibreOffice résumé (pinned by: test_pdf_nested_tables.py::TestNestedTables::
    test_inner_table_caught_as_cells_of_the_outer_table_is_not_repeated).

    The middle of a row or column is measured not from `row.bbox`/`column.bbox` but from the band where all the cells of
    that row or column overlap (the latest-starting cell to the earliest-ending cell) (deliberate). The box swells by the
    merged cell (a first row containing a cell that spans three rows, a first column containing a '합계' (total) cell that
    spans two columns), its middle moves into another row or column, and the repetition was lost in tables with both
    vertical and horizontal merges (pinned by: test_pdf.py::TestRuledTables::
    test_vertical_merge_is_repeated_in_every_row_when_another_row_merges_columns).
    """
    rows = table.extract(**_CELL_TEXT)
    _read_sideways_cells(table, rows)
    _join_soft_wraps(table, rows)
    row_middles = [_shared_middle(row.cells, 1, 3) for row in table.rows]
    column_middles = [_shared_middle(column.cells, 0, 2) for column in table.columns]
    containers: list[int] = []  # rows where a cell holding an inner table starts

    for cell in table.cells:
        x0, top, x1, bottom = cell
        spanned = [index for index, middle in enumerate(row_middles) if top <= middle <= bottom]
        column = next((index for index, middle in enumerate(column_middles) if x0 <= middle <= x1), None)
        if len(spanned) < 2 or column is None or column >= len(rows[spanned[0]]) or not rows[spanned[0]][column]:
            continue
        inner = [other for other in table.cells if other != cell and _encloses(cell, other)]
        if len(inner) >= 2:
            rows[spanned[0]][column] = _text_outside(table.page.chars, cell, inner)
            containers.append(spanned[0])
            continue
        for index in spanned[1:]:
            if rows[index][column] is None:
                rows[index][column] = rows[spanned[0]][column]

    return [row for index, row in enumerate(rows) if index not in containers or not _repeated_below(rows, index)]


def _text_outside(chars: list[dict], cell: tuple, inner: list[tuple]) -> str:
    """The text inside cell `cell` that lies outside the inner cells (`inner`) (a caption above the inner table, etc.). Each
    line is joined left to right."""
    outside = [char for char in chars if _inside_any(char, [cell]) and not _inside_any(char, inner)]
    lines: list[list[dict]] = []

    for char in sorted(outside, key=lambda char: (char["top"], char["x0"])):
        if lines and abs(char["top"] - lines[-1][0]["top"]) <= _TABLE_ROW_TOLERANCE:
            lines[-1].append(char)
        else:
            lines.append([char])

    return "\n".join(text for text in (_text_from_chars(line) for line in lines) if text)


def _repeated_below(rows: list[list[str | None]], index: int) -> bool:
    """Whether every text cell of row `index` is repeated in the same column of the row right below (a row holding only values
    repeated by a vertical merge)."""
    if index + 1 >= len(rows):
        return False
    return all(not cell or (column < len(rows[index + 1]) and rows[index + 1][column] == cell)
               for column, cell in enumerate(rows[index]))


def _shared_middle(cells: list[tuple | None], low: int, high: int) -> float:
    """The middle of the band where all the cells of one row (or column) overlap. `low` and `high` are the positions of the
    start and end coordinates in a cell box."""
    cells = [cell for cell in cells if cell is not None]
    return (max(cell[low] for cell in cells) + min(cell[high] for cell in cells)) / 2


def _join_soft_wraps(table: object, rows: list[list[str | None]]) -> None:
    """Join the lines in ruled-table cell text that wrapped because of the cell width (modifies `rows` in place).

    A `<br>` went in for every line of cell text, so one cell of an old-versus-new provisions comparison table was broken at
    every visual line, like '…임직원이 업무를 수<br>행함에…' (pinned by:
    test_pdf_cell_wraps.py::TestSoftWrappedLinesInRuledCells). A wrapped line is one where the cell has no room left for the
    first word of the next line (`_soft_wrapped`), and it is joined with a space. A cell with a single Hangul or Hanja
    character on every line ('인/적/사/항') is a vertically stacked word and is joined without spaces.
    Deliberate: a generator that wraps Korean per character (breaking in the middle of a word) is also joined with a space —
    there is no telling which kind it is, in that case '수 행함에' is broken exactly as much as the former '수<br>행함에',
    and with the word-level wrapping that is the default in Word and Hangul (HWP) the result is the original text. Cells
    containing a picture (the picture's position is decided from the line height of the cell text,
    `_place_images_in_cells`), cells containing rotated characters, and cells where the lines regrouped from characters do
    not match the lines of the cell text are left untouched.
    """
    images = [(image["x0"], image["top"], image["x1"], image["bottom"]) for image in table.page.images]
    bbox = table.bbox  # a property recomputed from all the cell boxes, so calling it per character is quadratic time
    # The page's characters are sorted once per page (`_text_by_middle`) and the table takes those of its own vertical range
    # (Pinned by: test_audit_pdf_mid.py::TestManyTablesOnOnePage::test_each_table_takes_only_its_own_part_of_the_page)
    middles, ordered = _text_by_middle(table.page)
    chars = [char for char in ordered[bisect_left(middles, bbox[1]):bisect_right(middles, bbox[3])] if _inside_any(char, [bbox])]
    middles = [(char["top"] + char["bottom"]) / 2 for char in chars]  # per cell, bisect to only the characters in its vertical range

    for row_index, row in enumerate(table.rows):
        for column_index, cell in enumerate(row.cells):
            text = rows[row_index][column_index] if column_index < len(rows[row_index]) else None
            if cell is None or not text or "\n" not in text or any(_overlaps(cell, image) for image in images):
                continue
            band = chars[bisect_left(middles, cell[1]):bisect_right(middles, cell[3])]
            lines = _char_lines([char for char in band if _inside_any(char, [cell])])
            parts = text.split("\n")
            if any(_is_sideways(char) for line in lines for char in line) or len(lines) != len(parts) or any(
                    _text_from_chars(line).replace(" ", "") != part.replace(" ", "") for line, part in zip(lines, parts, strict=True)):
                continue
            rows[row_index][column_index] = _joined_cell_lines(cell, lines, parts)


def _char_lines(chars: list[dict]) -> list[list[dict]]:
    """Group characters into lines whose top coordinates are within `_TABLE_ROW_TOLERANCE` (top to bottom, left to right
    inside a line)."""
    lines: list[list[dict]] = []

    for char in sorted(chars, key=lambda char: (char["top"], char["x0"])):
        if lines and abs(char["top"] - lines[-1][0]["top"]) <= _TABLE_ROW_TOLERANCE:
            lines[-1].append(char)
        else:
            lines.append([char])
    return [sorted(line, key=lambda char: char["x0"]) for line in lines]


def _joined_cell_lines(cell: tuple, lines: list[list[dict]], parts: list[str]) -> str:
    """Join the lines of a cell (`cell`; character groups `lines`, text `parts`) with a space for wrapped lines and a line
    break for real line breaks (`_join_soft_wraps`)."""
    if len(parts) > 1 and all(len(part.strip()) == 1 and _SPACED_CHAR.match(part.strip()) for part in parts):
        return "".join(part.strip() for part in parts)

    # Only left-aligned cells are considered (a first-line indent is fine). The lines of a centred cell
    # ('위와 같이 신청합니다. / 2024년 7월 25일 / 신청인') are real line breaks even though, measured against the widest line,
    # there seems to be no room left.
    starts = [min(char["x0"] for char in line) for line in lines]
    if max(starts[1:]) - min(starts[1:]) > _TABLE_ROW_TOLERANCE or starts[0] < min(starts[1:]) - _TABLE_ROW_TOLERANCE:
        return "\n".join(parts)
    margin = max(0.0, min(starts) - cell[0])  # assume the same cell padding on the right as on the left
    right = cell[2] - margin
    text = parts[0]

    for line, following, part in zip(lines, lines[1:], parts[1:], strict=False):
        text += (" " if _soft_wrapped(line, following, right) else "\n") + part
    return text


def _soft_wrapped(line: list[dict], following: list[dict], right: float) -> bool:
    """Whether line `line` of cell text wrapped because of the cell width: the next line does not start with a list item
    (`_BARE_LIST_NUMBER`, `_BULLET_START`), has the same font size, and its first word (plus a space) does not fit between the
    end of this line and the right end of the cell (`right`)."""
    size = line[-1]["size"]
    head = _text_from_chars(following)
    if abs(following[0]["size"] - size) > 0.5 or _BULLET_START.match(head) or _BARE_LIST_NUMBER.match(head):
        return False
    word: list[dict] = []

    for char in following:
        if word and char["x0"] - word[-1]["x1"] > _LAPARAMS["word_margin"] * max(char["width"], char["height"]):
            break
        word.append(char)
    return right - line[-1]["x1"] < word[-1]["x1"] - word[0]["x0"] + size * 0.5


def _overlaps(box: tuple, other: tuple) -> bool:
    """Whether two boxes (x0, top, x1, bottom) overlap."""
    return box[0] < other[2] and other[0] < box[2] and box[1] < other[3] and other[1] < box[3]


def _read_sideways_cells(table: object, rows: list[list[str | None]]) -> None:
    """Replace the text of cells whose characters are all rotated 90 degrees with the text read in the rotated direction
    (modifies `rows` in place).

    A category name written sideways in a vertically merged cell ('기계 설비') was read top to bottom like horizontal text and
    became '비설<br>계기' (pinned by:
    test_pdf.py::TestRuledTables::test_text_rotated_inside_a_cell_is_read_in_its_own_direction). A cell with even one
    horizontal character mixed in (the axis title and ticks of a chart caught as a table) is left as is — there is no way to
    decide in what order to join text of two directions in one cell.
    """
    index = _sideways_by_middle(table.page)
    if not index:
        return
    # Each cell looks only at the characters of its own vertical range. Scanning the rotated characters and then every
    # character of the page for every cell took 23 seconds on a table laid sideways on one page (100 rows of 20 columns,
    # a 10 KB file) (Pinned by: test_audit_pdf_mid.py::TestSidewaysCells::test_each_cell_looks_only_at_its_own_band)
    marked, every = index

    for row_index, row in enumerate(table.rows):
        for column_index, cell in enumerate(row.cells):
            if cell is None or not _chars_in_cell(marked, cell):
                continue
            chars = _chars_in_cell(every, cell)
            if all(_is_sideways(char) for char in chars if char["text"].strip()):
                rows[row_index][column_index] = "\n".join(_sideways_paragraphs(chars))


def _sideways_by_middle(page: object) -> tuple:
    """The page's non-blank rotated characters and all of its characters, each sorted by position (`_by_middle`). An empty
    tuple when the page has no rotated character. Built once per page (`_read_sideways_cells`), not once per table (Pinned by:
    test_audit_pdf_mid.py::TestManyTablesOnOnePage::test_each_table_takes_only_its_own_part_of_the_page)."""
    def build() -> tuple:
        sideways = [char for char in page.chars if char["text"].strip() and _is_sideways(char)]
        return (_by_middle(sideways), _by_middle(page.chars)) if sideways else ()

    return _page_index(page, "sideways", build)


def _by_middle(chars: list[dict]) -> list[tuple[list[float], list[tuple[float, int, dict]]]]:
    """The characters as (middle, position in `chars`, character) in order of the vertical middle and in order of the
    horizontal middle, each together with the list of those middles (to bisect on). A character whose middle is not a number
    is inside no box and is left out."""
    index = []
    for low, high in (("top", "bottom"), ("x0", "x1")):
        placed = [((char[low] + char[high]) / 2, position, char) for position, char in enumerate(chars)]
        ordered = sorted((item for item in placed if item[0] == item[0]), key=lambda item: item[0])
        index.append(([item[0] for item in ordered], ordered))
    return index


def _chars_in_cell(index: list[tuple[list[float], list[tuple[float, int, dict]]]], cell: tuple) -> list[dict]:
    """The characters of `index` (`_by_middle`) whose centre is inside `cell`, in the order of the list they came from. Only
    the characters of the cell's vertical range or of its horizontal range are examined, whichever are fewer (a table of one
    long row has every character in the same vertical range)."""
    (middles, by_row), (centres, by_column) = index
    rows = bisect_left(middles, cell[1]), bisect_right(middles, cell[3])
    columns = bisect_left(centres, cell[0]), bisect_right(centres, cell[2])
    band = by_row[rows[0]:rows[1]] if rows[1] - rows[0] <= columns[1] - columns[0] else by_column[columns[0]:columns[1]]
    return [char for _, _, char in sorted((item for item in band if _inside_any(item[2], [cell])), key=lambda item: item[1])]


def _open_sided_tables(page: object) -> list[object]:
    """Find tables without left and right borders again at their full width.

    A shape common in Korean reports: horizontal lines are drawn across the full table width and vertical lines only between
    the inner columns. Finding cells from the rules made only the columns between vertical lines a table, and the first and
    last columns scattered outside it. When there are three or more horizontal lines of the same width with vertical lines
    inside them but no vertical line at the ends of the horizontal lines, only that region is cropped and the table is
    searched for with both ends of the horizontal lines given as vertical borders
    (pinned by: test_quality.py::test_pdf_table_without_outer_borders_keeps_its_first_and_last_columns).

    When the first (or last) column is vertically merged there are two kinds of horizontal-line width (the full width, and
    the width that stops short of the merged cell), so a region also arises from the narrow width. The widest region is
    searched first, and a region inside a region where a table was found that shares only one end with it is part of the same
    table and is skipped. Searching it separately made the whole table that encloses the partial table get discarded as a
    layout box in `_real_tables`, and the category column went outside the table (the horizontal counterpart of
    `_outermost_spans`. Pinned by:
    test_pdf.py::TestRuledTables::test_open_sided_table_with_vertically_merged_first_column_is_kept_whole).
    Other overlapping regions (sharing no end, or sharing both ends) are searched separately as before and filtered by
    `_real_tables` — the lines of a chart drawing became a large table and repeated the tick labels on every row (public
    paper PDF).
    """
    # Pages with many vector shapes (graphs in a paper) have tens of thousands of lines. Rescanning all of them for every line
    # takes seconds per page (600 million comparisons on a 27-page paper), so vertical lines are sorted by x and horizontal
    # lines by height and only the needed range is examined.
    edges = [edge for edge in page.edges if not _is_invisible_edge(edge)]
    verticals = _chained_verticals([edge for edge in edges if edge["orientation"] != "h"])
    positions = [edge["x0"] for edge in verticals]

    def verticals_between(low: float, high: float) -> list[dict]:
        return verticals[bisect_left(positions, low):bisect_right(positions, high)]

    def at_column_border(x: float, top: float) -> bool:
        return any(edge["top"] - 1 <= top <= edge["bottom"] + 1 for edge in verticals_between(x - 1, x + 1))

    horizontals: dict[tuple[int, int], list[float]] = {}
    for left, right, top in _joined_rules([edge for edge in edges if edge["orientation"] == "h"], at_column_border):
        horizontals.setdefault((round(left), round(right)), []).append(top)

    page_left, page_top, page_right, page_bottom = page.bbox
    areas = []  # (region to crop, left end, right end)
    for (left, right), rule_tops in horizontals.items():
        if len(rule_tops) < 3 or right - left < _MIN_FIGURE_POINTS:
            continue
        rule_tops.sort()
        sides = [verticals_between(side - 2, side + 2) for side in (left, right)]
        # The vertical range of the inner vertical lines is the table's height. When several tables of the same width are
        # stacked, each range is examined separately.
        spans = {(round(edge["top"]), round(edge["bottom"])) for edge in verticals_between(left + 2, right - 2)
                 if left + 2 < edge["x0"] < right - 2}
        for top, bottom in _outermost_spans(spans):
            rules = bisect_right(rule_tops, bottom + 2) - bisect_left(rule_tops, top - 2)
            if rules < 3:
                continue
            closed = [side for side in sides
                      if any(edge["top"] <= top + 2 and edge["bottom"] >= bottom - 2 for edge in side)]
            if len(closed) == 2:
                continue
            box = (max(left - 1, page_left), max(top - 1, page_top), min(right + 1, page_right), min(bottom + 1, page_bottom))
            if box[0] >= box[2] or box[1] >= box[3]:
                continue  # lines drawn off the page
            areas.append((box, left, right))

    # region number → the tables found in that region. The widest region is searched first, but they are returned in region order
    found: dict[int, list[object]] = {}
    for index in sorted(range(len(areas)), key=lambda index: -_box_area(areas[index][0])):
        box, left, right = areas[index]
        outers = [areas[other][0] for other, tables in found.items() if tables]
        if any(_encloses(outer, box) and (abs(outer[0] - box[0]) <= 2) != (abs(outer[2] - box[2]) <= 2) for outer in outers):
            continue
        found[index] = page.crop(box).find_tables({**_TABLE_SETTINGS, "explicit_vertical_lines": [left, right]})

    # Horizontal lines whose ends are off by about 1pt fall into two width groups, so the same table is found in two regions.
    # Keeping both made `_real_tables` see each as 'a box enclosing another table' and discard both, and a two-column
    # definition table ('총배출량 | 순배출량', a design-tool guidebook) scattered line by line (pinned by:
    # test_pdf.py::TestRuledTables::test_open_sided_table_found_from_two_rule_widths_is_kept_once)
    tables: list[object] = []
    for table in (table for index in sorted(found) for table in found[index]):
        if not any(all(abs(a - b) <= 2 for a, b in zip(table.bbox, kept.bbox, strict=True)) for kept in tables):
            tables.append(table)
    return tables


def _is_invisible_edge(edge: dict) -> bool:
    """Whether this is a side of a rectangle that is only filled white and has no border. It is invisible on white paper and so
    is not a table's outer border.

    In an open-sided table with a white filled rectangle under every header cell, the outer sides of
    those rectangles looked like vertical lines closing the left and right on the header row, so the full-width horizontal
    lines were not joined and the first column (vertically merged '가격 기반') and the last column ('적용순위') scattered
    outside the table
    (pinned by: test_pdf.py::TestRuledTables::test_open_sided_table_with_white_filled_header_cells_keeps_its_outer_columns).
    Known accepted gap: the sides of white cells laid over a filled background are dropped even though they are visible —
    only `_open_sided_tables` uses this test, and ruled-table finding uses those sides as before.
    """
    return (edge.get("object_type") == "rect_edge" and not edge.get("stroke") and bool(edge.get("fill"))
            and _is_white(edge.get("non_stroking_color")))


def _chained_verticals(verticals: list[dict]) -> list[dict]:
    """The vertical line pieces at the same x joined into one line where they overlap or are broken by no more than
    `_VERTICAL_JOIN_GAP` (in x order).

    A generator that draws the vertical lines separately per cell breaks them by 0.25pt at each row boundary. Measuring the
    vertical range per piece made the table height look like a single row, no range held three or more horizontal lines, and
    the open-sided table was not found (pinned by: test_pdf.py::TestRuledTables::
    test_open_sided_table_whose_column_rules_are_drawn_row_by_row_keeps_its_outer_columns). Keeping pieces drawn on top of
    each other at the same place (a header cell border and a column separator) apart produced overlapping candidate regions
    and a legal-register table scattered into per-cell paragraphs. Known accepted gap: the lifelines of a sequence diagram
    drawn twice like a shadow also become one line, so a drawing that used to cancel itself out as two overlapping
    candidates remains as a small fake table.
    """
    chained: list[dict] = []

    for edge in sorted(verticals, key=lambda edge: (round(edge["x0"], 1), edge["top"])):
        last = chained[-1] if chained else None
        if (last is not None and round(last["x0"], 1) == round(edge["x0"], 1)
                and edge["top"] - last["bottom"] <= _VERTICAL_JOIN_GAP):
            last["bottom"] = max(last["bottom"], edge["bottom"])
        else:
            chained.append({"x0": edge["x0"], "top": edge["top"], "bottom": edge["bottom"]})

    chained.sort(key=lambda edge: edge["x0"])
    return chained


def _joined_rules(edges: list[dict], at_column_border: Callable[[float, float], bool]) -> list[tuple[float, float, float]]:
    """(x0, x1, top) of the horizontal lines. The horizontal lines of an open-sided table drawn broken per cell are returned
    with their pieces joined into one line.

    Some generators draw a table's horizontal lines broken per cell. Grouping widths piece by piece left an open-sided table
    with not a single full-width horizontal line, and the first and last columns stayed outside the table (pinned by:
    test_pdf.py::TestRuledTables::
    test_open_sided_table_whose_rules_are_drawn_cell_by_cell_keeps_its_outer_columns). The joining conditions are kept narrow.
    - At the same height the end of the previous piece and the start of the next piece touch (a gap of at most
      `_RULE_JOIN_GAP`, an overlap of at most `_RULE_JOIN_OVERLAP`), and there is a vertical line (a column boundary) at that
      place. Joining dotted lines (0.4pt pieces, 0.45pt gaps) turned dotted cell boundaries into full-width horizontal lines
      and split the table
      (pinned by: test_quality.py::test_pdf_table_with_dotted_cell_borders_keeps_rows_and_repeats_merged_cell).
    - There is no vertical line at either end of the joined line (the sides are open). When there is a vertical line at an
      end the pieces are left as they are — the borders of chart panels placed side by side were joined into one table and
      even the figure captions ended up in cells (public paper PDF).
    """
    rules: list[list[dict]] = []  # joined pieces (left to right)
    # (height, end x) → (end x, order, line) of the lines that ended there, in end-x order. When looking for a touching line,
    # only the range of end x that can be joined is examined — scanning the list from the start took 457 seconds for 45,000
    # lines stacked at the same place (a 2KB PDF) (pinned by: test_pdf_resource_limits.py::TestStackedRules).
    ends: dict[tuple[float, int], list[tuple[float, int, list[dict]]]] = {}
    for order, edge in enumerate(sorted(edges, key=lambda edge: edge["x0"])):
        level, start = round(edge["top"], 1), round(edge["x0"])
        rule = None
        for x in (start, start + 1, start - 1):
            ended = ends.get((level, x), [])
            at = bisect_left(ended, (edge["x0"] - _RULE_JOIN_GAP,))
            if at < len(ended) and ended[at][0] <= edge["x0"] + _RULE_JOIN_OVERLAP and ended[at][0] < edge["x1"]:
                if at_column_border(edge["x0"], edge["top"]):
                    rule = ended.pop(at)[2]
                break
        if rule is None:
            rule = [edge]
            rules.append(rule)
        else:
            rule.append(edge)
        insort(ends.setdefault((level, round(edge["x1"])), []), (edge["x1"], order, rule))

    joined = []
    for rule in rules:
        first, last = rule[0], rule[-1]
        if len(rule) > 1 and not at_column_border(first["x0"], first["top"]) and not at_column_border(last["x1"], last["top"]):
            joined.append((first["x0"], last["x1"], first["top"]))
        else:
            joined.extend((edge["x0"], edge["x1"], edge["top"]) for edge in rule)
    return joined


def _outermost_spans(spans: set[tuple[int, int]]) -> list[tuple[int, int]]:
    """Drop vertical ranges that lie inside another range. A contained range is part of the same table (the '상반기|하반기' line
    of a two-level header exists only on the lower row).

    Searching it separately produced a lower fragment table, the whole table enclosing the fragment was discarded as a layout
    box in `_real_tables`, and '구분' and '2025년' scattered outside the table
    (pinned by: test_quality.py::test_pdf_open_sided_table_with_two_level_header_is_kept_whole).
    """
    outermost: list[tuple[int, int]] = []
    for span in sorted(spans, key=lambda span: span[0] - span[1]):  # longest range first
        if not any(other[1] - other[0] > span[1] - span[0] and other[0] - 2 <= span[0] and span[1] <= other[1] + 2
                   for other in outermost):
            outermost.append(span)
    return outermost


def _merge_split_header(rows: list[list[str | None]]) -> list[list[str | None]]:
    """Merge a two-level header ('1월·2월' under '문의') into one row, '문의 1월 | 문의 2월'.

    pdfplumber gives None for cells that disappeared in a merge and "" for truly empty cells. The rows are treated as a
    header only when the first row has a horizontal merge (None), the second row below it has text, and both rows have two
    or more filled cells (a single-cell title row is handled separately by render_table).
    When an unmerged column has text of its own in the second row, that is data, not a sub-header. The first data row of a
    one-level header ('연락처' spanning two columns) was attached to the header and became '이름 홍길동'
    (pinned by: test_quality.py::test_pdf_one_level_header_spanning_two_columns_keeps_the_first_data_row).
    When the second row has only one cell under a merged cell (`_undivided_span`), it is the next row, not a sub-header.
    """
    if len(rows) < 3:
        return rows
    first, second = rows[0], rows[1]
    spanned = [index for index, cell in enumerate(first) if cell is None and index > 0]
    # the merged cells and the cell where the merge starts (a horizontal merge is a run of None)
    covered = {*spanned, *(index - 1 for index in spanned)}
    if (not spanned or sum(1 for cell in first if cell) < 2 or sum(1 for cell in second if cell) < 2
            or not any(index < len(second) and second[index] for index in spanned)
            or any(second[index] and second[index] != first[index]
                   for index in range(min(len(first), len(second))) if index not in covered)
            or _undivided_span(first, second)):
        return rows

    filled, last = [], ""
    for cell in first:
        last = cell if cell is not None else last
        filled.append(last or "")
    merged = [" ".join(dict.fromkeys(part.strip() for part in (filled[index], second[index] or "") if part and part.strip()))
              for index in range(min(len(first), len(second)))]
    return [merged, *rows[2:]]


def _undivided_span(first: list[str | None], second: list[str | None]) -> bool:
    """Whether, among the horizontally merged cells of the first row, there is one under which the second row has only a
    single (undivided) cell whose text differs from the merged cell.

    A two-level header ('1월·2월' under '문의') divides into two or more cells under the merged cell. In a form grid, the
    vertical lines of the item rows below make the '등록번호' cell look like a merge spanning two grid columns, and the
    '상호' cell below it spans the same two columns — merging it as a sub-header gave
    '등록번호 상호 | 123-45-67890 (주)라마바' (pinned by: test_pdf_form_grids.py::TestFormGridHeader).
    """
    width = min(len(first), len(second))
    for start in range(width):
        end = start + 1
        while end < width and first[end] is None:
            end += 1
        if first[start] is None or end - start < 2:
            continue
        below = [cell for cell in second[start:end] if cell and cell.strip() and cell != first[start]]
        if len(below) == 1:
            return True
    return False
