"""Characters and shapes that would be taken for the rules or the text of a table: glyph outlines, spaces that do not
advance, highlights drawn behind text, and rows of cells that are a tiled background.
"""

from bisect import bisect_left, bisect_right
from collections.abc import Callable
from itertools import accumulate, pairwise
import math

from opendockit.extractors._pdf_model import _BoxIndex


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
# Filled rectangle inside a filled rectangle of the same colour (`_without_nested_fills`): how far (pt) it may reach past the
# outer one's edges
_NESTED_FILL_SLACK = 0.5
# Bars of a bar chart (`_without_chart_bars`): at least this many, and how far (pt) their widths and stacked edges may differ
_CHART_BARS_MIN, _BAR_SLACK = 3, 0.5
# ... and how much wider the widest bar of a group may be than the narrowest (two series drawn 2.3pt and 3.1pt wide)
_BAR_WIDTH_RATIO = 1.5
# ... and how thick (pt) a bar is at least: Word draws the rules of a table as filled rectangles 0.5–1pt thick, in pieces per row
_BAR_MIN_THICKNESS = 2.0


# Glyph outline curve (_without_glyph_outlines): a curve smaller than this size (pt) both horizontally and vertically, and thicker
# than this on both sides (not a straight line)
_GLYPH_OUTLINE_MAX, _STRAIGHT_MAX = 20.0, 0.1


def _without_unbounded_shapes(page: object) -> object:
    """Page without the lines, rectangles and curves that have a coordinate that is not a finite number (for finding tables).
    `page` itself when there are none.

    A rule drawn to `1e400` (an infinite end) reached the table finders, which round and compare rule ends: `round(inf)` raised
    an OverflowError and the whole PDF was rejected (Pinned by:
    test_pdf_resource_limits.py::TestShapesWithoutAFinitePosition). Such a shape has no place on the page and is no rule of a
    table. Dropping it here, where the shapes enter table finding, keeps every finder below from seeing it.
    """
    def unbounded(shape: dict) -> bool:
        return not (all(math.isfinite(shape[side]) for side in ("x0", "x1", "top", "bottom"))
                    and all(math.isfinite(value) for point in shape.get("pts") or () for value in point))

    dropped = {id(shape) for shapes in (page.lines, page.rects, page.curves) for shape in shapes if unbounded(shape)}
    if not dropped:
        return page
    return page.filter(lambda obj: id(obj) not in dropped)


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


def _without_nested_fills(page: object) -> object:
    """Page without the filled rectangles drawn inside a filled rectangle of the same colour (for finding tables). `page` itself
    when there are none.

    Word shades a paragraph inside a shaded table cell once more, one borderless rectangle per line, in the cell's own colour.
    They are invisible — the same colour over the same colour — but pdfplumber took their edges for rules: a header cell of four
    lines split its row into four, every other header cell was repeated four times as a vertical merge, and the inset right edge
    of the line shading made an empty column (Pinned by: test_pdf_highlights.py::TestShadingInsideShadedCells). A rectangle is
    dropped only when it is filled without a border, lies within `_NESTED_FILL_SLACK` of a borderless filled rectangle of the same
    fill colour, and no rectangle of another colour drawn between the two overlaps it (that one would make its edges show).
    """
    rects = page.rects
    fills = [(order, rect) for order, rect in enumerate(rects) if rect.get("fill") and not rect.get("stroke")]
    if len(fills) < 2:
        return page
    # (x0, top, x1, bottom, drawing order) of the borderless fills, and of every rectangle
    index = _BoxIndex([(rect["x0"], rect["top"], rect["x1"], rect["bottom"], order) for order, rect in fills])
    drawn = _BoxIndex([(rect["x0"], rect["top"], rect["x1"], rect["bottom"], order) for order, rect in enumerate(rects)])
    colours = [_colour(rect) for rect in rects]
    hidden: set[int] = set()
    for order, rect in fills:
        box = (rect["x0"], rect["top"], rect["x1"], rect["bottom"])
        for x0, top, x1, bottom, other in index.near(box):
            if other == order or colours[other] != colours[order] or (x1 - x0) * (bottom - top) <= rect["width"] * rect["height"]:
                continue
            if not (x0 - _NESTED_FILL_SLACK <= box[0] and box[2] <= x1 + _NESTED_FILL_SLACK
                    and top - _NESTED_FILL_SLACK <= box[1] and box[3] <= bottom + _NESTED_FILL_SLACK):
                continue
            low, high = min(order, other), max(order, other)
            if any(low < between < high and colours[between] != colours[order] and _overlaps(rects[between], box)
                   for *_, between in drawn.near(box)):
                continue
            hidden.add(id(rect))
            break
    if not hidden:
        return page
    return page.filter(lambda obj: id(obj) not in hidden)


def _without_chart_bars(page: object) -> object:
    """Page without the bars of a bar chart (for finding tables). `page` itself when there are none.

    A bar chart drawn as vector shapes is a row of borderless filled rectangles standing on (or hanging from) one baseline. pdfplumber took the
    bar edges and the axis for rules, and the chart became a table of its value labels ('| 2 | 1 | 4 | 1 |', '| -3.1% |
    -4.4% |'; 11 of 200 pages of a pdf corpus held such a table, Pinned by: test_pdf_highlights.py::TestBarCharts). Bars
    are `_CHART_BARS_MIN` or more rectangles of one fill colour that share one edge (bottom or top; left or right for bars drawn
    sideways), at least `_BAR_MIN_THICKNESS` thick, about as wide (tall) as each other (`_BAR_WIDTH_RATIO`), more than
    `_BAR_SLACK` apart, not all of one length, and holding no text (one may) — the cells of a shaded table row share both
    edges and are all as tall, the cells of a column touch, shaded cells hold their text, and the rules Word draws as thin
    filled rectangles are thinner, so none of them are bars. The
    segments stacked on a bar (same left and right edges, touching or overlapping it; for a bar drawn sideways, the same top
    and bottom) go with it.
    """
    fills = [rect for rect in page.rects if rect.get("fill") and not rect.get("stroke")
             and min(rect["width"], rect["height"]) >= _BAR_MIN_THICKNESS]
    if len(fills) < _CHART_BARS_MIN:
        return page
    middles = sorted(((char["x0"] + char["x1"]) / 2, (char["top"] + char["bottom"]) / 2) for char in page.chars
                     if char["text"].strip())

    def holds_text(rect: dict) -> bool:
        return any(rect["top"] <= y <= rect["bottom"]
                   for _, y in middles[bisect_left(middles, (rect["x0"], -math.inf)):bisect_right(middles, (rect["x1"], math.inf))])

    bars: set[int] = set()
    for along, across, start, end in (("bottom", "width", "x0", "x1"), ("top", "width", "x0", "x1"),
                                      ("x0", "height", "top", "bottom"), ("x1", "height", "top", "bottom")):
        groups: dict[tuple[object, float], list[dict]] = {}
        for rect in fills:
            groups.setdefault((_colour(rect), round(rect[along])), []).append(rect)
        for group in groups.values():
            if len(group) < _CHART_BARS_MIN:
                continue
            thickness = sorted(rect[across] for rect in group)
            lengths = {round(rect["height" if across == "width" else "width"]) for rect in group}
            ordered = sorted(group, key=lambda rect: rect[start])
            apart = all(after[start] > before[end] + _BAR_SLACK for before, after in pairwise(ordered))
            if (thickness[-1] <= max(thickness[0] * _BAR_WIDTH_RATIO, thickness[0] + _BAR_SLACK) and len(lengths) > 1 and apart
                    and sum(1 for rect in group if holds_text(rect)) <= 1):
                bars.update(id(rect) for rect in group)
    if not bars:
        return page
    found = [rect for rect in fills if id(rect) in bars]
    stacked = {id(rect) for rect in fills if id(rect) not in bars and any(
        abs(rect["x0"] - bar["x0"]) <= _BAR_SLACK and abs(rect["x1"] - bar["x1"]) <= _BAR_SLACK
        and rect["top"] <= bar["bottom"] + _BAR_SLACK and bar["top"] <= rect["bottom"] + _BAR_SLACK
        for bar in found) or any(
        abs(rect["top"] - bar["top"]) <= _BAR_SLACK and abs(rect["bottom"] - bar["bottom"]) <= _BAR_SLACK
        and rect["x0"] <= bar["x1"] + _BAR_SLACK and bar["x0"] <= rect["x1"] + _BAR_SLACK
        for bar in found if bar["width"] > bar["height"])}
    dropped = bars | stacked
    return page.filter(lambda obj: id(obj) not in dropped)


def _colour(rect: dict) -> object:
    """The fill of a rectangle as a key: its colour components with its colour space and pattern. A fill whose colour is not
    known is a key of its own, equal to no other (two unknown colours may differ)."""
    colour = rect.get("non_stroking_color")
    if colour is None:
        return object()
    components = tuple(round(value, 3) if isinstance(value, int | float) else value for value in colour) \
        if isinstance(colour, list | tuple) else colour
    return (components, rect.get("ncs"), rect.get("non_stroking_pattern"))


def _overlaps(shape: dict, box: tuple[float, float, float, float]) -> bool:
    return shape["x0"] < box[2] and box[0] < shape["x1"] and shape["top"] < box[3] and box[1] < shape["bottom"]


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
    # Each line looks only at the words within its reach, sorted by top once — scanning every word of the page for every
    # line took time with the square of the lines (2,000 highlighted lines: 2.7 seconds) (Pinned by:
    # test_pdf_highlights.py::TestManyHighlightedLines)
    ordered = sorted((word for word in words if math.isfinite(word["top"])), key=lambda word: word["top"])
    tops = [word["top"] for word in ordered]
    cells = set()
    for top, line in lines.items():
        if len(line) < 2 or not _tiled_row(line):
            continue
        size = max(char["size"] for _, inside in line for char in inside)
        reach = size * _CELL_ROW_REACH
        if math.isfinite(top) and math.isfinite(reach):
            window = abs(reach) + abs(size) + 1
            nearby = ordered[bisect_left(tops, top - window):bisect_right(tops, top + window)]
        else:
            nearby = words
        near = [word for word in nearby if size / 2 < abs(word["top"] - top) <= reach]
        if (not any(abs(word["top"] - top) <= size / 2 for word in nearby)
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
