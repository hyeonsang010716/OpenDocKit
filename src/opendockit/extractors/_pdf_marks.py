"""Marks drawn as shapes beside text: check boxes with their ticks, and circles used as bullets."""

from bisect import bisect_left, bisect_right
import math

from opendockit.extractors._pdf_model import (
    _BULLET_START,
    _CHECKBOX_SIDE,
    _MARKER_GAP_RATIO,
    _SHADE_MIN_SIDE,
    _bbox,
    _encloses,
    _inside_any,
    _is_white,
    _Line,
)


_CHECKBOX_GAP_RATIO = 2.0
# Distance a check mark may spill outside the box (relative to the box width). A hand-drawn X or an angled stroke goes 1–3pt past
# the box corners.
_CHECKBOX_MARK_OVERFLOW_RATIO = 0.3


def _mark_checkboxes(page: object, lines: list[_Line], tables: list[tuple[float, float, float, float]]) -> None:
    """When a check box drawn as a rectangle sits right beside text, puts ☑ (a line or filled rectangle inside) or ☐ (empty box)
    on that line.

    When the '□ 동의함 □ 동의하지 않음' (agree / do not agree) of a consent form is drawn with rectangles and an angled stroke,
    there are no characters and which side was chosen was lost (Pinned by:
    test_pdf_toc_and_checkboxes.py::TestCheckboxesDrawnAsShapes). Only boxes meeting all of the following are considered
    (deliberate, conservative): an unfilled outlined rectangle, a square with a side in the `_CHECKBOX_SIDE` range (sides differ by
    at most 1.5pt), not touching another rectangle (a row of character cell boxes), outside ruled tables, the box centre within the
    label line's height, no characters inside the box. The label is, of the line starting to the right of the box (mark before the
    text) and the line ending to its left (mark after the text, '동의함 ☑'), the one closer to the box, and that distance is at
    most `_CHECKBOX_GAP_RATIO` × the larger of the box width and the label's character size. On a tie, the right one (the common
    form with the box in front). Looking only to the right, a box after its label attached to the next label and the chosen side
    was swapped (Pinned by: TestCheckboxesBesideTheirLabels). A line already starting (ending) with a box character (☐☑■□) is left
    alone.
    Known accepted gap: boxes inside ruled table cells and boxes wedged between words and grouped into one line are not
    considered. A square bullet drawn only as an outline becomes ☐.
    """
    rects = page.rects
    boxes = [rect for rect in rects
             if rect.get("stroke") and not rect.get("fill") and _CHECKBOX_SIDE[0] <= rect["width"] <= _CHECKBOX_SIDE[1]
             and abs(rect["width"] - rect["height"]) <= 1.5 and not _inside_any(rect, tables)]
    curve_boxes = _curve_boxes(page.curves, tables)
    if not (boxes or curve_boxes) or not lines:
        return

    marks = _MarkIndex([*page.curves, *page.lines, *(rect for rect in rects if rect.get("fill"))])
    before: set[int] = set()  # lines with the mark placed before the text
    after: set[int] = set()  # lines with the mark placed after the text
    # Rectangles at most `cell` pt on a side are looked up by the grid cell of their top-left corner: one that touches a box starts
    # at most `cell` + 1pt before the box and no later than 0.5pt after it, so it is in one of the cells searched below. Larger
    # ones (frames, cell backgrounds) and those with huge or non-finite coordinates are always scanned. Every box scanned all the
    # rectangles of the page, so a page of 10,000 separate 8pt squares took 17 seconds here
    # (Pinned by: test_audit_pdf_head.py::TestManySquaresOnOnePage).
    cell = 64.0
    near: dict[tuple[int, int], list[dict]] = {}
    far: list[dict] = []
    for rect in rects:
        small = (all(abs(rect[side]) < 1e9 for side in ("x0", "top", "x1", "bottom"))
                 and rect["x1"] - rect["x0"] <= cell and rect["bottom"] - rect["top"] <= cell)
        (near.setdefault((math.floor(rect["x0"] / cell), math.floor(rect["top"] / cell)), []) if small else far).append(rect)

    for box in boxes:
        around = rects
        if all(abs(box[side]) < 1e9 for side in ("x0", "top", "x1", "bottom")):
            cells = [near.get((column, row), ())
                     for column in range(math.floor((box["x0"] - 1) / cell) - 1, math.floor((box["x1"] + 0.5) / cell) + 1)
                     for row in range(math.floor((box["top"] - 1) / cell) - 1, math.floor((box["bottom"] + 0.5) / cell) + 1)]
            around = (other for group in (far, *cells) for other in group)
        if any(other is not box and other["x0"] <= box["x1"] + 0.5 and box["x0"] <= other["x1"] + 0.5
               and other["top"] <= box["bottom"] + 0.5 and box["top"] <= other["bottom"] + 0.5
               and not _encloses(_bbox(box), _bbox(other)) and not _encloses(_bbox(other), _bbox(box))
               for other in around):
            continue  # box touching another rectangle (a row of character cell boxes). A frame around the box or a filled mark inside is fine
        _put_checkbox_mark(box, any(mark is not box and _ticks(box, mark) for mark in marks.around(box)), page.chars, lines, before,
                           after)

    # For curve boxes (rounded-corner boxes, radio buttons) the curves drawn on top of each other on the same spot (white background
    # + outline) are taken as one group. Without a mark, only if the group has a drawn outline and there is another curve box in
    # the same row — circle bullets placed one per line are not boxes.
    rows = _GroupRows(curve_boxes)
    for group in curve_boxes:
        box = group[0]
        ticked = any(all(mark is not member for member in group) and _ticks(box, mark) for mark in marks.around(box))
        if not ticked and not (any(member.get("stroke") for member in group)
                               and rows.has_other(group, (box["top"] + box["bottom"]) / 2)):
            continue
        _put_checkbox_mark(box, ticked, page.chars, lines, before, after)


def _mark_circle_bullets(page: object, lines: list[_Line]) -> None:
    """Puts a small circle bullet drawn as a curve (filled circle → •, outlined circle → ◦) in front of the line that starts to its
    right at the same height.

    Chrome draws `<ul>` bullets not as characters but as 3pt curves (level 1 a filled circle, level 2 an outlined circle), so the
    bullets vanished and the list hierarchy went flat (Pinned by: test_pdf_toc_and_checkboxes.py::TestBulletsDrawnAsCircles). The
    position rule is the same as in `_marker_rows`: a line that starts close to the right of the circle (within
    `_MARKER_GAP_RATIO` × the line height) and whose height contains the circle's vertical centre. The circle has a nearly square
    box, a diameter of at least `_SHADE_MIN_SIDE`, smaller than a check box (`_CHECKBOX_SIDE`) and at most 0.6 × the line height —
    a circle as large as the line height (a radio button) is not a bullet. A line already starting with a bullet or box character
    is left alone.

    Chrome draws level 3 bullets as filled rectangles of the same size (not curves); by the same position rule ▪ is placed (▫ if
    outline only) (Pinned by:
    test_pdf_toc_and_checkboxes.py::TestBulletsDrawnAsCircles::test_third_level_square_bullets_are_kept).
    The lines whose height holds the circle's centre are looked up by binary search (`_LinesSpanning`) instead of scanning every
    line for every small shape (Pinned by: test_audit_pdf_text.py::TestManyCurveBoxes).
    """
    spanning = _LinesSpanning(lines)
    for curve in [*page.curves, *page.rects]:
        width, height = curve["x1"] - curve["x0"], curve["bottom"] - curve["top"]
        if not (_SHADE_MIN_SIDE <= width < _CHECKBOX_SIDE[0] and abs(width - height) <= 0.5) or not (curve.get("fill") or curve.get("stroke")):
            continue
        middle = (curve["top"] + curve["bottom"]) / 2
        line = min((line for line in spanning.at(middle) if line.top <= middle <= line.bottom and width <= line.height * 0.6
                    and curve["x1"] - 0.5 <= line.x0 <= curve["x1"] + line.height * _MARKER_GAP_RATIO),
                   key=lambda line: line.x0, default=None)
        if line is None or _BULLET_START.match(line.text) or line.text[:1] in "☐☑■□":
            continue
        filled = curve.get("fill") and not _is_white(curve.get("non_stroking_color"))
        square = curve.get("object_type") == "rect"
        line.text = f"{('▪' if filled else '▫') if square else ('•' if filled else '◦')} {line.text}"
        line.x0 = curve["x0"]


class _LinesSpanning:
    """A page's lines sorted by top edge. `at(y)` gives, in page order, every line whose top-to-bottom range can hold `y`: lines up
    to `_TALL` pt tall found by binary search, plus taller lines and lines with coordinates that are not ordinary (`_ordinary`),
    which are always given."""

    _TALL = 64.0

    def __init__(self, lines: list[_Line]) -> None:
        self._lines = lines
        short = sorted((line.top, index) for index, line in enumerate(lines)
                       if _ordinary(line.top, line.bottom) and line.bottom - line.top <= self._TALL)
        kept = {index for _, index in short}
        self._tall = [index for index in range(len(lines)) if index not in kept]
        self._tops = [top for top, _ in short]
        self._order = [index for _, index in short]

    def at(self, y: float) -> list[_Line]:
        if not _ordinary(y):
            return self._lines
        start, end = bisect_left(self._tops, y - self._TALL - 1.0), bisect_right(self._tops, y)
        return [self._lines[index] for index in sorted([*self._order[start:end], *self._tall])]


def _curve_boxes(curves: list[dict], tables: list[tuple[float, float, float, float]]) -> list[list[dict]]:
    """Candidate check boxes and radio buttons drawn as curves: closed (start and end point within 1pt) square-shaped curves (side
    `_CHECKBOX_SIDE`) grouped by same spot (each edge within 1.5pt), largest first. A group lying inside another group (the filled
    dot of a radio button) is a mark and is left out.

    The groups are looked up through a grid of the top-left corners of their first shapes (`_CornerGrid`) — a group that a shape
    joins or lies inside has its corner within one shape side of the shape's corner, and the first such group in creation order
    is taken, as a scan of all groups gives. Scanning all groups for every shape, with the row and tick scans below, took 35
    seconds on a page of 5,000 small circles (Pinned by: test_audit_pdf_text.py::TestManyCurveBoxes)."""
    shapes = [curve for curve in curves
              if (curve.get("stroke") or curve.get("fill")) and _CHECKBOX_SIDE[0] <= curve["width"] <= _CHECKBOX_SIDE[1]
              and abs(curve["width"] - curve["height"]) <= 1.5 and len(curve.get("pts") or ()) >= 4
              and math.dist(curve["pts"][0], curve["pts"][-1]) <= 1.0 and not _inside_any(curve, tables)]
    groups: list[list[dict]] = []
    reach = max((max(shape["x1"] - shape["x0"], shape["bottom"] - shape["top"]) for shape in shapes), default=0.0) + 2.0
    grid = _CornerGrid(reach)

    for shape in sorted(shapes, key=lambda curve: -curve["width"]):
        candidates = [groups[index] for index in grid.near(shape)]
        group = next((group for group in candidates
                      if all(abs(a - b) <= 1.5 for a, b in zip(_bbox(group[0]), _bbox(shape), strict=True))), None)
        if group is not None:
            group.append(shape)
        elif not any(_encloses(_bbox(group[0]), _bbox(shape)) for group in candidates):
            grid.add(shape, len(groups))
            groups.append([shape])

    return groups


def _ordinary(*values: float) -> bool:
    """Whether every value is a finite coordinate small enough (below 1e9 pt) for the grids and searches here to be exact."""
    return all(abs(value) < 1e9 for value in values)


class _CornerGrid:
    """Group numbers by the top-left corner of the group's first shape, in cells of `reach` pt. `near(shape)` gives, in creation
    order, every group whose corner lies within `reach` before to 1.5pt after the shape's corner — all a shape of side below
    `reach` - 2 can join (edges within 1.5pt) or lie inside (`_encloses`). Corners that are not ordinary numbers (`_ordinary`)
    are always given, and such a shape gets every group."""

    def __init__(self, reach: float) -> None:
        self._reach = max(reach, 1.0)
        self._cells: dict[tuple[int, int], list[int]] = {}
        self._odd: list[int] = []

    def add(self, shape: dict, number: int) -> None:
        if _ordinary(shape["x0"], shape["top"]):
            self._cells.setdefault((math.floor(shape["x0"] / self._reach), math.floor(shape["top"] / self._reach)), []).append(number)
        else:
            self._odd.append(number)

    def near(self, shape: dict) -> list[int]:
        if not _ordinary(shape["x0"], shape["top"]):
            return sorted([*self._odd, *(number for numbers in self._cells.values() for number in numbers)])
        found = list(self._odd)
        for column in range(math.floor((shape["x0"] - self._reach) / self._reach), math.floor((shape["x0"] + 1.5) / self._reach) + 1):
            for row in range(math.floor((shape["top"] - self._reach) / self._reach), math.floor((shape["top"] + 1.5) / self._reach) + 1):
                found.extend(self._cells.get((column, row), ()))
        return sorted(found)


class _GroupRows:
    """The curve-box groups sorted by the top of their first shape, answering 'is there another group whose first shape spans
    this height' with a binary search (the shapes are at most `_CHECKBOX_SIDE` + 1.5pt tall). Groups whose first shape has a
    coordinate that is not ordinary (`_ordinary`) are always looked at, and so is everything for a height that is not."""

    def __init__(self, groups: list[list[dict]]) -> None:
        finite = [group for group in groups if _ordinary(group[0]["top"], group[0]["bottom"])]
        self._odd = [group for group in groups if not _ordinary(group[0]["top"], group[0]["bottom"])]
        finite.sort(key=lambda group: group[0]["top"])
        self._groups = finite
        self._tops = [group[0]["top"] for group in finite]
        self._tallest = max((group[0]["bottom"] - group[0]["top"] for group in finite), default=0.0)

    def has_other(self, group: list[dict], middle: float) -> bool:
        if not _ordinary(middle):
            return any(other is not group and other[0]["top"] <= middle <= other[0]["bottom"] for other in [*self._groups, *self._odd])
        start, end = bisect_left(self._tops, middle - self._tallest - 1.0), bisect_right(self._tops, middle)
        return any(other is not group and other[0]["top"] <= middle <= other[0]["bottom"]
                   for other in [*self._groups[start:end], *self._odd])


class _MarkIndex:
    """Possible check marks (curves, lines, filled rectangles) by the 64pt grid cell of their centre. `around(box)` gives every
    mark whose centre can be inside the box — `_ticks` needs exactly that. A mark whose centre is not a finite number is inside no
    box and is left out; a box with a coordinate that is not finite gets every mark. Scanning every mark for every box was
    quadratic (`_curve_boxes`)."""

    _CELL = 64.0

    def __init__(self, marks: list[dict]) -> None:
        self._marks = marks
        self._cells: dict[tuple[int, int], list[dict]] = {}
        for mark in marks:
            x0, top, x1, bottom = _bbox(mark)
            middle_x, middle_y = (x0 + x1) / 2, (top + bottom) / 2
            if math.isfinite(middle_x) and math.isfinite(middle_y):
                self._cells.setdefault((math.floor(middle_x / self._CELL), math.floor(middle_y / self._CELL)), []).append(mark)

    def around(self, box: dict) -> list[dict]:
        if not all(math.isfinite(box[side]) for side in ("x0", "top", "x1", "bottom")) or box["x1"] < box["x0"] or box["bottom"] < box["top"]:
            return self._marks
        return [mark for column in range(math.floor(box["x0"] / self._CELL), math.floor(box["x1"] / self._CELL) + 1)
                for row in range(math.floor(box["top"] / self._CELL), math.floor(box["bottom"] / self._CELL) + 1)
                for mark in self._cells.get((column, row), ())]


def _put_checkbox_mark(box: dict, ticked: bool, chars: list[dict], lines: list[_Line], before: set[int], after: set[int]) -> None:
    """Puts ☑ (`ticked`) or ☐ on the label line of the box `box`. A box with characters inside or without a label is left alone."""
    if any(box["x0"] < char["x0"] and char["x1"] < box["x1"] and box["top"] < char["top"] and char["bottom"] < box["bottom"]
           for char in chars):
        return
    label = _checkbox_label(box, lines, before, after)
    if label is None:
        return
    line, on_right = label
    mark = "☑" if ticked else "☐"
    line.text = f"{mark} {line.text}" if on_right else f"{line.text} {mark}"
    line.parts = line.fine_parts = ()  # the split pieces do not carry the mark
    (before if on_right else after).add(id(line))


def _checkbox_label(box: dict, lines: list[_Line], before: set[int], after: set[int]) -> tuple[_Line, bool] | None:
    """The label line of the check box `box` and whether that line is to the right of the box. The closer of the line starting to
    the right and the line ending to the left."""
    middle = (box["top"] + box["bottom"]) / 2
    found: list[tuple[float, bool, float, _Line]] = []

    for line in lines:
        if not line.top <= middle <= line.bottom:
            continue
        reach = max(box["width"], line.size) * _CHECKBOX_GAP_RATIO
        right_gap, left_gap = line.x0 - box["x1"], box["x0"] - line.x1
        if 0 <= right_gap <= reach and id(line) not in before and not line.text.startswith(("☐", "☑", "■", "□")):
            found.append((right_gap, False, line.x0, line))
        elif 0 <= left_gap <= reach and id(line) not in after and not line.text.endswith(("☐", "☑", "■", "□")):
            found.append((left_gap, True, -line.x1, line))

    if not found:
        return None
    _, on_left, _, line = min(found, key=lambda item: item[:3])
    return line, not on_left


def _ticks(box: dict, mark: dict) -> bool:
    """Whether `mark` (a line, curve or filled rectangle) is a check mark inside the box: its centre is inside the box, it is at
    least 0.4 × the box width, and it spills outside the box by at most `_CHECKBOX_MARK_OVERFLOW_RATIO` × the box width. A
    hand-drawn X going 1.5pt past the corners used to give an empty box (Pinned by:
    test_pdf_toc_and_checkboxes.py::TestCheckboxesBesideTheirLabels::test_mark_that_overflows_the_box_a_little_is_still_a_tick).
    """
    x0, top, x1, bottom = _bbox(mark)
    slack = box["width"] * _CHECKBOX_MARK_OVERFLOW_RATIO
    return (box["x0"] <= (x0 + x1) / 2 <= box["x1"] and box["top"] <= (top + bottom) / 2 <= box["bottom"]
            and _encloses((box["x0"] - slack, box["top"] - slack, box["x1"] + slack, box["bottom"] + slack), (x0, top, x1, bottom))
            and max(x1 - x0, bottom - top) >= box["width"] * 0.4)
