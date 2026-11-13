"""Judgments about a table element: layout table or data table, header rows, cells of a row and their spans, indentation
inside a cell, and names paired with values.
"""

import re

from bs4 import Tag

from opendockit.extractors._html_base import _HEADINGS, _solid_length
from opendockit.extractors._package import decimal_int


# Maximum characters (excluding whitespace) of the name cell in a name | value table. Field names such as '교육명' (course
# name), '대상자' (attendees), '문서번호' (document number) (`_pairs_names_with_values`).
_NAME_CHARS = 20


# Indentation of a table's first column (`_indent_marks`): inline CSS indentation properties, lengths, px per unit, and the
# maximum depth rank to mark. Cells that look like numbers are left out.
_CSS_INDENT = re.compile(r"(?:^|;)\s*(?P<name>padding-left|padding|text-indent|margin-left)\s*:\s*(?P<value>[^;!]+)", re.IGNORECASE)
_CSS_LENGTH = re.compile(r"(\d+(?:\.\d+)?)(px|pt|em|rem|)", re.IGNORECASE)
_CSS_UNITS = {"px": 1.0, "pt": 4 / 3, "em": 16.0, "rem": 16.0, "": 0.0}
# Upper bound on one CSS length (px, `_css_length`). No real indentation comes near it.
_MAX_CSS_LENGTH = 10000.0


# Upper bound on the rows of a table taken for a layout table holding heading elements, and lower bound on the blocks in one
# cell for a table taken for a one-row layout table (`_holds_page_blocks`).
_MAX_HEADED_LAYOUT_ROWS = 3
_MIN_LAYOUT_CELL_BLOCKS = 3
_LAYOUT_BLOCKS = {"p", "ul", "ol", "blockquote", "pre", "dl", *_HEADINGS}


def _own_caption(table: Tag) -> Tag | None:
    """The caption of this table: the first one outside its cells and inner tables (the parser leaves a caption written inside
    tbody or a wrapping form where it is). `table.find("caption")` also found the caption of a table nested in a cell, so the
    outer table took it and it came out twice (Pinned by: test_audit_html_text.py::TestCaptionOfATableInsideACell)."""
    found = _nearest(table, wanted={"caption"}, stop={"table", "td", "th"})
    return found[0] if found else None


def _holds_blocks(cell: Tag) -> bool:
    """Whether the cell holds block content: a heading, list, table, quote or code block, or two or more paragraphs (p / div). A label
    cell wrapped in a single paragraph (`<p style="margin-left:…">` in HTML saved by Word) does not count."""
    if cell.find([*_HEADINGS, "ul", "ol", "table", "blockquote", "pre", "dl"]) is not None:
        return True
    return len(cell.find_all(["p", "div"], limit=2)) >= 2


def _indent_depth(cell: Tag) -> float:
    """Indent depth of the cell text (approximate px). Each leading `&nbsp;` is 4px, a full-width space 16px, plus the CSS indentation of
    the cell and of its first child element."""
    depth = 0.0
    for node in [cell, *([cell.find(True)] if cell.find(True) is not None else [])]:
        for match in _CSS_INDENT.finditer(str(node.get("style", ""))):
            depth += _css_length(match.group("value"), last=match.group("name") == "padding")
    for string in cell.strings:
        for char in string:
            if char == "\xa0":
                depth += 4
            elif char == "\u3000":
                depth += 16
            elif not char.isspace():
                return depth
    return depth


def _css_length(value: str, *, last: bool) -> float:
    """CSS length (px, pt, em, rem, unitless 0) in px. For the `padding` shorthand the left value is used (the fourth; the second when
    there are two or three)."""
    parts = value.split()
    if last and parts:
        parts = [parts[{1: 0, 2: 1, 3: 1}.get(len(parts), 3)]]
    match = _CSS_LENGTH.fullmatch(parts[0]) if parts else None
    if match is None:
        return 0.0
    # A length of hundreds of digits is `inf`, and rounding the depth raised OverflowError — one style attribute rejected the
    # whole document (Pinned by: test_audit_html_text.py::TestHugeCssLengthInATableCell)
    return min(float(match.group(1)) * _CSS_UNITS[match.group(2).lower()], _MAX_CSS_LENGTH)


def _row_cells(row: Tag) -> list[Tag]:
    """Cells (td / th) directly under the row. Same as `find_all(recursive=False)` but without building bs4's filter, which is several
    times faster on large tables."""
    return [child for child in row.contents if isinstance(child, Tag) and child.name in ("td", "th")]


def _inner_tables(cell: Tag) -> list[Tag]:
    """Tables with two or more rows inside the cell (outermost only). A one-row table is kept on one line like cell text."""
    return [table for table in _nearest(cell, wanted={"table"}, stop=set()) if len(_table_rows(table)) > 1]


def _is_html_table(rendered: str) -> bool:
    """Whether a `TableGrid.render` result is an HTML table. Table tags in cell text are neutralized (`neutralize_table_tags`), so a
    `<table>` at the start of a line can only be the table itself."""
    return rendered.startswith("<table>") or "\n<table>" in rendered


def _has_one_row_header(table: Tag, rows: list[Tag]) -> bool:
    """Whether it has no caption or multi-row header and so can sit as a one-line HTML table inside an outer HTML cell (`_embedded_grid`)."""
    return _own_caption(table) is None and _header_row_count(table, rows) <= 1


def _is_layout_table(rows: list[Tag]) -> bool:
    """A table holding a table of two or more rows in a cell is treated as screen layout when one of these holds: the outer table has at
    most two rows; it has one column; at least half of the text is in inner tables (name | value tables excepted —
    `_pairs_names_with_values`); one cell holding an inner table has at least half of the text and that cell's text is mostly paragraphs
    outside the inner table, or the outer table has only one row with two or more cells (the body cell of a notice or HTML mail laid out
    as logo / body / footer).

    A Markdown table cell cannot hold a table, so a data table inside a layout table was merged into the single line
    "이름 점수 홍 90 김 80" (name score Hong 90 Kim 80). When a data table with many rows, like an infobox, holds a small table in one
    cell, the row structure of the outer table matters more and it is left alone — that cell is just part of the table text. A large data
    table with a cell holding one line of description + a table of choices ('parameter | default | description' in the Apache manual) is
    also left alone, because that cell's text is mostly the inner table. One-row tables or paragraphs inside cells are common in data
    tables too, so they are not used as evidence.
    A mail with one head row (logo | notice number) above rows of a single cell is layout even when the table in the body cell is longer
    than that cell's paragraphs — columns only arise when two or more rows put cells side by side. An approval form with several
    name | value rows stays a data table
    (Pinned by: test_text_html.py::TestLayoutTables).
    """
    cells = [cell for row in rows for cell in row.find_all(["td", "th"], recursive=False)]
    holders = [(cell, tables) for cell in cells
               if (tables := [table for table in _nearest(cell, wanted={"table"}, stop=set()) if len(_table_rows(table)) > 1])]
    if not holders:
        return _holds_page_blocks(rows, cells)
    filled = [len(row.find_all(["td", "th"], recursive=False)) for row in rows]
    if sum(1 for count in filled if count) <= 2 or max(filled) == 1:
        return True
    total = sum(_solid_length(cell) for cell in cells)
    inner = {id(cell): sum(_solid_length(table) for table in tables) for cell, tables in holders}
    if 2 * sum(inner.values()) >= total and not (
            _pairs_names_with_values(rows)
            and all(_has_one_row_header(table, _table_rows(table)) for _, tables in holders for table in tables)):
        return True
    one_row_of_columns = sum(1 for count in filled if count > 1) <= 1
    return any(2 * _solid_length(cell) >= total and (one_row_of_columns or _solid_length(cell) >= 2 * inner[id(cell)])
               for cell, _ in holders)


def _holds_page_blocks(rows: list[Tag], cells: list[Tag]) -> bool:
    """A table that is layout even without an inner table: it has at most `_MAX_HEADED_LAYOUT_ROWS` rows and one cell with at least half
    of the table text holds a heading (h1–h6) together with other text, or it is a one-row table of two or more cells in which one cell
    holds at least `_MIN_LAYOUT_CELL_BLOCKS` blocks (paragraphs, lists, headings …) (`_is_layout_table`). Headings and blocks within a
    one-row table inside a cell are not counted. A table whose first row is a header row made entirely of th is a data table.

    A page laid out as left menu | body was drawn as a data table: the whole body ran on inside one header cell joined by `<br>` and the
    headings stayed in the cell complete with '## ' (Pinned by: test_core_html_layout_cells.py::TestLayoutTableWithoutANestedTable).
    Deliberately narrow: a list or a paragraph or two inside a cell is common in data tables too (an '항목 | 내용' (item | content) table
    with a separate header row), and a cell holding only a heading (`<h3>기본</h3>` in the header of a price table) is a column name. A
    table with `<h4>새 기능</h4>` (new features) and a long description in a cell under a '버전 | 변경 사항' (version | changes) header
    was scattered into paragraphs and lost the pairing of version and change — header cells are not used for layout
    (Pinned by: test_core_header_tables_cell_code_and_terminal_codes.py::TestSmallTableWithAHeaderRowIsNotLayout).
    """
    placed = [row for row in rows if _row_cells(row)]
    if len(placed) > _MAX_HEADED_LAYOUT_ROWS:
        return False  # do not measure the text of every cell in a large table
    if len(placed) > 1 and all(cell.name == "th" for cell in _row_cells(placed[0])):
        return False
    total = sum(map(_solid_length, cells))
    if any(
            (headings := _nearest(cell, wanted=set(_HEADINGS), stop={"table"}))
            and 2 * _solid_length(cell) >= total and _solid_length(cell) > sum(map(_solid_length, headings))
            for cell in cells):
        return True
    return len(placed) == 1 and len(cells) >= 2 and any(
        len(_nearest(cell, wanted=_LAYOUT_BLOCKS, stop={"table"})) >= _MIN_LAYOUT_CELL_BLOCKS for cell in cells)


def _pairs_names_with_values(rows: list[Tag]) -> bool:
    """A name | value table: at least three rows have two or more cells and the first cell of every such row is a short name (no inner
    table).

    A data table holding a small table in every row ('구분 | 값' (category | value) + a 2×2 table per row), or an '항목 | 내용'
    (item | content) table holding a long roster table in one cell, had more than half of its text in inner tables, so it was flattened
    as layout and lost its header and name-value pairs. Layouts such as logo | title / menu | body / footer have fewer than three rows
    with two or more cells. When the inner table has a multi-row header or a caption it cannot sit inside an outer HTML cell and is
    flattened into one line, so it stays layout as before (Pinned by: test_text_html.py::TestLayoutTables::
    test_page_layout_whose_body_cell_is_a_table_beside_a_menu_stays_a_layout,
    test_name_value_table_whose_nested_table_has_a_two_row_header_stays_a_layout).
    """
    named = 0
    for row in rows:
        cells = _row_cells(row)
        if len(cells) < 2:
            continue
        if cells[0].find("table") is not None or not 0 < _solid_length(cells[0]) <= _NAME_CHARS:
            return False
        named += 1
    return named >= 3


def _names_values_in_pairs(rows: list[Tag]) -> bool:
    """Whether this is an item / value table without a header: leaving out single-cell rows (full-width body cells) there are two or more
    rows, and the cells of every such row are repetitions of the pair `th, td` (`th, td, th, td`, `th, td(colspan)`).

    In approval forms and contract summary tables that write the name cells as th, the first row is not a column header, yet it was drawn
    as the header and '계약명 | 2024 서버 유지보수' (contract name | 2024 server maintenance) became column names
    (Pinned by: test_core_html.py::TestNameValueTablesWithRowHeaders). A full-width body cell row (`td colspan=4`) under an approval form
    broke the pair check, with the same result (Pinned by: test_core_html_blocks.py::TestNameValueTableWithAFullWidthRow). Deliberately
    narrow: a table where only the first cell is th ('역할(th) | 이름 | 연락처' — role | name | contact) may have a column header as its
    first row and is left alone. A table whose first row is all th is already counted as having a header by `_header_row_count`.
    """
    pairs = 0
    for row in rows:
        names = [cell.name for cell in _row_cells(row)]
        if len(names) == 1:
            continue
        if not names or len(names) % 2 or names != ["th", "td"] * (len(names) // 2):
            return False
        pairs += 1
    return pairs >= 2


def _mostly_nested_tables(rows: list[Tag]) -> bool:
    cells = [cell for row in rows for cell in _row_cells(row)]
    nested = sum(_solid_length(table) for cell in cells for table in _inner_tables(cell))
    return 2 * nested >= sum(_solid_length(cell) for cell in cells)


def _is_one_column_box(rows: list[Tag]) -> bool:
    """A table with at most one cell per row — a notice / information box. Drawn as a table it is a single column and gets unwrapped into
    paragraphs anyway (`render_table`), so from the start the blocks inside the cells are drawn as they are, as in a layout table. Drawn
    as cell text (`_cell_text`), the `##` of headings inside the cell and the blank lines between paragraphs vanished, and code blocks
    came out in the body without a fence, `&nbsp;` indentation and all. The same holds with a one-cell th header ('공지사항', notices) —
    a header row is evidence of a data table only when there are two or more columns
    (`_holds_page_blocks`, Pinned by: test_core_round44.py::TestOneColumnBoxTable)."""
    counts = [len(_row_cells(row)) for row in rows]
    return any(counts) and max(counts) == 1


def _header_row_count(table: Tag, rows: list[Tag]) -> int:
    """Number of header rows: the number of thead rows when there is a thead, otherwise the number of rows at the top made only of th.

    In a table where every row is made only of th (a name-value table written with th only) just the first row is the header. Counting
    them all as header rows merges the table into a single header row
    (Pinned by: test_text_html.py::TestHeaderRowsOfThOnlyTables). From the second row on, a row is a header only when the header row above
    ties it in with a span — a rowspan from a row above reaches down, or the row directly above has a colspan ('정가' / '할인가' under
    '요금'). When data rows written with th had one td row (a total) below them, all the rows above were merged into a single header row
    and overlapping values in the same column were erased
    (Pinned by: test_core_th_rows_front_tables_and_linear_time.py::TestThRowsAboveADataRow).
    """
    count = _header_rows_on_top(table, rows)
    return 1 if count == len(rows) > 1 else count


def _header_rows_on_top(table: Tag, rows: list[Tag]) -> int:
    """The header row count of `_header_row_count` before 'every row is th' is reduced to the first row."""
    head = table.find("thead")
    if head is not None and head.find_parent("table") is table:
        return sum(1 for row in rows if _nearest_head(row, table) is head)
    count = 0
    spanned_until = 0  # last row covered by a header row's rowspan, + 1
    above_spans_columns = False

    for index, row in enumerate(rows):
        cells = row.find_all(["td", "th"], recursive=False)
        if not cells or any(cell.name != "th" for cell in cells):
            break
        if index and index >= spanned_until and not above_spans_columns:
            break
        spanned_until = max(spanned_until, *(index + _span(cell, "rowspan") for cell in cells))
        above_spans_columns = any(_span(cell, "colspan") > 1 for cell in cells)
        count += 1
    return count


def _nearest_head(row: Tag, table: Tag) -> Tag | None:
    """The thead of `table` that its row `row` is in, or None. It looks no further up than the table: `find_parent("thead")`
    walked to the root of the document for every body row
    (Pinned by: test_audit_html_text.py::TestManyElementsThatLookUpwardsUnderDeepNesting)."""
    node = row.parent
    while node is not None and node is not table and node.name != "thead":
        node = node.parent
    return node if node is not table else None


def _table_rows(table: Tag) -> list[Tag]:
    """Collect only this table's rows, leaving out the rows of nested tables (rows inside thead / tbody / tfoot included). tfoot rows go
    last.

    HTML4 has `<tfoot>` written before `<tbody>`, so reading in document order put the total row ahead of the data
    (Pinned by: test_text_html.py::TestHtmlInlineRendering::test_table_footer_written_before_the_body_goes_last).
    """
    rows = _nearest(table, wanted={"tr"}, stop={"table"})
    return sorted(rows, key=lambda row: row.parent is not None and row.parent.name == "tfoot")  # stable sort: the rest keeps its order


def _nearest(node: Tag, *, wanted: set[str], stop: set[str]) -> list[Tag]:
    """Collect the `wanted` tags under `node` in document order. Does not descend into a found tag or into `stop` tags.

    Finding all descendants and then climbing the ancestors of each one gets slower with the square of the nesting depth; 20,000 levels
    of tables took 59 seconds. Scan only once, with a stack instead of recursion (Pinned by: test_text_html.py::TestDeepNesting).
    """
    found: list[Tag] = []
    stack = list(reversed(node.contents))
    while stack:
        child = stack.pop()
        if not isinstance(child, Tag):
            continue
        if child.name in wanted:
            found.append(child)
        elif child.name not in stop:
            stack.extend(reversed(child.contents))
    return found


def _span(cell: Tag, attribute: str) -> int:
    """Span count. 1 when it is not a decimal number — `²` and `⑤` pass `isdigit()` but make `int()` fail, which rejected the whole
    document (the `isdecimal()` rule of the module guide;
    Pinned by: test_text_html.py::test_span_attribute_that_is_not_a_plain_number_does_not_reject_the_document).
    The same goes for numbers longer than 4,300 digits (`decimal_int`;
    Pinned by: test_core_malformed_mail_and_html.py::TestHugeNumbersInHtmlAndMail).
    Large values are capped at 200."""
    value = decimal_int(str(cell.get(attribute, "1")).strip(), 1)
    return min(value, 200) if value > 0 else 1
