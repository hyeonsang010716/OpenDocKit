"""The prepared tree → Markdown: blocks and inline text, headings, code blocks, lists, definition lists, ARIA grids,
tables and the text of their cells.
"""

from contextvars import ContextVar
import re
import secrets

from bs4 import NavigableString, Tag

from opendockit.extractors._html_base import (
    _BLOCK_GAP,
    _BLOCK_TAGS,
    _CHECKED,
    _FOLDED_STYLE,
    _HEADER_PROBE_ROWS,
    _HEADINGS,
    _UNCHECKED,
    _WHITESPACE,
    _Inline,
    _inline,
    _list_number,
    _omitted_rows,
    _Places,
    _replace,
)
from opendockit.extractors._html_tables import (
    _has_one_row_header,
    _header_row_count,
    _holds_blocks,
    _indent_depth,
    _inner_tables,
    _is_html_table,
    _is_layout_table,
    _is_one_column_box,
    _mostly_nested_tables,
    _names_values_in_pairs,
    _nearest,
    _own_caption,
    _row_cells,
    _row_span,
    _span,
    _table_rows,
)
from opendockit.extractors._inline import (
    BLOCK_INSIDE,
    BOLD_OFF,
    BOLD_ON,
    ITALIC_OFF,
    ITALIC_ON,
    LINK_ON,
    InlineMark,
    link_off,
    link_target,
    resolve_inline,
)
from opendockit.extractors._lists import cell_level_mark, format_number
from opendockit.extractors._package import decimal_int
from opendockit.markdown import TableGrid, render_table, table_title


# ARIA tables (`_render_aria_grid`): table/cell roles, real table elements (handled as tables even when a role is attached), and
# the maximum number of columns `aria-colindex` may skip.
_GRID_ROLES = {"grid", "table", "treegrid"}
_ARIA_CELL_ROLES = {"cell", "gridcell", "columnheader", "rowheader"}
_ARIA_SKIPPED_TAGS = {"table", "thead", "tbody", "tfoot", "tr", "td", "th", "ul", "ol", "dl", "pre"}
_MAX_ARIA_COLUMN_SKIP = 200
# Maximum nesting depth at which an ARIA table inside the cell of another one is drawn as a table. Deeper ones are descended
# as blocks (recursion limit; Pinned by: test_audit_html_text.py::TestDeeplyNestedAriaGrids).
_MAX_ARIA_GRID_DEPTH = 20
_aria_grid_depth: ContextVar[int] = ContextVar("aria_grid_depth", default=0)


# `type` of a numbered list → number shape (`_lists.format_number`). Case-sensitive.
_LIST_NUMBER_SHAPES = {"a": "LATIN_SMALL", "A": "LATIN_CAPITAL", "i": "ROMAN_SMALL", "I": "ROMAN_CAPITAL"}


_BLANK_LINES = re.compile(r"\n{2,}")
# Characters a browser removes from anywhere in an address before reading it (the URL standard: ASCII tab or newline).
_ADDRESS_BREAKS = re.compile("[\t\n\r]")
# Placeholder for a nested list detached while its list item is rendered (private-use characters — clean_text removes them
# if they are left in the body). A key of private-use characters drawn once per process sits after the opening character, so
# text of the same shape written in the document (`\ue0240\ue025`, or the entity `&#xe024;`) is never taken for a placeholder:
# it pointed at a list that did not exist and rejected the whole document with an IndexError
# (Pinned by: test_core_html.py::TestTextShapedLikeAListPlaceholder). The key never reaches the output — a placeholder left in
# the body is private-use characters around a number, as before.
_LIST_SLOT_OPEN = "\ue024" + "".join(chr(0xE100 + byte) for byte in secrets.token_bytes(8))
_LIST_SLOT_CLOSE = "\ue025"
_LIST_SLOT = re.compile(f"{re.escape(_LIST_SLOT_OPEN)}(\\d+){_LIST_SLOT_CLOSE}")
# Maximum list indentation depth. Deeper lists are folded onto one line (a list 14,000 levels deep was rejected with a
# recursion-limit error) (Pinned by: test_text_html.py::TestDeepNesting).
_MAX_LIST_DEPTH = 20
# How many lists are being drawn inside one another (`_render_list`).
_list_nesting: ContextVar[int] = ContextVar("list_nesting", default=0)
# Maximum depth of stacked quote marks (`> `). Deeper quote blocks are only separated as blocks. The parser nests an unclosed
# `<blockquote>` one level per occurrence, and prefixing `> ` to the whole inner text again at every level is quadratic in the
# depth (4,500 levels, 58KB took 48 s and 2.9GB) (Pinned by: test_text_html.py::TestMalformedNesting).
_MAX_QUOTE_DEPTH = 20


# Maximum nesting depth at which a layout table is unfolded into blocks. Deeper layout tables are folded onto one line inside
# the cell like data tables (recursion limit).
_MAX_LAYOUT_DEPTH = 20
_layout_depth: ContextVar[int] = ContextVar("layout_depth", default=0)
# Maximum nesting depth at which a table inside a cell is handed over as a one-line HTML table. Deeper tables are flattened
# to their text on one line (recursion limit).
_MAX_EMBED_DEPTH = 20
_embed_depth: ContextVar[int] = ContextVar("embed_depth", default=0)


# Whether the text of a table cell is being rendered (`_cell_text`). Headings and code blocks inside a cell stay as lines,
# without the `#`/``` marks.
_in_cell: ContextVar[bool] = ContextVar("in_cell", default=False)


# Whether the text of a heading or table caption is being rendered: a title keeps its links but is not emphasized (`_title_text`).
_in_heading: ContextVar[bool] = ContextVar("in_heading", default=False)


_MAX_INDENT_RANK = 5
_NUMBER_TEXT = re.compile(r"[-+−▲▼△(]?\s*[₩$€¥£]?\s*\d[\d,]*(?:\.\d+)?\s*%?\)?")


# Definition lists (`_render_definitions`): maximum characters of a description kept on one `term: description` line, and the
# maximum nesting depth rendered as `term: description`.
_DEFINITION_CHARS = 200
_MAX_DEFINITION_DEPTH = 20
_definition_depth: ContextVar[int] = ContextVar("definition_depth", default=0)


_QUOTE_END = object()  # where a quote block ends on the stack of `_render_nodes`
_CODE_END = object()  # where an inline code element ends on the stack of `_render_nodes`
# Bold and italic elements (`ConvertOptions.keep_emphasis`). Deliberately only these four: `<cite>`, `<var>`, `<dfn>` and CSS
# `font-weight` / `font-style` are how a page is styled, and following them would mark navigation, captions and whole pages.
_EMPHASIS_MARKS = {"b": (BOLD_ON, BOLD_OFF), "strong": (BOLD_ON, BOLD_OFF), "i": (ITALIC_ON, ITALIC_OFF), "em": (ITALIC_ON, ITALIC_OFF)}
# Inline code. The text inside is not emphasized — `*` is a character of the code there.
_CODE_TAGS = {"code", "kbd", "samp", "tt"}
# Leaf blocks no link or emphasis span can hold (`_inline.BLOCK_INSIDE`). A heading is a single line and may sit inside a link.
_BLOCK_LEAVES = {"table", "pre", "ul", "ol"}


def _render_children(node: Tag, list_depth: int) -> str:
    return _render_nodes(node.contents, list_depth)


def _render(node: object, list_depth: int) -> str:
    return _render_nodes([node], list_depth)


def _render_nodes(nodes: list, list_depth: int) -> str:
    """Render the nodes in document order. Elements that only wrap (div, span, font, b …) and quote blocks are descended with a stack
    instead of recursion.

    Unclosed inline tags (a repeated `<font>line<br>`) are nested one level deeper per line by the parser. Recursing per element hit the
    recursion limit at around 330 levels and the document was rejected as a 'corrupted document'. Besides nodes, the stack holds the blank
    line for the spot where a block ends (str) and the spot where a quote block ends (`_QUOTE_END`)
    (Pinned by: test_text_html.py::TestDeepGenericNesting).

    Links and emphasis (`_inline`): `<a href>` and `<b>` / `<strong>` / `<i>` / `<em>` put an `InlineMark` before and after
    their content, and each frame is written out by `_inline.resolve_inline`, which decides what becomes `[text](url)` and
    `**bold**` (one block only, whitespace outside, a block that is bold throughout left unmarked …). The document tree is not
    touched, so everything that measures the text of an element (layout tables, name cells, folded answers) sees what it saw
    before. A span never reaches into a table, list, code block or quote block: those are rendered on their own, and
    `BLOCK_INSIDE` tells the resolver that a span around one is not written
    (Pinned by: test_inline_formatting.py::TestHtmlLinks, TestHtmlEmphasis).
    """
    inline = _inline.get()
    emphasis = inline is not None and inline.emphasis and not _in_heading.get()
    code_depth = 0  # inline code elements we are inside of: their text is not emphasized
    frames: list[list[str | InlineMark]] = [[]]  # the last one is where we are writing now. One is pushed per quote block
    stack: list[object] = list(reversed(nodes))

    while stack:
        node = stack.pop()
        if isinstance(node, NavigableString):
            frames[-1].append(_WHITESPACE.sub(" ", str(node)))
        elif node is _QUOTE_END:
            inner = _single_blank_lines(_frame_text(frames.pop(), inline).strip())
            quoted = "\n".join(f"> {line}" if line.strip() else ">" for line in inner.split("\n"))
            if inline is not None:
                frames[-1].append(BLOCK_INSIDE)
            frames[-1].append(f"{_BLOCK_GAP}{quoted}{_BLOCK_GAP}")
        elif node is _CODE_END:
            code_depth -= 1
        elif isinstance(node, (str, InlineMark)):
            frames[-1].append(node)
        elif isinstance(node, Tag):
            name = node.name
            definitions = _render_definitions(node) if name == "dl" else None
            if definitions is not None:
                if inline is not None:
                    frames[-1].append(BLOCK_INSIDE)
                frames[-1].append(definitions)
                continue
            if name in _LEAF_RENDERERS:
                if inline is not None and name in _BLOCK_LEAVES:
                    frames[-1].append(BLOCK_INSIDE)
                frames[-1].append(_LEAF_RENDERERS[name](node, list_depth))
                continue
            grid = _render_aria_grid(node) if _roles(node) & _GRID_ROLES and name not in _ARIA_SKIPPED_TAGS else None
            if grid is not None:
                if inline is not None:
                    frames[-1].append(BLOCK_INSIDE)
                frames[-1].append(f"\n\n{grid}\n\n")
                continue
            if name == "blockquote" and len(frames) <= _MAX_QUOTE_DEPTH:
                frames.append([])
                stack.append(_QUOTE_END)
            elif name in _BLOCK_TAGS or name == "li":  # a quote block past the limit is treated as a block too
                frames[-1].append(_BLOCK_GAP)
                stack.append(_BLOCK_GAP)
            elif inline is not None:
                if name == "a":
                    url = _link_address(node, inline)
                    if url is not None:
                        frames[-1].append(LINK_ON)
                        stack.append(link_off(url))
                elif name in _CODE_TAGS:
                    code_depth += 1
                    stack.append(_CODE_END)
                elif emphasis and not code_depth and name in _EMPHASIS_MARKS:
                    opening, closing = _EMPHASIS_MARKS[name]
                    frames[-1].append(opening)
                    stack.append(closing)
            stack.extend(reversed(node.contents))
    return _frame_text(frames[0], inline)


def _frame_text(frame: list[str | InlineMark], inline: _Inline | None) -> str:
    """The pieces of one frame of `_render_nodes` as text, with its link and emphasis spans written as Markdown."""
    return "".join(frame) if inline is None else resolve_inline(frame)


def _link_address(anchor: Tag, inline: _Inline) -> str | None:
    """The address `<a href>` is written with, or None when it stays text: links are not kept, the address is not an `http`,
    `https` or `mailto` one (relative addresses, `#fragment`, `javascript:` and empty ones — nothing is resolved against the page
    address, which a saved file does not have), or the element may be cut at the end of the front part of a large document."""
    if not inline.links or id(anchor) in inline.cut:
        return None
    address = anchor.get("href")
    # Tabs and line breaks in an address are removed first, as a browser does when it parses it: `https://e.com/\nfoo` was
    # written with `%0A` in it (Pinned by: test_core_html.py::TestLineBreaksInALinkAddress).
    return link_target(_ADDRESS_BREAKS.sub("", address)) if isinstance(address, str) else None


def _single_blank_lines(text: str) -> str:
    """Collapse consecutive blank lines into one. The inside of code blocks (``` fences) is left alone.

    A quote block prefixes every line of its inner text with `>`, so several blank lines at block boundaries (between `<div><br></div>`
    paragraphs) became 3–8 empty `>` lines that `clean_text` cannot collapse (1.6x the character count of a quoted Gmail reply;
    Pinned by: test_core_html_blocks.py::TestBlankLinesInsideAQuote).
    """
    lines: list[str] = []
    fenced = False

    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and not line.strip() and lines and not lines[-1].strip():
            continue
        lines.append(line)
    return "\n".join(lines)


def _roles(node: Tag) -> set[str]:
    return set(str(node.get("role", "")).lower().split())


def _render_aria_grid(node: Tag) -> str | None:
    """Draw a table built from ARIA roles (`role=grid|table|treegrid` > `row` > cells) as a grid, like a real table. Only when there are at
    least two rows and some row has at least two cells; otherwise None — it is descended as blocks as before.

    div tables with no `<table>` (AG Grid, list screens of in-house systems) were scattered into one paragraph per cell, and the columns
    shifted when a cell was empty (Pinned by: test_core_html.py::TestAriaGridTables). Rows are the nearest `row` descendants, cells the
    nearest cell descendants of that row, and an empty cell still counts as a cell. With `aria-colindex` (1-based) the cell goes in that
    column (cells left out by virtual scrolling). Spans (`aria-colspan`) are not considered.
    """
    if _aria_grid_depth.get() >= _MAX_ARIA_GRID_DEPTH:
        return None
    rows = _role_descendants(node, wanted={"row"})
    cells = [_role_descendants(row, wanted=_ARIA_CELL_ROLES) for row in rows]
    if len(rows) < 2 or max(map(len, cells)) < 2:
        return None

    grid = TableGrid()
    token = _aria_grid_depth.set(_aria_grid_depth.get() + 1)
    try:
        for row_index, row_cells in enumerate(cells):
            col = 0
            for cell in row_cells:
                index = decimal_int(str(cell.get("aria-colindex", "")).strip(), 0)
                if col < index <= col + _MAX_ARIA_COLUMN_SKIP:
                    col = index - 1
                grid.place(row_index, col, _cell_text(cell))
                col += 1
    finally:
        _aria_grid_depth.reset(token)

    omitted = _omitted_rows.get()
    rendered = grid.render()
    if omitted is not None:
        omitted.append(grid.omitted_rows())
    return rendered


def _role_descendants(node: Tag, *, wanted: set[str]) -> list[Tag]:
    """The nearest elements under `node` whose role is in `wanted`, in document order. Does not descend into a found element or into inner
    tables (`<table>`, ARIA tables) (with a stack, like `_nearest`)."""
    found: list[Tag] = []
    stack = list(reversed(node.contents))
    while stack:
        child = stack.pop()
        if not isinstance(child, Tag):
            continue
        roles = _roles(child)
        if roles & wanted:
            found.append(child)
        elif child.name != "table" and not roles & _GRID_ROLES:
            stack.extend(reversed(child.contents))
    return found


def _render_definitions(node: Tag) -> str | None:
    """Render a definition list (`<dl>`) as `term: description` lines. Several descriptions are joined with `; `. When a description is a
    block (multiple lines) or long (`_DEFINITION_CHARS`), the description blocks follow the term line. Terms and descriptions used to
    become separate paragraphs, blurring the pairing of '시행일' (effective date) and '2024-07-01'
    (Pinned by: test_text_html.py::TestDefinitionLists).

    None when there is text or an element outside dt / dd (and the divs wrapping them), when there is a folded answer (`display:none` dd —
    `_folded_answers`), or when the definition list is nested deeper than `_MAX_DEFINITION_DEPTH` levels — it is descended as blocks as
    before (question / answer paragraphs of a folded FAQ, the recursion limit in deep nesting).
    """
    entries = _definition_entries(node)
    if entries is None or _definition_depth.get() >= _MAX_DEFINITION_DEPTH:
        return None

    groups: list[tuple[list[str], list[str]]] = []  # (terms, descriptions). A term after a description opens a new group

    token = _definition_depth.set(_definition_depth.get() + 1)
    try:
        for entry in entries:
            text = _render_children(entry, list_depth=0).strip()
            if not groups or (entry.name == "dt" and groups[-1][1]):
                groups.append(([], []))
            if text:
                groups[-1][0 if entry.name == "dt" else 1].append(text)
    finally:
        _definition_depth.reset(token)

    blocks = []
    for terms, details in groups:
        inline = (len(terms) == 1 and details and "\n" not in terms[0] and not any("\n" in detail for detail in details)
                  and sum(map(len, details)) <= _DEFINITION_CHARS)
        if inline:
            separator = " " if terms[0].endswith((":", "：")) else ": "
            blocks.append(f"{terms[0]}{separator}{'; '.join(details)}")
        else:
            blocks.extend([*terms, *details])
    return f"{_BLOCK_GAP}{_BLOCK_GAP.join(blocks)}{_BLOCK_GAP}"


def _definition_entries(node: Tag) -> list[Tag] | None:
    """The dt / dd of a definition list in document order (including pairs wrapped in a div). None when there is other text or another
    element, a folded dd, or no dt."""
    entries: list[Tag] = []
    for child in node.contents:
        for item in (child.contents if isinstance(child, Tag) and child.name == "div" else [child]):
            if isinstance(item, NavigableString):
                if str(item).strip():
                    return None
            elif item.name not in ("dt", "dd") or (item.name == "dd" and _FOLDED_STYLE.search(str(item.get("style", "")))):
                return None
            else:
                entries.append(item)
    return entries if any(entry.name == "dt" for entry in entries) else None


def _title_text(node: Tag) -> str:
    """One-line text of a heading or a table caption (`_inline_text`). A title keeps its links but is not emphasized:
    `<h2><b>제목</b></h2>` is still just a heading, and headings and captions are often bold or italic as a whole
    (Pinned by: test_inline_formatting.py::TestHtmlEmphasis::test_heading_and_caption_are_not_emphasized)."""
    token = _in_heading.set(True)
    try:
        return _inline_text(node)
    finally:
        _in_heading.reset(token)


def _render_heading(node: Tag, list_depth: int) -> str:
    title = _title_text(node)
    if not title:
        return ""
    if _in_cell.get():
        return f"\n{title}\n"  # `## ` was left in the cell text (`_cell_text`)
    return f"\n\n{'#' * _HEADINGS[node.name]} {title}\n\n"


# A line inside code that starts with ```. The indentation is kept and a `\` is inserted before the first backtick.
_CODE_FENCE_LINE = re.compile(r"^([ \t]*)(?=```)", re.MULTILINE)
# Leading indentation of a code line inside a table cell (`_indent_entities`) and the number of columns one tab takes up.
_CODE_INDENT = re.compile(r"^[ \t]+(?=\S)", re.MULTILINE)
_TAB_WIDTH = 4


def _indent_entities(match: re.Match) -> str:
    """Turn the leading spaces / tabs of a code line inside a table cell into that many `&nbsp;`. Whitespace in cell text is trimmed and
    collapsed per line (`render_table`, `clean_text`), which destroyed the block structure of Python code (`def f():<br>if x:<br>return 1`).
    Deliberate: whitespace-like characters (NBSP, full-width space) are collapsed as well, so it is kept in HTML notation like the cell
    line break `<br>` (Pinned by: test_core_header_tables_cell_code_and_terminal_codes.py::TestIndentedCodeInsideATableCell)."""
    return "&nbsp;" * len(match.group().expandtabs(_TAB_WIDTH))


def _render_code_block(node: Tag, list_depth: int) -> str:
    """Render `<pre>` as a ``` code block. A ``` line inside the code is neutralized by inserting a backslash before the backticks.

    Deliberate: it is not wrapped in a longer fence (````). `clean_text` pairs fences by counting the lines that start with ``` regardless
    of fence length, so an inner ``` line looked like it closed the block, the whitespace of the code after it was collapsed, and the
    neutralizing of the unpaired line broke the outer closing fence
    (Pinned by: test_text_html.py::TestCodeBlockWithFenceInside). The neutralized notation is the same as `clean_text`'s unpaired fence.
    """
    places: _Places = {}
    for line_break in node.find_all("br"):
        _replace(line_break, "\n", places)  # it has no text, so it vanished in get_text and the lines ran together
    code = _CODE_FENCE_LINE.sub(r"\1\\", node.get_text().strip(chr(10)))
    if _in_cell.get():
        return f"\n{_CODE_INDENT.sub(_indent_entities, code)}\n"  # the fence was left in the cell text, mixed with `<br>` (`_cell_text`)
    return f"\n\n```\n{code}\n```\n\n"


# Elements that render themselves without being descended into. For elements not listed here `_render_nodes` descends into the children.
_LEAF_RENDERERS = {
    "br": lambda node, list_depth: "\n",
    "img": lambda node, list_depth: "",
    "table": lambda node, list_depth: f"\n\n{_render_table(node)}\n\n",
    "pre": _render_code_block,
    "ul": lambda node, list_depth: f"\n\n{_render_list(node, list_depth)}\n\n",
    "ol": lambda node, list_depth: f"\n\n{_render_list(node, list_depth)}\n\n",
    **dict.fromkeys(_HEADINGS, _render_heading),
}


def _render_list(node: Tag, list_depth: int) -> str:
    # The lists being drawn inside one another count too, not only the indentation: a list inside a quote block of an item is
    # drawn by the quote at the item's own depth, and `<ul><li>a<blockquote>` repeated 800 times (unclosed) hit the recursion
    # limit (Pinned by: test_core_html.py::TestListInsideAQuoteInsideAnItem::test_deeply_nested_quotes_and_lists_stay_bounded).
    if list_depth >= _MAX_LIST_DEPTH or _list_nesting.get() >= _MAX_LIST_DEPTH:
        return f"{'  ' * list_depth}- {' '.join(node.get_text(' ').split())}"
    token = _list_nesting.set(_list_nesting.get() + 1)
    try:
        return _render_list_items(node, list_depth)
    finally:
        _list_nesting.reset(token)


def _render_list_items(node: Tag, list_depth: int) -> str:
    lines = []
    ordered = node.name == "ol"
    entries = _list_entries(node)
    # Numbers follow the source: `start`, the counting-down `reversed` (starting at the item count), an item's `value`, and the letter /
    # Roman numeral `type` (Pinned by: test_text_html.py::TestOrderedListNumbers).
    step = -1 if ordered and node.has_attr("reversed") else 1
    number = _list_number(node.get("start"))
    if number is None:
        number = sum(1 for item in entries if isinstance(item, Tag) and item.name == "li") if step < 0 else 1
    shape = _LIST_NUMBER_SHAPES.get(str(node.get("type", "")).strip())
    # Items of a Confluence checklist (li class="checked") and a Notion to-do list (div.checkbox-on / checkbox-off in the li) carry their
    # done state as the same symbols as form checkboxes — the done state used to be lost
    # (Pinned by: test_core_html.py::TestConfluenceTaskList, test_core_html_blocks.py::TestNotionToDoList)
    classes = node.get_attribute_list("class")
    task_list = "inline-task-list" in classes
    to_do_list = "to-do-list" in classes
    for item in entries:
        if isinstance(item, str):
            lines.append(f"{'  ' * list_depth}- {item}")  # text outside the items. No number is written
            continue
        if item.name in {"ul", "ol"}:
            lines.append(_render_list(item, list_depth + 1))
            continue
        if item.name != "li":
            lines.append(f"\n{_render(item, list_depth).strip()}\n")  # a table or code block outside the items
            continue

        # Nested lists belonging to this item: not only direct children but also ones wrapped in div / p. Missing a wrapped one merges it
        # into the item text as the single line "- 하위 1 - 하위 2" (sub 1, sub 2). Lists of deeper items and lists inside table cells are
        # handled when those are rendered.
        # Each nested list is detached leaving a placeholder in its place, the item text is drawn, and the text is then split at the
        # placeholders to put the lists back in source order.
        # The detached lists are restored after drawing — a table with tables inside its cells draws the same cell twice
        # (`_render_table`), and the second drawing saw only the placeholder text and rejected the whole document with an IndexError
        # (Pinned by: test_core_html.py::TestNestedListInsideATableCell).
        # A list inside a quote block of the item stays in the quote, which draws it (Pinned by:
        # test_core_html.py::TestListInsideAQuoteInsideAnItem).
        nested = _nearest(item, wanted={"ul", "ol"}, stop={"li", "table", "blockquote"})
        slots = [NavigableString(f"{_LIST_SLOT_OPEN}{index}{_LIST_SLOT_CLOSE}") for index in range(len(nested))]
        # Swapped through `_replace` (one position lookup per parent): `replace_with` per list took 6.4 seconds for one item
        # with 20,000 lists (Pinned by: test_audit_html_text.py::TestManyListsOrTablesSetAsideWhileTheirParentIsRendered)
        places: _Places = {}
        for child, slot in zip(nested, slots, strict=True):
            _replace(child, slot, places)
        try:
            pieces = _item_pieces(_item_blocks(_render_children(item, list_depth)), len(nested))
        finally:
            for child, slot in zip(nested, slots, strict=True):
                _replace(slot, child, places)
        # A code block or table opens the item on the line below. Text that only starts like one (`| a | b |`, `` ```js ``) is a
        # single line and stays on the bullet line, a fence escaped — it was moved under an empty bullet
        # (Pinned by: test_core_html.py::TestItemTextStartingLikeABlock).
        first = pieces.pop(0) if pieces and isinstance(pieces[0], str) and not _is_drawn_block(pieces[0]) else ""
        if first.startswith("```"):
            first = "\\" + first

        # Markdown numbered lists use digits only. A letter number is written after a bullet — it has to stay a list line for the nested
        # indentation to be kept.
        marker = "-"
        if ordered:
            number = _list_number(item.get("value"), number)
            marker = f"- {format_number(number, shape)}." if shape else f"{number}."
        if task_list:
            marker += f" {_CHECKED if 'checked' in item.get_attribute_list('class') else _UNCHECKED}"
        elif to_do_list and (box := item.find(class_=["checkbox-on", "checkbox-off"], recursive=False)) is not None:
            marker += f" {_CHECKED if 'checkbox-on' in box.get_attribute_list('class') else _UNCHECKED}"
        lines.append(_item_text(f"{'  ' * list_depth}{marker} {first}".rstrip(), pieces, nested, list_depth))
        number += step
    return "\n".join(line for line in lines if line.strip()).rstrip("\n")  # leading whitespace is the indentation of nested lists


def _is_drawn_block(block: str) -> bool:
    """Is this item block a code block or table (`_item_blocks`), which takes more than one line, not a line of text that starts
    with a fence or `|`."""
    return block.startswith(("```", "|")) and "\n" in block


def _item_pieces(blocks: list[str], nested_count: int) -> list[str | int]:
    """Split the item blocks at the nested-list placeholders into text blocks (str) and nested-list indices (int), in source order. A
    placeholder inside a code block or table does not split the block; it goes after that block. Lists whose placeholder vanished while
    drawing go last (the old order)."""
    pieces: list[str | int] = []

    for block in blocks:
        if _is_drawn_block(block):
            pieces.append(_LIST_SLOT.sub("", block))
            pieces.extend(int(found) for found in _LIST_SLOT.findall(block))
            continue
        for position, part in enumerate(_LIST_SLOT.split(block)):
            if position % 2:
                pieces.append(int(part))
            elif part.strip():
                pieces.append(part.strip())

    seen = {piece for piece in pieces if isinstance(piece, int)}
    pieces.extend(index for index in range(nested_count) if index not in seen)
    return pieces


def _item_text(line: str, pieces: list[str | int], nested: list[Tag], list_depth: int) -> str:
    """Append the text blocks and nested lists after the item line, in source order.

    Previously, after the nested lists were detached, all the remaining text was drawn as the item line and blocks and the nested lists
    were appended last, so text after a nested list ('작성 후 서명합니다.' — "fill in, then sign.") moved ahead of the sub-items
    (Pinned by: test_core_text_and_mail.py::TestTextAfterANestedList). A second paragraph, code block or table inside an item is placed
    under the item as a block set off by a blank line — merged into one line, a multi-line command gets joined with spaces. A nested list
    goes on the line right below an item line or a list, and after a blank line when it follows a block. An item that ends with a block is
    set off from the next item by a blank line.
    """
    text, previous = line, "line"

    for piece in pieces:
        if isinstance(piece, int):
            rendered = _render_list(nested[piece], list_depth + 1)
            if not rendered.strip():
                continue
            text += ("\n" if previous in ("line", "list") else "\n\n") + rendered
            previous = "list"
        else:
            text += "\n\n" + piece
            previous = "block"
    return text + "\n" if previous == "block" else text


def _list_entries(node: Tag) -> list[Tag | str]:
    """The contents of a list in document order: items (li), nested lists / tables / code blocks placed directly outside the items, and
    text outside the items (consecutive text is one chunk).

    Reading only the direct `<li>` children silently lost the nested list in `<ul><li>상위</li><ul>…</ul></ul>` (상위 = parent), items
    wrapped in `<div>`, and text outside the items (common in old editors and hand-written HTML). Wrapping elements are unwrapped. A stack
    is used instead of recursion (same reason as `_nearest`)
    (Pinned by: test_text_html.py::TestListContentOutsideItems).
    """
    entries: list[Tag | str] = []
    loose: list[str] = []

    def flush() -> None:
        text = " ".join("".join(loose).split())
        loose.clear()
        if text:
            entries.append(text)

    stack: list[object] = list(reversed(node.contents))
    while stack:
        child = stack.pop()
        if child is None:  # the spot where a block ended. Break here so it does not join the text that follows
            flush()
        elif isinstance(child, NavigableString):
            loose.append(str(child))
        elif isinstance(child, Tag):
            if child.name in {"li", "ul", "ol", "table", "pre"}:
                flush()
                entries.append(child)
            else:
                if child.name in _BLOCK_TAGS or child.name == "br":
                    flush()
                    stack.append(None)
                stack.extend(reversed(child.contents))
    flush()
    return entries


def _split_at_fenced(text: str) -> list[str]:
    """The text split at its code blocks (from a "```" + newline to the next newline + "```"): text, block, text, block …, text
    — what splitting with the lazy pattern `(```\\n.*?\\n```)` gave. The pattern read on to the end of the text for every
    opening that has no closing: 20,000 of them in one list item (156KB) took 6 seconds. Once an opening finds no closing
    after it, no later one can (Pinned by: test_audit_html_text.py::TestListItemWithManyUnpairedFenceLines).
    """
    pieces: list[str] = []
    position = 0

    while True:
        start = text.find("```\n", position)
        end = text.find("\n```", start + 4) if start >= 0 else -1
        if end < 0:
            break
        pieces += [text[position:start], text[start:end + 4]]
        position = end + 4
    pieces.append(text[position:])
    return pieces


def _item_blocks(text: str) -> list[str]:
    """Split a list item's content into blocks. Code blocks, tables and quote blocks (every line starting with `>`) keep their
    line structure; in other paragraphs whitespace is collapsed to single spaces. A quote block in an item was folded onto one
    line, its nested list and all (`> - b > - c`) (Pinned by: test_core_html.py::TestListInsideAQuoteInsideAnItem)."""
    blocks = []
    for part in _split_at_fenced(text):
        if part.startswith("```"):
            blocks.append(part)
            continue
        for paragraph in _BLANK_LINES.split(part):
            if paragraph.strip():
                kept = paragraph.lstrip().startswith("|") or all(line.lstrip().startswith(">") for line in paragraph.strip().split("\n"))
                blocks.append(paragraph.strip() if kept else " ".join(paragraph.split()))
    return blocks


def _render_table(table: Tag) -> str:
    caption = _own_caption(table)
    caption_text = _title_text(caption) if caption else ""
    rows = _table_rows(table)
    if (_is_one_column_box(rows) or _is_layout_table(rows)) and _layout_depth.get() < _MAX_LAYOUT_DEPTH:
        rendered = _render_layout_table(rows)
        return f"{caption_text}\n\n{rendered}" if caption_text else rendered
    header_count = _header_row_count(table, rows)
    marks = _indent_marks(rows, header_count)

    grid, header, placed_rows = _table_grid(rows, header_count, embedding=False, marks=marks)
    titles = _header_titles(grid.rows(), rows, header_count, placed_rows) if header_count > 1 else []
    if header_count - len(titles) <= 1 < header_count:
        # When the header rows are just a title row + one row, there is nothing to merge. The grid drawing (`render_table`, `TableGrid`)
        # moves the title out as a paragraph above the table.
        header_count = 1
    if header_count > 1:
        # Join a multi-row header ("요금" (rate) above "정가" / "할인가" (list price / discount price)) top to bottom per column into one
        # row, so the LLM reads each column's meaning in one go.
        # In this case it is a Markdown table (horizontal spans in the body go in the first cell only).
        # The table title rows at the very top (`_header_titles`) are not merged but moved out as paragraphs above the table — the title
        # used to be attached to every column name
        # (Pinned by: test_text_html.py::TestTitleRowInsideMultiRowHeader).
        columns = zip(*header.rows()[len(titles):], strict=False)
        merged = [" ".join(dict.fromkeys(value for value in column if value)) for column in columns]
        # The grid is built without the rows that have no cells at all (an empty tr in thead). The header rows must be counted on the grid
        # as well, or the first data row gets cut off with them
        # (Pinned by: test_quality.py::test_html_empty_header_row_does_not_swallow_the_first_data_row).
        header_rows = sum(1 for index in range(header_count) if index in placed_rows)
        rendered = "\n\n".join([*titles, render_table([merged, *grid.rows()[header_rows:]])])
        drawn = grid
    else:
        headless = header_count == 0 and _names_values_in_pairs(rows)
        rendered, drawn = grid.render(headless=headless), grid  # an HTML table if horizontal spans remain (markdown.TableGrid)
        if any(_inner_tables(cell) for row in rows for cell in _row_cells(row)) and (
                _is_html_table(rendered) or _mostly_nested_tables(rows)):
            # If it is already an HTML table, tables inside cells are kept as one-line HTML tables (`TableGrid.embed`, same shape as
            # HWP / HWPX / DOCX). A Markdown table cannot hold a table in a cell, so the whole outer table would have to become HTML;
            # it is flattened into cell text as before instead — the shape of infoboxes and wiki tables is not changed. But in a
            # name | value table whose text is at least half inner tables (`_is_layout_table`) the inner table is the main content and
            # flattening it into one line loses most of the rows and columns, so it is switched to an HTML table
            # (Pinned by: test_text_html.py::TestLayoutTables::
            # test_tables_inside_the_cells_of_an_html_form_keep_their_rows_and_columns,
            # test_data_table_with_a_small_nested_table_in_one_cell_stays_a_table,
            # test_name_value_table_whose_text_is_mostly_a_nested_table_keeps_both_structures).
            drawn = _table_grid(rows, header_count, embedding=True, marks=marks)[0]
            rendered = drawn.render(headless=headless)

    # Rows dropped by the grid cell limit are warned about once at the end of the document, with the same wording as the other formats
    # (`_notation.omitted_rows_warning`) — the lower rows of an 'Excel download' HTML with very many rows vanished without a warning
    # (Pinned by: test_core_html.py::TestRowsLeftOutByTheGridLimit).
    omitted = _omitted_rows.get()
    if omitted is not None:
        omitted.append(drawn.omitted_rows())
    return f"{caption_text}\n\n{rendered}" if caption_text else rendered


def _header_titles(grid_rows: list[list[str]], rows: list[Tag], header_count: int, placed_rows: set[int]) -> list[str]:
    """Text of the table title rows at the top of a multi-row header. Only rows without vertical spans are considered, and the decision is
    `markdown.table_title` (same rules as grid drawing: a row with a single filled cell, a unit / basis note row, and in a two-column
    table only when what is below is a header or numbers). The last header row is never a title.
    Deliberate: in a two-column table only multi-word text is a title — a single word ('요금', rate) is merged as the parent header of
    '정가 | 할인가' (list price | discount price)
    (Pinned by: test_text_html.py::TestLayoutTables::test_name_value_table_whose_nested_table_has_a_two_row_header_stays_a_layout,
    TestTitleRowInsideMultiRowHeader::test_two_column_table_with_a_spanning_title_row)."""
    placed = [index for index in range(len(rows)) if index in placed_rows]  # grid row position → table row index
    keep = [col for col, column in enumerate(zip(*grid_rows, strict=False)) if any(cell.strip() for cell in column)]
    titles: list[str] = []

    for position, index in enumerate(placed[:min(header_count - 1, len(grid_rows) - 1)]):
        if index >= header_count - 1 or any(_row_span(cell, rows, index) > 1 for cell in _row_cells(rows[index])):
            break
        below = [[row[col] for col in keep] for row in grid_rows[position + 1:position + 2 + _HEADER_PROBE_ROWS]]
        title = table_title([grid_rows[position][col] for col in keep], below)
        if title is None or (len(keep) == 2 and " " not in title.strip()):
            break  # a one-word cell ('요금') in a two-column table is the parent header of the two cells below ('정가', '할인가'), so it is merged
        titles.append(title)
    return titles


def _indent_marks(rows: list[Tag], header_count: int) -> dict[int, str]:
    """First-column cell of a body row (`id`) → indent level mark (`└ `, `└─ `, `_lists.cell_level_mark` — the same marks as list levels
    inside a table cell).

    Financial statements and budgets show sub-accounts by indenting with `&nbsp;` or padding, but leading whitespace in cell text is
    stripped when the table is built, so '매출원가' (cost of sales) and its sub-account '재료비' (materials) read as the same level. The
    leading `&nbsp;` / full-width spaces and the inline CSS indentation (padding-left, text-indent, margin-left) of the first-column cells
    are measured, and only cells deeper than the shallowest cell in that column are marked, by depth rank — a table that gives every cell
    the same padding is unchanged. Cells that look like numbers are not touched
    (Pinned by: test_text_html.py::TestIndentedLabelsInTables).

    A one-column table, a cell spanning all columns and a cell holding blocks (a heading, a list, two or more paragraphs) are layout
    boxes, not label cells, so they are not measured — a mail body cell that differed only in padding became '└ ## 결재 요청 알림'
    (Pinned by: test_core_html_layout_cells.py::TestIndentMarksOnlyForLabelCells).
    """
    width = max((sum(_span(cell, "colspan") for cell in _row_cells(row)) for row in rows), default=0)
    if width < 2:
        return {}
    depths: dict[int, float] = {}
    covered_until = 0  # last row whose first column is covered by a rowspan from a row above, + 1

    for index, row in enumerate(rows):
        cells = _row_cells(row)
        if not cells or index < covered_until:
            continue
        first = cells[0]
        covered_until = index + _row_span(first, rows, index)
        if index < max(header_count, 1) or _span(first, "colspan") >= width or _holds_blocks(first):
            continue  # with no header row the first row is the header (`render_table`)
        text = " ".join(first.get_text(" ").split())
        if text and not _NUMBER_TEXT.fullmatch(text):
            depths[id(first)] = _indent_depth(first)
    if not depths:
        return {}

    base = min(depths.values())
    deeper = sorted({round(depth - base) for depth in depths.values() if depth - base >= 2})
    ranks = {depth: rank for rank, depth in enumerate(deeper, start=1)}
    return {key: cell_level_mark(min(ranks[round(depth - base)], _MAX_INDENT_RANK))
            for key, depth in depths.items() if round(depth - base) in ranks}


def _table_grid(rows: list[Tag], header_count: int, *, embedding: bool,
                marks: dict[int, str] | None = None) -> tuple[TableGrid, TableGrid, set[int]]:
    """The table's grid, a grid of the header rows only (horizontally spanned cells filled in as well), and the indices of the rows that
    have cells placed in them. With `embedding`, tables inside cells are handed to the grid.
    `marks` maps a cell (`id`) → the indent level mark to put before the cell text (`_indent_marks`)."""
    grid = TableGrid()
    header = TableGrid()
    placed_rows: set[int] = set()
    for row_index, row in enumerate(rows):
        for cell in _row_cells(row):
            # A rowspan longer than the table's remaining rows is cut at the end of the table, as browsers do. `rowspan=99` produced 97
            # empty rows (Pinned by: test_text_html.py::test_html_rowspan_longer_than_the_table_does_not_add_empty_rows).
            text, col_span = _cell_text(cell, embed_into=grid if embedding else None), _span(cell, "colspan")
            if marks and id(cell) in marks and text:
                text = marks[id(cell)] + text
            row_span = min(_row_span(cell, rows, row_index), len(rows) - row_index)
            grid.place_next(row_index, text, row_span=row_span, col_span=col_span)
            placed_rows.update(range(row_index, row_index + row_span))
            if row_index < header_count:
                header.place_next(row_index, text, row_span=min(row_span, header_count - row_index), col_span=col_span,
                                  fill_horizontal=True)
    return grid, header, placed_rows


def _embedded_grid(table: Tag) -> TableGrid | None:
    """Grid of a table to be handed to a cell. None for what cannot be drawn as a one-line HTML table inside the outer cell (caption,
    multi-row header, layout table, deep nesting) — only the text is flattened into one line as before."""
    rows = _table_rows(table)
    if _embed_depth.get() >= _MAX_EMBED_DEPTH or not _has_one_row_header(table, rows) or _is_layout_table(rows):
        return None
    header_count = _header_row_count(table, rows)

    token = _embed_depth.set(_embed_depth.get() + 1)
    try:
        return _table_grid(rows, header_count, embedding=True)[0]
    finally:
        _embed_depth.reset(token)


def _render_layout_table(rows: list[Tag]) -> str:
    """Layout table: flatten the cells into blocks in top-to-bottom, left-to-right order; inner tables are each rendered as tables."""
    token = _layout_depth.set(_layout_depth.get() + 1)
    try:
        cells = [_render_children(cell, list_depth=0).strip()
                 for row in rows for cell in row.find_all(["td", "th"], recursive=False)]
    finally:
        _layout_depth.reset(token)
    return "\n\n".join(cell for cell in cells if cell)


def _cell_text(cell: Tag, *, embed_into: TableGrid | None = None) -> str:
    """Text of a cell. A line break inside a cell (`<br>`) is emitted by `_render` as a newline and written as `<br>` by `render_table`.
    Turning it into a newline character here beforehand lets the whitespace cleanup of text nodes collapse it into a space
    (`A-100<br>B-200` → `A-100 B-200`)
    (Pinned by: test_text_html.py::TestHtmlInlineRendering::test_line_break_inside_a_table_cell_is_kept).

    Headings and code blocks inside a cell (`<h2>`, `<pre>`) are kept as text and line breaks, without the `#` / ``` marks (`_in_cell`).
    Left with their marks they became `| ## 칸제목 |` (칸제목 = cell heading) and ```` ```<br>x=1 ````
    (Pinned by: test_core_word_lists_cell_blocks_and_mail_lines.py::TestHeadingAndCodeInsideATableCell).

    A table inside the cell becomes a placeholder handed to that grid when `embed_into` is given; when it is not given, or the table
    cannot be handed over, only its text is kept on one line. The document tree is restored after drawing — it must be possible to draw
    the same cell twice (flattened, and embedded) (`_render_table`).
    """
    stand_ins: list[tuple[NavigableString, Tag]] = []
    places: _Places = {}  # swapped through `_replace`, like the nested lists of a list item (`_render_list`)
    for nested in _nearest(cell, wanted={"table"}, stop=set()):
        grid = _embedded_grid(nested) if embed_into is not None and len(_table_rows(nested)) > 1 else None
        placeholder = embed_into.embed(grid) if embed_into is not None and grid is not None else ""
        stand_in = NavigableString(placeholder or _flat_table_text(nested))
        _replace(nested, stand_in, places)
        stand_ins.append((stand_in, nested))
    token = _in_cell.set(True)
    try:
        return _render_children(cell, list_depth=0).strip()
    finally:
        _in_cell.reset(token)
        for stand_in, nested in stand_ins:
            _replace(stand_in, nested, places)


def _flat_table_text(table: Tag) -> str:
    """Turn an inner table that cannot be handed to the cell into one line of text: the caption and every cell, rendered inline the same
    way as a heading (`_inline_text`), joined with spaces.

    `get_text(" ")` inserts a space at every text node, so `매출 1<b>2</b>,345원` became `매출 1 2 ,345원` and `CO<sub>2</sub>` became
    `CO _2` (Pinned by: test_core_html_layout_cells.py::TestNestedTableFlattenedIntoACell).
    """
    caption = table.find("caption", recursive=False)
    parts = [_inline_text(caption)] if caption is not None else []
    parts.extend(_inline_text(cell) if cell.find([*_HEADINGS, "blockquote"]) is None else " ".join(cell.get_text(" ").split())
                 for row in _table_rows(table) for cell in _row_cells(row))  # keeps heading / quote marks (`#`, `>`) out of the one line
    return " ".join(part for part in parts if part)


def _inline_text(node: Tag) -> str:
    """Text of a heading or table caption on one line. Uses the same inline rendering as body paragraphs — inserting a space at every text
    node split words and scripts at inline tags (`H<sub>2</sub>O` → `H _2 O`, `인사<em>발령</em>` → `인사 발령`). For a heading that
    holds a table, list or code block only the text is collected, so that Markdown syntax does not get mixed into the one line
    (Pinned by: test_text_html.py::TestHtmlInlineRendering::test_heading_and_caption_use_the_same_inline_rendering_as_paragraphs)."""
    if node.find(["table", "ul", "ol", "pre"]) is not None:
        return " ".join(node.get_text(" ").split())
    return " ".join(_render_children(node, list_depth=0).split())
