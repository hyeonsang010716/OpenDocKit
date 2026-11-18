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
from opendockit.extractors._lists import is_placeholder_only, paragraph_head
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
# The text that follows a place directly, read ahead a little (`_Reader._text_ahead`): some `\'hh`, or a bounded piece of plain text
_TEXT_AHEAD = re.compile(rb"((?:\\'[0-9a-fA-F]{2}){1,16})|([^\\{}]{1,16})")
_TEXT_AHEAD_BYTES = 16
_BYTES = [bytes([value]) for value in range(256)]

_MAGIC = b"{\\rtf"
# Groups nested deeper than this are not read (one warning). Real documents stay below 50; the stack is a list, so the limit bounds
# memory, not recursion (Pinned by: test_rtf.py::TestHostileInput::test_groups_nested_past_the_limit_are_skipped_with_a_warning).
_MAX_GROUP_DEPTH = 500
_MAX_TABLE_DEPTH = 16
_MAX_ROW_CELLS = 4096
# Cells one table may hold, the cells of the tables inside it included. `\cell` is five bytes and a cell of the grid costs
# several hundred, text or no text: 4,000 rows of 1,000 empty cells (20 MB) took 13 s and 3.4 GB. The cells past the limit are
# left out — with or without a `\row` after them — and one warning says so
# (Pinned by: test_rtf.py::TestHostileInput::test_cells_past_the_cell_limit_of_a_table_are_left_out_with_a_warning).
_MAX_TABLE_CELLS = 500_000
# Tables one top-level table may hold inside it, at any depth. The depth is set per paragraph, so `\itap16 x\nestcell\itap1
# \cell` (31 bytes) opens and closes 15 tables, each a grid of its own drawn into the one around it: 80,000 of them (2.5 MB)
# took 11 s and 1.8 GB. The tables past the limit are left out with their text, and one warning says so
# (Pinned by: test_rtf.py::TestHostileInput::test_tables_past_the_nested_table_limit_are_left_out_with_a_warning).
_MAX_NESTED_TABLES = 10_000
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
    b"picprop", b"htmltag", b"mhtmltag", b"ud",   # `\ud` is read only as the second copy of a `\upr` group (`_Reader._upr`)
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
# The control symbols whose byte can be the trail byte of a double-byte character (`_Reader._completes`).
_TRAIL_SYMBOLS = frozenset((b"\\", b"{", b"}"))

# Windows code page of a font charset (`\fcharsetN`). 0 (ANSI) is decided at decode time (`_ANSI`); 1 (default) and 2 (symbol) use
# the document's code page.
_CHARSET_PAGES = {77: 10000, 128: 932, 129: 949, 130: 1361, 134: 936, 136: 950, 161: 1253, 162: 1254, 163: 1258, 177: 1255, 178: 1256,
                  186: 1257, 204: 1251, 222: 874, 238: 1250, 254: 437, 255: 850}
_PAGE_CODECS = {932: "cp932", 936: "gbk", 949: "cp949", 950: "cp950", 1361: "johab", 10000: "mac_roman", 10001: "shift_jis",
                10003: "euc_kr", 10008: "gb2312", 20127: "ascii", 28591: "latin-1", 65001: "utf-8"}
_DOUBLE_BYTE = frozenset(("cp932", "gbk", "cp949", "cp950", "johab"))
_ANSI = ""  # a font with `\fcharset0`
_LINE_SEPARATORS = frozenset("\u2028\u2029")  # a line break wherever they stand (`_Reader._unicode`)

_H_FIRST, _H_CONTINUES, _V_FIRST, _V_CONTINUES = 1, 2, 4, 8
_MERGE_FLAGS = {b"clmgf": _H_FIRST, b"clmrg": _H_CONTINUES, b"clvmgf": _V_FIRST, b"clvmrg": _V_CONTINUES}

_TRUNCATED = "The RTF document ends before it is complete; only the part that could be read was converted."
_TOO_DEEP = f"Groups nested deeper than {_MAX_GROUP_DEPTH} levels were not read."
_TABLE_CUT = f"A table has more than {_MAX_TABLE_CELLS:,} cells; the cells after that were left out."
_NESTED_CUT = f"A table holds more than {_MAX_NESTED_TABLES:,} tables inside it; the tables after that were left out."
_TOO_MANY_PICTURES = f"The document has more than {_MAX_PICTURES:,} pictures; the rest were left out."


# The characters an RTF file is written in: letters and digits, and the backslash and braces of its syntax. A code page that reads
# them as other characters is not one RTF text can be in (CP864 draws '%' differently and is still a real code page).
_RTF_ASCII = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789\\{} "


def _reads(data: bytes, codec: str, cut: bool) -> bool:
    """Are these bytes text of the code page (`cut`: they may end inside a character)?"""
    try:
        codecs.getincrementaldecoder(codec)().decode(data, not cut)
    except UnicodeDecodeError:
        return False
    return True


@lru_cache(maxsize=256)
def _page_codec(page: int) -> str | None:
    """Python codec of a Windows code page number, None when there is none (the document's code page is used then).

    A code page that does not read letters, digits, backslash and braces as ASCII (EBCDIC: 500, 875, 1026, …) is none: RTF
    is ASCII text, and once a run held one 8-bit byte the whole run, letters included, was decoded as EBCDIC — 'hello'
    became 'ΪΧ%%?'. Only those characters are compared: CP864 draws '%' as '٪' and is still a code page RTF can be in
    (Pinned by: test_rtf.py::TestCharacters::test_code_page_that_is_not_ascii_compatible_is_ignored)."""
    if not 0 < page < 100_000:
        return None
    try:
        name = codecs.lookup(_PAGE_CODECS.get(page) or f"cp{page}").name
        return name if _RTF_ASCII.decode(name) == _RTF_ASCII.decode("ascii") else None
    except (LookupError, UnicodeDecodeError):
        return None


class _State:
    """What a group scopes: the destination and the character formatting."""

    __slots__ = ("dest", "bold", "italic", "hidden", "font", "uc", "link", "closer", "nested_row", "ud", "in_upr")

    def __init__(self) -> None:
        self.dest = _BODY
        self.bold = self.italic = self.hidden = False
        self.font: int | None = None
        self.uc = 1
        self.link: str | None = None
        self.closer: str | None = None  # what to finish when this group closes (the group that opened a picture, a footnote, …)
        self.nested_row = False  # inside `\*\nesttableprops`: row definitions belong to the nested table
        self.ud: int | None = None  # a `\upr` group read through its `\ud` copy: the destination that copy is read into
        self.in_upr = False  # inside a `\upr` group already looked ahead of: a `\upr` here is not looked ahead of again

    def child(self) -> "_State":
        state = _State.__new__(_State)
        state.dest, state.bold, state.italic, state.hidden = self.dest, self.bold, self.italic, self.hidden
        state.font, state.uc, state.link, state.nested_row = self.font, self.uc, self.link, self.nested_row
        state.closer, state.ud, state.in_upr = None, None, self.in_upr
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
    __slots__ = ("grid", "rows", "cells", "cell", "budget", "stops_reading", "full", "root", "used", "nested", "dropped", "holds")

    def __init__(self, budget: TableBudget | None, root: "_Table | None" = None, *, dropped: bool = False,
                 stops_reading: bool = False) -> None:
        self.grid = TableGrid()
        self.rows: list[tuple[list[str], tuple[tuple[int | None, int], ...]]] = []
        self.cells: list[str] = []  # finished cells of the row being read
        self.cell: list[str] = []   # paragraphs of the cell being read
        self.budget = budget
        self.stops_reading = stops_reading  # past its character limit the reading stops (a table of the body or a header) …
        self.full = False                   # … or only this top-level table takes no more cells (a footer or footnote table)
        # The top-level table: it counts the cells (`_MAX_TABLE_CELLS`) and the nested tables (`_MAX_NESTED_TABLES`) of every
        # table inside it as well as its own
        self.root = root or self
        self.used = 0      # on the root: cells kept so far
        self.nested = 0    # on the root: tables opened inside it
        self.dropped = dropped  # a nested table past the limit (or inside one): nothing of it is kept
        self.holds = False  # the cell being read holds a nested table

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


def _takes_cells(table: _Table) -> bool:
    """Whether the text of a cell of `table` is kept: not inside a nested table past the limits, nor in a table past its character
    limit. The text of a cell that is not kept is not rendered — a 100,000-row footer table past its limit took 4.1 s rendering
    the paragraphs of cells it then threw away."""
    return not table.dropped and not table.root.full


class _Flow:
    """One stream of paragraphs and tables: the body, a header or footer, a footnote, the text of a shape."""

    def __init__(self, reader: "_Reader", kind: str, home: "_Flow | None" = None) -> None:
        self.reader = reader
        self.kind = kind  # "body", "header", "footer", "note", "box"
        # Where the text ends up: the text of a shape goes to the flow that anchors it, so a box has the home of that flow.
        self.home = home.home if home is not None else kind
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
            if not self.tables:
                # A top-level table watches the character limit while its cells are read (`_notation.TableBudget`). In the body —
                # also inside a text box there, whose text ends up in the body — it is charged with the characters counted so far,
                # and reading stops at the limit. A header, footer or footnote is written before or after the body but read where
                # it stands in the file, so its table is checked against its own characters only: charged with the body's, it
                # stopped short and the cut text was not the front of the full conversion. Past that, a header table stops the
                # reading (the headers come first, so the output is cut inside them); a footer or footnote table only stops taking
                # cells, since the body after it in the file is written before it (Pinned by: test_rtf.py::TestLimits::
                # test_table_in_a_text_box_stops_at_the_limit, test_tables_of_headers_footers_and_notes_stop_at_their_own_limit).
                # A 100,000-row table in a text box, header or footer was built whole: 4.4 s and 3.7 million characters counted
                # under a 5,000-character limit.
                reader = self.reader
                budget = TableBudget(reader.extraction if self.home == "body" else Extraction(), reader.options)
                self.tables.append(_Table(budget, stops_reading=self.home in ("body", "header")))
                continue
            # A table inside a table counts against the top-level table it is in (`_MAX_NESTED_TABLES`).
            outer = self.tables[-1]
            root = outer.root
            if root.full:   # a table past its character limit takes nothing more, the tables inside it neither
                self.tables.append(_Table(None, root, dropped=True))
                continue
            dropped = outer.dropped or root.nested >= _MAX_NESTED_TABLES
            if dropped and not outer.dropped:
                self.reader.nested_cut = True
            elif not dropped:
                root.nested += 1
            self.tables.append(_Table(None, root, dropped=dropped))

    def _close_table(self) -> None:
        table = self.tables.pop()
        if table.dropped:
            return
        if table.cell:  # text after the last `\cell` is a cell (`_Table.build`), inside the limit like any other
            self._keep(table, "\n".join(table.cell))
            table.cell = []
        grid = table.build()
        if self.tables:
            # A table inside a cell is drawn in place by the outer table, as in DOCX
            # (Pinned by: test_rtf.py::TestTables::test_nested_table_keeps_its_rows_and_columns).
            outer = self.tables[-1]
            placeholder = outer.grid.embed(grid)
            if placeholder:
                outer.cell.append(placeholder)
                outer.holds = True
        elif self.kind == "note":
            # A table inside a footnote is one line, as in DOCX/HWP (`_notation.note_table` shape: cells ' / ', rows '; ').
            self.add_block(NOTE_ROW_SEPARATOR.join(line for line in grid.flatten(self.reader.options.max_chars).split("\n") if line.strip()))
        else:
            self.add_block(grid.render(room=table.budget.room if table.budget is not None else None))
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
                if not self.tables[-1].dropped:
                    self.tables[-1].cell.append(block)
            else:
                self.add_block(block)

    def end_paragraph(self, *, marked: bool = False) -> None:
        """`marked`: the paragraph ends at its own paragraph mark (`\\par`), so it is there even when it is empty."""
        depth = self._depth()
        self._enter(depth)
        if depth:
            table = self.tables[-1]
            text = self._paragraph_text(in_cell=True, kept=_takes_cells(table), marked=marked)
            if text:
                table.cell.append(text)
            return
        text = self._paragraph_text(in_cell=False, marked=marked)
        if text:
            self.add_block(text)

    def end_cell(self, *, nested: bool) -> None:
        self._enter(max(self._depth(), 2) if nested else 1)
        table = self.tables[-1]
        text = self._paragraph_text(in_cell=True, kept=_takes_cells(table))
        if text:
            table.cell.append(text)
        cell = "\n".join(table.cell)
        table.cell = []
        if table.dropped or not self._keep(table, cell):
            return
        if table.budget is not None and table.budget.add(len(table.rows), len(table.cells) - 1, cell):
            if table.stops_reading:
                self.reader.stopped = True
            else:
                table.full = True

    def _keep(self, table: _Table, cell: str) -> bool:
        """Add a finished cell to the row being read, if the top-level table has room for it (`_MAX_TABLE_CELLS`, shared with
        the tables inside it: each nested table used to have a limit of its own
        (Pinned by: test_rtf.py::TestHostileInput::test_cells_of_nested_tables_count_against_the_cell_limit_of_the_outer_table))."""
        holds, table.holds = table.holds, False
        if table.root.full:
            return False
        if table.root.used >= _MAX_TABLE_CELLS:
            self.reader.table_cut = True   # the table is full: this cell and the ones after it are left out …
            if not holds:
                return False
            # … except a cell that holds a nested table: the cells of that table were kept inside the limit. Only the tables
            # open when the limit was reached can still hold kept cells, so this adds at most `_MAX_TABLE_DEPTH` cells.
        table.root.used += 1
        table.cells.append(cell)
        return True

    def end_row(self, *, nested: bool) -> None:
        depth = min(max(self._depth(), 2), len(self.tables)) if nested else 1
        if len(self.tables) < depth or (nested and depth < 2):
            return
        self._enter(depth)
        table = self.tables[-1]
        if table.dropped:
            return
        if table.cell:
            self._keep(table, "\n".join(table.cell))
            table.cell = []
        if table.cells:
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

    def _count_empty_item(self, *, in_cell: bool) -> None:
        """An empty paragraph of a list still takes its number, as Word draws it (DOCX counts it too); it is not written. Not
        counted, '3. three' after an empty item came out as '2.'
        (Pinned by: test_rtf.py::TestLists::test_empty_numbered_paragraph_still_takes_its_number). Only a paragraph ended by
        its own `\\par`: the end of a flow, a `\\cell` or a `\\sect` right after a `\\par` close no paragraph of the
        document, and counted they would take a number from the list the properties still name. One whose paragraph mark is
        hidden or deleted (`\\v`, `\\deleted` in force at `\\par`) is not in the document and is not counted either."""
        paragraph, reader = self.paragraph, self.reader
        if self.kind == "note" or (paragraph.ls is None and paragraph.pn is None) or reader.mark_hidden():
            return
        if in_cell or not reader.heading_level(paragraph) or paragraph.ls is not None:
            reader.list_mark(paragraph)

    def _paragraph_text(self, *, in_cell: bool, kept: bool = True, marked: bool = False) -> str:
        """The Markdown of the paragraph that ends. With `kept` False its text is not wanted (a cell of a table that takes no more
        cells): only what the paragraph changes for the ones after it — list numbers, a heading ending a numbering — is done.
        `marked`: it ends at its own `\\par` (`end_paragraph`)."""
        self._settle()
        runs = self.runs
        self.runs = []
        reader, paragraph, options = self.reader, self.paragraph, self.reader.options
        # What the paragraph is (empty, a heading, a list item) is decided on the text without marks, so the decisions are the
        # same with the options off — as in DOCX.
        plain = "".join(run[0] for run in runs)
        first_line = next((line for line in (" ".join(line.split()) for line in plain.split("\n")) if line), "")
        if not first_line:
            if marked:
                self._count_empty_item(in_cell=in_cell)
            return ""
        level = mark = None
        if self.kind != "note":
            if not in_cell and len(plain) <= _HEADING_MAX_CHARS:
                level = reader.heading_level(paragraph)
            if level:
                reader.end_simple_numbering()
                if paragraph.ls is not None:
                    # A heading of a list takes its number, which is not written (rtf.md): not counted, the numbers under it
                    # went on from those under the heading before (`# Chapter B` then `3.`, `4.` where Word shows 1, 2), and an
                    # item of the heading's level after them did not count the headings (Pinned by:
                    # test_rtf.py::TestLists::test_numbered_heading_takes_its_number_and_restarts_the_levels_under_it).
                    reader.list_mark(paragraph)
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
        if not kept:
            return ""
        # A heading keeps its links but is not emphasized; bold or italic over the whole paragraph (also a list item, a paragraph
        # of a cell) is how it is styled and is left unmarked (`_inline.resolve_inline`.
        # Pinned by: test_rtf.py::TestEmphasis::test_paragraph_list_item_and_cell_that_are_bold_throughout_are_not_marked).
        text = resolve_inline(_inline_tokens(runs, emphasis=options.keep_emphasis and level is None, links=options.keep_links),
                              emphasis=level is None)
        lines = [line for line in (" ".join(line.split()) for line in text.split("\n")) if line]
        if not lines:
            return ""
        if level and not is_placeholder_only(plain):
            return f"{'#' * level} {' '.join(lines)}"
        # A heading paragraph holding nothing but a picture is written as that picture, with no mark, as in DOCX: it still ends
        # the numbering a heading ends. Taken for a heading, the picture became a block of its own under a bare '#'
        # (Pinned by: test_rtf.py::TestParagraphsAndHeadings::test_heading_holding_only_a_picture_is_not_a_heading).
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
        self.nested_cut = False
        self._truncated = False
        self._too_deep = False

        self._state = _State()
        self._stack: list[_State] = []
        self._overflow = 0  # depth below the group limit
        self._star = False
        self._fresh = False  # nothing but white space has been read since the last `{`
        self._raw: list[bytes] = []  # text bytes waiting to be decoded with the code page of the state they were read in
        # (code page, incremental decoder, how many pieces of `_raw` it was fed) while a control symbol may complete a
        # character (`_completes`)
        self._run_decoder: tuple[str, codecs.IncrementalDecoder, int] | None = None
        self._skip = 0  # fallback characters still to drop after `\uN`
        self._dropped: list[bytes] = []  # … the bytes dropped so far, and those of them that may start the escaped character
        self._lead: bytes | None = None
        self._escaped = ""  # the character the last `\uN` stood for ('' when it gave none)
        self._high = 0  # a high surrogate waiting for its low half

        self._document_codec = "cp1252"
        self._cocoa = False  # written by the macOS text system (`\cocoartf`: TextEdit, Pages, Mail, `textutil`)
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
        # `\ls` number → the `\lfolevel` groups of its override, in level order: [start-at flag, start] (`_level_start`)
        self._override_starts: dict[int, list[list]] = {}
        self._override_levels: list[list] = []  # those of the override being read
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
                if self._skip and chunk:
                    dropped = min(self._skip, len(chunk))
                    self._drop(chunk[:dropped])
                    chunk = chunk[dropped:]
                # The rest of the escaped character the fallback started, as plain 8-bit text in this chunk or at the start of the next
                # (Pinned by: test_rtf.py::TestCharacters::test_fallback_counted_in_characters_drops_the_whole_double_byte_character).
                while self._lead is not None and chunk and self._drop_trail(chunk[:1], chunk, pos):
                    chunk = chunk[1:]
                if chunk:
                    self._raw.append(chunk)
            elif kind == 3:
                if dest == _SKIP or dest == _PICT:
                    continue
                value = _BYTES[int(token.group(3), 16)]
                if self._skip:
                    self._drop(value)
                elif self._lead is None or not self._drop_trail(value, b"", pos):
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
                    symbol = token.group(4)
                    # The trail byte of the double-byte character the run ends in, written as a symbol: part of the run
                    # (`_completes`; the dropping of a `\uN` fallback is left as it is).
                    if symbol in _TRAIL_SYMBOLS and not self._skip and self._lead is None and self._completes(symbol):
                        self._raw.append(symbol)
                        self._star = False
                        continue
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
                    if word == b"ud" and self._stack and self._stack[-1].ud is not None:
                        state.dest = self._stack[-1].ud   # the Unicode copy of a `\upr` group (`_upr`)
                    continue
                elif word == b"upr" and dest != _PICT:
                    self._upr(pos, fresh)
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

    def _upr(self, pos: int, first: bool) -> None:
        """`{\\upr{ANSI copy}{\\*\\ud{Unicode copy}}}`: the same text twice, for readers that do not know `\\uN` and for those that
        do. The Unicode copy is read and the ANSI copy skipped: it has '?' for every character its code page lacks
        (Pinned by: test_rtf.py::TestCharacters::test_upr_group_is_read_through_its_unicode_copy). Only that shape is read
        this way — `\\upr` the first word of its group (`first`), which holds nothing but the two copies (or the `\\ud` copy
        alone) and white space. Anything else is read as it is, through the ANSI copy: skipping the group from the `\\upr` on
        lost the text after the copies (`{\\b \\upr{…}{\\*\\ud{…}} tail}`), and the rest of the document for a `\\upr` written
        straight into the body. A `\\upr` inside a `\\upr` group is read as it is too: looking ahead of each of those again would
        read the same bytes once per level of nesting."""
        state = self._state
        if state.in_upr or not first:
            return
        state.in_upr = True
        if self._ud_follows(pos):
            state.ud, state.dest = state.dest, _SKIP

    def _ud_follows(self, pos: int) -> bool:
        """Is the rest of the group being read, from `pos` to its end, an ANSI copy group and a `\\ud` group (`\\*` and white
        space may stand in front of `\\ud`), or a `\\ud` group alone, with only white space between and around them, no `\\ud`
        inside the ANSI copy and text in the `\\ud` copy? Each byte is looked at here once at most: a `\\upr` inside the group is not
        looked ahead of again
        (Pinned by: test_rtf.py::TestHostileInput::test_upr_groups_are_looked_ahead_of_once)."""
        data, size, match = self._data, len(self._data), _TOKEN.match
        depth, groups, ud, text = 1, 0, False, False
        opening = False  # only `\*` and white space read since a group directly inside opened
        while pos < size:
            token = match(data, pos)
            if token is None:
                return False
            pos = token.end()
            kind = token.lastindex
            if kind == 5:
                if token.group(5) == b"}":
                    depth -= 1
                    if not depth:
                        return ud and text
                    opening = False
                    continue
                if depth == 1:
                    if ud or groups == 2:  # a group after the `\\ud` copy, or a second group that is not one
                        return False
                    groups += 1
                depth += 1
                opening = depth == 2
                continue
            if depth == 1:
                if kind == 6 and not token.group(6).strip():
                    continue
                return False  # text or a control word beside the copies
            if kind == 2 and token.group(1) == b"bin":
                pos = min(pos + max(int(token.group(2)), 0), size)
            if kind <= 2 and token.group(1) == b"ud":
                if not ud and not opening:
                    return False  # a `\ud` inside the ANSI copy: read as it is, through the ANSI copy
                ud = True  # (one inside the `\ud` copy belongs to a `\upr` there)
            elif ud and not text:
                # The `\ud` copy holds text (a letter, an escaped byte or `\uN`). An empty one is not read: its ANSI copy has
                # the text (`{\upr{A}{\*\ud{}}}` came out empty)
                text = (kind == 6 and bool(token.group(6).strip())) or kind == 3 or (kind <= 2 and token.group(1) == b"u")
            if opening:
                opening = (kind == 4 and token.group(4) == b"*") or (kind == 6 and not token.group(6).strip())
        return False

    def _drop(self, data: bytes) -> None:
        """Drop fallback bytes after `\\uN`. When they are only the start of the escaped character in the run's code page, the
        bytes that complete it are dropped too (`_drop_trail`): writers that count `\\ucN` in characters, not bytes, write
        `\\uc1\\u44032\\'b0\\'a1`, and the stray second byte came out as '�' (Pinned by:
        test_rtf.py::TestCharacters::test_fallback_counted_in_characters_drops_the_whole_double_byte_character).
        The dropped bytes are only looked at when a byte follows them directly; after a correct fallback the next thing is
        nearly always a control word, which ends the dropping, so they cost nothing to decode."""
        self._skip -= len(data)
        self._dropped.append(data)
        if not self._skip:
            dropped = b"".join(self._dropped) if len(self._dropped) > 1 else data
            self._dropped = []
            # The usual '?': ASCII starts no multi-byte character (every code page read here is ASCII-compatible)
            self._lead = None if dropped.isascii() else dropped

    def _drop_trail(self, byte: bytes, rest: bytes, pos: int) -> bool:
        """True (and the byte is dropped) when the dropped bytes (`_lead`) followed by `byte` are the escaped character itself
        in the run's code page, or the start of it (UTF-8, three bytes or more). Anything else ends the dropping and is text.
        `rest[1:]` and the tokens from `pos` are what follows the byte.

        That the pair is *some* character of the code page is not enough: Word writes `\\uc1\\u8217\\'92s` for "’s" in a
        Western font of a Chinese or Korean document, 0x92 0x73 is a GBK character, and the 's' was dropped with the
        fallback (Pinned by: test_rtf.py::TestCharacters::test_one_byte_fallback_never_takes_the_letter_after_it).

        A fallback that is only the first byte of the escaped character, followed by a character that happens to start with
        its second byte, is written with the same bytes. The byte is text then when the text after it cannot be read without
        it and can with it (Pinned by: test_rtf.py::TestCharacters::test_byte_that_the_text_after_it_needs_is_not_dropped).

        One byte that is the escaped character in CP1252 is a whole fallback when the font is Western or not named: '¢'
        is 0xA2 there and 0xA2 0x46 in Big5, and the 'F' of "¢F" is text, as the specification reads it."""
        dropped, self._lead = self._lead or b"", None
        escaped = self._escaped
        if not escaped or byte < b"0":  # no code page continues a character with a space or '!' … '/'
            return False
        font = self._font_codec()
        if not font:
            if len(dropped) == 1 and dropped.decode("cp1252", "replace") == escaped:
                return False
            codec = self._document_codec if font is None else self._ansi_codec()  # as `_codec`
        else:
            codec = font
        try:
            whole = escaped.encode(codec)
        except UnicodeEncodeError:
            whole = b""
        if whole == dropped:  # a fallback counted in bytes
            return False
        candidate = dropped + byte
        if whole.startswith(candidate):
            if len(whole) > len(candidate):
                self._lead = candidate
                return True
        else:
            try:
                # A second encoding of the same character (CP932 and CP950 hold some characters twice)
                if candidate.decode(codec) != escaped:
                    return False
            except UnicodeDecodeError:
                return False
        ahead, cut = self._text_ahead(rest, pos)
        return ahead.isascii() or _reads(ahead, codec, cut) or not _reads(byte + ahead, codec, cut)

    def _text_ahead(self, rest: bytes, pos: int) -> tuple[bytes, bool]:
        """The first bytes (`_TEXT_AHEAD_BYTES` at most) of the text that follows directly — `rest[1:]`, then the plain text and
        `\\'hh` from `pos` up to a control word or brace — and whether they were cut at that length."""
        data, match = self._data, _TEXT_AHEAD.match
        ahead = rest[1:_TEXT_AHEAD_BYTES + 1]
        while len(ahead) < _TEXT_AHEAD_BYTES:
            token = match(data, pos)
            if token is None:
                return ahead, False
            pos = token.end()
            if token.lastindex == 1:
                ahead += bytes.fromhex(token.group(1).replace(b"\\'", b"").decode("ascii"))
            else:
                ahead += token.group(2).translate(None, b"\r\n")
        return ahead[:_TEXT_AHEAD_BYTES], True

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
        self._run_decoder = None
        self._deliver(self._decode(raw))

    def _completes(self, symbol: bytes) -> bool:
        """Whether the byte of the control symbol `\\\\`, `\\{` or `\\}` is the trail byte of a double-byte character whose lead
        byte ends the run read so far (`_raw`): in Shift_JIS, GBK and Big5 a trail byte can be 5C, 7B or 7D, and a writer has
        to write it as the symbol. Delivered as a symbol of its own, the lead byte was decoded alone: 'ソ' (83 5C) came out as
        '�' and a backslash (Pinned by: test_rtf.py::TestCharacters::
        test_double_byte_character_whose_trail_is_an_escaped_symbol_is_one_character). Only a lead byte that makes a character
        with it: after a whole character, after ASCII, in a single-byte code page or where the pair is no character (CP949 has
        no such trail) the symbol is itself (test_escaped_symbol_that_completes_no_character_is_the_symbol). So is it in a font
        with `\\fcharset0`, whose bytes may be Western text: taken as a trail there, 'café{' in a Japanese document became
        'caf' and a kanji.

        The run is fed to an incremental decoder once, a piece at a time as it grows, so a run of many such characters is not
        decoded again at each symbol (Pinned by: test_rtf.py::TestHostileInput::
        test_double_byte_characters_with_an_escaped_trail_take_linear_time)."""
        codec = self._codec()
        if codec not in _DOUBLE_BYTE or self._font_codec() == _ANSI:
            return False
        if self._run_decoder is None or self._run_decoder[0] != codec:
            self._run_decoder = (codec, codecs.getincrementaldecoder(codec)("replace"), 0)
        codec, decoder, fed = self._run_decoder
        for piece in self._raw[fed:]:
            decoder.decode(piece)
        self._run_decoder = (codec, decoder, len(self._raw))
        pending = decoder.getstate()[0]
        if not pending:
            return False
        try:
            (pending + symbol).decode(codec)
        except UnicodeDecodeError:
            return False
        return True

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
            return self._ansi_codec()
        return codec or self._document_codec

    def _ansi_codec(self) -> str:
        """The code page a font with `\\fcharset0` is read with first: the document's when it is double-byte (writers that
        declare every font as ANSI store Hangul in it), otherwise CP1252.

        The macOS text system declares every font as ANSI too, but writes each character outside CP1252 as `\\uN` and only
        CP1252 ones as bytes. Its `\\ansicpg` follows the system language, so on a Korean, Japanese or Chinese Mac a smart
        apostrophe or a no-break space before a letter is a valid pair in the document's code page: "It\\'92s" came out as
        'It뭩' and "5\\'a0kg" as '5쟪g'. In its files the font's bytes are CP1252
        (Pinned by: test_rtf.py::TestCharacters::test_ansi_font_written_by_the_macos_text_system_is_western)."""
        if self._cocoa or self._document_codec not in _DOUBLE_BYTE:
            return "cp1252"
        return self._document_codec

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
        escaped = ""
        if 0xD800 <= code < 0xDC00:
            self._high = code
        elif 0xDC00 <= code < 0xE000:
            if high:
                escaped = chr(0x10000 + ((high - 0xD800) << 10) + (code - 0xDC00))
        elif code:
            escaped = chr(code)
        if escaped:
            # The line and paragraph separators are line breaks, as `\line`: the macOS text system writes `\uc0\u8232` for every
            # line break (TextEdit, Pages, Mail), and a line is joined at its white space, so 'Line one' and 'Line two' ran into one
            # line (Pinned by: test_rtf.py::TestCharacters::test_line_separator_of_the_macos_text_system_is_a_line_break).
            self._deliver("\n" if escaped in _LINE_SEPARATORS else escaped)
        self._skip, self._dropped, self._lead, self._escaped = skip, [], None, escaped

    def _symbol(self, symbol: bytes) -> None:
        if symbol == b"*":
            self._star = True
            return
        self._star = False
        if self._state.dest == _SKIP:
            return
        if symbol in (b"\r", b"\n"):  # a backslash before a line break is `\par`
            if self._state.dest == _BODY:
                self._flow.end_paragraph(marked=True)
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
            flow.end_paragraph(marked=word == b"par")
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
        elif word == b"cocoartf":
            self._cocoa = True
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
                self._override_levels = []
            elif word == b"listid":
                self._override_id = number
            elif word == b"lfolevel":
                if len(self._override_levels) < _MAX_LIST_LEVELS:
                    self._override_levels.append([False, None])
            elif word == b"listoverridestartat":
                if self._override_levels:
                    self._override_levels[-1][0] = True
            elif word == b"levelstartat":
                if self._override_levels:
                    self._override_levels[-1][1] = number if number is not None and 0 <= number <= 1_000_000 else 1
            elif word == b"ls" and number is not None and self._override_id is not None:
                self._overrides[number] = self._override_id
                self._override_starts[number] = self._override_levels
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
        self._flow = _Flow(self, kind, self._flow if kind == "box" else None)
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

    def mark_hidden(self) -> bool:
        """Whether the paragraph mark being read is hidden or deleted text."""
        return self._state.hidden

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
            return self._next_number(counters, level, self._level_start(paragraph.ls, level, levels[level][1])), depth
        if paragraph.pn is None:
            self.end_simple_numbering()
            return None
        if paragraph.pn == "-":
            return "-", paragraph.pn_level
        return self._next_number(self._simple_counters, paragraph.pn_level, 1), paragraph.pn_level

    def _level_start(self, ls: int, level: int, start: int) -> int:
        """The first number of `level` in the list override `\\lsN`: the start-at the override sets for it (Word's 'Set numbering
        value' writes `{\\lfolevel\\listoverridestartat\\levelstartat5}`, the n-th `\\lfolevel` for level n), else the list's
        `start`. Without it, a list set to start at 5 was numbered from 1
        (Pinned by: test_rtf.py::TestLists::test_start_at_of_a_list_override_sets_the_first_number)."""
        overridden = self._override_starts.get(ls, [])
        if level < len(overridden) and overridden[level][0] and overridden[level][1] is not None:
            return overridden[level][1]
        return start

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
        if self.nested_cut:
            warnings.append(_NESTED_CUT)
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
