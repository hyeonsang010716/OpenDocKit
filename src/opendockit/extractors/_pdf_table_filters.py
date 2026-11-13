"""Characters and shapes that would be taken for the rules or the text of a table: glyph outlines, spaces that do not
advance, highlights drawn behind text, and rows of cells that are a tiled background.
"""

from bisect import bisect_left, bisect_right
from collections.abc import Callable
from itertools import accumulate, pairwise


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


# Glyph outline curve (_without_glyph_outlines): a curve smaller than this size (pt) both horizontally and vertically, and thicker
# than this on both sides (not a straight line)
_GLYPH_OUTLINE_MAX, _STRAIGHT_MAX = 20.0, 0.1


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
