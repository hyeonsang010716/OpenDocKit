"""From the lines and tables of a page to its Markdown: paragraphs, headings by font size, pictures placed in cells, and
the columns a paragraph belongs to.
"""

from bisect import bisect_left
from collections.abc import Callable
from itertools import accumulate, pairwise
import math
import re

from opendockit.extractors._hangul import _CONTACT, _KOREAN_DATE, _WEEKDAY, korean_sentence_end
from opendockit.extractors._pdf_model import (
    _BOLD_BODY_MAX_RATIO,
    _BOLD_HEADING_MIN_RATIO,
    _BULLET_START,
    _CAPTION_START,
    _HEADING_MIN_RATIO,
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
    paragraphs = _group_paragraphs(page.lines, line_gap)
    side_by_side = _side_by_side_lines(page.lines, paragraphs, body_size) if heading_levels else frozenset()
    previous = None
    for paragraph in paragraphs:
        key = _heading_key(paragraph, heading_levels, body_size, side_by_side, previous)
        previous = paragraph
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
    it, so it could not be a heading. A bold sentence wrapped onto a full line, a bold word inside a sentence, and a bold
    sentence closing a paragraph keep their paragraph (Pinned by: test_pdf_headings.py::TestBoldHeadings,
    TestBoldSentenceEnds::test_a_bold_sentence_closing_a_paragraph_keeps_it)."""
    if previous.bold and not line.bold:
        return previous.x1 < line.x1 - 2 * previous.height
    if line.bold and not previous.bold:
        # A bold line that is itself a sentence ('Do not discard this notice!') closes the paragraph it is set in
        return previous.text.rstrip().endswith((".", "!", "?", ":", "。")) and not _ends_like_a_sentence(line.text.strip())
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
    is not counted), mostly letters and holding one (a large '56%' or '20%↑' of an infographic is a value) or a year alone
    ('2023', '2023–2024', the headings of a timeline; a bold year at the body size is still a value, `_is_bold_heading`),
    does not start with a bullet, and is not a form row with check boxes ('☐ Yes ☑') or a table-of-contents row with a dot
    leader ('2.4. Results … 7').

    Deliberate: asked for a letter, a year set large over its part of a timeline was no heading any more (Pinned by:
    test_pdf_headings.py::TestHeadingSizes::test_a_large_year_is_a_heading)."""
    text = " ".join(line.text for line in paragraph.lines)
    if _CHECKBOX_MARK.search(text) or _LEADER_RUN.search(text):
        return False
    if not _HEADING_LETTER.search(text) and not _YEAR.fullmatch(text.strip()):
        return False
    counted = [line for line in paragraph.lines if not _LONE_SECTION_NUMBER.match(line.text.strip())]
    if not counted or not max(line.size for line in paragraph.lines) or len(counted) > 2 or len(text) > 100 or _BULLET_START.match(text):
        return False
    word_chars = len(_WORD_CHAR.findall(text))
    return word_chars >= 2 and word_chars >= len(text.replace(" ", "")) * 0.5


def _is_heading(paragraph: _Paragraph, body_size: float, side_by_side: frozenset[int] = frozenset()) -> bool:
    """A heading-shaped paragraph (`_heading_shaped`) at least `_HEADING_SIZE_RATIO` times the body size, not a header cell
    of a table (`_header_cells`) and not a sentence set a little larger (`_slightly_larger_text`)."""
    return (_heading_shaped(paragraph) and bool(body_size) and not any(id(line) in side_by_side for line in paragraph.lines)
            and max(line.size for line in paragraph.lines) / body_size >= _HEADING_SIZE_RATIO
            and not _slightly_larger_text(paragraph, body_size))


def _slightly_larger_text(paragraph: _Paragraph, body_size: float) -> bool:
    """Whether a paragraph under `_HEADING_MIN_RATIO` times the body size (the 1.15–1.2 band a heading by size may also be
    in) ends like a sentence (`_ends_like_a_sentence`) or is longer than `_SLIGHTLY_LARGER_MAX_CHARS`: a pull quote or a
    paragraph set a little larger than the body, not a heading. From 1.2 times the body on, the size alone makes a heading,
    as it did before the band.

    Deliberate: the band was looked at by size only, so an 11.5pt pull quote over a 10pt body and 10.5pt paragraphs over a
    9pt body became headings ('## Revenue grew in every region this year.') (Pinned by:
    test_pdf_headings.py::TestHeadingSizes::test_a_sentence_a_little_larger_than_the_body_is_body_text)."""
    if not body_size or max(line.size for line in paragraph.lines) / body_size >= _HEADING_MIN_RATIO:
        return False
    text = " ".join(line.text for line in paragraph.lines).strip()
    return len(text) > _SLIGHTLY_LARGER_MAX_CHARS or _ends_like_a_sentence(text)


def _is_bold_heading(paragraph: _Paragraph, body_size: float, side_by_side: frozenset[int] = frozenset(),
                     previous: _Paragraph | None = None) -> bool:
    """A heading set in a bold face at about the body size: every line is bold, the size is at least
    `_BOLD_HEADING_MIN_RATIO` of the body, it holds a letter, is not a caption or note label ('Figure 3.', 'Source:',
    `_CAPTION_START`), and does not end like a sentence (',' or ';'; '.' only when the whole text is a numbered label,
    `_NUMBERED_LABEL`, such as 'CHAPTER 1.' or 'Part IV.'; `_ends_like_a_sentence`). A short heading may end in ':'
    ('Procedure:'), '?' or '!'. A bold line with another bold line beside it on the same row (`side_by_side`,
    `_header_cells`) is a header cell of a table the page did not find, not a heading. Nor is a value line (an amount, a
    date, an address or a contact, `_is_value_text`) or the name under the closing of a letter (the paragraph before it,
    `previous`, ends in ',': 'Sincerely,').

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
    if previous is not None and previous.lines[-1].text.rstrip().endswith(","):
        return False
    return bool(_HEADING_LETTER.search(text)) and not _ends_like_a_sentence(text) and not _is_value_text(text)


def _is_value_text(text: str) -> bool:
    """Whether a short bold text is a value set in bold, not a title: it holds an amount (a currency sign or code, '원', or
    digits grouped in thousands, `_AMOUNT`), is a date or a period ('March 3, 2024', 'Q3 2024', '2024-03-01', '2024년 3월
    5일', `_DATE_LINE`, `_hangul._KOREAN_DATE`), is an address or a list holding a number between two commas or more
    ('Acme Corp, PO Box 12, Springfield'), or holds a web or e-mail address or a phone number (`_hangul._CONTACT`).

    Deliberate: a bold total, date line or address at the body size became a heading ('# Total due USD 1,234.00', '# 합계
    금액 1,000,000원', '# March 3, 2024', '# Q3 2024') (Pinned by: test_pdf_headings.py::TestBoldValues)."""
    if _AMOUNT.search(text) or _DATE_LINE.fullmatch(text) or _KOREAN_DATE.fullmatch(_WEEKDAY.sub("", text)):
        return True
    return (text.count(",") >= 2 and any(char.isdigit() for char in text)) or bool(_CONTACT.search(text))


def _ends_like_a_sentence(text: str) -> bool:
    """Whether a short bold text is a sentence rather than a title: it ends in ',' or ';'; in '.' unless the whole text is
    a numbered label (`_NUMBERED_LABEL`); in '!', '?' or ':' after `_SENTENCE_MIN_WORDS` words or more; it holds a '!' or
    '?' with more text after it ('Questions? Call 1-800-555-0100'); or it ends like a Korean sentence without its full stop
    (`_hangul.korean_sentence_end`, the rule the Hangul readers tell sentences from titles by: `…합니다`, `…할 수 없다`).

    Deliberate: with only ',', ';' and '.' looked at, bold notices and lead-ins at the body size became headings ('# Do not
    discard this notice!', '# The following documents must be attached:', '# 을은 갑의 사전 동의 없이 업무를 위탁할 수
    없다'), where 'Procedure:', 'Why entropy?' and '제1조(목적)' are headings (Pinned by:
    test_pdf_headings.py::TestBoldSentences, TestBoldSentenceEnds)."""
    if text.endswith((",", ";")):
        return True
    if text.endswith("."):
        return not _NUMBERED_LABEL.fullmatch(text)
    if _INNER_SENTENCE_END.search(text):
        return True
    if text.endswith(("!", "?", ":")) and len(text.split()) >= _SENTENCE_MIN_WORDS:
        return True
    return korean_sentence_end(text) or _korean_notice_end(text)


def _korean_notice_end(text: str) -> bool:
    """Whether a Korean line ends like the clause of a notice or an instruction, which `_hangul.korean_sentence_end` (the
    rule the Hangul readers keep for their titles) does not look at: a verb made a noun after a stem or tense (`…같음`,
    `…있음`, `…하였음`), a verbal noun closed by 함, 됨 or 임 after two words or more (`기한 내 제출함`, `본 계약은
    무효임`; not `책임`, `모임` or a single `포함`), `…할 것`, `… 바람` after two words or more, the polite request
    `…세요` and `…하십시오` / `…시오`, the plain command `…하라`, and `…는다` after two words or more (`결과를 얻는다`).

    Deliberate: in a PDF a bold notice at the body size ('# 서류는 기한 내 제출함', '# 확인해 주세요') became a heading,
    where '개요', '필요', '다음', '모임' and '향후 과제' are titles (Pinned by:
    test_pdf_headings.py::TestBoldSentenceEnds::test_a_korean_notice_is_no_heading)."""
    words = text.rstrip(_KOREAN_CLOSING).split()
    if not words:
        return False
    last = words[-1]
    if _KOREAN_VERB_END.search(last):
        return True
    if len(words) < 2:
        return False
    if last == "것" and "가" <= words[-2][-1] <= "힣" and (ord(words[-2][-1]) - 0xAC00) % 28 == _FINAL_L:
        return True
    return bool(_KOREAN_NOMINAL_END.fullmatch(last)) or last.endswith(("바람", "는다"))


# The longest heading by a size 1.15 to 1.2 times the body (`_slightly_larger_text`)
_SLIGHTLY_LARGER_MAX_CHARS = 60
# A bold text ending in '!', '?' or ':' with this many words or more is a sentence ('Do not discard this notice!', 'The
# following documents must be attached:'), not a title ('Procedure:', 'Why entropy?')
_SENTENCE_MIN_WORDS = 5
# Closing marks after the end of a Korean clause (`_korean_notice_end`)
_KOREAN_CLOSING = ".!?\"'”’)） "
# Endings of a Korean notice or instruction in the last word, whatever the words before it: a verb made a noun after a stem
# or tense (`같음`, `있음`, `제출하였음`), the polite request (`주세요`, `하십시오`, `기입하시오`) and the plain command
# (`확인하라`)
_KOREAN_VERB_END = re.compile(r"(?:[하되있없같았었였했됐겠않]음|세요|시오|[하되]라)\Z")
# A verbal noun closed by 함, 됨 or 임 (`제출함`, `신청함`, `완료됨`, `무효임`): three syllables or more, not `책임` or `모임`
_KOREAN_NOMINAL_END = re.compile(r"[가-힣]{2,}[함됨임]")
# The final consonant ㄹ (종성 index) of `할` in `…할 것`
_FINAL_L = 8
# A '!' or '?' after a word with more text after it: two sentences on the line ('Questions? Call 1-800-555-0100')
_INNER_SENTENCE_END = re.compile(r"[^\W\d_][!?]\s+\S")
# An amount: a currency sign or code by a number ('$110.00', 'USD 1,234.00', '1,000원', '30 EUR') or digits grouped in thousands
# ('1,000,000')
_AMOUNT = re.compile(r"[$€£¥₩]\s?\d|\b(?:USD|EUR|GBP|JPY|KRW|CNY|CHF)\s?\d|\d\s?(?:USD|EUR|GBP|JPY|KRW|CNY|CHF)\b"
                     r"|\d\s?(?:원|달러|엔|위안)(?![가-힣])|\d{1,3}(?:,\d{3})+")
# A date or a period alone on the line ('March 3, 2024', '3 March 2024', 'March 2024', 'Q3 2024', 'FY2024', '2024-03-01',
# '03/01/2024')
_MONTH = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?"
          r"|Nov(?:ember)?|Dec(?:ember)?)\.?")
_DATE_LINE = re.compile(rf"(?:[A-Z][a-z]{{2,8}},?\s+)?(?:{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?\s+\d{{4}}"
                        rf"|\d{{1,2}}\.?\s+{_MONTH},?\s+\d{{4}}|{_MONTH},?\s+\d{{4}})"
                        r"|(?:Q[1-4]|H[12])\s*(?:FY\s*)?'?\d{2,4}|FY\s*'?\d{2,4}"
                        r"|\d{4}\s*[-./]\s*\d{1,2}\s*[-./]\s*\d{1,2}\.?|\d{1,2}\s*[-./]\s*\d{1,2}\s*[-./]\s*\d{2,4}")
# A letter of a word (not a number or a symbol): a bold '2024' or '12.5%' alone is a value, not a heading.
_HEADING_LETTER = re.compile(r"[A-Za-z\u00c0-\u024f가-힣一-鿿]")
# The rows of cells under heading-like lines side by side that make them the header row of a table (`_header_cells`): the
# panels of a slide have a line or two of text under their headings
_HEADER_DATA_ROWS = 2
# A line of this many words or more under a header column is a line of prose, not a cell (`_cell_rows_under`)
_PROSE_LINE_MIN_WORDS = 3
# The most words of a value beside a form label on its row ('Kim', 'Acme Corporation', '010-1234-5678') (`_stacked_labels`)
_LABEL_VALUE_MAX_WORDS = 4
# The most words of a form label stacked over another ('Name', 'Date of birth') (`_stacked_labels`)
_LABEL_MAX_WORDS = 3
# A year or a span of years alone ('2023', '1990–1999', '2023-24'): a heading of a timeline by its size (`_heading_shaped`)
_YEAR = re.compile(r"(?:1[5-9]|20)\d{2}(?:\s*[-–—~]\s*(?:(?:1[5-9]|20)\d{2}|\d{2}))?")
# A check box drawn or typed on the line (`_mark_checkboxes`).
_CHECKBOX_MARK = re.compile(r"[☐☑☒]")
# A line holding only a section number ('3.', '12', 'III.'), set above the title of a numbered heading.
_LONE_SECTION_NUMBER = re.compile(r"^(?:\d{1,3}(?:\.\d{1,3})*|[IVXLC]{1,6})\.?$")
# A bold heading that ends in a full stop: the whole text is a numbered label ('CHAPTER 1.', 'Part IV.', 'Section 2.3.',
# 'Appendix C.', '제 3 장.'). A sentence that only ends in a number or a capital ('… received by June 30.', '… given in
# Appendix C.') is no heading (Pinned by: test_pdf_headings.py::TestBoldSentences::test_a_sentence_ending_in_a_number_is_no_heading).
_NUMBERED_LABEL = re.compile(r"(?:(?i:chapter|part|section|article|appendix|annex|book|unit|lesson|lecture|step|stage|phase)\s+"
                             r"(?:\d{1,3}(?:\.\d{1,3})*|[IVXLC]{1,6}|[A-Z])|\d{1,3}(?:\.\d{1,3})*|[IVXLC]{1,6}"
                             r"|제\s*\d{1,3}\s*[장절조편부관항])\.")


def _rows(lines: list[_Line]) -> list[list[_Line]]:
    """The lines grouped into rows: sorted by their middle, a line joins the row whose first line it overlaps by half the
    lower height. The rows are made once and each is looked at once: comparing every line of a crowded row with the others
    took minutes on a page of a thousand letters set one by one on a row."""
    rows: list[list[_Line]] = []
    for line in sorted(lines, key=lambda line: (line.top + line.bottom) / 2):
        first = rows[-1][0] if rows else None
        if first is not None and min(first.bottom, line.bottom) - max(first.top, line.top) >= min(first.height, line.height) * 0.5:
            rows[-1].append(line)
        else:
            rows.append([line])
    return rows


def _side_by_side_lines(lines: list[_Line], paragraphs: list[_Paragraph], body_size: float) -> frozenset[int]:
    """ids of the lines that are no heading for what stands beside them on their row: the header cells of a table
    (`_header_cells`) and the lines of stacked labels (`_stacked_labels`)."""
    return _header_cells(lines, body_size) | _stacked_labels(lines, paragraphs, body_size)


def _stacked_labels(lines: list[_Line], paragraphs: list[_Paragraph], body_size: float) -> frozenset[int]:
    """ids of the lines of a paragraph of two lines or more where every line is a label with a value on its right on its
    row: the labels of a form stacked in one column ('Name' over 'Phone', each with its value beside it), not a heading
    wrapped over two lines. A label is a bold line under `_HEADING_SIZE_RATIO` times the body size (a heading by its size
    is no form label) of `_LABEL_MAX_WORDS` words at most; its value is the next line on its right, not bold, not larger
    than the label, of `_LABEL_VALUE_MAX_WORDS` words at most.

    Deliberate: two bold labels at the usual line gap were joined into one heading, and their line break was lost ('# Name
    Phone'). Taken for labels whatever their size and whatever stood on their right, a heading wrapped over two lines beside
    a short line was lost: a cover title beside 'Draft' / 'May 2024', the left one of two headings of two columns on the
    same rows, a heading beside a list of facts or beside 'Page 3' / 'Draft' in the margin (Pinned by:
    test_pdf_headings.py::TestStackedLabels)."""
    valued: set[int] = set()
    for row in _rows(lines):
        ordered = sorted(row, key=lambda line: line.x0)
        for line, after in pairwise(ordered):
            if (line.bold and not after.bold and after.x0 >= line.x1 and line.size < body_size * _HEADING_SIZE_RATIO
                    and after.size <= line.size * 1.05 and len(line.text.split()) <= _LABEL_MAX_WORDS
                    and len(after.text.split()) <= _LABEL_VALUE_MAX_WORDS):
                valued.add(id(line))
    return frozenset(id(line) for paragraph in paragraphs if len(paragraph.lines) >= 2
                     and all(id(line) in valued for line in paragraph.lines) for line in paragraph.lines)


def _header_cells(lines: list[_Line], body_size: float) -> frozenset[int]:
    """ids of the heading-like lines (short, and bold or at least `_HEADING_SIZE_RATIO` times the body) that stand on one
    row (overlapping by half the lower height vertically, apart horizontally) with other heading-like lines and have rows
    of cells under them (`_cell_rows_under`): the header cells of a table the page did not find ('Item | Qty | Price',
    'Service Stage | Function | Explanation | Benefit'), not headings. Headings side by side with prose under them, or one
    line each (the panels of a slide: 'Plan', 'Build', 'Test', 'Ship', each over a line of text), and the headings of two
    columns of a page on one row stay headings, however many stand on the row.

    Deliberate: with the size rule alone, the 7pt bold header row of a table over a 5pt body came out as four headings, and
    the bold header of a table of two or three columns came out as a heading ('# Description'); counting four heading-like
    lines on a row instead dropped the four panel headings of a slide (Pinned by: test_pdf_headings.py::TestHeaderRows)."""
    if not body_size:
        return frozenset()

    def heading_like(line: _Line) -> bool:
        return (len(line.text) <= 100 and bool(_HEADING_LETTER.search(line.text))
                and (line.bold or line.size >= body_size * _HEADING_SIZE_RATIO))

    found: set[int] = set()
    rows = _rows(lines)
    for index, row in enumerate(rows):
        alike = [line for line in row if heading_like(line)]
        if len(alike) < 2:
            continue
        apart, end = [], -math.inf  # heading-like lines side by side, left to right, not overlapping each other
        for line in sorted(alike, key=lambda line: line.x0):
            if line.x0 >= end:
                apart.append(line)
                end = line.x1
        if len(apart) >= 2 and _cell_rows_under(apart, rows[index + 1:index + 1 + _HEADER_DATA_ROWS], heading_like):
            found.update(id(line) for line in alike)
    return frozenset(found)


def _cell_rows_under(header: list[_Line], below: list[list[_Line]], heading_like: Callable[[_Line], bool]) -> bool:
    """Whether the `_HEADER_DATA_ROWS` rows right under a row of heading-like lines (`header`, left to right) are rows of
    the cells of a table under them: each row follows the one above within twice its height, holds no heading-like line,
    has a line in two columns of the header or more (a column runs from a header line's left edge, less its height, to the
    next one's) and none left of the first, and has a line narrower than half the pitch of its column (a cell, where the
    lines of prose under side-by-side headings fill their column); and one column of the header or more holds a cell
    there: a line of fewer than `_PROSE_LINE_MIN_WORDS` words. A column whose every line under the header holds that many
    words or more is a column of prose, however short its lines ('Open Monday to Friday' / 'from nine to five and' under
    'Opening Hours'), and headings over columns of prose only are headings.

    Known accepted gap: a table with a single data row is not told from headings with one line under each (the panels of
    a slide), and its header cells are headings; so is the header of a table whose every cell holds three words or more
    (Pinned by: test_pdf_headings.py::TestHeaderRows)."""
    if len(below) < _HEADER_DATA_ROWS:
        return False
    lefts = [line.x0 - line.height for line in header]
    pitches = [after - left for left, after in pairwise(lefts)]
    pitches.append(pitches[-1])
    above = header
    cells = set()  # the columns with a line of fewer than `_PROSE_LINE_MIN_WORDS` words under the header
    for row in below:
        if min(line.top for line in row) - max(line.bottom for line in above) > 2 * max(line.height for line in above):
            return False
        columns = set()
        narrow = False
        for line in row:
            column = bisect_left(lefts, line.x0 + 1e-9) - 1
            if column < 0 or heading_like(line):
                return False
            columns.add(column)
            narrow = narrow or line.x1 - line.x0 < pitches[column] * 0.5
            if len(line.text.split()) < _PROSE_LINE_MIN_WORDS:
                cells.add(column)
        if len(columns) < 2 or not narrow:
            return False
        above = row
    return bool(cells)


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
                 side_by_side: frozenset[int] = frozenset(), previous: _Paragraph | None = None) -> _HeadingKey | None:
    """The level key of a paragraph that is a heading: its size when that is a heading size of the document, otherwise
    (size, True) when the document has bold headings at about the body size and the paragraph is one. None for body text."""
    if not _heading_shaped(paragraph) or any(id(line) in side_by_side for line in paragraph.lines):
        return None
    size = _heading_size(paragraph)
    if (size, False) in heading_levels:
        return None if _slightly_larger_text(paragraph, body_size) else (size, False)
    if (size, True) in heading_levels and _is_bold_heading(paragraph, body_size, side_by_side, previous):
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
        paragraphs = _group_paragraphs(page.lines, line_gap)
        side_by_side = _side_by_side_lines(page.lines, paragraphs, body_size)
        previous = None
        for paragraph in paragraphs:
            if _is_heading(paragraph, body_size, side_by_side):
                keys.add((_heading_size(paragraph), False))
            elif bold_allowed and _is_bold_heading(paragraph, body_size, side_by_side, previous):
                keys.add((_heading_size(paragraph), True))
            previous = paragraph
    ranked = sorted(keys, key=lambda key: (-key[0], key[1]))
    return {key: min(rank, 3) for rank, key in enumerate(ranked, start=1)}
