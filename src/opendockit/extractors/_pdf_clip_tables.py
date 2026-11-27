"""Tables of a PDF whose writer sets a clipping path around each table cell (Hangul's own PDF export).

Hancom PDF draws a table cell by cell: for each cell a clipping path that is the box of the cell, then what the cell
holds. The boxes of one table share their edges exactly, so they are the cells of the table as the document had them —
its columns, its rows, its merged cells and the tables inside its cells — where the rules drawn on the page give only
the borders that are visible. The boxes are noted while the page is read (`_pdf_pdfminer._note_clip`, for a document
whose Producer is one of `pdf._CELL_CLIPPING_PRODUCERS`) and handed to the reading of ruled tables as tables found
(`_pdf_ruled_tables._real_tables`), which reads their text, their merged cells and the tables inside them as it does
for a table whose every border is drawn.

What a group of boxes is follows Hangul's own reader (`hwpx.py`, `_hangul.py`), since the boxes are the document's
tables: a row of boxes is a line of the document unless one of them holds several lines, a column of boxes its
paragraphs, and boxes that frame text (no two of them with text side by side) are that text.
"""

from pdfplumber.table import Table

from opendockit.extractors._pdf_pdfminer import _CLIPS


# Two edges this close (pt) are one edge. The boxes of one table share their edges to the hundredth of a point; a box
# glued to a table (a caption box, a title box under a date box) stands 0.2pt or more off.
_EDGE = 0.15
# A clip as wide or as high as this share of the page is the page's own (the body), not a cell.
_PAGE_SHARE = 0.9
# A page with more boxes than this is read from its rules: the grouping compares every box with every other. The
# largest page of 113 press releases holds 206.
_MAX_BOXES = 600
# The comparisons one page may cost while its boxes are put into tables; past them the page is read from its rules.
_MAX_COMPARISONS = 400_000
# A box this close (pt) to a picture on every side is the clip of that picture, set inside the cell that holds it.
_PICTURE_SLACK = 1.0

_Box = tuple[float, float, float, float]


def _clip_tables(page: object) -> list[Table]:
    """The tables the clip boxes of the page state, each a pdfplumber table of its cell boxes; none for a page whose
    clips were not noted, a turned page, or one with too many boxes."""
    root = getattr(page, "root_page", page)
    noted = getattr(root.layout, _CLIPS, None)  # the layout the characters of the page were read from
    if not noted or getattr(root, "rotation", 0):
        return []
    width, height = float(root.width), float(root.height)
    left, top = float(root.mediabox[0]), float(root.mediabox[1])
    boxes = {(x0 + left, height - y1 + top, x1 + left, height - y0 + top) for x0, y0, x1, y1 in noted
             if 0 < x1 - x0 < _PAGE_SHARE * width and 0 < y1 - y0 < _PAGE_SHARE * height}
    if len(boxes) < 2 or len(boxes) > _MAX_BOXES:
        return []
    pictures = [(float(image["x0"]), float(image["top"]), float(image["x1"]), float(image["bottom"])) for image in page.images]
    groups = _cell_groups(sorted(boxes), page.chars, pictures)
    return [Table(page, group) for group in groups]


def _cell_groups(boxes: list[_Box], chars: list[dict], pictures: list[_Box]) -> list[list[_Box]]:
    """The boxes (x0, top, x1, bottom) that are the cells of a table, table by table from the top of the page.

    The boxes that touch, at one depth of boxes inside each other, are one group: the cells of one table lie beside
    each other and never over each other, so a box set a little into the row above it (the row of dates under the
    title box of a press release) starts another group, and so do the cells of a table inside a cell. A group is a
    table when it has two columns or more, two rows or more — or one row with a box of several lines — and is no frame
    (`_is_frame`). The clip of a picture is no cell."""
    cells = [box for box in _snapped(boxes)
             if not any(all(abs(a - b) <= _PICTURE_SLACK for a, b in zip(box, picture, strict=True)) for picture in pictures)]
    home = _homes(cells)
    # after the snapping, boxes that touch have an edge of the same value: the boxes by each of their edges
    by_edge: dict[tuple[int, float], list[_Box]] = {}
    for box in cells:
        for side in range(4):
            by_edge.setdefault((side, box[side]), []).append(box)
    groups, seen, compared = [], set(), 0
    for start in cells:
        if start in seen:
            continue
        seen.add(start)
        group, todo = [], [start]
        while todo:
            box = todo.pop()
            group.append(box)
            beside = (*by_edge.get((0, box[2]), ()), *by_edge.get((2, box[0]), ()),
                      *by_edge.get((1, box[3]), ()), *by_edge.get((3, box[1]), ()))
            for other in sorted(beside):
                if other in seen or home[other] != home[box] or not _touching(box, other):
                    continue
                compared += len(group) + len(todo)
                if compared > _MAX_COMPARISONS:
                    return []
                if not any(_over(other, member) for member in (*group, *todo)):
                    seen.add(other)
                    todo.append(other)
        rows, columns = len({box[1] for box in group}), len({box[0] for box in group})
        if columns >= 2 and (rows >= 2 or _holds_lines(group, chars)) and not _is_frame(group, chars):
            groups.append(sorted(group, key=lambda box: (box[1], box[0])))
    return sorted(groups, key=lambda group: (min(box[1] for box in group), min(box[0] for box in group)))


def _homes(cells: list[_Box]) -> dict[_Box, _Box | None]:
    """The smallest box each box lies inside, or None: the cell that holds a table inside it, for the cells of that
    table."""
    home: dict[_Box, _Box | None] = dict.fromkeys(cells)
    by_left = sorted(cells)
    for index, outer in enumerate(by_left):
        x0, top, x1, bottom = outer
        area = (x1 - x0) * (bottom - top)
        # a box inside `outer` starts no further left than it, give or take an edge
        start = index
        while start > 0 and by_left[start - 1][0] >= x0 - _EDGE:
            start -= 1
        for inner in by_left[start:]:
            if inner[0] > x1:
                break
            if (inner is not outer and inner[1] >= top - _EDGE and inner[2] <= x1 + _EDGE and inner[3] <= bottom + _EDGE
                    and inner != outer):
                held = home[inner]
                if held is None or area < (held[2] - held[0]) * (held[3] - held[1]):
                    home[inner] = outer
    return home


def _snapped(boxes: list[_Box]) -> list[_Box]:
    """The boxes with edges within `_EDGE` of each other set to one value, so that the cells of a row start at one
    height and those of a column at one place (pdfplumber builds the rows and columns of a table from equal edges)."""
    def lines(values: list[float]) -> dict[float, float]:
        placed: dict[float, float] = {}
        line = None
        for value in sorted(set(values)):
            if line is None or value - line > _EDGE:
                line = value
            placed[value] = line
        return placed

    across = lines([value for box in boxes for value in (box[0], box[2])])
    down = lines([value for box in boxes for value in (box[1], box[3])])
    return sorted({(across[x0], down[top], across[x1], down[bottom]) for x0, top, x1, bottom in boxes
                   if across[x1] > across[x0] and down[bottom] > down[top]})


def _within(box: _Box, chars: list[dict]) -> list[dict]:
    """The characters that are no space whose middle lies in the box."""
    return [char for char in chars if box[0] <= (char["x0"] + char["x1"]) / 2 <= box[2]
            and box[1] <= (char["top"] + char["bottom"]) / 2 <= box[3] and char["text"].strip()]


def _holds_lines(group: list[_Box], chars: list[dict]) -> bool:
    """Whether a box of the group holds two lines of text or more, one under the other: Hangul's reader keeps a table
    of one row as a table when a cell holds more than one line (`_hangul.SizeHeadings.row`)."""
    for box in group:
        inside = _within(box, chars)
        # a character of the lower line starts below the middle of the tallest one of the upper line
        if inside and (max(char["top"] for char in inside) - min(char["top"] for char in inside)
                       >= max(char["bottom"] - char["top"] for char in inside) / 2):
            return True
    return False


def _is_frame(group: list[_Box], chars: list[dict]) -> bool:
    """Whether the boxes are a frame around text and not a table: at no height do two of them hold text side by side
    (a box with its title in a tab above it, a column of boxes beside an empty one). Hangul's own reader writes such a
    table as the text it frames (`_hangul.is_frame`). The text of a box is what lies in it and in no smaller box of the
    group inside it."""
    x0, top = min(box[0] for box in group), min(box[1] for box in group)
    x1, bottom = max(box[2] for box in group), max(box[3] for box in group)
    by_size = sorted(group, key=lambda box: (box[2] - box[0]) * (box[3] - box[1]))
    filled: set[_Box] = set()
    for char in _within((x0, top, x1, bottom), chars):
        x, y = (char["x0"] + char["x1"]) / 2, (char["top"] + char["bottom"]) / 2
        for box in by_size:
            if box[0] <= x <= box[2] and box[1] <= y <= box[3]:
                filled.add(box)
                break
    tops = sorted({box[1] for box in group})
    for upper, lower in zip(tops, [*tops[1:], bottom], strict=True):
        middle = (upper + lower) / 2
        if sum(1 for box in group if box in filled and box[1] <= middle <= box[3]) > 1:
            return False
    return True


def _inside(inner: _Box, outer: _Box) -> bool:
    return (inner != outer and inner[0] >= outer[0] - _EDGE and inner[1] >= outer[1] - _EDGE
            and inner[2] <= outer[2] + _EDGE and inner[3] <= outer[3] + _EDGE)


def _over(first: _Box, second: _Box) -> bool:
    """Whether two boxes cover a common area."""
    return (min(first[2], second[2]) - max(first[0], second[0]) > _EDGE
            and min(first[3], second[3]) - max(first[1], second[1]) > _EDGE)


def _touching(first: _Box, second: _Box) -> bool:
    """Whether two boxes share an edge: side by side with a common stretch of height, or one above the other with a
    common stretch of width."""
    beside = min(first[3], second[3]) - max(first[1], second[1]) > _EDGE
    above = min(first[2], second[2]) - max(first[0], second[0]) > _EDGE
    return ((beside and (abs(first[2] - second[0]) <= _EDGE or abs(second[2] - first[0]) <= _EDGE))
            or (above and (abs(first[3] - second[1]) <= _EDGE or abs(second[3] - first[1]) <= _EDGE)))
