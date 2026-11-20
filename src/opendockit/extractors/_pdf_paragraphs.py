"""From the lines and tables of a page to its Markdown: paragraphs, headings by font size, pictures placed in cells, and
the columns a paragraph belongs to.
"""

from bisect import bisect_left
from itertools import accumulate
import math
import re

from opendockit.extractors._pdf_model import (
    _BOLD_BODY_MAX_RATIO,
    _BOLD_HEADING_MIN_RATIO,
    _BULLET_START,
    _CAPTION_START,
    _HEADING_SIZE_RATIO,
    _LONE_BULLET,
    _TABLE_ROW_TOLERANCE,
    _WORD_CHAR,
    _Line,
    _Page,
    _Paragraph,
    _Table,
)
from opendockit.extractors._pdf_ruled_tables import _in_cell
from opendockit.markdown import render_table


# Dot leader left in the page text (5 or more dots/middle dots or 3 or more ellipses; they may be spaced one apart). Shortened to a
# single ellipsis (`_shortened_leaders`).
_LEADER_RUN = re.compile(r"[ \t]*(?:[.·∙・](?:[ \t]?[.·∙・]){4,}|[…‥](?:[ \t]?[…‥]){2,})[ \t]*")


# Maximum number of lines of a label paragraph that sits to the left of a table or picture and is read before it (`_is_side_label`).
_SIDE_LABEL_MAX_LINES = 3


# Minimum width (relative to the table/picture width) for a paragraph that does not overlap a table or picture horizontally to
# count as 'body text of another column'. Narrow text such as a category name placed separately to the left of a table stays between
# the tables in height order as before — treated as a column, the category names were separated from their tables (public paper PDF).
_COLUMN_MIN_WIDTH_RATIO = 0.5


# Pictures placed side by side at the same height are kept together as one row when there are at least this many (`_picture_rows`).
_PICTURE_ROW_MIN = 3


def _render_page(page: _Page, heading_levels: dict[tuple[float, bool], int], line_gap: float,
                 images: list[tuple[tuple[float, ...], str]] | None = None, body_size: float = 0.0) -> str:
    """Join the page's paragraphs, tables and pictures in reading order. `images` is a list of ((top, x0, x1, bottom),
    placeholder)."""
    items: list[tuple[float, str, bool]] = []
    spans: list[tuple[float, float]] = []  # horizontal ranges (x0, x1) in the same order as items
    sizes: list[tuple[float, int]] = []  # (bottom coordinate, line count) in the same order as items. Tables and pictures are (bottom, 0)
    boxes: list[tuple[float, float, float, float]] = []  # (top, bottom, x0, x1) of paragraphs. Tables and pictures are not included
    side_by_side = _header_cells(page.lines, body_size) if heading_levels else frozenset()
    for paragraph in _group_paragraphs(page.lines, line_gap):
        key = _heading_key(paragraph, heading_levels, body_size, side_by_side)
        level = heading_levels[key] if key else 0
        text = (f"{'#' * level} {' '.join(line.text for line in paragraph.lines)}" if level
                else _escaped_block_marks(_paragraph_text(paragraph)))
        items.append((paragraph.top, text, False))
        spans.append((min(line.x0 for line in paragraph.lines), max(line.x1 for line in paragraph.lines)))
        sizes.append((paragraph.lines[-1].bottom, len(paragraph.lines)))
        boxes.append((paragraph.top, paragraph.lines[-1].bottom, *spans[-1]))

    # The flow order is kept, and a table or picture is inserted before the first paragraph that starts below it. When that
    # paragraph is a label placed to the left of the table or picture, it is inserted after it. A picture inside a ruled-table
    # cell is put in that cell.
    images = _place_images_in_cells(page.tables, images or [])
    blocks = [((table.top, table.x0, table.x1, table.bottom), table.text) for table in page.tables] + images
    follows = _picture_rows(images)
    # pictures joined as a row are placed left to right at the height of the row's first picture
    starts = {text: box[0] for box, text in images}

    for text in follows:
        start = text
        while start in follows:
            start = follows[start]
        starts[text] = starts[start]

    def order(block: tuple[tuple[float, ...], str]) -> tuple[float, float]:
        return (starts[block[1]], block[0][1]) if block[1] in follows else (block[0][0], -math.inf)

    for (block_top, x0, x1, block_bottom), block_text in sorted(blocks, key=order):
        position = next((index for index, (top, _, is_block) in enumerate(items)
                         if not is_block and top >= block_top), len(items))
        if block_text in follows:
            position = next(index for index, (_, text, is_block) in enumerate(items)
                            if is_block and text == follows[block_text]) + 1
        elif (position < len(items) and _in_other_column(spans[position], x0, x1, spans)
                and _columns_apart(spans[position], (block_top, block_bottom, x0, x1), boxes)):
            position = _position_in_column(items, spans, block_top, x0, x1)
        elif ((position == len(items) or not _in_other_column(spans[position], x0, x1, spans))
              and _after_other_column(items, spans, position, (block_top, block_bottom, x0, x1), boxes)):
            position = _position_in_column(items, spans, block_top, x0, x1)
        else:
            while (position < len(items) and not items[position][2]
                   and _is_side_label(items[position][0], *sizes[position], spans[position], (block_top, block_bottom, x0, x1), spans)):
                position += 1
        items.insert(position, (block_top, block_text, True))
        spans.insert(position, (x0, x1))
        sizes.insert(position, (block_bottom, 0))
    return _shortened_leaders("\n\n".join(text for text in [*(text for _, text, _ in items), *page.sideways,
                                                            "\n".join(page.form_values)] if text.strip()))


def _shortened_leaders(text: str) -> str:
    """The page text with dot leaders (`_LEADER_RUN`) shortened to a single ellipsis (' … ', without the space on that side at
    the start or end of a line).

    Deliberate: the dot leaders (tab fill) of a table of contents or price list are only a drawing that connects positions,
    yet left as they are more than 1,300 dots remained on one page and used over five times as much of the character limit
    (1,696 characters) as the DOCX of the same document (325 characters) (pinned by:
    test_pdf_toc_and_checkboxes.py::TestDotLeaderPageNumbers::test_dot_leaders_are_shortened_to_one_ellipsis). The leader
    test (`_leader_rows`) is done on the original line text, not the shortened text. Only PDF is shortened — DOCX and HWP
    draw leaders as tab fill and have no characters for them.
    """
    def shortened(match: re.Match[str]) -> str:
        before = "" if match.start() == 0 or text[match.start() - 1] == "\n" else " "
        after = "" if match.end() == len(text) or text[match.end()] == "\n" else " "
        return f"{before}…{after}"

    return _LEADER_RUN.sub(shortened, text)


def _picture_rows(images: list[tuple[tuple[float, ...], str]]) -> dict[str, str]:
    """In a row of `_PICTURE_ROW_MIN` or more pictures placed side by side at the same height (top and bottom ends within
    `_TABLE_ROW_TOLERANCE`) without overlapping horizontally: placeholder of a picture to put right after the one on its
    left → placeholder of the picture just before it (on the left).

    In a photo grid of four or more columns (product guide, product line-up) the caption under a photo is wider than half
    the photo width and looked like 'body text of another column' (`_in_other_column`) to the neighbouring photo, so photos
    and captions were mixed up arbitrarily (with 2 or 3 columns each row reads as photos → captions). Only the first picture
    of a row gets a position looked up and the rest follow it (pinned by: test_pdf_images.py::TestPictureGrid). Two pictures
    side by side may be the pictures of two columns and are left as before.
    """
    follows: dict[str, str] = {}
    pictures = sorted(images, key=lambda image: (image[0][0], image[0][1]))
    used: set[int] = set()

    for index, ((top, _, _, bottom), _) in enumerate(pictures):
        if index in used:
            continue
        row = [other for other, ((other_top, _, _, other_bottom), _) in enumerate(pictures)
               if other not in used and abs(other_top - top) <= _TABLE_ROW_TOLERANCE
               and abs(other_bottom - bottom) <= _TABLE_ROW_TOLERANCE]
        row.sort(key=lambda other: pictures[other][0][1])
        boxes = [pictures[other][0] for other in row]
        if len(row) < _PICTURE_ROW_MIN or any(right[1] < left[2] for left, right in zip(boxes, boxes[1:], strict=False)):
            continue
        used.update(row)
        follows.update((pictures[right][1], pictures[left][1]) for left, right in zip(row, row[1:], strict=False))
    return follows


def _place_images_in_cells(tables: list[_Table], images: list[tuple[tuple[float, ...], str]]) -> list[tuple[tuple[float, ...], str]]:
    """Put the placeholder of each picture lying inside one cell box of a ruled table (with `_CELL_IMAGE_SLACK` allowed)
    between the lines of that cell's text at the picture's height (`_with_pictures`) and rebuild the table text. Returns the
    list of remaining pictures. Tables whose row and column ranges could not be determined (tables with empty `bands` or
    `spans`) and borderless tables are left as they are.

    Photos inside cells (photo registers, product lists) piled up after the table with their cells left empty, so there was
    no telling which row a photo belonged to. The other formats put the placeholder inside the cell (pinned by:
    test_pdf_images.py::TestPicturesInTableCells). A picture in a vertically merged cell is put only on the first row of the
    merge.
    """
    left: list[tuple[tuple[float, ...], str]] = []
    placed: dict[tuple[int, int, int, int], list[tuple[float, str]]] = {}  # (table, row, column, cell) → [(picture top, placeholder)]

    for box, placeholder in images:
        top, x0, x1, bottom = box
        found = None
        for table_index, table in enumerate(tables):
            if not (table.bands and table.spans):
                continue
            for cell_index, cell in enumerate(table.cells):
                if not _in_cell((x0, top, x1, bottom), cell):
                    continue
                row = next((index for index, band in enumerate(table.bands) if abs(band[0] - cell[1]) <= 1.0), None)
                column = next((index for index, span in enumerate(table.spans) if abs(span[0] - cell[0]) <= 1.0), None)
                if row is not None and column is not None:
                    found = (table_index, row, column, cell_index)
                    break
            if found:
                break
        if found is None:
            left.append((box, placeholder))
            continue
        placed.setdefault(found, []).append((top, placeholder))

    for (table_index, row, column, cell_index), pictures in placed.items():
        table = tables[table_index]
        table.rows[row][column] = _with_pictures(table.rows[row][column] or "", table.cell_lines.get(cell_index, ()), pictures)

    for table_index in {key[0] for key in placed}:
        tables[table_index].text = _table_text(tables[table_index])
    return left


def _table_text(table: _Table) -> str:
    """The table text rebuilt from the table's cell text (`rows`). For a ruled table continued from the previous page, the top
    row is pulled out as a title only when it is a horizontally merged cell — in the cell text a cell covered by a merge is
    also "", so the covered columns of the top row are found from the cell boxes and passed as None (`merged_titles_only` of
    `render_table`).

    Only an empty cell is passed as None. A cell with text and no box starting at the top edge is the rest of a merged cell open
    at the previous page's end, filled with that cell's text (`_carry_open_cells`); passed as None, the carried text was
    dropped and the first row of a table continued without its header showed an empty first cell (Pinned by:
    test_pdf_table_columns.py::TestMergedCellOpenAtThePageEnd::
    test_rows_continued_without_a_header_get_the_open_cell_whatever_their_values)."""
    if not table.continued:
        return render_table(table.rows)
    rows = [list(row) for row in table.rows]

    if table.bands and table.spans and math.isfinite(table.bands[0][0]):
        top = table.bands[0][0]
        starts = [cell[0] for cell in table.cells if abs(cell[1] - top) <= 1.0]
        rows[0] = [cell if cell or any(abs(start - span[0]) <= 1.0 for start in starts) else None
                   for cell, span in zip(rows[0], table.spans, strict=False)]
    return render_table(rows, merged_titles_only=True)


def _with_pictures(text: str, tops: tuple[float, ...], pictures: list[tuple[float, str]]) -> str:
    """Insert picture placeholders into cell text `text` between the lines at each picture's height (`tops` is the top
    coordinate of each line of the cell text). When the line counts do not match (a cell whose text was split differently)
    they go at the end of the cell text."""
    lines = text.split("\n") if text else []
    if len(lines) != len(tops):
        tops = (-math.inf,) * len(lines)

    for top, placeholder in sorted(pictures, key=lambda picture: picture[0], reverse=True):
        lines.insert(sum(1 for line_top in tops if line_top < top), placeholder)
    return "\n".join(lines)


def _is_side_label(top: float, bottom: float, lines: int, span: tuple[float, float], block: tuple[float, float, float, float],
                   spans: list[tuple[float, float]]) -> bool:
    """Whether a paragraph (top and bottom `top`/`bottom`, line count `lines`, horizontal range `span`) is a label placed to
    the left of a table or picture (`block` = top, bottom, x0, x1): at most `_SIDE_LABEL_MAX_LINES` lines, within the height
    of the table or picture, its right end before the left end of the table or picture, and not body text of another column
    (`_in_other_column`).

    Deliberate: 'AS-IS' and 'TO-BE' placed vertically centred to the left of a table start below the top of the table, were
    pushed after the table and read like the title of the next table (pinned by:
    test_pdf.py::TestBlockPlacement::test_label_beside_a_table_comes_before_it,
    test_wide_paragraph_left_of_a_table_is_not_taken_for_its_label). Text placed on the right goes after the table, in
    reading order.
    """
    block_top, block_bottom, x0, x1 = block
    return (lines <= _SIDE_LABEL_MAX_LINES and block_top - 1 <= top and bottom <= block_bottom + 1 and span[1] <= x0 + 1
            and not _in_other_column(span, x0, x1, spans))


def _in_other_column(span: tuple[float, float], x0: float, x1: float, spans: list[tuple[float, float]]) -> bool:
    """Whether a paragraph (horizontal range `span`) is body text of a different column from the table or picture (x0~x1): it
    does not overlap horizontally and is at least `_COLUMN_MIN_WIDTH_RATIO` times as wide as the table or picture. Narrow
    text placed separately beside a table (a category name, a note marker) is not another column.

    A narrow paragraph is also another column when its left end matches such other-column body text (one of `spans`). A
    short subheading of the left column ('2. 관련 연구') looked like narrow text beside a table, and a table lower in the
    right column was inserted before it (pinned by: test_pdf.py::TestBlockPlacement::
    test_table_in_the_right_column_is_not_inserted_before_a_short_heading_of_the_left_column). The body text of a
    single-column page overlaps the table horizontally, so narrow text beside a table is handled as before.
    """
    def is_body(other: tuple[float, float]) -> bool:
        return not (other[0] < x1 and x0 < other[1]) and other[1] - other[0] >= (x1 - x0) * _COLUMN_MIN_WIDTH_RATIO

    left, right = span
    if left < x1 and x0 < right:
        return False
    return is_body(span) or any(is_body(other) and abs(other[0] - left) <= _TABLE_ROW_TOLERANCE for other in spans)


def _columns_apart(span: tuple[float, float], block: tuple[float, float, float, float],
                   boxes: list[tuple[float, float, float, float]]) -> bool:
    """Whether the columns really divide between a paragraph that `_in_other_column` took for another column (horizontal range
    `span`) and the table or picture (`block` = top, bottom, x0, x1). They divide when any paragraph (`boxes` = top, bottom,
    x0, x1) either (1) overlaps the table or picture horizontally, does not overlap `span`, is at least
    `_COLUMN_MIN_WIDTH_RATIO` times as wide as the table or picture, and has a paragraph of `span`'s column beside it
    (body text of the table's or picture's column, flowing side by side with the other column), or (2) does not overlap the
    table or picture horizontally but overlaps it in height (text beside the table or picture, including slides that put a
    picture beside text). Side by side means overlapping in height by more than `_TABLE_ROW_TOLERANCE`.

    With neither, the table or picture sits alone on its line in a single-column flow and is left in height order. On a
    single-column page the only paragraphs overlapping a centred narrow table or picture are page-width paragraphs that also
    overlap the left-aligned paragraph below it, yet that left-aligned paragraph was taken for another column and the table
    or picture was moved after the paragraph at the top of the page (or to the end of the page), away from its subheading
    (pinned by: test_pdf.py::TestBlockPlacement::
    test_centred_narrow_table_on_a_one_column_page_stays_under_its_heading).

    Deliberate: both cases ask for a real overlap in height and a column of its own. A short label right above a centred
    table reaches about 0.1pt into its top edge with its descent, and a centred title wider than half the table touches
    neither the table's height nor the short left-aligned lines; either one made a one-column page two columns, so the
    tables went to the end of the page away from their labels, or before the short list above them (pinned by:
    test_pdf.py::TestBlockPlacement::test_table_right_under_a_touching_label_stays_under_it,
    test_centred_title_above_a_centred_narrow_table_does_not_make_two_columns).
    """
    top, bottom, x0, x1 = block
    if any(not (left < x1 and x0 < right) and _beside((other_top, other_bottom), (top, bottom))
           for other_top, other_bottom, left, right in boxes):
        return True

    body = [box for box in boxes if box[2] < x1 and x0 < box[3] and not (box[2] < span[1] and span[0] < box[3])
            and box[3] - box[2] >= (x1 - x0) * _COLUMN_MIN_WIDTH_RATIO]
    near = [box for box in boxes if box[2] < span[1] and span[0] < box[3]]
    if not body or not near:
        return False
    if not all(math.isfinite(value) for box in (*body, *near) for value in box):
        return any(_beside(box[:2], other[:2]) and not (box[2] < other[3] and other[2] < box[3]) for other in body for box in near)
    # A paragraph that does not overlap `span` lies wholly on one side of it, and a paragraph of `span`'s column clears it
    # only on that side: before it when it lies to the right of `span`, after it when to the left (mirrored, so that both
    # read 'the near edge is at most the body edge')
    return (_side_by_side([(other_top, other_bottom, left) for other_top, other_bottom, left, _ in body if left >= span[1]],
                          [(near_top, near_bottom, right) for near_top, near_bottom, _, right in near])
            or _side_by_side([(other_top, other_bottom, -right) for other_top, other_bottom, left, right in body
                              if left < span[1]],
                             [(near_top, near_bottom, -left) for near_top, near_bottom, left, _ in near]))


def _beside(first: tuple[float, float], second: tuple[float, float]) -> bool:
    """Whether two vertical ranges (top, bottom) overlap by more than `_TABLE_ROW_TOLERANCE`."""
    return min(first[1], second[1]) - max(first[0], second[0]) > _TABLE_ROW_TOLERANCE


def _side_by_side(bodies: list[tuple[float, float, float]], nears: list[tuple[float, float, float]]) -> bool:
    """Whether some range of `bodies` (top, bottom, edge) is `_beside` some range of `nears` (top, bottom, edge) whose edge
    is at most its own. All values are finite.

    The bodies are taken by edge, the nears admitted as their edge is reached into a tree of the largest bottom by order
    of top, and each body asks it about the nears whose top lies far enough above its bottom: n log n for each table or
    picture. Pairing every body with every near took the square of the paragraphs (Pinned by: test_pdf.py::
    TestBlockPlacement::test_many_blocks_over_two_columns_of_many_paragraphs_take_linear_time). `_beside` holds exactly
    when every bottom is more than the tolerance below every top of the pair, and each of those is monotone in one value.
    """
    nears = [near for near in nears if near[1] - near[0] > _TABLE_ROW_TOLERANCE]
    if not nears:
        return False
    by_top = sorted(range(len(nears)), key=lambda index: nears[index][0])
    place = {index: rank for rank, index in enumerate(by_top, start=1)}
    tops = [nears[index][0] for index in by_top]
    deepest = [-math.inf] * (len(nears) + 1)  # Fenwick tree: the largest bottom among the admitted nears up to each rank
    admitted = sorted(range(len(nears)), key=lambda index: nears[index][2])
    taken = 0

    for body_top, body_bottom, edge in sorted(bodies, key=lambda body: body[2]):
        while taken < len(admitted) and nears[admitted[taken]][2] <= edge:
            rank, lowest = place[admitted[taken]], nears[admitted[taken]][1]
            while rank < len(deepest):
                deepest[rank] = max(deepest[rank], lowest)
                rank += rank & -rank
            taken += 1
        if body_bottom - body_top <= _TABLE_ROW_TOLERANCE:
            continue
        rank, reach = bisect_left(tops, True, key=lambda near_top: not body_bottom - near_top > _TABLE_ROW_TOLERANCE), -math.inf
        while rank:
            reach = max(reach, deepest[rank])
            rank -= rank & -rank
        if reach - body_top > _TABLE_ROW_TOLERANCE:
            return True
    return False


def _after_other_column(items: list[tuple[float, str, bool]], spans: list[tuple[float, float]], position: int,
                        block: tuple[float, float, float, float], boxes: list[tuple[float, float, float, float]]) -> bool:
    """Whether the paragraph right before the position chosen by height (`position`, the end of the page or before narrow text
    such as a page number) is body text of another column, so the table or picture (`block` = top, bottom, x0, x1) has to
    be moved into the flow of its own column. On a two-column page a table at the very bottom of the left column was
    attached after the body text of the right column (the end of the page) (pinned by:
    test_pdf.py::TestBlockPlacement::test_table_at_the_bottom_of_the_left_column_stays_in_the_left_column and 1 more).

    It is moved only when that paragraph comes down beside the table or picture or starts before the paragraph above in the
    table's or picture's column (a caption row above two side-by-side tables starting at the same height is handled as
    before), and there is a height above the table or picture where two columns flow side by side (text to the right of a
    picture has no text of the picture's column at that height and is read after the picture as before —
    test_pdf_pictures_beside_text.py::TestTextBesideAPicture).
    """
    top, _, x0, x1 = block
    previous = next((index for index in range(position - 1, -1, -1) if not items[index][2]), None)
    if previous is None:
        return False
    box = boxes[sum(1 for _, _, is_block in items[:previous] if not is_block)]
    if not _in_other_column(box[2:], x0, x1, spans):
        return False
    column = [(other_top, bottom) for other_top, bottom, left, right in boxes if other_top < top and left < x1 and x0 < right]
    if not column or (box[1] <= top and box[0] >= max(column)[0] - _TABLE_ROW_TOLERANCE):
        return False

    beside = sorted((other_top, bottom) for other_top, bottom, left, right in boxes
                    if not (left < x1 and x0 < right) and _in_other_column((left, right), x0, x1, spans))
    tops, lowest = [other_top for other_top, _ in beside], list(accumulate((bottom for _, bottom in beside), max))
    return any((reach := bisect_left(tops, bottom)) and lowest[reach - 1] > other_top for other_top, bottom in column)


def _position_in_column(items: list[tuple[float, str, bool]], spans: list[tuple[float, float]],
                        block_top: float, x0: float, x1: float) -> int:
    """When the position chosen by height is a paragraph of another column, the position that puts the table or picture in the
    flow of its own column (the paragraphs overlapping it horizontally).

    On a two-column page a table of the right column cut in before a paragraph of the left column that was 'the first
    paragraph starting below the table'. It goes after the last paragraph above it in the same column (after any table or
    picture already placed there), or, when there is no such paragraph, before the first paragraph below it in the same
    column (pinned by: test_pdf.py::TestBlockPlacement).
    """
    column = [index for index, (_, _, is_block) in enumerate(items)
              if not is_block and spans[index][0] < x1 and x0 < spans[index][1]]
    above = [index for index in column if items[index][0] < block_top]
    if not above:
        # before the topmost paragraph, not the first in reading order (a page where a slide's picture caption was read
        # after the row of cards)
        return min(column, key=lambda index: items[index][0]) if column else len(items)

    # after the tables and pictures already placed in the same column. A table of another column (the lower rows of a table
    # that broke over from the previous column) is not skipped
    position = above[-1] + 1
    while position < len(items) and items[position][2] and spans[position][0] < x1 and x0 < spans[position][1]:
        position += 1
    return position


def _group_paragraphs(lines: list[_Line], line_gap: float) -> list[_Paragraph]:
    paragraphs: list[_Paragraph] = []
    for line in lines:
        if paragraphs and _continues(paragraphs[-1].lines[-1], line, line_gap):
            paragraphs[-1].lines.append(line)
        else:
            paragraphs.append(_Paragraph([line]))
    return paragraphs


def _continues(previous: _Line, line: _Line, line_gap: float) -> bool:
    """Whether `line` is in the same paragraph as `previous`. `line_gap` is the ratio of the usual line gap to the line height."""
    if line.footnote_start or line.footnote != previous.footnote:
        # Each linked note is a paragraph of its own: a footnote definition joined to the body line above it, or to the note
        # before it, would not start a definition in Markdown. The wrapped lines of a note stay with it whatever their spacing —
        # a paragraph of their own would fall out of the definition (Pinned by:
        # test_pdf_footnotes.py::TestLinked::test_each_note_is_a_paragraph_of_its_own)
        return False
    if line.footnote:
        return True
    if _LONE_BULLET.match(previous.text):
        # A line holding only a bullet character is one line with the next line at the same height.
        return abs((previous.top + previous.bottom) / 2 - (line.top + line.bottom) / 2) <= previous.height
    if previous.size and line.size and abs(previous.size - line.size) > 0.15 * max(previous.size, line.size):
        return False
    if _weight_breaks(previous, line):
        return False
    gap = line.top - previous.bottom
    allowed = previous.height * (line_gap if line_gap else 0.5) * 1.5 + 1.0
    horizontally_overlaps = line.x0 < previous.x1 and previous.x0 < line.x1
    return -previous.height * 0.3 <= gap <= allowed and horizontally_overlaps


def _weight_breaks(previous: _Line, line: _Line) -> bool:
    """Whether a change of weight between two lines starts a new paragraph: a bold line that stops clearly short of the
    regular line below it (by more than twice its height) is a title over that text, and a bold line after a regular line
    that ends a sentence starts something new.

    Deliberate: a bold title set at the body size and spacing ('5. The dynamics', 'Procedure:') was joined to the text below
    it, so it could not be a heading. A bold sentence wrapped onto a full line, and a bold word inside a sentence, keep
    their paragraph (Pinned by: test_pdf_headings.py::TestBoldHeadings)."""
    if previous.bold and not line.bold:
        return previous.x1 < line.x1 - 2 * previous.height
    if line.bold and not previous.bold:
        return previous.text.rstrip().endswith((".", "!", "?", ":", "。"))
    return False


# A line start Markdown reads as the mark of a block: `#` … `######` and a space (a heading) or `>` (a quote)
_BLOCK_MARK_START = re.compile(r"^(?:#{1,6}(?=\s|$)|>)", re.MULTILINE)


def _escaped_block_marks(text: str) -> str:
    """Body text whose lines start with a heading or quote mark, escaped (`\\#`, `\\>`). The text of a page is text, never
    structure: a form row '# DNA replications' (a count column) became a Markdown heading (Pinned by:
    test_pdf_headings.py::TestBodyTextMarks)."""
    return _BLOCK_MARK_START.sub(lambda match: "\\" + match.group(0), text)


def _paragraph_text(paragraph: _Paragraph) -> str:
    parts: list[str] = []
    for line in paragraph.lines:
        if parts and _LONE_BULLET.match(parts[-1]):
            parts[-1] = f"{parts[-1]} {line.text}"
        else:
            parts.append(line.text)
    return "\n".join(parts)


def _heading_size(paragraph: _Paragraph) -> float:
    return round(max(line.size for line in paragraph.lines) * 2) / 2


# A heading level key: (heading size in 0.5pt steps, whether it is a bold heading at about the body size).
_HeadingKey = tuple[float, bool]


def _heading_shaped(paragraph: _Paragraph) -> bool:
    """A paragraph that is short (two lines, 100 characters at most; a line holding only the section number, '3.' or 'III.',
    is not counted), mostly letters and holding one (a large '56%' or '20%↑' of an infographic is a value), does not start
    with a bullet, and is not a form row with check boxes ('☐ Yes ☑') or a table-of-contents row with a dot leader
    ('2.4. Results … 7')."""
    text = " ".join(line.text for line in paragraph.lines)
    if _CHECKBOX_MARK.search(text) or _LEADER_RUN.search(text) or not _HEADING_LETTER.search(text):
        return False
    counted = [line for line in paragraph.lines if not _LONE_SECTION_NUMBER.match(line.text.strip())]
    if not counted or not max(line.size for line in paragraph.lines) or len(counted) > 2 or len(text) > 100 or _BULLET_START.match(text):
        return False
    word_chars = len(_WORD_CHAR.findall(text))
    return word_chars >= 2 and word_chars >= len(text.replace(" ", "")) * 0.5


def _is_heading(paragraph: _Paragraph, body_size: float, side_by_side: frozenset[int] = frozenset()) -> bool:
    """A heading-shaped paragraph (`_heading_shaped`) at least `_HEADING_SIZE_RATIO` times the body size, not a header cell
    of a table (`_header_cells`)."""
    return (_heading_shaped(paragraph) and bool(body_size) and not any(id(line) in side_by_side for line in paragraph.lines)
            and max(line.size for line in paragraph.lines) / body_size >= _HEADING_SIZE_RATIO)


def _is_bold_heading(paragraph: _Paragraph, body_size: float, side_by_side: frozenset[int] = frozenset()) -> bool:
    """A heading set in a bold face at about the body size: every line is bold, the size is at least
    `_BOLD_HEADING_MIN_RATIO` of the body, it holds a letter, is not a caption or note label ('Figure 3.', 'Source:',
    `_CAPTION_START`), and does not end like a sentence (',' or ';'; '.' only after a section number such as 'CHAPTER 1.'
    or '5.'). A heading may end in ':' ('Procedure:') or '?'. A bold line with another bold line beside it on the same row
    (`side_by_side`, `_header_cells`) is a header cell of a table the page did not find, not a heading.

    Deliberate: a heading in many books and papers differs from the body only by its weight ('5. The dynamics' in 11pt
    bold over a 10pt body), and with the size rule alone 53 of 193 headings of a 200-page pdf corpus were body text (Pinned
    by: test_pdf_headings.py::TestBoldHeadings)."""
    if not body_size or not _heading_shaped(paragraph) or not all(line.bold for line in paragraph.lines):
        return False
    if any(id(line) in side_by_side for line in paragraph.lines):
        return False
    text = " ".join(line.text for line in paragraph.lines).strip()
    if max(line.size for line in paragraph.lines) < body_size * _BOLD_HEADING_MIN_RATIO or _CAPTION_START.match(text):
        return False
    if not _HEADING_LETTER.search(text) or text.endswith((",", ";")):
        return False
    return not text.endswith(".") or bool(_SECTION_NUMBER_END.search(text))


# A letter of a word (not a number or a symbol): a bold '2024' or '12.5%' alone is a value, not a heading.
_HEADING_LETTER = re.compile(r"[A-Za-z\u00c0-\u024f가-힣一-鿿]")
# Heading-like lines side by side on one row that make it the header row of a table (`_header_cells`): three panels of a slide
# have a heading each, a table header has more cells
_HEADER_ROW_CELLS = 4
# A check box drawn or typed on the line (`_mark_checkboxes`).
_CHECKBOX_MARK = re.compile(r"[☐☑☒]")
# A line holding only a section number ('3.', '12', 'III.'), set above the title of a numbered heading.
_LONE_SECTION_NUMBER = re.compile(r"^(?:\d{1,3}(?:\.\d{1,3})*|[IVXLC]{1,6})\.?$")
# A heading that ends in a section number and a full stop ('CHAPTER 1.', '5.', 'Part IV.').
_SECTION_NUMBER_END = re.compile(r"(?:^|\s)(?:\d+(?:\.\d+)*|[IVXLC]+)\.$")


def _header_cells(lines: list[_Line], body_size: float) -> frozenset[int]:
    """ids of the heading-like lines (short, and bold or at least `_HEADING_SIZE_RATIO` times the body) that stand on one
    row (overlapping by half the lower height vertically, apart horizontally) with other heading-like lines, `_HEADER_ROW_CELLS`
    or more of them. They are the header cells of a table the page did not find
    ('Service Stage', 'Function Name', 'Explanation', 'Expected Benefit'), not headings. The headings of three panels of a
    slide side by side ('Our Purpose', 'Our Mission', 'What We Do') and two bold headings of two columns of a page on one row
    stay headings.

    Deliberate: with the size rule alone, the 7pt bold header row of a table over a 5pt body came out as four headings
    (Pinned by: test_pdf_headings.py::TestHeaderRows)."""
    if not body_size:
        return frozenset()

    def heading_like(line: _Line) -> bool:
        return (len(line.text) <= 100 and bool(_HEADING_LETTER.search(line.text))
                and (line.bold or line.size >= body_size * _HEADING_SIZE_RATIO))

    # The lines are grouped into rows once (sorted by their middle, a line joins the row whose first line it overlaps by half
    # the lower height) and each row is looked at once: comparing every line of a crowded row with the others took minutes on
    # a page of a thousand letters set one by one on a row
    rows: list[list[_Line]] = []
    for line in sorted(lines, key=lambda line: (line.top + line.bottom) / 2):
        first = rows[-1][0] if rows else None
        if first is not None and min(first.bottom, line.bottom) - max(first.top, line.top) >= min(first.height, line.height) * 0.5:
            rows[-1].append(line)
        else:
            rows.append([line])
    found: set[int] = set()
    for row in rows:
        alike = [line for line in row if heading_like(line)]
        if len(alike) < _HEADER_ROW_CELLS:
            continue
        apart, end = [], -math.inf  # heading-like lines side by side, left to right, not overlapping each other
        for line in sorted(alike, key=lambda line: line.x0):
            if line.x0 >= end:
                apart.append(line)
                end = line.x1
        if len(apart) >= _HEADER_ROW_CELLS:
            found.update(id(line) for line in alike)
    return frozenset(found)


def _bold_body(pages: list[_Page]) -> bool:
    """Whether the document sets its body in a bold face: at least `_BOLD_BODY_MAX_RATIO` of the characters of its lines are
    in bold lines. A bold line is then no sign of a heading."""
    total = bold = 0
    for page in pages:
        if page.simple:
            continue
        for line in page.lines:
            total += len(line.text)
            bold += len(line.text) if line.bold else 0
    return bool(total) and bold >= total * _BOLD_BODY_MAX_RATIO


def _heading_key(paragraph: _Paragraph, heading_levels: dict[_HeadingKey, int], body_size: float,
                 side_by_side: frozenset[int] = frozenset()) -> _HeadingKey | None:
    """The level key of a paragraph that is a heading: its size when that is a heading size of the document, otherwise
    (size, True) when the document has bold headings at about the body size and the paragraph is one. None for body text."""
    if not _heading_shaped(paragraph) or any(id(line) in side_by_side for line in paragraph.lines):
        return None
    size = _heading_size(paragraph)
    if (size, False) in heading_levels:
        return (size, False)
    if (size, True) in heading_levels and _is_bold_heading(paragraph, body_size, side_by_side):
        return (size, True)
    return None


def _heading_levels(pages: list[_Page], body_size: float, line_gap: float) -> dict[_HeadingKey, int]:
    """Heading key (`_heading_key`) → level. The heading sizes in the document are mapped, largest first, to #, ## and ###
    (### from the fourth size onward); at the same size a bold heading at about the body size comes after a larger one, so
    those come last."""
    bold_allowed = not _bold_body(pages)
    keys: set[_HeadingKey] = set()
    for page in pages:
        if page.simple:
            continue
        side_by_side = _header_cells(page.lines, body_size)
        for paragraph in _group_paragraphs(page.lines, line_gap):
            if _is_heading(paragraph, body_size, side_by_side):
                keys.add((_heading_size(paragraph), False))
            elif bold_allowed and _is_bold_heading(paragraph, body_size, side_by_side):
                keys.add((_heading_size(paragraph), True))
    ranked = sorted(keys, key=lambda key: (-key[0], key[1]))
    return {key: min(rank, 3) for rank, key in enumerate(ranked, start=1)}
