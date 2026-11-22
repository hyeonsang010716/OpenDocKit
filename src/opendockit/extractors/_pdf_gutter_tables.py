"""Tables set without any rule whose cells wrap: lines of text in columns that stay apart from the first line to the last,
with nothing between two rows but the next line. Run before the borderless tables, which tell a wrapped line from a row by a
wider gap between rows than between the lines of a cell — a gap Word and LibreOffice do not leave.
"""

from itertools import pairwise
import re
from statistics import median

from opendockit.extractors._pdf_aligned_tables import _is_formula, _table_columns
from opendockit.extractors._pdf_banded_tables import _FILLED_MIN, _GUTTER_MIN, _columns, _joined, _pieces
from opendockit.extractors._pdf_model import _Line, _original_lines_of, _Page, _Table, _visual_rows
from opendockit.markdown import render_table


# The rows of text of a block follow each other no farther apart than this many line heights.
_LINE_GAP_MAX = 1.6
# A table has this many rows, lines of text and columns at least. Two columns are left to the searches that ask for more
# than columns standing apart: two columns of a page stand apart too.
_MIN_ROWS = 3
_MIN_LINES = 3
_MIN_COLUMNS = 3
# A word had no room on the line above when the two together are wider than this share of the room of the cell.
_FIT_TOLERANCE = 0.97
# The widest text of a column is taken for the room of its cells when it fills this share of the way to the next column.
_FILLED_ROOM = 0.8
# A line that fills no more of the text of its column than this stops short of it.
_SHORT_LINE = 0.75
# The share of the rows after the first above which a cell must be seen to end (`_ended`).
_ENDED_CELLS_MIN = 0.8
# A column is narrow text, not a column of prose, when the middle one of its pieces fills no more of its width than this,
# or when no more than this share of the lines has anything in it.
_SHORT_FILL = 0.6
_SPARSE_LINES = 0.7
# How much of the lines one row of the table may take: more, and the rows are the lines of one paragraph beside another.
_ROW_LINES_MAX = 0.7
# A page of more lines than this is a drawing or a plot with its labels and is left; and inside a block that is no table,
# only this many rows further down are tried as its start.
_PAGE_LINES_MAX = 1500
_STARTS_TRIED = 3
# A word of a header cell: two letters, or one of a script that writes a word with one (Hangul, Han, kana) — '제 1 조'
# holds two words and no two letters side by side
_WORD = re.compile(r"[^\W\d_]{2}|[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7a3]")


def _extract_gutter_tables(page: _Page) -> None:
    """Make tables of the blocks of lines whose columns stay apart on every line and whose cells wrap, and take their
    lines out of the body flow.

    A block is rows of text following each other at the distance of lines (`_LINE_GAP_MAX`), every one of which leaves
    the same stretches of the width empty (`_GUTTER_MIN`, `_block_end`). A line continues the row above it when it has
    text in fewer columns, all of them among those of the line that started the row, and in each of them its first word
    had no room on the line above (`_wrapped`): a line is broken only where the next word does not fit, so 'Program'
    under 'Eco-Ecole' in a narrow column is the same cell, and '5' under '4' is the next row with a cell left empty.
    Any other line starts a row. The first row is the header.

    Columns of prose, a list of references in two columns with their numbers, the labels of a chart and the parts of a
    displayed formula are blocks of that kind too. A block is a table only with three columns or more, a cell that
    wraps, a column of short or scattered text (`_is_tabular`), a header with a word in every column but one at most,
    most cells filled, rows that start in two columns or more, and cells seen to end above nearly every row (`_ended`:
    the lines of a column of prose go on under each other and stop short only where a paragraph ends). On a page set in
    two columns a block across both is left. Everything else is read by the borderless search as before.

    Deliberate: the conditions are those under which the borderless search cannot read the table — it cuts it at the
    first wrapped line — and no others, so a table it reads is read by it still.
    Pinned by: test_pdf_gutter_tables.py; judged on the scorecards (`evals/README.md`).
    """
    if page.simple or not _MIN_LINES * 2 <= len(page.lines) <= _PAGE_LINES_MAX:
        return
    # With the cells set in the font of another script at the height of the number beside them (`_visual_rows`): a number
    # that opens a row was a row of its own, and a table whose rows start in one column is none
    rows = _visual_rows(page.lines, staggered=True)
    page_columns = _table_columns(page)
    used: set[int] = set()
    start = tried = 0
    while start < len(rows):
        end = _block_end(rows, start)
        found = _gutter_table(rows[start:end]) if end - start >= _MIN_LINES else None
        if found is not None and page_columns is not None and found.x0 < page_columns[0] and found.x1 > page_columns[1]:
            found = None
        if found is not None:
            page.tables.append(found)
            used.update(id(original) for row in rows[start:end] for line in row for original in _original_lines_of(line))
            start, tried = end, 0
        elif end - start >= _MIN_LINES and tried < _STARTS_TRIED:
            # A block that is no table from its first row may be one from a row further down, under a line of two pieces
            # that is not the table's. A few rows are tried, not each: a page of prose in columns is one long block.
            start, tried = start + 1, tried + 1
        else:
            start, tried = end, 0
    if used:
        page.lines = [line for line in page.lines if id(line) not in used]


def _block_end(rows: list[list[_Line]], start: int) -> int:
    """The row after the last one of the block that starts at `start`: the rows down to the first that lies farther off
    than a line, or whose text reaches across the last gap the rows above left between their columns.

    Only the gaps still open are kept and each row is looked at once against them, so a long block costs its rows times
    the gaps of its first row."""
    first = sorted((start_x, end_x) for line in rows[start] for start_x, end_x, _ in _pieces(line))
    gaps = [(left[1], right[0]) for left, right in pairwise(first) if right[0] - left[1] >= _GUTTER_MIN]
    end = start + 1
    while gaps and end < len(rows):
        row, above = rows[end], rows[end - 1]
        height = max(line.height for line in above)
        if min(line.top for line in row) - max(line.bottom for line in above) > height * (_LINE_GAP_MAX - 1):
            break
        left = gaps
        for line in row:
            for piece_start, piece_end, _ in _pieces(line):
                narrowed = []
                for low, high in left:
                    # what the piece leaves of the gap: the part before it, the part after it, or both
                    for part in ((low, min(high, piece_start)), (max(low, piece_end), high)):
                        if part[1] - part[0] >= _GUTTER_MIN:
                            narrowed.append(part)
                left = narrowed
        if not left:
            break
        gaps = left
        end += 1
    return end


def _row_pieces(row: list[_Line], columns: list[tuple[float, float]]) -> dict[int, tuple[float, float, str]]:
    """Column → (left end, right end, text) of what a row of text has in it, its pieces in one column taken together."""
    found: dict[int, tuple[float, float, str]] = {}
    for line in row:
        for start, end, text in _pieces(line):
            middle = (start + end) / 2
            column = next((index for index, (low, high) in enumerate(columns) if low <= middle <= high), None)
            if column is None or not text.strip():
                continue
            if column in found:
                left, right, before = found[column]
                found[column] = (min(left, start), max(right, end), f"{before} {text.strip()}")
            else:
                found[column] = (start, end, text.strip())
    return found


def _wrapped(above: tuple[float, float, str], below: tuple[float, float, str], width: float) -> bool:
    """Whether the text `below` is the next line of the cell that ends in `above`: its first word had no room left on
    that line. A line is broken only where the next word does not fit, so a value under a value ('4' over '5', '7.41%'
    over '11.52%') is a cell of the next row, and 'Program' under 'Eco-Ecole' in a column that narrow is the same cell."""
    word = below[2].split(" ", 1)[0]
    per_character = (below[1] - below[0]) / max(len(below[2]), 1)
    return (above[1] - above[0]) + per_character * (len(word) + 1) > width * _FIT_TOLERANCE


def _ended(above: tuple[float, float, str], below: tuple[float, float, str], width: float) -> bool:
    """Whether the cell that ends in `above` is seen to end there: the line stops well short of the width of its column, or
    the first word of `below` had room on it. The lines of a column of prose stop short only where a paragraph ends."""
    return above[1] - above[0] <= width * _SHORT_LINE or not _wrapped(above, below, width)


def _gutter_table(block: list[list[_Line]]) -> _Table | None:
    lines = [line for row in block for line in row]
    columns = _columns(lines)
    if len(columns) < _MIN_COLUMNS:
        return None
    widths = [high - low for low, high in columns]
    # The room of a cell is not written anywhere. It is no less than the widest text of its column and no more than the
    # stretch from where the column starts to where the next one does. Where the text nearly fills that stretch
    # (`_FILLED_ROOM`) the widest text is the measure; in a column of short entries it says nothing — measured by it, the
    # labels of a column ('Alpha' over '합계') each looked like a wrapped line of the one above — and the stretch is.
    stretches = [following[0] - low - _GUTTER_MIN for (low, _), following in pairwise(columns)] + [widths[-1]]
    rooms = [width if width >= stretch * _FILLED_ROOM else stretch for width, stretch in zip(widths, stretches, strict=True)]
    grid: list[list[list[str]]] = []   # row of the table → its lines as cells
    starts: list[frozenset[int]] = []  # the columns the first line of each row has text in
    last: dict[int, tuple[float, float, str]] = {}  # column → the last line of its cell in the row being read
    row_starts = ended = 0
    for row in block:
        pieces = _row_pieces(row, columns)
        if not pieces:
            continue
        cells = [pieces[column][2] if column in pieces else "" for column in range(len(columns))]
        if grid and pieces.keys() < starts[-1] and all(
                column in last and _wrapped(last[column], piece, rooms[column]) for column, piece in pieces.items()):
            grid[-1].append(cells)
        else:
            # A cell ended above a new row: in one of its columns the line above stops short, or the first word of the
            # new line had room on it (`_ended`). Where no row but a few starts so, the lines are those of columns of
            # prose, each going on under itself, and a column of them ending early made the rest look like wrapped cells.
            if grid:
                row_starts += 1
                ended += any(column in last and _ended(last[column], piece, widths[column]) for column, piece in pieces.items())
            grid.append([cells])
            starts.append(frozenset(pieces))
            last = {}
        last.update(pieces)
    if ended < row_starts * _ENDED_CELLS_MIN:
        return None
    if len(grid) < _MIN_ROWS or not _is_tabular(lines, columns, len(block), max(len(row) for row in grid)):
        return None
    # Only what the borderless search cannot read is taken from it: a cell that wraps, under a header with a word in
    # every column but one at most, with most cells filled, every row starting in two columns or more, and no formula
    if all(len(row) == 1 for row in grid) or _is_formula(block):
        return None
    header = _joined(grid[0], " ")
    if sum(1 for cell in header if _WORD.search(cell)) < len(columns) - 1 or any(len(start) < 2 for start in starts):
        return None
    if sum(1 for row in grid for cell in _joined(row, " ") if cell) < len(grid) * len(columns) * _FILLED_MIN:
        return None
    # The lines of a cell are one text broken where it did not fit, so they are joined by a space, not kept as lines
    drawn = [_joined(row, " ") for row in grid]
    return _Table(top=min(line.top for line in lines), text=render_table(drawn), bottom=max(line.bottom for line in lines),
                  x0=min(line.x0 for line in lines), x1=max(line.x1 for line in lines), rows=drawn, spans=tuple(columns))


def _is_tabular(lines: list[_Line], columns: list[tuple[float, float]], line_count: int, longest_row: int) -> bool:
    """Whether the columns of a block are those of a table and not columns of prose side by side: one of them holds short
    text or is left empty on many lines, and no row takes most of the lines."""
    if longest_row > max(line_count * _ROW_LINES_MAX, 1) and line_count > _MIN_LINES:
        return False
    fills, shares = [], []
    for low, high in columns:
        widths = [end - start for line in lines for start, end, _ in _pieces(line) if low <= (start + end) / 2 <= high]
        on_lines = len({round(line.top) for line in lines for start, end, _ in _pieces(line) if low <= (start + end) / 2 <= high})
        fills.append(median(widths) / max(high - low, 1.0) if widths else 0.0)
        shares.append(on_lines / max(line_count, 1))
    return min(fills) <= _SHORT_FILL or min(shares) <= _SPARSE_LINES
