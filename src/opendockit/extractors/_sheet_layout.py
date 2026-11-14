"""From the rows of one sheet to its Markdown, shared by XLSX, XLS, XML Spreadsheet, CSV and ODS: collecting rows within the
limits, indent-level marks, merged cells repeated over the cells they cover, splitting a sheet into its tables and notes,
header rows, and the comments of the cells.
"""

from bisect import bisect_left, bisect_right
from collections.abc import Iterator
from dataclasses import dataclass
import re

from openpyxl.utils.cell import get_column_letter

from opendockit.extractors._lists import cell_level_mark
from opendockit.extractors._notation import comment_line
from opendockit.extractors._sheet_format import _format_value
from opendockit.markdown import join_blocks, render_table, solid_length
from opendockit.models import ConvertOptions, Extraction, Section, SectionKind


# Upper bound on the number of cells covered by merge ranges in a full load (same as the merged-cell total limit of
# markdown.TableGrid). openpyxl creates a cell object for every covered position: a 4.8KB file with a single merge range
# (A1:CV20000) took 8.8s and 800MB. Above the limit the workbook is streamed, which does not read merges. Hyperlink ranges, which
# openpyxl expanded the same way, are no longer parsed while reading, so they are not counted. XLS uses the same limit.
_MAX_MERGED_CELLS = 500_000
# Cells spanned by a horizontal merge: row (`id(row)`) → [(column after the first cell, end column (exclusive), value)].
# Filled in only on data rows, after the table is split (`_fill_across`).
_Across = dict[int, list[tuple[int, int, str]]]


# Sheets where hundreds of thousands of empty rows holding only formatting follow one another are common. A streamed sheet is taken
# to have ended after this many consecutive empty rows, with a warning. Full load, XLS and CSV read to the end because their row
# count is tied to the file size — a totals row after 5,000 empty rows used to vanish without a warning
# (Pinned by: test_spreadsheet.py::TestEmptyRowGaps).
_EMPTY_ROW_STOP = 5_000


# Leading whitespace of the first text cell of a row (used as indentation). A full-width space counts as two.
_LEADING_SPACES = re.compile(r"[ \t\xa0\u3000]*")
# A label with spaced-out letters ('합   계', '구 분', '총　계' — "total", "category", "grand total"): each character is separated by
# spaces. Deliberate: labels with wide gaps between words ('  매출  원가') are not considered — the leading spaces of sub-accounts
# would be discarded too.
_SPACED_LABEL = re.compile(r"\S(?:[ \t\xa0\u3000]+\S)+")
# Maximum level of the indent-level mark (`_level_marked`).
_MAX_INDENT_LEVEL = 5
# Indent-level mark (`_lists.cell_level_mark`) — removed from the first row (header row) of a table (`_header_without_level_mark`).
_LEVEL_MARK = re.compile(r"└─* ")


def _level_marked(text: str, raw: str, indent: float) -> str:
    """Add the indent-level mark (`└ `, `└─ `, `_lists.cell_level_mark` — the same mark as list levels inside table cells) to the
    first text cell of a row (`text`, original text `raw`). Level = cell indent (`indent`) + one per two leading spaces (a single
    space is one too; a full-width space is two).

    Sub-accounts in financial statements and budgets are shown by indentation or leading spaces; both were discarded, so '매출원가'
    (cost of sales) and its sub-item '재료비' (materials) read as the same level. Text that looks like a number (an amount stored as
    text) and the second and later cells of a row are left alone — leading spaces in an amount cell are for alignment
    (common to XLSX, XLS and XML, Pinned by: test_spreadsheet.py::TestIndentedLabels).
    The leading spaces of a label with spaced-out letters (`_SPACED_LABEL` — '   합   계', '  구 분') are not indentation either but
    centering done with spaces. Totals rows and headers became sub-items (`└ 합 계`)
    (Pinned by: test_core_spreadsheet.py::TestLeadingSpacesThatAreNotIndentation).
    """
    leading = _LEADING_SPACES.match(raw).group()
    spaces = 0 if _SPACED_LABEL.fullmatch(raw.strip()) else len(leading) + leading.count("\u3000")
    level = int(indent) + (max(spaces // 2, 1) if spaces else 0)
    if level <= 0 or _NUMBER_LIKE.fullmatch(text.strip()):
        return text
    return cell_level_mark(min(level, _MAX_INDENT_LEVEL)) + text.strip()


def _collect_rows(rows: object, options: ConvertOptions, sheet_name: str, extraction: Extraction) -> list[list[str]]:
    """Collect rows. Stops at the row where the character limit (`max_chars`) is exceeded — the remaining hundreds of thousands of
    rows are not read to the end.

    Deliberate: rows are not cut by row count. The only size limit is the character count (a 2,390-row sheet hit a 2,000-row limit
    and lost 390 rows). Merge filling maps by row number, so empty rows keep their place too.
    """
    width = options.max_sheet_cols
    collected: list[list[str]] = []
    for raw in rows:
        values = [_format_value(value) for value in raw]
        if not any(values):
            collected.append([])
            continue

        if any(values[width:]):
            _warn_once(extraction.warnings, f"Sheet '{sheet_name}': columns beyond column {width} were excluded.")
        collected.append(values[:width])
        if extraction.count(sum(solid_length(value) for value in values[:width]), options):
            break
    return collected


def _until_long_empty_gap(rows: Iterator[tuple[int, tuple[str, ...]]], sheet_name: str,
                          warnings: list[str]) -> Iterator[tuple[int, tuple[str, ...]]]:
    """Yield the (row number, row) pairs of a streamed sheet only up to a run of `_EMPTY_ROW_STOP` empty rows, and warn when
    stopping there. Row numbers the file skips count as empty rows of the run and are yielded as one empty row however many
    they are — a run of empty rows renders the same as a single one (`_sheet_text`), as in a full load (`_with_blank_gaps`)
    (Pinned by: test_audit_spreadsheet.py::TestStreamedRowsFarApart)."""
    empty_streak = previous = 0
    for number, row in rows:
        skipped = max(number - previous - 1, 0)
        run = empty_streak + skipped + (0 if any(row) else 1)  # empty rows up to and including this row
        if run >= _EMPTY_ROW_STOP:
            warnings.append(f"Sheet '{sheet_name}': {_EMPTY_ROW_STOP:,} consecutive empty rows were found, "
                            f"so nothing was read from row {previous - empty_streak + 1:,} on.")
            return
        if skipped:
            yield previous + 1, ()
        empty_streak, previous = (0 if any(row) else run), number
        yield number, row


def _append_sheet(extraction: Extraction, name: str, rows: list[list[str]], drawn: list[str] | None = None,
                  across: _Across | None = None) -> None:
    index = len(extraction.sections) + 1
    text = join_blocks([_sheet_text(rows, across), *(drawn or [])])
    extraction.sections.append(Section(SectionKind.SHEET, index, text, title=name))


# Cell text that looks like a number: sign, currency, thousands separators, decimals, percent, parenthesised negatives, a quantity
# unit appended at the end (`_UNIT_SUFFIX`), dates (2024-01-02) and times (9:30). Used to decide header rows (`_looks_like_header`).
# Units: values of number formats with a unit appended (`#,##0"원"`, `0"명"`) looked like text, so the header of the second table
# was not recognised as a header and was merged into the first table
# (Pinned by: test_core_spreadsheet.py::TestTablesWithUnitFormats). Deliberate: words common in column names ('1월', '2024년',
# '1차') such as 월, 년, 일, 차, 회, 분기 are not included — if a header looks like a number the new table is missed.
# The whitespace after the currency sign belongs to the sign (`(?:[₩$€¥£]\s*)?`): as `\s*[₩$€¥£]?\s*` a sign followed by N spaces
# and no digit was split between the two runs in N² ways (Pinned by: test_audit_spreadsheet.py::TestNumberLikeText).
_UNIT_SUFFIX = r"(?:\s?(?:[천만억조]|백만|십억)?원|\s?(?:명|건|개|대|톤|%p|kg|㎏|천|만|억|백만))?"
_NUMBER_LIKE = re.compile(r"[-+−▲▼△(]?\s*(?:[₩$€¥£]\s*)?\d[\d,]*(?:\.\d+)?\s*%?" + _UNIT_SUFFIX + r"\)?"
                          r"|\d{4}[-./]\d{1,2}[-./]\d{1,2}\b.*|-?\d{1,3}:\d{2}(?::\d{2})?")
# Years, common in headers (`구분 | 2023 | 2024`). Treated as header cells even though they look like numbers.
_YEAR = re.compile(r"(?:19|20)\d{2}")
# Number of rows below a header candidate in which to look for number cells.
_HEADER_PROBE_ROWS = 5


def _sheet_text(rows: list[list[str]], across: _Across | None = None) -> str:
    """Turn the rows of a sheet (or CSV) into Markdown. When a block separated by empty rows is a new table the tables are split,
    and one-cell text blocks between or after tables (titles, notes) are kept as paragraphs (`_sheet_segments`). If there is
    nowhere to split, the whole sheet is one table as before.

    Deliberate: a single empty row alone does not split. A single table with empty rows before the total or between groups is
    common. In a report layout, the '2. 인원' table below the '1. 매출' table became data rows of the first table, so the header
    '부서 | 정원 | 현원' ended up in the columns '사업부 | 1월 | …'
    (Pinned by: test_spreadsheet.py::TestSeveralTablesInOneSheet). `across` holds the horizontal merges to fill into the data rows
    of each table (`_fill_across`).
    """
    left = min((next(column for column, value in enumerate(row) if value) for row in rows if any(row)), default=0)
    segments = _sheet_segments(rows, left)
    if segments is None:
        _fill_across(rows, across)
        return join_blocks(_table_blocks(rows, across))

    parts = []

    for is_table, segment in segments:
        lead = _leading_note_rows(segment, left) if is_table else len(segment)
        parts.extend(_note_text(row) for row in segment[:lead])
        if lead < len(segment):
            _fill_across(segment[lead:], across)
            parts.extend(_table_blocks(segment[lead:], across))
    return join_blocks(parts)


def _table_blocks(rows: list[list[str]], across: _Across | None) -> list[str]:
    """Render one table (a block separated by empty rows). Summary cells to the right of the header row become a paragraph above
    the table (`_header_summary`), and side-by-side tables whose every row is separated by empty columns are rendered separately
    (`_side_by_side`)."""
    summary, rows = _header_summary(rows)
    tables = [render_table(_header_without_level_mark(table)) for table in _side_by_side(rows, across)]
    return [summary, *tables] if summary else tables


def _header_row(rows: list[list[str]]) -> int | None:
    """Index of the table's header row: the first row with two or more filled cells. One-cell rows above it are title rows
    (`render_table` moves them into paragraphs above the table)."""
    return next((index for index, row in enumerate(rows) if sum(1 for value in row if value) >= 2), None)


def _is_number_or_formula(value: str) -> bool:
    text = value.strip()
    return text.startswith("=") or (_NUMBER_LIKE.fullmatch(text) is not None and _YEAR.fullmatch(text) is None)


def _header_summary(rows: list[list[str]]) -> tuple[str, list[list[str]]]:
    """If the right end of the header row — the columns that are empty in every data row below — holds a number or formula (a
    '총원 | 26' placed next to the header), detach those cells and return them as paragraph text. Otherwise return empty text and
    the rows unchanged.

    The summary value became a column name, as in '| 본부 | 팀 | 인원 | 총원 | 26 |'
    (Pinned by: test_core_sheet_layouts.py::TestSummaryCellsRightOfTheHeader). Deliberate: an empty column without a number or
    formula ('비고') and year column names ('2023') stay column names, and this applies only when the remaining header looks like a
    header (`_looks_like_header`) — the heading cells of a quotation ('수신 | … | 견적일 | 2024-03-05') are not a table.
    """
    index = _header_row(rows)
    data = [row for row in rows[index + 1:] if any(row)] if index is not None else []
    if not data:
        return "", rows

    width = _width(data)
    header = rows[index]
    extra = [value.strip() for value in header[width:] if value]
    if not any(map(_is_number_or_formula, extra)) or not _looks_like_header(tuple(header[:width]), data[:_HEADER_PROBE_ROWS]):
        return "", rows
    return " ".join(extra), [*rows[:index], header[:width], *rows[index + 1:]]


def _side_by_side(rows: list[list[str]], across: _Across | None) -> list[list[list[str]]]:
    """If the column groups into which empty columns divide every row (header included) are each a table standing on its own
    (`_is_side_table`), cut the rows per group into several tables; otherwise `[rows]`.

    Two tables placed side by side with an empty column between them were merged into one table and their rows paired up — '김대리'
    was read as the person in charge of the Seoul branch (Pinned by: test_core_sheet_layouts.py::TestSideBySideTables).
    Deliberately narrow: every group must have two or more columns, the group's header row (`_header_row`) must have two or more
    cells that are all non-numeric text, and the data cells of the first column must contain a non-numeric name for it to be a
    table. A group with numbers only (right of a spacer column between '1월 | 2월'), a one-column group and an empty column spanned
    by a horizontal merge belong to the same table. Known accepted gap: a single table with name columns on both sides and a spacer
    column ('성명 | 부서 | | 비상연락 | 관계') is split as well — the row order is unchanged, so the pairing survives by position.
    """
    filled = [row for row in rows if any(row)]
    width = _width(filled)
    covered = {column for row in filled for first, end, _ in (across or {}).get(id(row), []) for column in range(first, end)}
    used = [column in covered or any(column < len(row) and row[column] for row in filled) for column in range(width)]
    groups: list[tuple[int, int]] = []

    for column, is_used in enumerate(used):
        if not is_used:
            continue
        if groups and groups[-1][1] == column:
            groups[-1] = (groups[-1][0], column + 1)
        else:
            groups.append((column, column + 1))
    if len(groups) < 2:
        return [rows]

    tables = [[row[start:end] for row in filled if any(row[start:end])] for start, end in groups]
    return tables if all(map(_is_side_table, tables)) else [rows]


def _is_side_table(rows: list[list[str]]) -> bool:
    """Whether one group of side-by-side tables is a table standing on its own (`_side_by_side`): two or more columns, a header of
    two or more non-numeric text cells, a non-numeric name in the first column."""
    index = _header_row(rows)
    if _width(rows) < 2 or index is None or any(map(_is_number_or_formula, filter(None, rows[index]))):
        return False
    return any(row and row[0] and not _is_number_or_formula(row[0]) for row in rows[index + 1:])


def _header_without_level_mark(rows: list[list[str]]) -> list[list[str]]:
    """Remove the indent-level mark (`_level_marked`) from the first text cell of the table's first non-empty row (the header).

    A header is not a sub-item. A header cell centred with spaces ('  구분') or given an indent format became '└ 구분'
    (Pinned by: test_core_spreadsheet.py::TestLeadingSpacesThatAreNotIndentation). The rows are not modified; a changed list is
    returned.
    """
    index = next((index for index, row in enumerate(rows) if any(row)), None)
    if index is None:
        return rows
    header = list(rows[index])
    first = next(column for column, value in enumerate(header) if value)
    mark = _LEVEL_MARK.match(header[first])
    if mark is None:
        return rows
    header[first] = header[first][mark.end():]
    return [*rows[:index], header, *rows[index + 1:]]


def _sheet_segments(rows: list[list[str]], left: int) -> list[tuple[bool, list[list[str]]]] | None:
    """Group the blocks separated by empty rows into (is a table, rows) segments. None if there is only one segment (nowhere to
    split).

    A one-cell text block (a block of `_is_note_row` rows only) is a paragraph when a new table follows or the sheet ends, and rows
    of the table when the same table continues. A table block is a new table only when `_starts_new_table` holds.
    """
    blocks: list[list[list[str]]] = [[]]

    for row in rows:
        if any(row):
            blocks[-1].append(row)
        elif blocks[-1]:
            blocks.append([])
    blocks = [block for block in blocks if block]
    if len(blocks) < 2:
        return None

    segments: list[tuple[bool, list[list[str]]]] = []
    table: list[list[str]] | None = None
    notes: list[list[str]] = []

    for block in blocks:
        if all(_is_note_row(row, left) for row in block):
            notes.extend(block)
            continue
        if table is not None and not _starts_new_table(table, block, left, titled=bool(notes)):
            table.extend([*notes, *block])
            notes = []
            continue
        if table is not None:
            segments.append((True, table))
        if notes:
            segments.append((False, notes))
        table, notes = list(block), []

    if table is None:
        return None
    segments.append((True, table))
    if notes:
        segments.append((False, notes))
    return segments if len(segments) > 1 else None


def _is_note_row(row: list[str], left: int) -> bool:
    """A row in which only the cell in the sheet's leftmost column (`left`) is filled and its text is not a number or formula (a
    table title, or a note such as '※ 단위: 백만원'). A total number standing alone, or a single cell in another column (a value of
    that column), is a row of the table."""
    filled = [(column, value) for column, value in enumerate(row) if value]
    if len(filled) != 1 or filled[0][0] != left:
        return False
    text = filled[0][1].strip()
    return not text.startswith("=") and _NUMBER_LIKE.fullmatch(text) is None


def _leading_note_rows(rows: list[list[str]], left: int) -> int:
    count = 0
    while count < len(rows) and _is_note_row(rows[count], left):
        count += 1
    return count


def _note_text(row: list[str]) -> str:
    return next(value for value in row if value).strip()


def _starts_new_table(table: list[list[str]], block: list[list[str]], left: int, titled: bool) -> bool:
    """Whether the block `block` after an empty row is a new table different from the preceding one (`table`). True when the
    block's first table row (after its leading title rows) equals the preceding table's header, or when it looks like a header
    (`_looks_like_header`) and either a title precedes it (inside the block or as the one-cell block right before, `titled`) or
    the column count differs.

    Deliberate: even if it looks like a header, no split happens without a title when the column count is the same — a data row
    with text ('결시') in a number column that comes after an empty row cannot be told apart from a header. Known accepted gap: a
    text-only table (one without number columns) is split only when the headers are equal.
    """
    lead = _leading_note_rows(block, left)
    body = block[lead:]
    first = _trimmed(body[0])
    table_body = table[_leading_note_rows(table, left):]
    if first == _trimmed(table_body[0]):
        return True
    if not _looks_like_header(first, body[1:1 + _HEADER_PROBE_ROWS]):
        return False
    return titled or lead > 0 or _width(body) != _width(table_body)


def _trimmed(row: list[str]) -> tuple[str, ...]:
    values = list(row)
    while values and not values[-1]:
        values.pop()
    return tuple(values)


def _width(rows: list[list[str]]) -> int:
    return max((len(_trimmed(row)) for row in rows), default=0)


def _looks_like_header(row: tuple[str, ...], below: list[list[str]]) -> bool:
    """A row with two or more filled cells, all non-numeric text (years count as text), where at least one of those text cells has
    a number in its column in the rows below."""
    filled = [(column, value.strip()) for column, value in enumerate(row) if value]
    if len(filled) < 2 or any(_NUMBER_LIKE.fullmatch(value) and not _YEAR.fullmatch(value) for _, value in filled):
        return False
    return any(column < len(other) and _NUMBER_LIKE.fullmatch(other[column].strip()) is not None
               for column, _ in filled for other in below)


@dataclass
class _SheetRows:
    """Sheet row number (1-based) → cell texts of the row as read. `offset` is the number of leading empty columns removed from
    the rows (`_first_used_column`)."""

    rows: dict[int, list[str]]
    offset: int = 0

    def values(self, row: int) -> list[str]:
        return self.rows.get(row) or []

    @staticmethod
    def address(row: int, column: int) -> str:
        return f"{get_column_letter(min(max(column, 1), 16384))}{row}"

    def place(self, row: int, column: int) -> str:
        """`cell C2: 제품A` — the cell address and the first text cell of that row other than that cell (one that does not look like
        a number). Tables carry no row/column numbers, so the address alone does not tell which row it is. Just the address if the
        row was not read or has no text cell."""
        values, index = self.values(row), column - 1 - self.offset
        label = next((value for position, value in enumerate(values)
                      if value and position != index and not _NUMBER_LIKE.fullmatch(value)), "")
        return f"cell {self.address(row, column)}: {label}" if label else f"cell {self.address(row, column)}"

    def target(self, row: int, column: int) -> str:
        """The text of the commented cell and the first text cell of its row: `2월: 120`. Just the cell text if that cell is the
        first text cell."""
        values, index = self.values(row), column - 1 - self.offset
        value = values[index] if 0 <= index < len(values) else ""
        label = next((cell for position, cell in enumerate(values)
                      if cell and position != index and not _NUMBER_LIKE.fullmatch(cell)), "")
        if label and next((position for position, cell in enumerate(values) if cell), index) >= index:
            label = ""  # the commented cell is the first text cell of the row
        return f"{label}: {value}" if label and value else value or label


# (row, column (1-based), author, text, replies [(author, text)])
_CellComment = tuple[int, int, str, str, list[tuple[str, str]]]


def _comment_block(comments: list[_CellComment], row_at: _SheetRows, extraction: Extraction, options: ConvertOptions) -> str:
    """Turn cell comments, in cell order (top→bottom, left→right), into `> Comment(author, cell E5, on "row text: cell text"): text`
    lines (`_notation.comment_line`).

    Comments were dropped without a warning, so review remarks could not be seen (same direction as DOCX/PPTX/HWPX comments; they
    are placed at the end of the sheet. Pinned by:
    test_spreadsheet_notes_pictures_and_times.py::TestXlsxCellComments, TestXlsCellComments). Excel writes the author as an
    `author:` line in front of the comment text, so that is removed. The text counts toward the character limit (whether it was
    exceeded is reported by `extraction.count`).
    """
    lines = []

    for row, column, author, text, replies in sorted(comments, key=lambda comment: comment[:2]):
        text = text.strip()
        if author and text.startswith(f"{author}:"):
            text = text[len(author) + 1:]
        if not text.strip():
            continue
        lines.append(comment_line(author, text, row_at.target(row, column), cell=row_at.address(row, column)))
        lines.extend(comment_line(name, reply, reply=True) for name, reply in replies if reply.strip())

    block = "\n".join(lines)
    if block:
        extraction.count(solid_length(block), options)
    return block


_CHECKED, _UNCHECKED = "☒", "☐"  # same characters as DOCX form fields, HWPX check boxes and HTML


def _fill_merges(sheet: object, rows: list[list[str]], numbers: list[int], width: int, offset: int = 0) -> _Across:
    """`numbers` holds the sheet row number of each row in rows (ascending). If the top row of a merge is not in rows there is no
    value either. `offset` is the number of leading empty columns removed from the rows (`_first_used_column`) — a merge starting
    in those columns has an empty first cell and no value to fill."""
    across: _Across = {}
    # Merges of upper rows first — a left cell filled earlier by a vertical merge also serves as the label cell of a horizontal
    # merge (`_repeat_merge`)
    for merged in sorted(sheet.merged_cells.ranges, key=lambda merged: (merged.min_row, merged.min_col)):
        top = bisect_left(numbers, merged.min_row)
        if top < len(numbers) and numbers[top] == merged.min_row and merged.min_col > offset:
            _repeat_merge(rows, top, bisect_right(numbers, merged.max_row), merged.min_col - 1 - offset,
                          min(merged.max_col - offset, width), across)
    return across


def _repeat_merge(rows: list[list[str]], top: int, bottom: int, left: int, right: int, across: _Across) -> None:
    """Repeat a vertical merge value in the rows below (`_repeat_down`), and for a horizontal merge with text to its left in the
    same row collect the spanned cells in `across`.

    `top`/`left` are 0-based; `bottom`/`right` are exclusive row/column numbers (`right` is already cut at the column limit).
    Horizontally spanned cells are not filled here — table splitting and title-row detection (`_sheet_text`) see the rows as
    before, and filling happens afterwards in data rows only (`_fill_across`).
    """
    _repeat_down(rows, top, bottom, left)
    if right <= left + 1 or top >= len(rows) or left >= len(rows[top]) or not rows[top][left] or not any(rows[top][:left]):
        return
    for row_index in range(top, min(bottom, len(rows))):
        if rows[row_index]:
            across.setdefault(id(rows[row_index]), []).append((left + 1, right, rows[top][left]))


def _repeat_down(rows: list[list[str]], top: int, bottom: int, left: int) -> None:
    """Repeat a vertical merge value in the rows below (same rule as markdown.TableGrid).

    `top`/`left` are 0-based; `bottom` is an exclusive row number. A short row that has no cells up to the merge column (XLS reads
    each row only up to that row's cells) is extended to that column — the top row has a value there, so it is within the column
    limit.
    """
    if top >= len(rows) or left >= len(rows[top]) or not rows[top][left]:
        return
    value = rows[top][left]
    for row_index in range(top + 1, min(bottom, len(rows))):
        row = rows[row_index]
        if not row:
            continue
        if left >= len(row):
            row.extend([""] * (left + 1 - len(row)))
        if not row[left]:
            row[left] = value


def _fill_across(table: list[list[str]], across: _Across | None) -> None:
    """Repeat the horizontal merge values of a table (the rows of one table split out of a sheet) over the spanned cells. Only when
    every spanned column is a column that has text in a row above (header and so on).

    A merge spanning March–April with '육아휴직' in a data row was left in the first cell only, so the other month read as blank
    (Pinned by: test_core_spreadsheet.py::TestHorizontalMergesInDataRows). Deliberately narrow: the first row of the table (header
    or title row) has no row above it and is not filled; merges that start in the leftmost column or have nothing to their left
    (table title, group name '[개발팀]') are not collected (`_repeat_merge`); and merges spanning into unnamed columns (a
    quotation's '합계금액 | 일금 … 원정' spanning into empty columns) are not filled — repeating a value in cells without a column
    name is noise. This is done after table splitting so that the filled cells do not change the shape of the table.
    """
    if not across:
        return
    named: set[int] = set()

    for row in table:
        filled = {column for column, value in enumerate(row) if value}
        for first, end, value in across.get(id(row), []):
            if not all(column in named for column in range(first, end)):
                continue
            if end > len(row):
                row.extend([""] * (end - len(row)))
            for column in range(first, end):
                row[column] = row[column] or value
        named |= filled


def _warn_once(warnings: list[str], message: str) -> None:
    if message not in warnings:
        warnings.append(message)
