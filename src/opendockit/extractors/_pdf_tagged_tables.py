"""Tables a tagged PDF states itself: the Table elements of its structure tree (ISO 32000-1, 14.7 and 14.8), which name
the rows and the cells of a table and the marked content each cell holds. Word and LibreOffice write the tree by
default; where a page has one, a table set without any rule is read from it and not guessed from its alignment.
"""

from dataclasses import dataclass, field

from pdfminer.pdftypes import PDFObjRef, resolve1

from opendockit.extractors._pdf_chars import _text_from_chars
from opendockit.extractors._pdf_model import _TABLE_ROW_TOLERANCE, _original_lines_of, _Page, _runs_on, _Table
from opendockit.markdown import render_table


# How much of a structure tree is looked at in one document: elements, how deep they nest, and the cells of one table. A
# tree past them is left, and the document is read as one without tags.
_MAX_ELEMENTS = 200_000
_MAX_DEPTH = 64
_MAX_TABLE_CELLS = 20_000
_MAX_SPAN = 200
# A standard type is looked up through this many entries of the role map at most (a map may name itself).
_ROLE_STEPS = 8
_ROW_GROUPS = frozenset(("THead", "TBody", "TFoot"))
_CELLS = frozenset(("TH", "TD"))
# The elements that are a paragraph of their own in a cell, beside P: an item of a list and a heading
_BLOCKS = frozenset(("LI", "H", "H1", "H2", "H3", "H4", "H5", "H6"))
# How far (pt) the boxes of two cells of a row may reach into each other, a row may start above the row before it, and a
# line of the page may lie outside the cell that holds it
_OVERLAP_TOLERANCE = 2.0


@dataclass(slots=True)
class _TaggedCell:
    """A cell as the tree states it: the marked content of each paragraph in it, and how many rows and columns it spans."""

    paragraphs: list[list[int]] = field(default_factory=list)
    row_span: int = 1
    col_span: int = 1


@dataclass(slots=True)
class _TaggedTable:
    """The rows of a tagged table on one page: per cell its text, its box (x0, top, x1, bottom; None without text) and its
    spans (`_page_tagged_tables`)."""

    rows: list[list[tuple[str, tuple[float, float, float, float] | None, int, int]]]


class _Untrusted(Exception):
    """The tree is not one a table can be read from (too large, nested too deep, a table in a cell, a cell over several
    rows in a row cut by the end of a page)."""


def _tagged_tables(document: object) -> dict[int, list[list[list[_TaggedCell]]]]:
    """Page object id → the tables the structure tree of `document` (a pdfminer `PDFDocument`) states on that page, each a
    list of rows of cells. Empty for a document without a tree, or with one that cannot be read within the bounds.

    The tree is walked once for the document, each element once (`_MAX_ELEMENTS`, `_MAX_DEPTH`; an element is known by
    its object id, so a tree that refers back into itself ends). A table whose rows lie on several pages gives the rows
    of each page as a table of that page. Left out, with the table read as without tags: a table with a table in one of
    its cells, a row cut by the end of a page that holds a cell over several rows, a span of more than `_MAX_SPAN`. A
    row the end of a page cuts in two is a row of each of the two pages, with what each cell holds there.
    Pinned by: test_pdf_tagged_tables.py::TestTheTree.
    """
    found: dict[int, list[list[list[_TaggedCell]]]] = {}
    try:
        catalog = document.catalog  # type: ignore[attr-defined]
        root = resolve1(catalog.get("StructTreeRoot"))
        if not isinstance(root, dict):
            return found
        roles = resolve1(root.get("RoleMap"))
        walk = _Walk(roles if isinstance(roles, dict) else {})
        stack: list[tuple[object, int, int | None]] = [(kid, 0, None) for kid in reversed(_kids(root))]
        while stack:
            kid, depth, page = stack.pop()
            element = walk.element(kid, depth)
            if element is None:
                continue
            page = _page_of(element, page)
            if walk.type_of(element) == "Table":
                try:
                    for table_page, rows in walk.table(element, depth, page).items():
                        found.setdefault(table_page, []).append(rows)
                except _Untrusted:
                    pass  # this table is read from the page; the others are still the tree's
                continue
            stack.extend((child, depth + 1, page) for child in reversed(_kids(element)))
    except _Untrusted:
        return {}
    except Exception:  # noqa: BLE001 — a tree that cannot be parsed is no tree; the pages are read as without tags
        return {}
    return found


def _kids(element: dict) -> list[object]:
    kids = resolve1(element.get("K"))
    return kids if isinstance(kids, list) else [] if kids is None else [kids]


def _page_of(element: dict, inherited: int | None) -> int | None:
    page = element.get("Pg")
    return page.objid if isinstance(page, PDFObjRef) else inherited


def _name(value: object) -> str:
    name = getattr(value, "name", value)
    if isinstance(name, bytes):
        return name.decode("latin-1")
    return name if isinstance(name, str) else ""


class _Walk:
    """The reading of one structure tree: the elements seen so far, counted against the bounds."""

    def __init__(self, roles: dict) -> None:
        self._roles = roles
        self._seen: set[int] = set()
        self._count = 0

    def element(self, kid: object, depth: int) -> dict | None:
        """The structure element `kid` stands for, or None for marked content, an object reference, and an element met
        before."""
        if depth > _MAX_DEPTH:
            raise _Untrusted
        if isinstance(kid, PDFObjRef):
            if kid.objid in self._seen:
                return None
            self._seen.add(kid.objid)
            kid = resolve1(kid)
        self._count += 1
        if self._count > _MAX_ELEMENTS:
            raise _Untrusted
        if not isinstance(kid, dict) or _name(kid.get("Type")) in ("MCR", "OBJR") or "S" not in kid:
            return None
        return kid

    def type_of(self, element: dict) -> str:
        """The standard structure type of an element, through the role map."""
        name = _name(element.get("S"))
        for _ in range(_ROLE_STEPS):
            mapped = self._roles.get(name)
            if mapped is None or _name(mapped) == name:
                break
            name = _name(mapped)
        return name

    def table(self, table: dict, depth: int, page: int | None) -> dict[int, list[list[_TaggedCell]]]:
        """Page → the rows of `table` that lie on it."""
        rows: dict[int, list[list[_TaggedCell]]] = {}
        cells = 0
        pending = [(kid, page) for kid in _kids(table)]
        position = 0
        while position < len(pending):  # the row groups (THead, TBody, TFoot) are opened in place, in order
            kid, inherited = pending[position]
            position += 1
            element = self.element(kid, depth + 1)
            if element is None:
                continue
            kind, inherited = self.type_of(element), _page_of(element, inherited)
            if kind in _ROW_GROUPS:
                pending[position:position] = [(child, inherited) for child in _kids(element)]
                continue
            if kind != "TR":
                continue  # a caption is body text, read from the page
            row: list[tuple[list[list[tuple[int | None, int]]], int, int]] = []
            pages: dict[int, None] = {}  # the pages the content of the row lies on, in the order of the tree
            for child in _kids(element):
                cell = self.element(child, depth + 2)
                if cell is None or self.type_of(cell) not in _CELLS:
                    continue
                row.append(self._cell(cell, depth + 2, _page_of(cell, inherited), pages))
                cells += 1
                if cells > _MAX_TABLE_CELLS:
                    raise _Untrusted
            if len(pages) > 1 and any(row_span > 1 for _, row_span, _ in row):
                raise _Untrusted  # a cell over several rows, in a row cut by the end of a page
            # A row the end of a page cuts in two is a row on each of the two pages: what each cell holds there
            # (content whose page the tree does not say is that of the first of them, as it was with one page)
            for number, page in enumerate(pages if row else ()):
                rows.setdefault(page, []).append([
                    _TaggedCell([contents for paragraph in paragraphs
                                 if (contents := [content for place, content in paragraph
                                                  if place == page or (place is None and number == 0)])],
                                row_span, col_span)
                    for paragraphs, row_span, col_span in row])
        return rows

    def _cell(self, cell: dict, depth: int, page: int | None,
              pages: dict[int, None]) -> tuple[list[list[tuple[int | None, int]]], int, int]:
        """The paragraphs of a cell, each the marked content it holds as (page, content id), and its spans."""
        row_span, col_span = _spans(cell)
        paragraphs: list[list[tuple[int | None, int]]] = [[]]
        for kid in _kids(cell):
            self._content(kid, depth + 1, page, paragraphs, pages, in_item=False)
        return [paragraph for paragraph in paragraphs if paragraph], row_span, col_span

    def _content(self, kid: object, depth: int, page: int | None, into: list[list[tuple[int | None, int]]],
                 pages: dict[int, None], *, in_item: bool) -> None:
        """Add the marked content under `kid` to the paragraphs `into`, in order. A paragraph, a heading and an item of
        a list each start one and end it; the paragraph an item of a list holds is the item itself, after its label."""
        if isinstance(kid, int):
            into[-1].append((page, kid))
            if page is not None:
                pages[page] = None
            return
        if depth > _MAX_DEPTH:
            raise _Untrusted
        if isinstance(kid, PDFObjRef):
            if kid.objid in self._seen:
                return
            self._seen.add(kid.objid)
            kid = resolve1(kid)
        self._count += 1
        if self._count > _MAX_ELEMENTS:
            raise _Untrusted
        if not isinstance(kid, dict):
            return
        page = _page_of(kid, page)
        if _name(kid.get("Type")) == "MCR":
            content = resolve1(kid.get("MCID"))
            if isinstance(content, int) and "Stm" not in kid:  # content of the page itself, not of a form drawn on it
                into[-1].append((page, content))
                if page is not None:
                    pages[page] = None
            return
        if "S" not in kid:
            return
        kind = self.type_of(kid)
        if kind == "Table":
            raise _Untrusted
        block = kind in _BLOCKS or (kind == "P" and not in_item)
        if block and into[-1]:
            into.append([])
        for child in _kids(kid):
            self._content(child, depth + 1, page, into, pages, in_item=in_item or kind == "LI")
        if block and into[-1]:
            into.append([])


def _spans(cell: dict) -> tuple[int, int]:
    """RowSpan and ColSpan of a cell, from its attribute objects (one, or an array of them among revision numbers)."""
    attributes = resolve1(cell.get("A"))
    spans = {"RowSpan": 1, "ColSpan": 1}
    for attribute in attributes if isinstance(attributes, list) else [attributes]:
        attribute = resolve1(attribute)
        if not isinstance(attribute, dict):
            continue
        for key in spans:
            value = resolve1(attribute.get(key))
            if isinstance(value, int) and not isinstance(value, bool) and value > 1:
                if value > _MAX_SPAN:
                    raise _Untrusted
                spans[key] = value
    return spans["RowSpan"], spans["ColSpan"]


def _page_tagged_tables(tables: list[list[list[_TaggedCell]]], chars: list[dict]) -> list[_TaggedTable]:
    """The tagged tables of a page with the text and the box of each cell, from the characters of the page (`chars`) that
    carry the marked-content ids of the cell.

    The paragraphs of a cell are joined by a line break and the lines of a paragraph by a space: a line of a cell is
    broken where the cell ends. The break is a newline, which a Markdown table and an HTML one both write as `<br>`;
    joined as the tag itself, it was the cell's text to an HTML table, which writes it as text (`&lt;br>`) (Pinned by:
    test_pdf_tagged_tables.py::TestTablesFromTheTree::test_a_cell_of_two_paragraphs_in_a_table_with_spans). Where the
    cell is too narrow for a word the word itself is broken ('Differen' over 'ce'), and the two lines are joined with
    nothing between them when the page shows it (`_breaks_a_word`).
    """
    by_content: dict[int, list[dict]] = {}
    for char in chars:
        content = char.get("mcid")
        if isinstance(content, int):
            by_content.setdefault(content, []).append(char)
    # per table, row and cell: the lines of each paragraph, the spans, and the box of all of it
    drawn_tables = []
    for rows in tables:
        drawn_rows = []
        for row in rows:
            drawn_cells = []
            for cell in row:
                paragraphs = [lines for paragraph in cell.paragraphs
                              if (lines := _lines([char for content in paragraph for char in by_content.get(content, ())]))]
                inside = [char for lines in paragraphs for line in lines for char in line]
                # with the space a line ends in: the lines of the page are measured with it
                box = ((min(char["x0"] for char in inside), min(char["top"] for char in inside),
                        max(char["x1"] for char in inside), max(char["bottom"] for char in inside)) if inside else None)
                drawn_cells.append((paragraphs, box, cell.row_span, cell.col_span))
            drawn_rows.append(drawn_cells)
        drawn_tables.append(drawn_rows)
    spaced = any(line[-1]["text"] == " " for rows in drawn_tables for row in rows for paragraphs, *_ in row
                 for lines in paragraphs for line in lines[:-1])
    read = []
    for rows in drawn_tables:
        boxes = [box for row in rows for _, box, _, _ in row if box is not None]
        table = []
        for row in rows:
            cells = []
            for paragraphs, box, row_span, col_span in row:
                # the right end of the column: the farthest text of the cells that start where this one does
                right = max((other[2] for other in boxes if box is not None and abs(other[0] - box[0]) <= _OVERLAP_TOLERANCE),
                            default=0.0)
                texts = []
                for lines in paragraphs:
                    text = ""
                    for index, line in enumerate(lines):
                        part = _text_from_chars(line)
                        if text and part:
                            text += "" if _runs_on(text, part) or (spaced and _breaks_a_word(lines[index - 1], right)) else " "
                        text += part
                    if text:
                        texts.append(text)
                cells.append(("\n".join(texts), box if texts else None, row_span, col_span))
            table.append(cells)
        read.append(_TaggedTable(table))
    return read


def _breaks_a_word(line: list[dict], right: float) -> bool:
    """Whether a line of a cell was broken inside a word: it ends without a space, on a page whose cells end a line
    broken at a space with that space, and it runs to the right end of its column (within a character of it) — a line
    that stops short was ended by the writer ('Name' over 'Title'), and is another word."""
    last = line[-1]
    return last["text"] != " " and last["x1"] >= right - max(last.get("size") or 0.0, 1.0)


def _lines(chars: list[dict]) -> list[list[dict]]:
    lines: list[list[dict]] = []
    for char in sorted(chars, key=lambda char: (char["top"], char["x0"])):
        if lines and abs(char["top"] - lines[-1][0]["top"]) <= _TABLE_ROW_TOLERANCE:
            lines[-1].append(char)
        else:
            lines.append([char])
    return lines


def _extract_tagged_tables(page: _Page) -> None:
    """Make tables of the tagged tables of the page (`_Page.tagged`) and take their lines out of the body flow, before
    any table is looked for by the alignment of its text.

    A tag is what the program that wrote the PDF says, and a PDF can say anything: the table is taken only when the
    page agrees with it (`_trusted_cells`). It has two rows and two columns or more; the cells of a row stand side by
    side in their order — from the left, or in every row from the right — and the rows one under the other; every line
    of the page inside the box of the table lies in one of its cells, so nothing else is taken with it; and no table
    found by its rules stands there — a ruled table is read from its rules as before. A table that fails any of these
    is left to the searches that follow, as on a page without tags.

    Deliberate: tags are used for tables only. Headings and reading order of a tagged PDF are still read from the page.
    Pinned by: test_pdf_tagged_tables.py; judged on the PDF twins (`evals/README.md`).
    """
    if page.simple or not page.tagged or not page.lines:
        return
    used: set[int] = set()
    for tagged in page.tagged:
        boxes = [box for row in tagged.rows for _, box, _, _ in row if box is not None]
        trusted = _trusted_cells(tagged)
        if trusted is None or not boxes:
            continue
        grid, merges = trusted
        x0, top = min(box[0] for box in boxes), min(box[1] for box in boxes)
        x1, bottom = max(box[2] for box in boxes), max(box[3] for box in boxes)
        if any(table.top < bottom and table.bottom > top and table.x0 < x1 and table.x1 > x0 for table in page.tables):
            continue
        inside = [line for line in page.lines if id(line) not in used
                  and x0 - _OVERLAP_TOLERANCE <= (line.x0 + line.x1) / 2 <= x1 + _OVERLAP_TOLERANCE
                  and top - _OVERLAP_TOLERANCE <= (line.top + line.bottom) / 2 <= bottom + _OVERLAP_TOLERANCE]
        if not inside or not all(_in_a_cell(line.x0, line.x1, (line.top + line.bottom) / 2, boxes) for line in inside):
            continue
        page.tables.append(_Table(top=top, text=render_table(grid), bottom=bottom, x0=x0, x1=x1, rows=grid, stated=True,
                                  merges=merges))
        used.update(id(original) for line in inside for original in _original_lines_of(line))
    if used:
        page.lines = [line for line in page.lines if id(line) not in used]
        page.tables.sort(key=lambda table: table.top)


def _in_a_cell(x0: float, x1: float, middle: float, boxes: list[tuple[float, float, float, float]]) -> bool:
    """Whether a line of the page lies in the cells of the table: at its height some cell starts no later than it and
    some cell ends no earlier (one line may run through several cells of a row)."""
    at_height = [box for box in boxes if box[1] - _OVERLAP_TOLERANCE <= middle <= box[3] + _OVERLAP_TOLERANCE]
    return (any(box[0] <= x0 + _OVERLAP_TOLERANCE for box in at_height)
            and any(box[2] >= x1 - _OVERLAP_TOLERANCE for box in at_height))


def _trusted_cells(tagged: _TaggedTable) -> tuple[list[list[str]], tuple[tuple[int, int, int, int], ...]] | None:
    """The rows of a tagged table as a grid of cell text, with the cells the tree says span rows or columns as (row,
    column, rows, columns) in it; None when the page does not agree with the tags.

    A cell that spans rows has its text on every row it covers and one that spans columns in the first of them, as the
    tables of every format are written (`docs/decisions/core.md`); the spans are those the tree states, which the table
    is written with (`_pdf_ruled_tables._spanned_text`). They are left out when two of them would cover one place (a
    span over the cells a span from above already covers, in a tree that says so), and then the table is written
    without them, as before.
    """
    rows = tagged.rows
    if len(rows) < 2:
        return None
    previous_top = None
    # The cells of every row stand side by side in the order of the tree, from the left — or, in a table laid out from
    # right to left (Arabic, Hebrew), from the right: the tree is in the order of reading either way, and so is the grid
    rightwards = leftwards = True
    for row in rows:
        placed = [box for _, box, _, _ in row if box is not None]
        pairs = list(zip(placed, placed[1:], strict=False))
        rightwards = rightwards and not any(first[2] > second[0] + _OVERLAP_TOLERANCE for first, second in pairs)
        leftwards = leftwards and not any(second[2] > first[0] + _OVERLAP_TOLERANCE for first, second in pairs)
        if not rightwards and not leftwards:
            return None
        tops = [box[1] for _, box, row_span, _ in row if box is not None and row_span == 1] or [box[1] for box in placed]
        if tops:
            if previous_top is not None and min(tops) < previous_top - _OVERLAP_TOLERANCE:
                return None
            previous_top = min(tops)
    grid: list[list[str | None]] = [[] for _ in rows]
    covered: dict[tuple[int, int], str] = {}  # the cells under a cell that spans rows
    merges: list[tuple[int, int, int, int]] = []
    spanned: set[tuple[int, int]] = set()  # the places the spans cover
    overlapping = False
    for index, row in enumerate(rows):
        column = 0
        for text, _, row_span, col_span in row:
            while (index, column) in covered:
                grid[index].append(covered[index, column])
                column += 1
            grid[index] += [text] + [""] * (col_span - 1)
            for below in range(index + 1, min(index + row_span, len(rows))):
                for offset in range(col_span):
                    covered[below, column + offset] = text if offset == 0 else ""
            height = min(row_span, len(rows) - index)
            if height > 1 or col_span > 1:
                merges.append((index, column, height, col_span))
                places = {(index + down, column + right) for down in range(height) for right in range(col_span)}
                overlapping = overlapping or not places.isdisjoint(spanned)
                spanned |= places
            column += col_span
        while (index, column) in covered:
            grid[index].append(covered[index, column])
            column += 1
    width = max(len(row) for row in grid)
    if width < 2 or width * len(grid) > _MAX_TABLE_CELLS:
        return None
    filled = [[cell or "" for cell in row] + [""] * (width - len(row)) for row in grid]
    if sum(1 for row in filled if any(row)) < 2:
        return None
    return filled, () if overlapping else tuple(merges)
