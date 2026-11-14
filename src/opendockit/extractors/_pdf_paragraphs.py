"""From the lines and tables of a page to its Markdown: paragraphs, headings by font size, pictures placed in cells, and
the columns a paragraph belongs to.
"""

from bisect import bisect_left
from itertools import accumulate
import math
import re

from opendockit.extractors._pdf_model import (
    _BULLET_START,
    _HEADING_MIN_RATIO,
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


def _render_page(page: _Page, heading_levels: dict[float, int], line_gap: float,
                 images: list[tuple[tuple[float, ...], str]] | None = None) -> str:
    """Join the page's paragraphs, tables and pictures in reading order. `images` is a list of ((top, x0, x1, bottom),
    placeholder)."""
    items: list[tuple[float, str, bool]] = []
    spans: list[tuple[float, float]] = []  # horizontal ranges (x0, x1) in the same order as items
    sizes: list[tuple[float, int]] = []  # (bottom coordinate, line count) in the same order as items. Tables and pictures are (bottom, 0)
    boxes: list[tuple[float, float, float, float]] = []  # (top, bottom, x0, x1) of paragraphs. Tables and pictures are not included
    for paragraph in _group_paragraphs(page.lines, line_gap):
        level = heading_levels.get(_heading_size(paragraph), 0) if _is_heading(paragraph, heading_levels) else 0
        text = f"{'#' * level} {' '.join(line.text for line in paragraph.lines)}" if level else _paragraph_text(paragraph)
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
    x0, x1) either (1) overlaps the table or picture horizontally, does not overlap `span`, and is at least
    `_COLUMN_MIN_WIDTH_RATIO` times as wide as the table or picture (body text of the table's or picture's column), or (2)
    does not overlap the table or picture horizontally but overlaps it in height (text beside the table or picture,
    including slides that put a picture beside text).

    With neither, the table or picture sits alone on its line in a single-column flow and is left in height order. On a
    single-column page the only paragraphs overlapping a centred narrow table or picture are page-width paragraphs that also
    overlap the left-aligned paragraph below it, yet that left-aligned paragraph was taken for another column and the table
    or picture was moved after the paragraph at the top of the page (or to the end of the page), away from its subheading
    (pinned by: test_pdf.py::TestBlockPlacement::
    test_centred_narrow_table_on_a_one_column_page_stays_under_its_heading).
    """
    top, bottom, x0, x1 = block
    for other_top, other_bottom, left, right in boxes:
        overlaps_block = left < x1 and x0 < right
        if overlaps_block and not (left < span[1] and span[0] < right) and right - left >= (x1 - x0) * _COLUMN_MIN_WIDTH_RATIO:
            return True
        if not overlaps_block and other_top < bottom and top < other_bottom:
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
    if _LONE_BULLET.match(previous.text):
        # A line holding only a bullet character is one line with the next line at the same height.
        return abs((previous.top + previous.bottom) / 2 - (line.top + line.bottom) / 2) <= previous.height
    if previous.size and line.size and abs(previous.size - line.size) > 0.15 * max(previous.size, line.size):
        return False
    gap = line.top - previous.bottom
    allowed = previous.height * (line_gap if line_gap else 0.5) * 1.5 + 1.0
    horizontally_overlaps = line.x0 < previous.x1 and previous.x0 < line.x1
    return -previous.height * 0.3 <= gap <= allowed and horizontally_overlaps


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


def _is_heading(paragraph: _Paragraph, heading_levels: dict[float, int] | None = None, body_size: float = 0.0) -> bool:
    """A paragraph that is short, mostly letters, does not start with a bullet, and is clearly larger than the body text."""
    text = " ".join(line.text for line in paragraph.lines)
    size = max(line.size for line in paragraph.lines)
    if not size or len(paragraph.lines) > 2 or len(text) > 100 or _BULLET_START.match(text):
        return False
    word_chars = len(_WORD_CHAR.findall(text))
    if word_chars < 2 or word_chars < len(text.replace(" ", "")) * 0.5:
        return False
    if heading_levels is not None:
        return _heading_size(paragraph) in heading_levels
    return bool(body_size) and size / body_size >= _HEADING_MIN_RATIO


def _heading_levels(pages: list[_Page], body_size: float, line_gap: float) -> dict[float, int]:
    """Heading size (in 0.5pt steps) → level. The heading sizes in the document are mapped, largest first, to #, ## and ###
    (### from the fourth size onward)."""
    sizes = {
        _heading_size(paragraph)
        for page in pages if not page.simple
        for paragraph in _group_paragraphs(page.lines, line_gap)
        if _is_heading(paragraph, body_size=body_size)
    }
    return {size: min(rank, 3) for rank, size in enumerate(sorted(sizes, reverse=True), start=1)}
