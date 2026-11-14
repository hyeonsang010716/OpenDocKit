"""RTF → Markdown.

A single pass over the bytes: one token at a time (control word with an optional number, `\\'hh`, control symbol, brace, text) feeding a
state machine with a group stack. Nothing recurses and nothing looks back, so the time is linear in the file size whatever the file
says about itself. Decisions are recorded in docs/decisions/rtf.md.

Character formatting and the destination (where text goes) follow the group stack, as the format defines them. Paragraph and table-row
properties do not: they are kept per text flow and changed only by `\\pard` / `\\trowd` — writers put `\\par` and `\\row` at any group
depth, and scoping the properties to groups lost the list level or the row definition of such documents.
"""

import codecs
from functools import lru_cache
import re

from opendockit.exceptions import CorruptedDocumentError
from opendockit.extractors._inline import (
    BOLD_OFF,
    BOLD_ON,
    ITALIC_OFF,
    ITALIC_ON,
    LINK_ON,
    InlineMark,
    field_link,
    link_off,
    resolve_inline,
)
from opendockit.extractors._lists import paragraph_head
from opendockit.extractors._notation import NOTE_ROW_SEPARATOR, TableBudget, omitted_rows_warning
from opendockit.markdown import TableGrid, join_blocks, merged_copy, solid_length
from opendockit.models import ConvertOptions, Extraction, Section, SectionKind


# One token. Every alternative is a single bounded or character-class repetition, so matching is linear. A number is at most 10 digits
# (the format's own limit): `int()` on thousands of digits is quadratic and raises past 4,300
# (Pinned by: test_rtf.py::TestHostileInput::test_number_with_thousands_of_digits_is_not_parsed_as_one_number).
_TOKEN = re.compile(
    rb"\\([a-zA-Z]{1,32})(-?[0-9]{1,10})? ?"  # 1, 2: control word, number; one space after it is a delimiter
    rb"|\\'([0-9a-fA-F]{2})"                  # 3: a byte in the current code page
    rb"|\\([^a-zA-Z])"                        # 4: control symbol
    rb"|([{}])"                               # 5: group
    rb"|([^\\{}]+)",                          # 6: text (or the hex digits of a picture)
    re.DOTALL)
_NOT_HEX = re.compile(rb"[^0-9a-fA-F]+")
_BYTES = [bytes([value]) for value in range(256)]

_MAGIC = b"{\\rtf"
# Groups nested deeper than this are not read (one warning). Real documents stay below 50; the stack is a list, so the limit bounds
# memory, not recursion (Pinned by: test_rtf.py::TestHostileInput::test_groups_nested_past_the_limit_are_skipped_with_a_warning).
_MAX_GROUP_DEPTH = 500
_MAX_TABLE_DEPTH = 16
_MAX_ROW_CELLS = 4096
# Cells one table may hold. `\cell` is five bytes and a cell of the grid costs several hundred, text or no text: 4,000 rows of
# 1,000 empty cells (20 MB) took 13 s and 3.4 GB. The cells past the limit are left out — with or without a `\row` after
# them — and one warning says so
# (Pinned by: test_rtf.py::TestHostileInput::test_cells_past_the_cell_limit_of_a_table_are_left_out_with_a_warning).
_MAX_TABLE_CELLS = 500_000
# Runs one paragraph may have. Runs that cannot be merged (`{\b a}b` again and again) cost several hundred bytes each when
# the marks are worked out: 2.8 million of them in one paragraph (20 MB) took 16 s and 2.3 GB. Past the limit the text is
# still read, as one last run without bold, italic or link — nothing is lost and nothing is cut
# (Pinned by: test_rtf.py::TestHostileInput::test_paragraph_of_endless_runs_keeps_its_text_in_a_bounded_number_of_runs).
_MAX_PARAGRAPH_RUNS = 50_000
_PLAIN_TAIL = (False, False, None, False)
# Style references. Writers put `\*` in front of them in the middle of a run (StarWriter: `{\b0\*\cs7 text}`); there they are
# formatting, not the start of an ignorable destination.
_STYLE_WORDS = frozenset((b"cs", b"s", b"ds", b"ts"))
_MAX_LIST_LEVELS = 64
_MAX_LEVEL = 8
_MAX_UC = 16
_MAX_PICTURES = 10_000
_MAX_INSTRUCTION_CHARS = 4096
# Same value and rule as DOCX/HWP: a longer paragraph is body text even with a heading style or outline level.
_HEADING_MAX_CHARS = 120
# Cell edges (twips) of different rows that are this close are the same column edge (rounding differences between rows).
_EDGE_TOLERANCE = 30

_HEADING_NAME = re.compile(r"(?:heading|제목)\s*([1-9])", re.IGNORECASE)
_TITLE_NAMES = frozenset(("title", "제목"))
# Word's "List Bullet 2" / "List Number 3" styles carry the level in the digit of the name, not in `\ilvl` (the DOCX rule).
_LIST_STYLE_LEVEL = re.compile(r"List (?:Bullet|Number|Continue|Paragraph)\s+([1-9])", re.IGNORECASE)
# A bullet typed as text and followed by a tab (pandoc writes its lists this way: `\bullet \tab text`).
_TYPED_BULLETS = frozenset("•·◦▪‣○●–-")

# Destinations (where the text of a group goes).
_BODY, _SKIP, _FONTTBL, _STYLESHEET, _INFO, _INFO_TEXT, _PICT, _FLDINST, _LISTTABLE, _LISTOVERRIDE, _PN, _SHAPE = range(12)

# Destinations without `\*` whose text is not body text.
_SKIPPED_WORDS = frozenset((
    b"colortbl", b"filetbl", b"revtbl", b"rsidtbl", b"xe", b"tc", b"tcn", b"rxe", b"txe", b"ftnsep", b"ftnsepc", b"ftncn", b"aftnsep",
    b"aftnsepc", b"aftncn", b"atnid", b"atnauthor", b"annotation", b"atrfstart", b"atrfend", b"atnref", b"atndate", b"atntime",
    b"atnparent", b"atnicn", b"bkmkstart", b"bkmkend", b"datafield", b"formfield", b"falt", b"panose", b"fname", b"fontemb",
    b"fontfile", b"pntext", b"listtext", b"objdata", b"objclass", b"objname", b"objalias", b"objsect", b"objtime", b"themedata",
    b"colorschememapping", b"latentstyles", b"datastore", b"generator", b"userprops", b"xmlnstbl", b"listpicture", b"mmathPr",
    b"pgdsctbl", b"nonshppict", b"shprslt", b"nonesttables", b"background", b"template", b"private", b"passwordhash", b"blipuid",
    b"picprop", b"htmltag", b"mhtmltag", b"ud",
))
_HEADER_WORDS = frozenset((b"header", b"headerl", b"headerr", b"headerf"))
_FOOTER_WORDS = frozenset((b"footer", b"footerl", b"footerr", b"footerf"))
_NOTE_WORDS = frozenset((b"footnote",))
# Known destinations that are read through (their content is body content), so that `\*` in front of them does not skip the group.
_TRANSPARENT_WORDS = frozenset((b"shppict", b"shp", b"shpgrp", b"object", b"result", b"upr", b"mmath", b"mmathPara"))
_PICTURE_KINDS = frozenset((b"pngblip", b"jpegblip", b"emfblip", b"wmetafile", b"dibitmap", b"wbitmap", b"macpict", b"pmmetafile"))
_NUMBERED_PN = frozenset((b"pndec", b"pncard", b"pnucltr", b"pnucrm", b"pnlcltr", b"pnlcrm", b"pnord", b"pnordt", b"pnlvlbody"))

_CHARACTERS = {
    b"tab": "\t", b"line": "\n", b"lbr": "\n", b"page": "\n", b"column": "\n", b"emdash": "—", b"endash": "–", b"lquote": "‘",
    b"rquote": "’", b"ldblquote": "“", b"rdblquote": "”", b"bullet": "•", b"emspace": " ", b"enspace": " ", b"qmspace": " ",
}
# `\~` non-breaking space, `\_` non-breaking hyphen (a plain hyphen, so that the word is found by search). `\-` (optional hyphen) and
# the other symbols produce nothing.
_SYMBOLS = {b"~": " ", b"_": "-", b"{": "{", b"}": "}", b"\\": "\\", b"\t": "\t"}

# Windows code page of a font charset (`\fcharsetN`). 0 (ANSI) is decided at decode time (`_ANSI`); 1 (default) and 2 (symbol) use
# the document's code page.
_CHARSET_PAGES = {77: 10000, 128: 932, 129: 949, 130: 1361, 134: 936, 136: 950, 161: 1253, 162: 1254, 163: 1258, 177: 1255, 178: 1256,
                  186: 1257, 204: 1251, 222: 874, 238: 1250, 254: 437, 255: 850}
_PAGE_CODECS = {932: "cp932", 936: "gbk", 949: "cp949", 950: "cp950", 1361: "johab", 10000: "mac_roman", 10001: "shift_jis",
                10003: "euc_kr", 10008: "gb2312", 20127: "ascii", 28591: "latin-1", 65001: "utf-8"}
_DOUBLE_BYTE = frozenset(("cp932", "gbk", "cp949", "cp950", "johab"))
_ANSI = ""  # a font with `\fcharset0`

_H_FIRST, _H_CONTINUES, _V_FIRST, _V_CONTINUES = 1, 2, 4, 8
_MERGE_FLAGS = {b"clmgf": _H_FIRST, b"clmrg": _H_CONTINUES, b"clvmgf": _V_FIRST, b"clvmrg": _V_CONTINUES}

_TRUNCATED = "The RTF document ends before it is complete; only the part that could be read was converted."
_TOO_DEEP = f"Groups nested deeper than {_MAX_GROUP_DEPTH} levels were not read."
_TABLE_CUT = f"A table has more than {_MAX_TABLE_CELLS:,} cells; the cells after that were left out."
_TOO_MANY_PICTURES = f"The document has more than {_MAX_PICTURES:,} pictures; the rest were left out."


_PRINTABLE_ASCII = bytes(range(0x20, 0x7F))


@lru_cache(maxsize=256)
def _page_codec(page: int) -> str | None:
    """Python codec of a Windows code page number, None when there is none (the document's code page is used then).

    A code page that does not read ASCII as ASCII (EBCDIC: 037, 500, 875, 1026, …) is none: RTF is ASCII text, and once a run
    held one 8-bit byte the whole run, letters included, was decoded as EBCDIC — 'hello' became 'ΪΧ%%?'
    (Pinned by: test_rtf.py::TestCharacters::test_code_page_that_is_not_ascii_compatible_is_ignored)."""
    if not 0 < page < 100_000:
        return None
    try:
        name = codecs.lookup(_PAGE_CODECS.get(page) or f"cp{page}").name
        return name if _PRINTABLE_ASCII.decode(name) == _PRINTABLE_ASCII.decode("ascii") else None
    except (LookupError, UnicodeDecodeError):
        return None


class _State:
    """What a group scopes: the destination and the character formatting."""

    __slots__ = ("dest", "bold", "italic", "hidden", "font", "uc", "link", "closer", "nested_row")

    def __init__(self) -> None:
        self.dest = _BODY
        self.bold = self.italic = self.hidden = False
        self.font: int | None = None
        self.uc = 1
        self.link: str | None = None
        self.closer: str | None = None  # what to finish when this group closes (the group that opened a picture, a footnote, …)
        self.nested_row = False  # inside `\*\nesttableprops`: row definitions belong to the nested table

    def child(self) -> "_State":
        state = _State.__new__(_State)
        state.dest, state.bold, state.italic, state.hidden = self.dest, self.bold, self.italic, self.hidden
        state.font, state.uc, state.link, state.nested_row = self.font, self.uc, self.link, self.nested_row
        state.closer = None
        return state


class _Paragraph:
    """Paragraph properties; `\\pard` starts them over."""

    __slots__ = ("style", "outline", "ls", "ilvl", "pn", "pn_level", "intbl", "itap", "indent")

    def __init__(self) -> None:
        self.style: int | None = None
        self.outline: int | None = None
        self.ls: int | None = None
        self.ilvl = 0
        self.pn: str | None = None  # the older `\pn` numbering: "-" bullet, "1" numbered
        self.pn_level = 0
        self.intbl = False
        self.itap = 0
        self.indent = 0  # `\li`, twips


class _RowDefinition:
    """`\\trowd` … `\\cellx`: the right edge and merge flags of each cell of a row."""

    __slots__ = ("cells", "flags", "_shared")

    def __init__(self) -> None:
        self.cells: list[tuple[int | None, int]] = []
        self.flags = 0
        self._shared: tuple[tuple[int | None, int], ...] | None = None

    def start(self) -> None:
        self.cells = []
        self.flags = 0
        self._shared = None

    def add(self, edge: int | None) -> None:
        if len(self.cells) < _MAX_ROW_CELLS:
            self.cells.append((edge, self.flags))
            self._shared = None
        self.flags = 0

    def shared(self) -> tuple[tuple[int | None, int], ...]:
        """The definition as stored with a row. Rows that reuse a definition (no new `\trowd`) share one copy; copying it per row
        cost rows × cells for a wide definition followed by many short rows
        (Pinned by: test_rtf.py::TestHostileInput::test_time_is_linear_in_the_size_of_the_file)."""
        if self._shared is None:
            self._shared = tuple(self.cells)
        return self._shared


class _Table:
    __slots__ = ("grid", "rows", "cells", "cell", "budget", "count")

    def __init__(self, budget: TableBudget | None) -> None:
        self.grid = TableGrid()
        self.rows: list[tuple[list[str], tuple[tuple[int | None, int], ...]]] = []
        self.cells: list[str] = []  # finished cells of the row being read
        self.cell: list[str] = []   # paragraphs of the cell being read
        self.budget = budget
        self.count = 0     # cells in `rows`

    def build(self) -> TableGrid:
        """Place the rows read so far into the grid.

        Placement waits for the end of the table because the columns are not known before: a cell spanning columns is written as
        one wider cell, so the column of a cell is the position of its right edge among the edges of all rows
        (Pinned by: test_rtf.py::TestTables::test_wider_cell_spans_the_columns_its_edges_cover). When a row has more cells than
        edges, or edges that do not increase, the edges cannot be trusted and every row is placed cell by cell
        (test_row_without_cell_edges_is_placed_in_order). Vertical merges follow the DOCX rule (`docx._place_cells`): the
        continuation cells become the row span of the cell above when they are as wide as it.
        """
        if self.cell:
            self.cells.append("\n".join(self.cell))
            self.cell = []
        if self.cells:  # cells after the last `\row` (a truncated file, or a writer that never ends the row) are a row too
            self.rows.append((self.cells, ()))
            self.cells = []

        aligned = True
        for texts, definition in self.rows:
            previous = None
            for edge, _ in definition:
                if edge is None or (previous is not None and edge <= previous):
                    aligned = False
                    break
                previous = edge
            if not aligned or len(definition) < len(texts):
                aligned = False
                break
        columns: dict[int, int] = {}
        if aligned:
            column, start = -1, None
            for edge in sorted({edge for _, definition in self.rows for edge, _ in definition}):
                if start is None or edge - start > _EDGE_TOLERANCE:
                    column, start = column + 1, edge
                columns[edge] = column

        placed: list[tuple[int, int, int, bool, str]] = []  # row, column, column span, continues the cell above, text
        for row, (texts, definition) in enumerate(self.rows):
            column, first = 0, len(placed)
            for index, text in enumerate(texts):
                flags = definition[index][1] if index < len(definition) else 0
                span = max(columns[definition[index][0]] + 1 - column, 1) if aligned else 1
                # A merged-away cell that still has text is an ordinary cell: the text is never dropped
                # (Pinned by: test_rtf.py::TestTables::test_merged_away_cell_with_text_keeps_its_text).
                empty = not text.strip()
                if flags & _H_CONTINUES and empty and len(placed) > first:
                    last = placed[-1]
                    placed[-1] = (last[0], last[1], last[2] + span, last[3], last[4])
                else:
                    placed.append((row, column, span, bool(flags & _V_CONTINUES) and empty, text))
                column += span

        grid = self.grid
        continuing = {(row, column): span for row, column, span, continues, _ in placed if continues}
        shown: dict[tuple[int, int], str] = {}
        for row, column, span, continues, text in placed:
            if continues:
                grid.reach(row, column)
                if (row, column) not in shown:
                    shown[row, column] = shown.get((row - 1, column), "")
                    grid.place(row, column, shown[row, column], col_span=span)
                continue
            row_span = 1
            shown[row, column] = merged_copy(text)
            while continuing.get((row + row_span, column)) == span:
                shown[row + row_span, column] = shown[row, column]
                row_span += 1
            grid.place(row, column, text, row_span=row_span, col_span=span)
        return grid


# A piece of a paragraph: text, bold, italic, the vetted address (`_inline.link_target`) of the link it is part of.
type _Run = tuple[str, bool, bool, str | None]


def _inline_tokens(runs: list[_Run], *, emphasis: bool, links: bool) -> list[str | InlineMark]:
    """Runs → the pieces and span marks `_inline.resolve_inline` turns into Markdown. This only says where bold, italic and a link
    start and end; merging runs, placing whitespace, whole-block and part-of-word emphasis, escaping and the "text is its address"
    rule are the shared rules of docs/decisions/inline-formatting.md.

    The marks are `InlineMark` items between the pieces (the HTML way), not control characters inside the text (the DOCX way):
    RTF text can hold any character (`\\'12`, `\\u19`), so marks inside the text could be forged by the document
    (Pinned by: test_rtf.py::TestLinks::test_control_characters_in_the_text_cannot_forge_a_link).
    """
    tokens: list[str | InlineMark] = []
    open_link: str | None = None
    for text, bold, italic, link in runs:
        if not links:
            link = None
        if link != open_link:
            if open_link is not None:
                tokens.append(link_off(open_link))
            if link is not None:
                tokens.append(LINK_ON)
            open_link = link
        bold, italic = emphasis and bold, emphasis and italic
        if not (bold or italic) or not text.strip():  # whitespace joins the runs around it
            tokens.append(text)
            continue
        # Bold is the outer mark, as in `_inline.mark_emphasis`
        if bold:
            tokens.append(BOLD_ON)
        if italic:
            tokens.append(ITALIC_ON)
        tokens.append(text)
        if italic:
            tokens.append(ITALIC_OFF)
        if bold:
            tokens.append(BOLD_OFF)
    if open_link is not None:
        tokens.append(link_off(open_link))
    return tokens


class _Flow:
    """One stream of paragraphs and tables: the body, a header or footer, a footnote, the text of a shape."""

    def __init__(self, reader: "_Reader", kind: str) -> None:
        self.reader = reader
        self.kind = kind  # "body", "header", "footer", "note", "box"
        self.blocks: list[str] = []
        self.runs: list[_Run] = []
        self._pieces: list[str] = []   # text of the run being collected (`add_run`) …
        self._look: tuple[bool, bool, str | None, bool] | None = None   # … and its bold, italic, link, "is white space"
        self.paragraph = _Paragraph()
        self.tables: list[_Table] = []  # the open tables, outermost first
        self.row = _RowDefinition()
        self.nested_row = _RowDefinition()

    def _depth(self) -> int:
        paragraph = self.paragraph
        return min(paragraph.itap if paragraph.itap > 0 else int(paragraph.intbl), _MAX_TABLE_DEPTH)

    def _enter(self, depth: int) -> None:
        """Make the open tables exactly `depth` deep: tables deeper than that are finished and handed to the cell that holds them."""
        while len(self.tables) > depth:
            self._close_table()
        while len(self.tables) < depth:
            # Only a top-level table of the body watches the character limit while its cells are read (`_notation.TableBudget`).
            counted = self.kind == "body" and not self.tables
            self.tables.append(_Table(TableBudget(self.reader.extraction, self.reader.options) if counted else None))

    def _close_table(self) -> None:
        table = self.tables.pop()
        grid = table.build()
        if self.tables:
            # A table inside a cell is drawn in place by the outer table, as in DOCX
            # (Pinned by: test_rtf.py::TestTables::test_nested_table_keeps_its_rows_and_columns).
            placeholder = self.tables[-1].grid.embed(grid)
            if placeholder:
                self.tables[-1].cell.append(placeholder)
        elif self.kind == "note":
            # A table inside a footnote is one line, as in DOCX/HWP (`_notation.note_table` shape: cells ' / ', rows '; ').
            self.add_block(NOTE_ROW_SEPARATOR.join(line for line in grid.flatten().split("\n") if line.strip()))
        else:
            self.add_block(grid.render())
            self.reader.omitted_rows += grid.omitted_rows()

    def add_block(self, text: str) -> None:
        if not text.strip():
            return
        self.blocks.append(text)
        if self.kind == "body":
            self.reader.count(text)

    def add_blocks(self, blocks: list[str]) -> None:
        """Blocks of a shape's text anchored in this flow: lines of the current cell, or blocks before the current paragraph."""
        depth = self._depth()
        self._enter(depth)
        for block in blocks:
            if depth:
                self.tables[-1].cell.append(block)
            else:
                self.add_block(block)

    def end_paragraph(self) -> None:
        depth = self._depth()
        self._enter(depth)
        text = self._paragraph_text(in_cell=depth > 0)
        if not text:
            return
        if depth:
            self.tables[-1].cell.append(text)
        else:
            self.add_block(text)

    def end_cell(self, *, nested: bool) -> None:
        self._enter(max(self._depth(), 2) if nested else 1)
        table = self.tables[-1]
        text = self._paragraph_text(in_cell=True)
        if text:
            table.cell.append(text)
        cell = "\n".join(table.cell)
        table.cell = []
        if table.count + len(table.cells) >= _MAX_TABLE_CELLS:
            self.reader.table_cut = True   # the table is full: this cell and the ones after it are left out
            return
        table.cells.append(cell)
        if table.budget is not None and table.budget.add(len(table.rows), len(table.cells) - 1, cell):
            self.reader.stopped = True

    def end_row(self, *, nested: bool) -> None:
        depth = min(max(self._depth(), 2), len(self.tables)) if nested else 1
        if len(self.tables) < depth or (nested and depth < 2):
            return
        self._enter(depth)
        table = self.tables[-1]
        if table.cell:
            table.cells.append("\n".join(table.cell))
            table.cell = []
        if table.cells:
            table.count += len(table.cells)
            # Only the edges of the cells the row really has are kept: the rest of a definition says nothing about this row.
            table.rows.append((table.cells, (self.nested_row if nested else self.row).shared()[:len(table.cells)]))
            table.cells = []

    def finish(self) -> list[str]:
        """The end of the flow. Text after the last `\\par` is a paragraph (the last paragraph of a document has none)."""
        self.end_paragraph()
        self._enter(0)
        return self.blocks

    def add_run(self, text: str, bold: bool, italic: bool, link: str | None, *, alone: bool = False) -> None:
        """Text for the paragraph being read. Pieces that follow each other with the same formatting are one run: a run per
        token (10 million `\\_` in 20 MB) was a tuple each — 22 s and 4.6 GB
        (Pinned by: test_rtf.py::TestHostileInput::test_pieces_with_the_same_formatting_are_one_run)."""
        # White space (a tab, a `\line` break, a space between two groups) stays a run of its own, as it was when every piece
        # was one: the shared rules place marks around it, and a line break inside a marked run reads as one span over two
        # lines (Pinned by: test_rtf.py::TestEmphasis::test_emphasis_is_marked_line_by_line_inside_a_run).
        # A picture placeholder and a footnote mark are runs of their own too (`alone`): the rules ask of a piece whether it is
        # only a placeholder.
        look = (bold, italic, link, not text.strip())
        if len(self.runs) >= _MAX_PARAGRAPH_RUNS:
            look, alone = _PLAIN_TAIL, False   # everything after the limit is one plain run (`_MAX_PARAGRAPH_RUNS`)
        if alone or look != self._look:
            self._settle()
            self._look = look
        self._pieces.append(text)
        if alone:
            self._settle()

    def _settle(self) -> None:
        if self._pieces:
            self.runs.append(("".join(self._pieces), *self._look[:3]))
            self._pieces = []
        self._look = None

    def _paragraph_text(self, *, in_cell: bool) -> str:
        self._settle()
        runs = self.runs
        if not runs:
            return ""
        self.runs = []
        reader, paragraph, options = self.reader, self.paragraph, self.reader.options
        # What the paragraph is (empty, a heading, a list item) is decided on the text without marks, so the decisions are the
        # same with the options off — as in DOCX.
        plain = "".join(run[0] for run in runs)
        first_line = next((line for line in (" ".join(line.split()) for line in plain.split("\n")) if line), "")
        if not first_line:
            return ""
        level = mark = None
        if self.kind != "note":
            if not in_cell and len(plain) <= _HEADING_MAX_CHARS:
                level = reader.heading_level(paragraph)
            if level:
                reader.end_simple_numbering()
            else:
                mark = reader.list_mark(paragraph)
                start = plain.lstrip()
                if mark is None and start[1:2] == "\t" and start[0] in _TYPED_BULLETS and len(first_line) > 2:
                    # A typed bullet + tab is a list item; its level is the place of its left indent among the items before it
                    # (Pinned by: test_rtf.py::TestLists::test_typed_bullet_followed_by_a_tab_is_a_list_item).
                    mark = "-", reader.typed_level(paragraph.indent)
                    first = next(index for index, run in enumerate(runs) if run[0].strip())
                    runs[first] = (runs[first][0].lstrip()[1:], *runs[first][1:])
                else:
                    reader.typed_indents.clear()
        # A heading keeps its links but is not emphasized; bold or italic over the whole paragraph (also a list item, a paragraph
        # of a cell) is how it is styled and is left unmarked (`_inline.resolve_inline`.
        # Pinned by: test_rtf.py::TestEmphasis::test_paragraph_list_item_and_cell_that_are_bold_throughout_are_not_marked).
        text = resolve_inline(_inline_tokens(runs, emphasis=options.keep_emphasis and level is None, links=options.keep_links),
                              emphasis=level is None)
        lines = [line for line in (" ".join(line.split()) for line in text.split("\n")) if line]
        if not lines:
            return ""
        if level:
            return f"{'#' * level} {' '.join(lines)}"
        if mark is not None:
            lines[0] = paragraph_head(mark[0], mark[1], in_cell=in_cell, heading=False) + lines[0]
        return "\n".join(lines)


class _Reader:
    def __init__(self, data: bytes, start: int, options: ConvertOptions, extraction: Extraction) -> None:
        self._data = data
        self._start = start
        self.options = options
        self.extraction = extraction
        self.stopped = False  # the character limit was exceeded: nothing more is read
        self.omitted_rows = 0
        self.table_cut = False
        self._truncated = False
        self._too_deep = False

        self._state = _State()
        self._stack: list[_State] = []
        self._overflow = 0  # depth below the group limit
        self._star = False
        self._fresh = False  # nothing but white space has been read since the last `{`
        self._raw: list[bytes] = []  # text bytes waiting to be decoded with the code page of the state they were read in
        self._skip = 0  # fallback characters still to drop after `\uN`
        self._dropped: list[bytes] = []  # … the bytes dropped so far, and the lead byte of a double-byte character they ended in
        self._lead: bytes | None = None
        self._high = 0  # a high surrogate waiting for its low half

        self._document_codec = "cp1252"
        self._default_font: int | None = None
        self._fonts: dict[int, str | None] = {}
        self._font: int | None = None  # font table entry being read
        # style number → (heading level, based-on style, list level from a name such as 'List Bullet 2')
        self._styles: dict[int, tuple[int | None, int | None, int]] = {}
        self.typed_indents: list[int] = []  # left indents of the open typed-bullet items, outermost first
        self._style: list | None = None  # style being read: [number, name parts, outline level, based on, is a paragraph style]
        self._lists: dict[int, list[list[int]]] = {}  # list id → levels [number format, start]
        self._list: list[list[int]] | None = None
        self._overrides: dict[int, int] = {}  # `\ls` number → list id
        self._override_id: int | None = None
        self._counters: dict[int, list[int | None]] = {}
        self._simple_counters: list[int | None] = [None] * (_MAX_LEVEL + 1)
        self._info_key = ""
        self._info: list[str] = []
        self._fields: list[list] = []  # open fields: [instruction parts, instruction length, length when resolved, link target]
        self._pictures: list[list[bytes]] = []  # open `\pict` groups: data chunks
        self._picture_count = 0
        self._too_many_pictures = False

        self._body = _Flow(self, "body")
        self._flow = self._body
        self._flows: list[_Flow] = [self._body]
        self._headers: dict[str, None] = {}  # texts in order of appearance, each once
        self._footers: dict[str, None] = {}
        self._notes: list[str] = []

    # ---- Reading -----------------------------------------------------------------------------------------------------

    def read(self) -> None:
        data, size, match = self._data, len(self._data), _TOKEN.match
        pos = self._start
        closed = False
        while pos < size and not self.stopped:
            token = match(data, pos)
            if token is None:  # a lone backslash at the very end
                self._truncated = True
                break
            pos = token.end()
            kind = token.lastindex
            if self._overflow:
                if kind == 5:
                    self._overflow += 1 if token.group(5) == b"{" else -1
                elif kind == 2 and token.group(1) == b"bin":
                    pos = self._binary(pos, int(token.group(2)))
                continue

            fresh = self._fresh
            if kind != 5 and not (kind == 6 and not token.group(6).strip()):
                self._fresh = False

            state = self._state
            dest = state.dest
            if kind == 6:
                if dest == _SKIP:
                    continue
                chunk = token.group(6)
                if dest == _PICT:
                    self._picture_hex(chunk)
                    continue
                if b"\n" in chunk or b"\r" in chunk:
                    chunk = chunk.translate(None, b"\r\n")
                if self._lead is not None and chunk:
                    chunk = chunk[self._drop_trail(chunk[:1]):]
                if self._skip and chunk:
                    dropped = min(self._skip, len(chunk))
                    self._drop(chunk[:dropped])
                    chunk = chunk[dropped:]
                if chunk:
                    self._raw.append(chunk)
            elif kind == 3:
                if dest == _SKIP or dest == _PICT:
                    continue
                value = _BYTES[int(token.group(3), 16)]
                if self._skip:
                    self._drop(value)
                elif self._lead is None or not self._drop_trail(value):
                    self._raw.append(value)
            elif kind == 5:
                if self._raw:
                    self._flush()
                self._skip, self._lead = 0, None
                self._star = False
                self._fresh = token.group(5) == b"{"
                if self._fresh:
                    self._open_group()
                else:
                    self._close_group()
                    if not self._stack:  # the document group closed; anything after it is not part of the document
                        closed = True
                        break
            elif kind == 4:
                if self._raw:
                    self._flush()
                self._skip, self._lead = 0, None
                self._symbol(token.group(4))
                if self._star and not fresh:
                    # `\*` that is not the first thing in its group. It still makes an unknown word that follows an ignorable
                    # destination, but not a style reference: `{\b0\*\cs7 text}` is body text
                    # (Pinned by: test_rtf.py::TestSkippedDestinations::test_star_in_the_middle_of_a_group_does_not_hide_its_text).
                    self._star = 2
            else:
                if self._raw:
                    self._flush()
                self._skip, self._lead = 0, None
                star, self._star = self._star, False
                word = token.group(1)
                number = int(token.group(2)) if kind == 2 else None
                if word == b"bin":
                    pos = self._binary(pos, number or 0)
                elif dest == _SKIP:
                    continue
                elif not self._common(word, number):
                    if dest == _BODY:
                        self._body_word(word, number, star)
                    else:
                        self._other_word(dest, word, number, star)

        if self._raw:
            self._flush()
        # A file that ends inside its groups keeps what was read, with a warning. Stopping at the character limit is not truncation
        # (Pinned by: test_rtf.py::TestHostileInput::test_truncated_document_keeps_what_was_read_with_a_warning).
        if not closed and not self.stopped:
            self._truncated = True
        while self._stack:
            self._close_group()

    def _open_group(self) -> None:
        if len(self._stack) >= _MAX_GROUP_DEPTH:
            self._overflow = 1
            self._too_deep = True
            return
        state = self._state
        self._stack.append(state)
        self._state = child = state.child()
        if state.dest == _STYLESHEET and self._style is None:
            child.closer = "style"
            self._style = [0, [], None, None, True]

    def _close_group(self) -> None:
        closer = self._state.closer
        if closer is not None:
            self._finish(closer)
        if self._stack:
            self._state = self._stack.pop()

    def _drop(self, data: bytes) -> None:
        """Drop fallback bytes after `\\uN`. When the last one is the lead byte of a double-byte character of the run's code page,
        the byte that completes it is dropped too (`_drop_trail`): writers that count `\\ucN` in characters, not bytes, write
        `\\uc1\\u44032\\'b0\\'a1`, and the stray second byte came out as '�' (Pinned by:
        test_rtf.py::TestCharacters::test_fallback_counted_in_characters_drops_the_whole_double_byte_character)."""
        self._skip -= len(data)
        self._dropped.append(data)
        if self._skip:
            return
        dropped, self._dropped, self._lead = b"".join(self._dropped), [], None
        if dropped.isascii():
            return   # the usual '?': ASCII ends no character half-way (every code page read here is ASCII-compatible)
        decoder = codecs.getincrementaldecoder(self._codec())(errors="ignore")
        decoder.decode(dropped)
        pending = decoder.getstate()[0]
        self._lead = pending if len(pending) == 1 else None

    def _drop_trail(self, byte: bytes) -> bool:
        """True (and the byte is dropped) when `byte` completes the double-byte character whose lead byte ended the fallback."""
        lead, self._lead = self._lead or b"", None
        try:
            return len((lead + byte).decode(self._codec())) == 1
        except UnicodeDecodeError:
            return False

    def _binary(self, pos: int, length: int) -> int:
        """`\\binN`: the next N bytes are raw data. A length past the end of the file takes what is there
        (Pinned by: test_rtf.py::TestHostileInput::test_binary_length_past_the_end_of_the_file_is_cut_to_the_file)."""
        end = pos + max(length, 0)
        if end > len(self._data):
            end = len(self._data)
            self._truncated = True
        if self._state.dest == _PICT and self._pictures and not self._overflow:
            self._pictures[-1].append(self._data[pos:end])
        return end

    def _picture_hex(self, chunk: bytes) -> None:
        if not self._pictures:
            return
        try:
            data = bytes.fromhex(chunk.decode("ascii"))  # skips the line breaks between the digits
        except ValueError:
            digits = _NOT_HEX.sub(b"", chunk)
            data = bytes.fromhex(digits[:len(digits) - len(digits) % 2].decode("ascii"))
        if data:
            self._pictures[-1].append(data)

    # ---- Text --------------------------------------------------------------------------------------------------------

    def _flush(self) -> None:
        raw = b"".join(self._raw)
        self._raw.clear()
        self._deliver(self._decode(raw))

    def _decode(self, raw: bytes) -> str:
        """Bytes → text with the code page of the current font, or the document's.

        The bytes of one run are decoded together, so a double-byte character written as two `\\'hh` (or as `\\'hh` and a plain
        letter) is one character (Pinned by: test_rtf.py::TestCharacters::test_cp949_double_byte_escapes_become_hangul). A font
        with `\\fcharset0` in a document whose code page is double-byte is tried with the document's code page first: writers that
        declare every font as ANSI still store Hangul in the document's code page, and reading it as Western text gave '°¡'
        (test_ansi_charset_font_in_a_korean_document_reads_hangul). Bytes that are not valid there are Western text ('café').
        """
        if raw.isascii():
            return raw.decode("ascii")
        codec = self._codec()
        if self._font_codec() == _ANSI and codec != "cp1252":
            try:
                return raw.decode(codec)
            except UnicodeDecodeError:
                codec = "cp1252"
        try:
            return raw.decode(codec)
        except UnicodeDecodeError:
            return raw.decode(codec, "replace")

    def _font_codec(self) -> str | None:
        state = self._state
        if state.dest == _BODY or state.dest == _FLDINST:
            return self._fonts.get(state.font if state.font is not None else self._default_font)
        return None

    def _codec(self) -> str:
        """The code page the bytes of the current run are read with first (`_decode`)."""
        codec = self._font_codec()
        if codec == _ANSI:
            return self._document_codec if self._document_codec in _DOUBLE_BYTE else "cp1252"
        return codec or self._document_codec

    def _deliver(self, text: str) -> None:
        self._high = 0
        state = self._state
        dest = state.dest
        if dest == _BODY:
            if not state.hidden:
                self._flow.add_run(text, state.bold, state.italic, state.link)
        elif dest == _INFO_TEXT:
            self._info.append(text)
        elif dest == _STYLESHEET:
            if self._style is not None:
                self._style[1].append(text)
        elif dest == _FLDINST and self._fields and self._fields[-1][1] < _MAX_INSTRUCTION_CHARS:
            text = text[:_MAX_INSTRUCTION_CHARS - self._fields[-1][1]]   # the cap holds for one long piece too
            self._fields[-1][0].append(text)
            self._fields[-1][1] += len(text)

    def _unicode(self, number: int) -> None:
        """`\\uN`: N is a signed 16-bit value, so characters above U+7FFF are negative and characters outside the BMP are two
        values (a surrogate pair). A half without its pair is dropped — a lone surrogate cannot be encoded as UTF-8
        (Pinned by: test_rtf.py::TestCharacters::test_unicode_escapes_cover_negative_values_and_surrogate_pairs)."""
        skip = self._state.uc
        code = number + 65536 if number < 0 else number
        high, self._high = self._high, 0
        if 0xD800 <= code < 0xDC00:
            self._high = code
        elif 0xDC00 <= code < 0xE000:
            if high:
                self._deliver(chr(0x10000 + ((high - 0xD800) << 10) + (code - 0xDC00)))
        elif code:
            self._deliver(chr(code))
        self._skip, self._dropped, self._lead = skip, [], None

    def _symbol(self, symbol: bytes) -> None:
        if symbol == b"*":
            self._star = True
            return
        self._star = False
        if self._state.dest == _SKIP:
            return
        if symbol in (b"\r", b"\n"):  # a backslash before a line break is `\par`
            if self._state.dest == _BODY:
                self._flow.end_paragraph()
            return
        text = _SYMBOLS.get(symbol)
        if text:
            self._deliver(text)

    def _common(self, word: bytes, number: int | None) -> bool:
        """Words that mean the same in every destination that collects text."""
        if word == b"u":
            if number is not None and -32768 <= number <= 65535:
                self._unicode(number)
            return True
        if word == b"uc":
            # The number of fallback characters after `\uN`. A value out of range is the default 1; dropping stops at the next
            # control word or brace anyway (Pinned by: test_rtf.py::TestHostileInput::test_absurd_uc_value_does_not_swallow_the_text).
            self._state.uc = number if number is not None and 0 <= number <= _MAX_UC else 1
            return True
        text = _CHARACTERS.get(word)
        if text is not None:
            self._deliver(text)
            return True
        return False

    # ---- Control words in the body -----------------------------------------------------------------------------------

    def _body_word(self, word: bytes, number: int | None, star: bool) -> None:
        state, flow = self._state, self._flow
        paragraph = flow.paragraph
        on = number is None or number != 0
        if word == b"par" or word == b"sect":
            flow.end_paragraph()
        elif word == b"pard":
            flow.paragraph = _Paragraph()
        elif word == b"plain":
            state.bold = state.italic = state.hidden = False
            state.font = None
        elif word == b"b":
            state.bold = on
        elif word == b"i":
            state.italic = on
        elif word == b"v" or word == b"deleted":
            # Hidden text and tracked deletions are not text of the document (DOCX leaves out w:vanish and w:del the same way).
            state.hidden = on
        elif word == b"f":
            state.font = number
        elif word == b"s":
            paragraph.style = number
        elif word == b"outlinelevel":
            paragraph.outline = number
        elif word == b"ls":
            paragraph.ls = number
        elif word == b"ilvl":
            paragraph.ilvl = number or 0
        elif word == b"intbl":
            paragraph.intbl = True
        elif word == b"itap":
            paragraph.itap = number or 0
        elif word == b"li":
            paragraph.indent = number or 0
        elif word == b"cell":
            flow.end_cell(nested=False)
        elif word == b"row":
            flow.end_row(nested=False)
        elif word == b"nestcell":
            flow.end_cell(nested=True)
        elif word == b"nestrow":
            flow.end_row(nested=True)
        elif word == b"trowd":
            (flow.nested_row if state.nested_row else flow.row).start()
        elif word == b"cellx":
            (flow.nested_row if state.nested_row else flow.row).add(number)
        elif word in _MERGE_FLAGS:
            definition = flow.nested_row if state.nested_row else flow.row
            definition.flags |= _MERGE_FLAGS[word]
        elif word == b"nesttableprops":
            state.nested_row = True
        elif word == b"pict":
            self._open_picture()
        elif word == b"field":
            if state.closer is None:
                state.closer = "field"
                self._fields.append([[], 0, -1, None])
        elif word == b"fldinst":
            state.dest = _FLDINST
        elif word == b"fldrslt":
            if self._fields:
                # The address is worked out once per instruction, not at every `\fldrslt`: a 3 MB instruction followed by 1,000
                # results took 11 s (Pinned by: test_rtf.py::TestHostileInput::test_long_field_instruction_with_many_results_is_cheap).
                field = self._fields[-1]
                if field[2] != field[1]:
                    field[2], field[3] = field[1], field_link("".join(field[0]))
                state.link = field[3] or state.link
        elif word in _NOTE_WORDS:
            # A footnote inside a footnote cannot be referenced from Markdown; it is left out.
            self._open_flow("note" if self.options.include_footnotes and flow.kind != "note" else None)
        elif word in _HEADER_WORDS:
            self._open_flow("header" if self.options.include_headers_footers and flow.kind == "body" else None)
        elif word in _FOOTER_WORDS:
            self._open_flow("footer" if self.options.include_headers_footers and flow.kind == "body" else None)
        elif word == b"shpinst" or word == b"do":
            state.dest = _SHAPE
        elif word == b"shptxt" or word == b"dptxbxtext":
            self._open_flow("box")
        elif word == b"pn":
            state.dest = _PN
        elif word == b"fonttbl":
            state.dest = _FONTTBL
        elif word == b"stylesheet":
            state.dest = _STYLESHEET
        elif word == b"info":
            state.dest = _INFO
        elif word == b"listtable":
            state.dest = _LISTTABLE
        elif word == b"listoverridetable":
            state.dest = _LISTOVERRIDE
        elif word == b"ansicpg":
            self._document_codec = _page_codec(number or 0) or self._document_codec
        elif word == b"mac":
            self._document_codec = "mac_roman"
        elif word == b"pc":
            self._document_codec = "cp437"
        elif word == b"pca":
            self._document_codec = "cp850"
        elif word == b"deff":
            self._default_font = number
        elif word in _SKIPPED_WORDS or (star and word not in _TRANSPARENT_WORDS and not (star == 2 and word in _STYLE_WORDS)):
            # `\*` marks a destination that may be ignored when it is not known: its text is not body text.
            state.dest = _SKIP

    def _other_word(self, dest: int, word: bytes, number: int | None, star: bool) -> None:
        state = self._state
        if dest == _FONTTBL:
            if word == b"f":
                self._font = number
            elif word == b"fcharset" and self._font is not None:
                page = _CHARSET_PAGES.get(number or 0)
                self._fonts[self._font] = _ANSI if number == 0 else _page_codec(page) if page else None
            elif word == b"cpg" and self._font is not None:
                self._fonts[self._font] = _page_codec(number or 0) or self._fonts.get(self._font)
            elif star or word in _SKIPPED_WORDS:
                state.dest = _SKIP
        elif dest == _STYLESHEET:
            style = self._style
            if style is None:
                pass
            elif word == b"s":
                style[0] = number or 0
            elif word in (b"cs", b"ds", b"ts"):
                style[4] = False
            elif word == b"outlinelevel":
                style[2] = number
            elif word == b"sbasedon":
                style[3] = number
            elif star:
                state.dest = _SKIP
        elif dest == _INFO:
            if word in (b"title", b"author") and state.closer is None:
                state.dest = _INFO_TEXT
                state.closer = "info"
                self._info_key = word.decode("ascii")
                self._info = []
            elif star:
                state.dest = _SKIP
        elif dest == _PICT:
            if star or word not in _PICTURE_KINDS and word in _SKIPPED_WORDS:
                state.dest = _SKIP
        elif dest == _LISTTABLE:
            if word == b"list":
                self._list = []
            elif self._list is None:
                pass
            elif word == b"listlevel":
                if len(self._list) < _MAX_LIST_LEVELS:
                    self._list.append([0, 1])
            elif word in (b"levelnfc", b"levelnfcn"):
                if self._list:
                    self._list[-1][0] = number or 0
            elif word == b"levelstartat":
                if self._list:
                    self._list[-1][1] = number if number is not None and 0 <= number <= 1_000_000 else 1
            elif word == b"listid" and number is not None:
                self._lists[number] = self._list
        elif dest == _LISTOVERRIDE:
            if word == b"listoverride":
                self._override_id = None
            elif word == b"listid":
                self._override_id = number
            elif word == b"ls" and number is not None and self._override_id is not None:
                self._overrides[number] = self._override_id
        elif dest == _PN:
            paragraph = self._flow.paragraph
            if word == b"pnlvlblt":
                paragraph.pn = "-"
            elif word in _NUMBERED_PN:
                paragraph.pn = paragraph.pn or "1"
            elif word == b"pnlvl":
                paragraph.pn = paragraph.pn or "1"
                paragraph.pn_level = min(max((number or 1) - 1, 0), _MAX_LEVEL)
        elif dest == _SHAPE:
            if word == b"shptxt" or word == b"dptxbxtext":
                state.dest = _BODY
                self._open_flow("box")
            elif word == b"pict":
                self._open_picture()
            elif word == b"shprslt" or (star and word not in _TRANSPARENT_WORDS):
                state.dest = _SKIP
        elif star:  # field instructions, document properties
            state.dest = _SKIP

    # ---- Destinations that end with their group ----------------------------------------------------------------------

    def _open_picture(self) -> None:
        state = self._state
        if state.closer is not None:
            state.dest = _SKIP
            return
        state.dest = _PICT
        state.closer = "picture"
        self._pictures.append([])

    def _open_flow(self, kind: str | None) -> None:
        state = self._state
        if kind is None or state.closer is not None:
            state.dest = _SKIP
            return
        state.closer = "flow"
        state.dest = _BODY
        # The text of a header, footnote or shape starts with plain formatting, whatever the run that anchors it looks like.
        state.bold = state.italic = state.hidden = False
        state.link = None
        self._flow = _Flow(self, kind)
        self._flows.append(self._flow)

    def _finish(self, closer: str) -> None:
        if closer == "picture":
            data = b"".join(self._pictures.pop())
            if data and self.options.extract_images:
                # A file of nothing but tiny pictures would otherwise register millions of them
                # (Pinned by: test_rtf.py::TestPictures::test_pictures_past_the_limit_are_left_out_with_a_warning).
                if self._picture_count >= _MAX_PICTURES:
                    self._too_many_pictures = True
                    return
                self._picture_count += 1
                # A picture inside a link stays in the span of the link, as in DOCX: the shared rules keep it out of the brackets
                # at the edge and write no link around a picture alone.
                self._flow.add_run(f" {self.extraction.add_image(data)} ", False, False, self._state.link, alone=True)
        elif closer == "flow":
            flow = self._flows.pop()
            self._flow = self._flows[-1]
            blocks = flow.finish()
            if flow.kind == "note":
                # The reference goes where the footnote group is; numbers follow the order of reference. A footnote without text
                # leaves no dangling `[^n]`.
                text = " ".join(" ".join(block.split()) for block in blocks).strip()
                if text:
                    self._notes.append(text)
                    link = self._stack[-1].link if self._stack else None  # of the text around the footnote group
                    self._flow.add_run(f"[^{len(self._notes)}]", False, False, link, alone=True)
            elif flow.kind == "box":
                self._flow.add_blocks(blocks)
            else:
                # The header of the first page and of left/right pages usually repeat the same text: each text once.
                target = self._headers if flow.kind == "header" else self._footers
                text = join_blocks(blocks)
                if text:
                    target.setdefault(text)
        elif closer == "field":
            self._fields.pop()
        elif closer == "info":
            value = " ".join("".join(self._info).split())
            if value:
                self.extraction.metadata.setdefault(self._info_key, value)
            self._info = []
        elif closer == "style":
            style, self._style = self._style, None
            if style is not None and style[4]:
                name = "".join(style[1]).split(";", 1)[0].split(",", 1)[0].strip()  # 'heading 1,H1;': ends with ';', may list aliases
                listed = _LIST_STYLE_LEVEL.fullmatch(name)
                self._styles[style[0]] = (_style_heading(name, style[2]), style[3], int(listed.group(1)) - 1 if listed else 0)

    # ---- Headings and lists ------------------------------------------------------------------------------------------

    def heading_level(self, paragraph: _Paragraph) -> int | None:
        """`\\outlinelevelN` on the paragraph wins (0 is level 1; 9 and above mean body text); otherwise the paragraph style or one
        it is based on, by name ('heading 2', '제목 2', 'Title') or by its own outline level — the DOCX rule."""
        if paragraph.outline is not None:
            return min(paragraph.outline + 1, 6) if 0 <= paragraph.outline < 9 else None
        number = paragraph.style
        for _ in range(10):
            entry = self._styles.get(number) if number is not None else None
            if entry is None:
                return None
            if entry[0]:
                return entry[0]
            number = entry[1]
        return None

    def end_simple_numbering(self) -> None:
        self._simple_counters = [None] * (_MAX_LEVEL + 1)

    def list_mark(self, paragraph: _Paragraph) -> tuple[str, int] | None:
        """('-' or 'N.', level) of a list paragraph, None for a paragraph outside a list.

        The kind comes from the list table (`\\lsN` → list override → list → `\\levelnfcN`: 23 is a bullet, 255 no number, the rest
        are numbers); a list that is not in the table is a bullet list. Numbers are counted here (the `\\listtext` copy of the
        writer is skipped, it is also what a bullet glyph in a symbol font would come from) and are always written as `N.`, the
        only form Markdown reads as an ordered item (Pinned by: test_rtf.py::TestLists).
        """
        if paragraph.ls is not None:
            level = depth = min(max(paragraph.ilvl, 0), _MAX_LEVEL)
            if level == 0 and paragraph.style in self._styles:
                # The kind still comes from the level the paragraph names; only the indentation follows the style name
                # (Pinned by: test_rtf.py::TestLists::test_list_bullet_2_style_is_one_level_down).
                depth = self._styles[paragraph.style][2]
            levels = self._lists.get(self._overrides.get(paragraph.ls, -1))
            if not levels or level >= len(levels) or levels[level][0] == 23:
                return "-", depth
            if levels[level][0] == 255:
                return None
            counters = self._counters.setdefault(paragraph.ls, [None] * (_MAX_LEVEL + 1))
            return self._next_number(counters, level, levels[level][1]), depth
        if paragraph.pn is None:
            self.end_simple_numbering()
            return None
        if paragraph.pn == "-":
            return "-", paragraph.pn_level
        return self._next_number(self._simple_counters, paragraph.pn_level, 1), paragraph.pn_level

    def typed_level(self, indent: int) -> int:
        indents = self.typed_indents
        while indents and indents[-1] > indent:
            indents.pop()
        if not indents or indents[-1] < indent:
            indents.append(indent)
        return min(len(indents) - 1, _MAX_LEVEL)

    @staticmethod
    def _next_number(counters: list[int | None], level: int, start: int) -> str:
        current = counters[level]
        counters[level] = start if current is None else current + 1
        for deeper in range(level + 1, len(counters)):
            counters[deeper] = None
        return f"{counters[level]}."

    def count(self, text: str) -> None:
        if self.extraction.count(solid_length(text), self.options):
            self.stopped = True

    # ---- Result ------------------------------------------------------------------------------------------------------

    def result(self) -> str:
        body = join_blocks(self._body.finish())
        warnings = self.extraction.warnings
        if self._truncated:
            warnings.append(_TRUNCATED)
        if self._too_deep:
            warnings.append(_TOO_DEEP)
        if self._too_many_pictures:
            warnings.append(_TOO_MANY_PICTURES)
        if self.omitted_rows:
            warnings.append(omitted_rows_warning(self.omitted_rows))
        if self.table_cut:
            warnings.append(_TABLE_CUT)
        notes = "\n".join(f"[^{number}]: {text}" for number, text in enumerate(self._notes, 1))
        return join_blocks([join_blocks(self._headers), body, join_blocks(self._footers), notes])


def _style_heading(name: str, outline: int | None) -> int | None:
    """Heading level of a paragraph style: by its name, else by its own outline level."""
    match = _HEADING_NAME.fullmatch(name)
    if match:
        return min(int(match.group(1)), 6)
    if name.lower() in _TITLE_NAMES:
        return 1
    if outline is not None and 0 <= outline < 9:
        return min(outline + 1, 6)
    return None


def extract_rtf(data: bytes, options: ConvertOptions) -> Extraction:
    # The header may follow a byte order mark or blank lines (the same window `detect` looks at).
    start = data.find(_MAGIC, 0, 256)
    if start < 0 or data[:start].strip(b"\xef\xbb\xbf \t\r\n"):
        raise CorruptedDocumentError("The RTF document structure could not be read.")

    extraction = Extraction()
    reader = _Reader(data, start, options, extraction)
    reader.read()
    extraction.sections.append(Section(SectionKind.DOCUMENT, 1, reader.result()))
    return extraction
