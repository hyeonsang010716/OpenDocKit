"""The text of the cells of a ruled table: the rows of a cell, lines wrapped inside a cell joined, text repeated below a
merged cell, and cells whose text is drawn sideways.
"""

from bisect import bisect_left, bisect_right
import re

from opendockit.extractors._pdf_artifacts import _is_sideways, _sideways_paragraphs
from opendockit.extractors._pdf_chars import _text_from_chars
from opendockit.extractors._pdf_model import (
    _BULLET_START,
    _SPACED_CHAR,
    _TABLE_ROW_TOLERANCE,
    _BoxIndex,
    _encloses,
    _inside_any,
    _page_index,
    _text_by_middle,
)
from opendockit.extractors._pdf_pdfminer import _LAPARAMS


# Tolerance for grouping the characters of a table cell into words. The pdfplumber default (a fixed 3pt) is larger than the word
# gap of a 10–11pt font (0.2 × the character size), so spaces disappeared in table cells only. It is made proportional to the
# character size, on the same basis as body lines (pdfminer word_margin 0.1)
# (Pinned by: test_quality.py::test_pdf_table_cell_keeps_spaces_between_words_in_small_fonts).
_CELL_TEXT = {"x_tolerance_ratio": _LAPARAMS["word_margin"]}


# List number at the start of a cell text line ('1)', '(2)', '가.', '①'). The break before such a line is not a wrap caused by the
# cell width (`_soft_wrapped`).
_BARE_LIST_NUMBER = re.compile(r"^(?:\(?(?:\d{1,3}|[가나다라마바사아자차카타파하])[.)]|\((?:\d{1,3}|[가나다라마바사아자차카타파하])\)|[①-⑳])")


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
    # The rows and columns a cell covers are looked up by bisection over the sorted middles, and the cells a merged cell may
    # enclose only among those of its own height bands (`_BoxIndex.near`): scanning every cell of the table for every
    # merged cell took time with the square of the cells, 1.3 seconds for a ruled table of 4,500 cells merged over two rows
    # each (Pinned by: test_pdf_resource_limits.py::TestManyMergedCells)
    by_row, by_column = _sorted_middles(row_middles), _sorted_middles(column_middles)
    nearby = _BoxIndex([cell for cell in table.cells if cell])

    for cell in table.cells:
        x0, top, x1, bottom = cell
        spanned = sorted(_middles_within(by_row, top, bottom))
        column = min(_middles_within(by_column, x0, x1), default=None)
        if len(spanned) < 2 or column is None or column >= len(rows[spanned[0]]) or not rows[spanned[0]][column]:
            continue
        inner = [other for other in nearby.near(cell) if other != cell and _encloses(cell, other)]
        if len(inner) >= 2:
            rows[spanned[0]][column] = _text_outside(table.page.chars, cell, inner)
            containers.append(spanned[0])
            continue
        for index in spanned[1:]:
            if rows[index][column] is None:
                rows[index][column] = rows[spanned[0]][column]

    return [row for index, row in enumerate(rows) if index not in containers or not _repeated_below(rows, index)]


def _sorted_middles(middles: list[float]) -> tuple[list[float], list[int]]:
    """The middles in ascending order with the index each had, as (middles, indexes). A middle that is not a number lies
    within no range and is left out."""
    order = sorted((index for index, middle in enumerate(middles) if middle == middle), key=middles.__getitem__)
    return [middles[index] for index in order], order


def _middles_within(middles: tuple[list[float], list[int]], low: float, high: float) -> list[int]:
    """The indexes of the middles (`_sorted_middles`) from `low` to `high`, both included."""
    values, order = middles
    return order[bisect_left(values, low):bisect_right(values, high)]


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
