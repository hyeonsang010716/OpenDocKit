"""OpenDocument (ODT, ODS, ODP) → Markdown.

An ODF package is a ZIP: `content.xml` holds the body (`office:text`, `office:spreadsheet` or `office:presentation`) and the
automatic styles, `styles.xml` the named styles, list styles and master pages (headers and footers), `meta.xml` the document
properties, and `META-INF/manifest.xml` says whether the parts are encrypted. Everything is read with `zipfile` + `lxml`;
decisions are recorded in docs/decisions/odf.md.

The three formats share one renderer (`_Renderer`): paragraphs, lists, tables, frames and notes are the same elements in all of them.
"""

from base64 import b64decode
import binascii
from collections.abc import Iterator
from dataclasses import dataclass, replace
from io import BytesIO
from itertools import groupby
import re
from urllib.parse import unquote
import zipfile
import zlib

from lxml import etree

from opendockit.exceptions import CorruptedDocumentError, EncryptedDocumentError
from opendockit.extractors._charts import embedded_table
from opendockit.extractors._inline import (
    LINK_OPEN,
    SUBSCRIPT,
    SUPERSCRIPT,
    link_close,
    link_target,
    mark_emphasis,
    mark_script,
    mark_strike,
    merge_emphasis_marks,
    resolve_inline,
    resolve_scripts,
    resolve_strikes,
)
from opendockit.extractors._lists import cell_level_mark, format_number, is_placeholder_only, paragraph_head
from opendockit.extractors._notation import TableBudget, comment_line, format_dutmal, note_table, omitted_rows_warning
from opendockit.extractors._package import decimal_int

# Splitting a sheet into tables, title rows and merge filling are the spreadsheet extractor's rules. They are imported as they are
# (private names, nothing edited there) so that an ODS sheet and the XLSX it was saved from come out the same — an exception to the
# dependency direction, like DOCX altChunk → html (docs/decisions/odf.md.
# Pinned by: test_odf.py::TestSpreadsheet::test_two_tables_on_one_sheet_are_split_like_xlsx).
from opendockit.extractors._sheet_layout import _append_sheet, _collect_rows, _warn_once
from opendockit.markdown import TableGrid, join_blocks, merged_copy, render_table, solid_length
from opendockit.models import ConvertOptions, Extraction, Section, SectionKind


_OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
_TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
_TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
_DRAW = "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0"
_STYLE = "urn:oasis:names:tc:opendocument:xmlns:style:1.0"
_FO = "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0"
_SVG = "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
_PRESENTATION = "urn:oasis:names:tc:opendocument:xmlns:presentation:1.0"
_CHART = "urn:oasis:names:tc:opendocument:xmlns:chart:1.0"
_META = "urn:oasis:names:tc:opendocument:xmlns:meta:1.0"
_DC = "http://purl.org/dc/elements/1.1/"
_XLINK = "http://www.w3.org/1999/xlink"
_MATH = "http://www.w3.org/1998/Math/MathML"

_P, _H, _SPAN, _A = f"{{{_TEXT}}}p", f"{{{_TEXT}}}h", f"{{{_TEXT}}}span", f"{{{_TEXT}}}a"
_S, _TAB, _LINE_BREAK = f"{{{_TEXT}}}s", f"{{{_TEXT}}}tab", f"{{{_TEXT}}}line-break"
_LIST, _LIST_ITEM, _LIST_HEADER = f"{{{_TEXT}}}list", f"{{{_TEXT}}}list-item", f"{{{_TEXT}}}list-header"
_NOTE, _NOTE_BODY = f"{{{_TEXT}}}note", f"{{{_TEXT}}}note-body"
_RUBY, _RUBY_BASE, _RUBY_TEXT = f"{{{_TEXT}}}ruby", f"{{{_TEXT}}}ruby-base", f"{{{_TEXT}}}ruby-text"
_SECTION = f"{{{_TEXT}}}section"
_TEXT_STYLE_NAME, _OUTLINE_LEVEL = f"{{{_TEXT}}}style-name", f"{{{_TEXT}}}outline-level"
_TABLE_ELEMENT, _ROW = f"{{{_TABLE}}}table", f"{{{_TABLE}}}table-row"
_CELL, _COVERED = f"{{{_TABLE}}}table-cell", f"{{{_TABLE}}}covered-table-cell"
_ROWS_REPEATED, _COLS_REPEATED = f"{{{_TABLE}}}number-rows-repeated", f"{{{_TABLE}}}number-columns-repeated"
_ROWS_SPANNED, _COLS_SPANNED = f"{{{_TABLE}}}number-rows-spanned", f"{{{_TABLE}}}number-columns-spanned"
_FRAME, _TEXT_BOX, _IMAGE, _OBJECT = f"{{{_DRAW}}}frame", f"{{{_DRAW}}}text-box", f"{{{_DRAW}}}image", f"{{{_DRAW}}}object"
_PAGE, _GROUP, _DRAW_LINK = f"{{{_DRAW}}}page", f"{{{_DRAW}}}g", f"{{{_DRAW}}}a"
_DRAW_PREFIX = f"{{{_DRAW}}}"
_ANNOTATION = f"{{{_OFFICE}}}annotation"
_HREF = f"{{{_XLINK}}}href"
_CLASS, _PLACEHOLDER = f"{{{_PRESENTATION}}}class", f"{{{_PRESENTATION}}}placeholder"
_NOTES = f"{{{_PRESENTATION}}}notes"
_STYLE_NAME, _PARENT_STYLE, _FAMILY = f"{{{_STYLE}}}name", f"{{{_STYLE}}}parent-style-name", f"{{{_STYLE}}}family"

# Wrappers around table rows (header rows, row groups of an outline). Rows are read through them in document order.
_ROW_GROUPS = frozenset(f"{{{_TABLE}}}{name}" for name in ("table-header-rows", "table-rows", "table-row-group"))
# Wrappers around body blocks: sections and the generated indexes (a table of contents keeps its entries as paragraphs inside
# `text:index-body`). Their `…-source` children are the index settings, not content, and are not descended into.
_BLOCK_WRAPPERS = frozenset(f"{{{_TEXT}}}{name}" for name in (
    "section", "index-body", "index-title", "table-of-content", "illustration-index", "table-index", "object-index", "user-index",
    "alphabetical-index", "bibliography", "numbered-paragraph"))
# Inline elements whose text is not document content: the page number fields hold the number cached at the last save, and the
# citation of a note is replaced by `[^n]`.
_SKIPPED_INLINE = frozenset(f"{{{_TEXT}}}{name}" for name in ("page-number", "page-count", "note-citation", "script"))
# Placeholders of a slide or notes page that are not content (the same ones the PPTX extractor leaves out).
_SKIPPED_CLASSES = frozenset(("header", "footer", "date-time", "page-number", "page", "notes", "handout"))
_HEADERS = tuple(f"{{{_STYLE}}}{name}" for name in ("header", "header-first", "header-left"))
_FOOTERS = tuple(f"{{{_STYLE}}}{name}" for name in ("footer", "footer-first", "footer-left"))

_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, remove_comments=True, remove_pis=True)
# ODF collapses white space in paragraph text (runs of spaces, tabs and line ends count as one space); real spaces, tabs and line
# breaks are the elements `text:s`, `text:tab` and `text:line-break`. Indented XML otherwise became line breaks inside a sentence.
_XML_SPACE = re.compile(r"[ \t\r\n]+")
_HEADING_STYLE = re.compile(r"Heading_20_([1-9])")
_TIME_VALUE = re.compile(r"PT(\d+)H(\d+)M(\d+)(?:[.,]\d+)?S")

# ---- Bounds on values written in the file (docs/decisions/odf.md) ------------------------------------------------
# Nesting of blocks (sections, lists, tables in cells, text boxes, groups) and of inline elements (spans in spans). Deeper content
# is kept as plain text. Rendering recurses, so without a bound a crafted file ended in a RecursionError
# (Pinned by: test_odf.py::TestHostileInput::test_deeply_nested_elements_keep_their_text_without_recursing).
_MAX_DEPTH = 40
# `text:s text:c="…"`: spaces are collapsed to one by the converter anyway, so only a few are written.
_MAX_SPACES = 8
# A position or repeat count far beyond any real sheet. Counts are clamped to it so that arithmetic stays on small integers.
_FAR = 1 << 40
# A repeated row or cell that has text is written out this many times at most …
_MAX_REPEAT = 10_000
# … and all repeated copies of one document together may add this many characters at most. `number-rows-repeated="1000000"` on a
# row with text is a few bytes in the file; expanded without a bound it was gigabytes of output when `max_chars` is None
# (Pinned by: test_odf.py::TestHostileInput::test_repeated_rows_with_text_are_cut_short_with_a_warning).
_REPEAT_BUDGET = 5_000_000
# In a table of a text document or a slide every copy of a cell is a cell of the grid, with or without text, so the copies are
# bounded by their number too: a row of one letter and 500 empty cells, repeated 10,000 times, was 5 million grid cells — 13 s
# and 2.7 GB from a file of a few hundred bytes
# (Pinned by: test_odf.py::TestHostileInput::test_repeated_row_of_many_cells_in_a_text_table_is_bounded_by_cell_count).
_REPEAT_CELLS = 200_000
# The same for the rows of a sheet, where a cell is cheaper (text in a list, no grid) and long runs of equal rows are real:
# the copies of a document may add this many cells, empty ones included. One character in column CV of a row repeated
# 10,000 times is 10,000 characters of the text budget and a million cells: 500 such rows (2 KB) ran for a minute with the
# default options and for ten without a character limit
# (Pinned by: test_odf.py::TestHostileInput::test_repeated_sheet_rows_are_bounded_by_cell_count).
_REPEAT_SHEET_CELLS = 2_000_000
# Columns of a sheet beyond this are not materialized whatever `max_sheet_cols` says (the widest sheet LibreOffice and Excel have).
_MAX_COLUMNS = 16_384

# `style:num-format` of a numbered list level → number shape of `_lists.format_number`. LibreOffice writes the letter and Korean
# formats as the start of the sequence ("가, 나, 다, ..."), so the first character decides.
_NUMBER_SHAPES = {"1": "DIGIT", "a": "LATIN_SMALL", "A": "LATIN_CAPITAL", "i": "ROMAN_SMALL", "I": "ROMAN_CAPITAL",
                  "가": "HANGUL_SYLLABLE", "ㄱ": "HANGUL_JAMO", "①": "CIRCLED_DIGIT", "㉮": "CIRCLED_HANGUL_SYLLABLE",
                  "㉠": "CIRCLED_HANGUL_JAMO", "일": "HANGUL_PHONETIC", "一": "IDEOGRAPH"}


@dataclass(frozen=True, slots=True)
class _Context:
    """Where the content being rendered will end up."""

    in_cell: bool = False   # inside a table cell: list levels are marked with `cell_level_mark`, headings are plain text
    in_note: bool = False   # inside a footnote or speaker notes: a table is flattened to one line (`note_table`)
    plain: bool = False     # no emphasis and no links (slide titles, sheet cells, speaker notes, chart text)
    quote: bool = False     # speaker notes: list levels are marked without a bullet, like the PPTX extractor
    grid: TableGrid | None = None   # the table whose cell is being read (a nested table is handed to it)


_BODY = _Context()
_PLAIN = _Context(plain=True)
_SHEET_CELL = _Context(in_cell=True, plain=True)
_TITLE = _Context(in_cell=True, plain=True, quote=True)
_SPEAKER_NOTES = _Context(in_cell=True, in_note=True, plain=True, quote=True)


@dataclass(slots=True)
class _Run:
    """A piece of paragraph text with the formatting in effect."""

    text: str
    bold: bool = False
    italic: bool = False
    script: str = ""        # SUPERSCRIPT / SUBSCRIPT / ""
    strike: bool = False
    href: str | None = None     # the address of the link the text is in, already vetted by `_inline.link_target`


@dataclass(slots=True)
class _Block:
    """Something inside a paragraph that stands as a block of its own (a picture, a text box, a chart)."""

    text: str


class _Formula(str):
    """The `$…$` text of a formula object, told apart from other frame content so that it can stay inline."""

    __slots__ = ()


@dataclass(frozen=True, slots=True)
class _ListLevel:
    kind: str = "bullet"    # "bullet", "number" or "none" (a level that shows no label)
    shape: str = "DIGIT"
    prefix: str = ""
    suffix: str = "."
    start: int = 1


_DEFAULT_LEVEL = _ListLevel()
# (bold, italic, script, strike, bold of East Asian text, italic of East Asian text); None = the style does not say, so the
# enclosing text decides (for the East Asian pair: the Western value is used).
_Look = tuple[bool | None, bool | None, str | None, bool | None, bool | None, bool | None]
_NO_LOOK: _Look = (None, None, None, None, None, None)
_PLAIN_LOOK: _Look = (False, False, "", False, None, None)
# Hangul, kana, Han characters and full-width forms: the text ODF formats with the `…-asian` properties.
_EAST_ASIAN = re.compile("[\u1100-\u11ff\u2e80-\u9fff\ua960-\ua97f\uac00-\ud7ff\uf900-\ufaff\uff00-\uffef]")


class _Styles:
    """The styles the renderer looks at: character formatting of text styles, the title paragraph style, list styles, hidden
    sheets and hidden slides. Named styles come from styles.xml, automatic ones from the part being rendered."""

    def __init__(self) -> None:
        self._text: dict[str, tuple[str | None, _Look]] = {}    # text style → (parent, own formatting)
        self._resolved: dict[str, _Look] = {}
        self._paragraph_parents: dict[str, str | None] = {}
        self._lists: dict[str, dict[int, _ListLevel]] = {}
        self.hidden_tables: set[str] = set()
        self.hidden_pages: set[str] = set()

    def add(self, container: etree._Element | None) -> None:
        """Read `office:styles` or `office:automatic-styles`."""
        for node in container if container is not None else ():
            tag = node.tag
            if not isinstance(tag, str):
                continue
            name = node.get(_STYLE_NAME)
            if name is None:
                continue
            if tag == f"{{{_TEXT}}}list-style":
                self._lists[name] = _list_levels(node)
            elif tag == f"{{{_STYLE}}}style":
                family = node.get(_FAMILY)
                if family == "text":
                    self._text[name] = (node.get(_PARENT_STYLE), _own_look(node.find(f"{{{_STYLE}}}text-properties")))
                    self._resolved.clear()
                elif family == "paragraph":
                    self._paragraph_parents[name] = node.get(_PARENT_STYLE)
                elif family == "table":
                    properties = node.find(f"{{{_STYLE}}}table-properties")
                    if properties is not None and properties.get(f"{{{_TABLE}}}display") == "false":
                        self.hidden_tables.add(name)
                elif family == "drawing-page":
                    properties = node.find(f"{{{_STYLE}}}drawing-page-properties")
                    if properties is not None and properties.get(f"{{{_PRESENTATION}}}visibility") == "hidden":
                        self.hidden_pages.add(name)

    def look(self, name: str | None) -> _Look:
        """Formatting of a text style with what it inherits from its parents. The chain is followed 16 styles at most (a style that
        names itself as parent would otherwise never end)."""
        if name is None or name not in self._text:
            return _NO_LOOK
        if name not in self._resolved:
            look: _Look = _NO_LOOK
            current: str | None = name
            for _ in range(16):
                if current is None or current not in self._text:
                    break
                current, own = self._text[current]
                look = tuple(have if have is not None else inherited for have, inherited in zip(look, own, strict=True))
            self._resolved[name] = look
        return self._resolved[name]

    def heading_level(self, name: str | None) -> int:
        """Heading level a paragraph style stands for when the paragraph is a `text:p`: 1 for the 'Title' style, n for the
        built-in 'Heading n' styles (LibreOffice names them `Heading_20_n`), also for styles derived from them; 0 otherwise.
        The chain of parents is followed 16 styles at most."""
        for _ in range(16):
            if name is None:
                return 0
            if name == "Title":
                return 1
            match = _HEADING_STYLE.fullmatch(name)
            if match:
                return min(int(match.group(1)), 6)
            name = self._paragraph_parents.get(name)
        return 0

    def list_level(self, name: str | None, level: int) -> _ListLevel:
        return self._lists.get(name or "", {}).get(level, _DEFAULT_LEVEL)

    def shown_depth(self, name: str | None, level: int) -> int:
        """Indentation of a list level: the number of levels above it that show a label. Impress writes the unbulleted first level
        of a text box as a list level without a label, and its bullets one level down are the top level of what is shown."""
        levels = self._lists.get(name or "", {})
        return sum(1 for above in range(level) if levels.get(above, _DEFAULT_LEVEL).kind != "none")


def _own_look(properties: etree._Element | None) -> _Look:
    if properties is None:
        return _NO_LOOK
    weight, asian_weight = properties.get(f"{{{_FO}}}font-weight"), properties.get(f"{{{_STYLE}}}font-weight-asian")
    slant, asian_slant = properties.get(f"{{{_FO}}}font-style"), properties.get(f"{{{_STYLE}}}font-style-asian")
    position = (properties.get(f"{{{_STYLE}}}text-position") or "").split()
    line = properties.get(f"{{{_STYLE}}}text-line-through-style")
    script: str | None = None
    if position:
        first = position[0]
        if first == "super" or (first[:1].isdigit() and first.rstrip("%").strip("0.") != ""):
            script = SUPERSCRIPT
        elif first == "sub" or first.startswith("-"):
            script = SUBSCRIPT
        else:
            script = ""
    return (_is_bold(weight), _is_italic(slant), script, None if line is None else line != "none",
            _is_bold(asian_weight), _is_italic(asian_slant))


def _is_bold(weight: str | None) -> bool | None:
    return None if weight is None else weight == "bold" or (weight.isdecimal() and len(weight) == 3 and weight >= "600")


def _is_italic(slant: str | None) -> bool | None:
    return None if slant is None else slant in ("italic", "oblique")


def _run(text: str, look: _Look, href: str | None) -> "_Run":
    """A run with the formatting that applies to its text. ODF keeps bold and italic separately for Western and East Asian text
    (`fo:font-weight` / `style:font-weight-asian`), and LibreOffice may set only the one for the script the text is in — a bold
    Korean word had only the Asian property, so reading the Western one alone lost the emphasis of Korean documents
    (Pinned by: test_odf.py::TestInlineFormatting::test_bold_korean_text_is_read_from_the_asian_font_properties)."""
    bold, italic, script, strike, asian_bold, asian_italic = look
    if (asian_bold is not None or asian_italic is not None) and _EAST_ASIAN.search(text):
        bold = bold if asian_bold is None else asian_bold
        italic = italic if asian_italic is None else asian_italic
    return _Run(text, bool(bold), bool(italic), script or "", bool(strike), href)


def _list_levels(style: etree._Element) -> dict[int, _ListLevel]:
    """Levels of a `text:list-style`, keyed from 0. A numbered level without a number format shows no label."""
    levels: dict[int, _ListLevel] = {}
    for node in style:
        tag = node.tag
        if not isinstance(tag, str) or not tag.startswith(f"{{{_TEXT}}}list-level-style-"):
            continue
        level = decimal_int(node.get(f"{{{_TEXT}}}level"), 0)
        if not 1 <= level <= _MAX_DEPTH:
            continue
        if tag.endswith("-number"):
            number_format = node.get(f"{{{_STYLE}}}num-format") or ""
            if not number_format:
                levels[level - 1] = _ListLevel(kind="none")
                continue
            start = decimal_int(node.get(f"{{{_TEXT}}}start-value"), 1)
            levels[level - 1] = _ListLevel("number", _NUMBER_SHAPES.get(number_format[0], "DIGIT"),
                                           node.get(f"{{{_STYLE}}}num-prefix") or "", node.get(f"{{{_STYLE}}}num-suffix") or "",
                                           min(max(start, 0), _FAR))
        else:
            levels[level - 1] = _DEFAULT_LEVEL
    return levels


class _Package:
    """The parts of the ODF archive. A part that is missing, too large or cannot be decompressed reads as None, so one broken
    picture or sub-document costs only itself."""

    def __init__(self, archive: zipfile.ZipFile, limit: int) -> None:
        self._archive = archive
        self._infos = {info.filename: info for info in archive.infolist()}
        self._limit = limit
        self._pictures: dict[str, bytes | None] = {}

    def has(self, name: str) -> bool:
        return name in self._infos

    def read(self, name: str) -> bytes | None:
        info = self._infos.get(name)
        if info is None or info.is_dir() or info.file_size > self._limit:
            return None
        try:
            with self._archive.open(info) as part:
                data = part.read(self._limit + 1)
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError, RuntimeError, NotImplementedError, ValueError):
            return None
        return data if len(data) <= self._limit else None

    def xml(self, name: str) -> etree._Element | None:
        data = self.read(name)
        return _parse(data) if data is not None else None

    def linked(self, href: str | None) -> str | None:
        """Name of the part an `xlink:href` points to, or None for a link out of the package (external pictures are never fetched)."""
        if not href:
            return None
        name = href.removeprefix("./")
        if "://" in name or name.startswith(("/", "#")) or ".." in name.split("/"):
            return None
        if name in self._infos or f"{name}/content.xml" in self._infos:
            return name
        return unquote(name)

    def picture(self, href: str | None) -> bytes | None:
        """A picture part is decompressed once however many frames show it; the same bytes object is handed on, so the converter
        decodes it once as well."""
        name = self.linked(href)
        if name is None:
            return None
        if name not in self._pictures:
            self._pictures[name] = self.read(name) or None
        return self._pictures[name]


def _parse(data: bytes) -> etree._Element | None:
    try:
        return etree.fromstring(data, parser=_XML_PARSER)
    except (etree.XMLSyntaxError, ValueError):
        return None


def _rows(table: etree._Element) -> Iterator[etree._Element]:
    """The `table:table-row` elements of a table in document order, through the header-row and row-group wrappers. Walks with a
    stack, not recursion (row groups nest)."""
    stack = [iter(table)]
    while stack:
        for node in stack[-1]:
            if node.tag == _ROW:
                yield node
            elif node.tag in _ROW_GROUPS and len(stack) < _MAX_DEPTH:
                stack.append(iter(node))
                break
        else:
            stack.pop()


def _first_used_column(table: etree._Element) -> int:
    """Number of columns before the leftmost cell of a sheet that holds a value (0 when there is none). The column limit counts
    from there, as for XLSX and XLS (`spreadsheet._first_used_column`): a small table that starts at column GW is not 'beyond
    column 100' (Pinned by: test_odf.py::TestSpreadsheet::test_column_limit_counts_from_the_first_used_column).
    Each row is read only up to its first value or up to the best column found so far."""
    best = _FAR
    value_type = f"{{{_OFFICE}}}value-type"
    for row in _rows(table):
        column = 0
        for cell in row:
            tag = cell.tag
            if tag != _CELL and tag != _COVERED:
                continue
            if column >= best:
                break
            if tag == _CELL and (cell.get(value_type) is not None or cell.find(_P) is not None):
                best = column
                break
            column += _repeat(cell, _COLS_REPEATED)
    return best if best < _FAR else 0


def _top_level_blocks(body: etree._Element) -> Iterator[etree._Element]:
    """The blocks of a text document, looking through sections and indexes, so that the character limit is checked per paragraph
    even when the whole document sits in one section. Hidden sections are left out."""
    stack = [iter(body)]
    while stack:
        for node in stack[-1]:
            if node.tag in _BLOCK_WRAPPERS and len(stack) < _MAX_DEPTH:
                if not _is_hidden_section(node):
                    stack.append(iter(node))
                    break
            else:
                yield node
        else:
            stack.pop()


def _is_hidden_section(node: etree._Element) -> bool:
    return node.tag == _SECTION and node.get(f"{{{_TEXT}}}display") == "none"


def _repeat(node: etree._Element, attribute: str) -> int:
    return min(max(decimal_int(node.get(attribute), 1), 1), _FAR)


def _flat_text(node: etree._Element) -> str:
    return " ".join("".join(node.itertext()).split())


_MATH_SCRIPTS = {"msup": "^", "msub": "_", "msubsup": "_", "munder": "_", "mover": "^", "munderover": "_"}
_MATH_IGNORED = frozenset(("annotation", "annotation-xml", "none", "mprescripts", "mphantom"))


def _math_group(text: str) -> str:
    return text if len(text) == 1 else f"{{{text}}}"


def _mathml_latex(node: etree._Element, depth: int) -> str:
    """Presentation MathML as one LaTeX-style line: scripts, fractions, radicals, limits, fences and matrices; unknown elements
    have their children joined. The same rules as `_html_math._mathml_latex` (which works on a parsed HTML tree), so an equation reads
    the same from HTML and from ODF. Deeper than `_MAX_DEPTH` only the characters are joined."""
    tag = node.tag
    if not isinstance(tag, str):
        return ""
    name = tag.rsplit("}", 1)[-1]
    if name in _MATH_IGNORED:
        return ""
    if depth >= _MAX_DEPTH:
        return "".join("".join(node.itertext()).split())
    parts = [_mathml_latex(child, depth + 1) for child in node if isinstance(child.tag, str)]
    if not parts:
        return " ".join((node.text or "").split())   # mi, mn, mo, mtext, ms
    if name == "semantics":
        return parts[0]   # the first child is the presentation form, the rest are annotations
    if name == "mspace":
        return " "
    if name in _MATH_SCRIPTS and len(parts) >= 2:
        base, scripts = parts[0], parts[1:]
        if name in ("msup", "msub"):
            return f"{base}{_MATH_SCRIPTS[name]}{_math_group(scripts[0])}"
        if name == "msubsup" and len(scripts) >= 2:
            return f"{base}_{_math_group(scripts[0])}^{_math_group(scripts[1])}"
        if name == "munderover" and len(scripts) >= 2:
            return f"{base}_{{{scripts[0]}}}^{{{scripts[1]}}}"
        return f"{base}{_MATH_SCRIPTS[name]}{{{scripts[0]}}}"
    if name == "mfrac" and len(parts) >= 2:
        return f"\\frac{{{parts[0]}}}{{{parts[1]}}}"
    if name == "msqrt":
        return f"\\sqrt{{{''.join(parts)}}}"
    if name == "mroot" and len(parts) >= 2:
        return f"\\sqrt[{parts[1]}]{{{parts[0]}}}"
    if name == "mfenced":
        separator = (node.get("separators") or ",").strip()[:1]
        return f"{node.get('open', '(')}{separator.join(parts)}{node.get('close', ')')}"
    if name == "mtable":
        return f"\\begin{{matrix}}{' \\\\ '.join(parts)}\\end{{matrix}}"
    if name in ("mtr", "mlabeledtr"):
        return " & ".join(parts)
    return "".join(parts)


def _number_text(value: float) -> str:
    return str(int(value)) if value.is_integer() and abs(value) < 1e15 else format(value, ".15g")


def _stored_value(cell: etree._Element) -> str:
    """Text for a cell that has a value but no `text:p` (files written by other programs than LibreOffice): the stored value in a
    plain form. A cell LibreOffice wrote always has its displayed text."""
    kind = cell.get(f"{{{_OFFICE}}}value-type")
    if kind in ("float", "currency", "percentage"):
        try:
            value = float(cell.get(f"{{{_OFFICE}}}value", ""))
        except ValueError:
            return ""
        # Multiplied before the test for a finite number: 1e308 passed it and came out as 'inf%' (Pinned by: test_audit_office.py::
        # TestOdsPercentageBeyondTheLargestNumber::test_stored_percentage_that_overflows_is_left_empty_like_an_infinite_value).
        if kind == "percentage":
            value *= 100
        if value != value or value in (float("inf"), float("-inf")):
            return ""
        return f"{_number_text(round(value, 10))}%" if kind == "percentage" else _number_text(value)
    if kind == "date":
        return (cell.get(f"{{{_OFFICE}}}date-value") or "").removesuffix("T00:00:00").replace("T", " ")
    if kind == "time":
        value = cell.get(f"{{{_OFFICE}}}time-value") or ""
        match = _TIME_VALUE.fullmatch(value)
        return f"{int(match.group(1)[:9]):02d}:{int(match.group(2)[:9]):02d}:{int(match.group(3)[:9]):02d}" if match else value
    if kind == "boolean":
        return "TRUE" if cell.get(f"{{{_OFFICE}}}boolean-value") == "true" else "FALSE"
    if kind == "string":
        return cell.get(f"{{{_OFFICE}}}string-value") or ""
    return ""


def _cell_reference(row: int, column: int) -> str:
    """A1-style name of a cell (0-based row and column) for a comment line. Empty beyond the sheet sizes real programs have."""
    if row >= 1 << 21 or column >= _MAX_COLUMNS:
        return ""
    letters = ""
    column += 1
    while column:
        column, remainder = divmod(column - 1, 26)
        letters = chr(ord("A") + remainder) + letters
    return f"{letters}{row + 1}"


class _Renderer:
    def __init__(self, package: _Package, options: ConvertOptions, extraction: Extraction, styles: _Styles) -> None:
        self._package = package
        self._options = options
        self._extraction = extraction
        self.styles = styles
        self.notes: list[str] = []          # `[^n]: text` definitions, in the order of reference
        self._note_count = 0
        self.comments: list[str] = []       # `> Comment(author): text` lines
        self.omitted_rows = 0               # rows dropped by the table grid cell limit (`TableGrid.omitted_rows`)
        self._repeat_budget = _REPEAT_BUDGET
        self._repeat_cells = _REPEAT_CELLS
        self._sheet_cells = _REPEAT_SHEET_CELLS
        self._objects: dict[str, etree._Element | None] = {}   # embedded sub-documents by folder name, parsed once
        self._formulas: dict[str, list[str]] = {}   # … and a formula converted once
        self._charts: dict[str, etree._Element | None] = {}   # … and its chart element found once (None when it has none)
        self._counters: dict[str, dict[int, int]] = {}   # list style → level → last number given
        self.frames: list[etree._Element] = []          # frames found in sheet cells (charts, pictures), for after the rows

    # ---- Inline text ---------------------------------------------------------------------------------------------

    def _inline(self, node: etree._Element, look: _Look, href: str | None,
                pieces: list[_Run | _Block], context: _Context, depth: int) -> None:
        """Collect the content of a paragraph (or of an element inside one) as runs and blocks."""
        if node.text:
            pieces.append(_run(_XML_SPACE.sub(" ", node.text), look, href))
        for child in node:
            tag = child.tag
            if tag == _S:
                pieces.append(_run(" " * min(max(decimal_int(child.get(f"{{{_TEXT}}}c"), 1), 1), _MAX_SPACES), look, href))
            elif tag == _TAB:
                pieces.append(_run("\t", look, href))
            elif tag == _LINE_BREAK:
                pieces.append(_run("\n", look, href))
            elif tag == _NOTE:
                reference = self._note(child, context, depth)
                if reference:
                    pieces.append(_Run(reference))
            elif tag == _ANNOTATION:
                self._comment(child)
            elif tag in _SKIPPED_INLINE or not isinstance(tag, str):
                pass
            elif depth >= _MAX_DEPTH:
                pieces.append(_run(_XML_SPACE.sub(" ", "".join(child.itertext())), look, href))
            elif tag == _SPAN:
                own = self.styles.look(child.get(_TEXT_STYLE_NAME))
                inner = tuple(have if have is not None else outer for have, outer in zip(own, look, strict=True))
                self._inline(child, inner, href, pieces, context, depth + 1)
            elif tag == _A:
                # Only web and mail addresses are links (`_inline.link_target`); a bookmark or a file keeps its text only.
                self._inline(child, look, link_target(child.get(_HREF)) or href, pieces, context, depth + 1)
            elif tag == _RUBY:
                base, annotation = child.find(_RUBY_BASE), child.find(_RUBY_TEXT)
                text = format_dutmal(_flat_text(base) if base is not None else "", _flat_text(annotation) if annotation is not None else "")
                pieces.append(_run(text, look, href))
            elif tag.startswith(_DRAW_PREFIX):
                # A formula stays in the sentence it is part of; pictures, text boxes, tables and charts stand as blocks.
                pieces.extend(_Run(block, href=href) if isinstance(block, _Formula) else _Block(block)
                              for block in self._shape(child, context, depth + 1))
            else:
                # Fields (dates, cross-references, sequence numbers of captions), bookmarks and the like: their text is what was shown.
                self._inline(child, look, href, pieces, context, depth + 1)
            if child.tail:
                pieces.append(_run(_XML_SPACE.sub(" ", child.tail), look, href))

    def _format(self, runs: list[_Run], context: _Context, emphasis: bool) -> str:
        """Runs → Markdown text. The runs only announce where a link, bold or italic starts and ends (the temporary marks of
        `_inline`); merging runs, placing white space, whole-block and part-of-word emphasis, link text and escaping are the
        shared rules of `_inline.resolve_inline` (docs/decisions/inline-formatting.md), in the order docx.py applies them, so that
        an ODT and the DOCX it was saved from read the same
        (Pinned by: test_odf.py::TestInlineFormatting::test_paragraph_that_is_bold_as_a_whole_is_left_unmarked).
        Runs that follow each other with the same address are one link. With both options off, and in plain contexts (slide
        titles, sheet cells, speaker notes), no marks are written at all."""
        links = self._options.keep_links and not context.plain
        marks = self._options.keep_emphasis and not context.plain
        parts = []
        for target, linked in groupby(runs, key=lambda run: run.href if links else None):
            text = "".join(_marked(run, marks) for run in linked)
            parts.append(f"{LINK_OPEN}{text}{link_close(target)}" if target else text)
        text = "".join(parts)
        if not (links or marks):
            return resolve_scripts(resolve_strikes(text))
        return resolve_inline(resolve_scripts(resolve_strikes(merge_emphasis_marks(text))), emphasis=emphasis)

    def _paragraph(self, node: etree._Element, context: _Context, depth: int, *, emphasis: bool = True) -> list[str]:
        """A paragraph as blocks: its text, with the pictures and text boxes anchored in it as blocks of their own in place."""
        pieces: list[_Run | _Block] = []
        self._inline(node, _PLAIN_LOOK, None, pieces, context, depth)
        blocks: list[str] = []
        runs: list[_Run] = []
        for piece in [*pieces, _Block("")]:
            if isinstance(piece, _Run):
                runs.append(piece)
                continue
            text = "\n".join(line.strip() for line in self._format(runs, context, emphasis).split("\n")).strip("\n")
            if text:
                blocks.append(text)
            if piece.text:
                blocks.append(piece.text)
            runs = []
        return blocks

    def _note(self, node: etree._Element, context: _Context, depth: int) -> str:
        """A footnote or endnote: `[^n]` in the text and `[^n]: text` at the end of the document. Numbered in the order of
        reference, footnotes and endnotes together (the same as DOCX). With `include_footnotes` off the mark is left out as well —
        a mark without its text is noise."""
        body = node.find(_NOTE_BODY)
        if not self._options.include_footnotes or context.in_note or body is None or depth >= _MAX_DEPTH:
            return ""
        inside = replace(context, in_cell=True, in_note=True, quote=False, grid=None)
        text = " ".join(" ".join(self._blocks(body, inside, depth + 1)).split())
        if not text:
            return ""
        self._note_count += 1
        self.notes.append(f"[^{self._note_count}]: {text}")
        return f"[^{self._note_count}]"

    def _comment(self, node: etree._Element, cell: str = "") -> None:
        """A comment (`office:annotation`) → a comment line at the end of the document, sheet or slide, as the other formats do."""
        author = node.find(f"{{{_DC}}}creator")
        text = " ".join(_flat_text(child) for child in node if child.tag in (_P, _LIST))
        if text.strip():
            self.comments.append(comment_line(author.text or "" if author is not None else "", text, cell=cell))

    # ---- Blocks --------------------------------------------------------------------------------------------------

    def _blocks(self, parent: etree._Element, context: _Context, depth: int) -> list[str]:
        if depth >= _MAX_DEPTH:
            text = _flat_text(parent)
            return [text] if text else []
        blocks: list[str] = []
        for child in parent:
            blocks.extend(self.block(child, context, depth))
        return blocks

    def block(self, node: etree._Element, context: _Context, depth: int = 0) -> list[str]:
        tag = node.tag
        if tag == _P:
            # A paragraph in the 'Title' or a 'Heading n' style is a heading even when it is not a `text:h` (LibreOffice writes the
            # title, and headings whose outline level was removed, as `text:p`).
            level = 0 if context.in_cell or context.in_note or context.plain else self.styles.heading_level(node.get(_TEXT_STYLE_NAME))
            return self._heading(node, context, depth, level) if level else self._paragraph(node, context, depth)
        if tag == _H:
            return self._heading(node, context, depth, min(max(decimal_int(node.get(_OUTLINE_LEVEL), 1), 1), 6))
        if tag == _LIST:
            return self._list(node, context, depth, node.get(_TEXT_STYLE_NAME), 0)
        if tag == _TABLE_ELEMENT:
            return [self._table(node, context, depth)]
        if tag in _BLOCK_WRAPPERS:
            return [] if _is_hidden_section(node) else self._blocks(node, context, depth + 1)
        if isinstance(tag, str) and tag.startswith(_DRAW_PREFIX):
            return self._shape(node, context, depth)
        return []

    def _heading(self, node: etree._Element, context: _Context, depth: int, level: int) -> list[str]:
        """`text:h` → `#` by outline level (1-6). No emphasis inside a heading (links are kept). Inside a table cell, a note or a
        slide title it is not a heading — a `#` there is not one — but a paragraph, emphasized like any other (as in DOCX). The
        'Title' paragraph style is a level-1 heading as in DOCX."""
        if context.in_cell or context.in_note or context.plain:
            return self._paragraph(node, context, depth)
        blocks = self._paragraph(node, context, depth, emphasis=False)
        for index, block in enumerate(blocks):
            if not is_placeholder_only(block):
                blocks[index] = f"{'#' * level} {' '.join(block.split())}"
                break
        return blocks

    def _list(self, node: etree._Element, context: _Context, depth: int, style: str | None, level: int) -> list[str]:
        """`text:list` → `- ` / `1. ` items, two spaces of indentation per level. A nested list takes the style of the outermost
        list unless it names its own. Whether a level is bulleted, numbered or shows no label comes from the list style."""
        if level == 0 and node.get(f"{{{_TEXT}}}continue-numbering") != "true" and node.get(f"{{{_TEXT}}}continue-list") is None:
            self._counters.pop(style or "", None)
        counters = self._counters.setdefault(style or "", {})
        blocks: list[str] = []
        for item in node:
            if item.tag != _LIST_ITEM and item.tag != _LIST_HEADER:
                continue
            headed = item.tag == _LIST_HEADER   # a list header is a paragraph of the list without a label
            start = decimal_int(item.get(f"{{{_TEXT}}}start-value"), None)
            for child in item:
                if child.tag == _LIST:
                    if level + 1 < _MAX_DEPTH and depth + 1 < _MAX_DEPTH:
                        blocks.extend(self._list(child, context, depth + 1, child.get(_TEXT_STYLE_NAME) or style, level + 1))
                    else:
                        blocks.append(_flat_text(child))
                    continue
                rendered = self.block(child, context, depth + 1)
                if rendered and not headed and child.tag == _P:
                    rendered[0] = self._item_head(style, level, counters, start, context) + rendered[0]
                    headed = True
                blocks.extend(rendered)
        return [block for block in blocks if block]

    def _item_head(self, style: str | None, level: int, counters: dict[int, int], start: int | None, context: _Context) -> str:
        spec = self.styles.list_level(style, level)
        shown = self.styles.shown_depth(style, level)
        if context.quote:
            return cell_level_mark(shown)
        if spec.kind == "none":
            return ""
        if spec.kind == "bullet":
            return paragraph_head("-", shown, in_cell=context.in_cell, heading=False)
        value = min(start, _FAR) if start is not None else counters.get(level, spec.start - 1) + 1
        counters[level] = value
        for deeper in [key for key in counters if key > level]:
            del counters[deeper]
        # Arabic numbers are always written '1.' so that Markdown reads an ordered list; other shapes keep the file's prefix and suffix.
        number = f"{value}." if spec.shape == "DIGIT" else f"{spec.prefix}{format_number(value, spec.shape)}{spec.suffix}"
        return paragraph_head(number, shown, in_cell=context.in_cell, heading=False)

    # ---- Tables --------------------------------------------------------------------------------------------------

    def _copies(self, repeat: int, length: int, cells: int = 0) -> int:
        """How many times a repeated row or cell with `length` characters of text is written (`_MAX_REPEAT`, `_REPEAT_BUDGET`).
        Repeats of empty rows and cells are never written out — they only move the position. `cells` is the number of grid cells
        one copy adds (tables of text documents and slides; `_REPEAT_CELLS`)."""
        if repeat <= 1 or length <= 0:
            return 1
        copies = min(repeat, _MAX_REPEAT, 1 + self._repeat_budget // length)
        if cells:
            copies = min(copies, 1 + self._repeat_cells // cells)
            self._repeat_cells -= (copies - 1) * cells
        self._repeat_budget -= (copies - 1) * length
        return copies

    def _table(self, table: etree._Element, context: _Context, depth: int) -> str:
        """A table of a text document or a slide → `TableGrid`. `table:number-columns-spanned` / `number-rows-spanned` are passed
        on as spans (the grid repeats a vertically merged value and decides between Markdown and HTML); a
        `table:covered-table-cell` only takes up its position."""
        grid = TableGrid()
        # A top-level table checks the character limit cell by cell and stops reading rows once it is exceeded.
        budget = None if context.grid is not None or context.in_cell or context.in_note else TableBudget(self._extraction, self._options)
        inside = replace(context, in_cell=True, grid=grid)
        row_at = 0
        stopped = cut = False
        for row in _rows(table):
            cells: list[tuple[int, str | None, int, int]] = []
            column = 0
            for cell in row:
                repeat = _repeat(cell, _COLS_REPEATED)
                if cell.tag == _COVERED:
                    cells.append((min(column + repeat, _MAX_COLUMNS) - 1, None, 1, 1))
                elif cell.tag == _CELL:
                    text = "\n".join(self._blocks(cell, inside, depth + 1)) if len(cell) else ""
                    copies = self._copies(repeat, len(text), 1) if text else 1
                    cut = cut or (bool(text) and copies < repeat)
                    spans = decimal_int(cell.get(_ROWS_SPANNED), 1), decimal_int(cell.get(_COLS_SPANNED), 1)
                    cells.extend((column + copy, text if copy == 0 else merged_copy(text), *spans) for copy in range(copies))
                else:
                    continue
                column = min(column + repeat, _FAR)
            repeat = _repeat(row, _ROWS_REPEATED)
            copies = self._copies(repeat, sum(len(text) for _, text, _, _ in cells if text), len(cells))
            cut = cut or copies < repeat and any(text for _, text, _, _ in cells)
            for copy in range(copies):
                grid.reach(row_at, 0)   # a row whose cells are all covered by spans from above is still a row of the table
                for column, text, row_span, col_span in cells:
                    if text is None:
                        grid.reach(row_at, column)
                        continue
                    if copy:
                        text = merged_copy(text)
                    # A cell on a position an earlier span already covers (a file without its covered cells) moves to the next
                    # free position instead of losing its text.
                    grid.place_or_next(row_at, column, text, row_span=row_span, col_span=col_span)
                    if budget is not None and budget.add(row_at, column, text, row_span=row_span, col_span=col_span):
                        stopped = True
                        break
                row_at += 1
                if stopped:
                    break
            if stopped:
                break
        if cut:
            _warn_once(self._extraction.warnings, f"A table row or cell repeated more than {_MAX_REPEAT:,} times "
                                                  "(or beyond the repeat budget) was cut short.")

        # A table inside a note is one line; a table inside a table is drawn in place by the outer table (`TableGrid.embed`).
        if context.in_note:
            return note_table(grid.rows())
        if context.grid is not None:
            return context.grid.embed(grid)
        if context.in_cell:
            return grid.flatten()
        rendered = grid.render()
        self.omitted_rows += grid.omitted_rows()
        return rendered

    # ---- Frames, pictures, embedded objects ----------------------------------------------------------------------

    def _shape(self, node: etree._Element, context: _Context, depth: int, *, chart_values: bool = True) -> list[str]:
        """A drawing element: a frame (text box, picture, table, embedded object), a group, or a shape with text in it."""
        if depth >= _MAX_DEPTH:
            text = _flat_text(node)
            return [text] if text else []
        tag = node.tag
        if tag == _FRAME:
            return self._frame(node, context, depth, chart_values=chart_values)
        if tag == _GROUP or tag == _DRAW_LINK:
            return [block for child in node if isinstance(child.tag, str) and child.tag.startswith(_DRAW_PREFIX)
                    for block in self._shape(child, context, depth + 1, chart_values=chart_values)]
        return self._blocks(node, context, depth + 1)

    def _frame(self, frame: etree._Element, context: _Context, depth: int, *, chart_values: bool = True) -> list[str]:
        """The children of a `draw:frame` are alternative forms of one object (a table and its preview picture, a chart and its
        replacement picture), so the first one that gives content is used and the rest are left out. An empty placeholder of a
        slide layout ("Click to add text") is nothing."""
        if frame.get(_PLACEHOLDER) == "true":
            return []
        for child in frame:
            tag = child.tag
            if tag == _TEXT_BOX:
                blocks = self._blocks(child, context, depth + 1)
            elif tag == _TABLE_ELEMENT:
                blocks = [self._table(child, context, depth + 1)]
            elif tag == _OBJECT:
                blocks = self._object(child, context, chart_values)
            elif tag == _IMAGE:
                blocks = self._picture(child, frame)
            else:
                continue
            blocks = [block for block in blocks if block.strip()]
            if blocks:
                return blocks
        return []

    def _picture(self, image: etree._Element, frame: etree._Element) -> list[str]:
        if not self._options.extract_images:
            return []
        data = self._package.picture(image.get(_HREF))
        if data is None:
            embedded = image.find(f"{{{_OFFICE}}}binary-data")
            if embedded is not None and embedded.text:
                try:
                    data = b64decode(embedded.text)
                except (binascii.Error, ValueError):
                    data = None
        if not data:
            return []
        alt = next((child.text for name in ("title", "desc") for child in frame.iterchildren(f"{{{_SVG}}}{name}") if child.text), None)
        return [self._extraction.add_image(data, alt=alt)]

    def _object(self, node: etree._Element, context: _Context, chart_values: bool) -> list[str]:
        """An embedded object is a sub-document in a folder of the package. A chart gives `[Chart] title` and its value table; a
        formula gives the text LibreOffice Math keeps beside the MathML. Anything else (and a broken sub-document) gives nothing,
        and the frame falls back to its replacement picture."""
        name = self._package.linked(node.get(_HREF))
        root = None
        if name:
            # A sub-document is read and parsed once however many frames point at it, and its values are written at the first
            # of them only: 100 frames on one chart of 20,000 rows (a 10 KB file) took 17 s and 650 MB
            # (Pinned by: test_odf.py::TestHostileInput::test_object_referenced_many_times_is_parsed_once_and_written_once).
            if name in self._objects:
                chart_values = False
            else:
                self._objects[name] = self._package.xml(f"{name}/content.xml")
            root = self._objects[name]
        linked = name if root is not None else None   # the sub-document itself is used, not an object written in place
        if root is None:
            root = next((child for child in node if isinstance(child.tag, str)), None)   # an object written in place
            if root is None:
                return []
        if root.tag == f"{{{_MATH}}}math":
            if not name:
                return self._formula(root)
            if name not in self._formulas:
                self._formulas[name] = self._formula(root)
            return list(self._formulas[name])
        # Whether the sub-document holds a chart is found once: one that is neither a chart nor a formula was searched again for
        # every frame — 2 million elements and 3,000 frames took 15 s (Pinned by: test_audit_office.py::
        # TestOdfObjectThatIsNotAChart::test_many_frames_on_a_large_object_do_not_search_it_again).
        if linked in self._charts:
            chart = self._charts[linked]
        else:
            chart = next(root.iter(f"{{{_CHART}}}chart"), None)
            if linked:
                self._charts[linked] = chart
        return self._chart(chart, context, chart_values) if chart is not None else []

    def _formula(self, math: etree._Element) -> list[str]:
        """A LibreOffice Math object → `$LaTeX$` from its presentation MathML, the notation the other formats use for equations.
        When the MathML gives nothing, the StarMath text kept beside it ('a over b') is written as it is — not LaTeX, but the
        formula is not lost (Pinned by: test_odf.py::TestObjects::test_formula_becomes_latex,
        test_formula_without_readable_mathml_keeps_its_starmath_text)."""
        latex = " ".join(_mathml_latex(math, 0).split())
        if not latex:
            annotation = next(math.iter(f"{{{_MATH}}}annotation"), None)
            latex = " ".join((annotation.text or "").split()) if annotation is not None else ""
        return [_Formula(f"${latex}$")] if latex else []

    def _chart(self, chart: etree._Element, context: _Context, values: bool) -> list[str]:
        """`[Chart] title` and the chart's own copy of its values (`table:table` in the chart sub-document), as the PPTX extractor
        writes a chart. In a sheet only the title is written: the values are the cells of that sheet."""
        titles = [_flat_text(node) for name in ("title", "subtitle") for node in chart.iterchildren(f"{{{_CHART}}}{name}")]
        head = "[Chart] " + " — ".join(title for title in titles if title)
        table = chart.find(_TABLE_ELEMENT)
        rows = [[" ".join(_flat_text(child) for child in cell if child.tag == _P) for cell in row if cell.tag == _CELL]
                for row in _rows(table)] if values and table is not None else []
        rows = [row for row in rows if any(row)]
        if not rows:
            return [head.strip()]
        if not rows[0][0] and len(rows) > 1:
            rows[0][0] = "Category"
        if context.in_note:
            return [head.strip(), note_table(rows)]
        return [head.strip(), (embedded_table(context.grid) if context.grid is not None else render_table)(rows)]

    # ---- Text document -------------------------------------------------------------------------------------------

    def headers_and_footers(self, styles_root: etree._Element | None, master_styles: _Styles) -> tuple[list[str], list[str]]:
        """Header and footer text of the master pages (styles.xml), each distinct text once. Page-number fields are left out."""
        found: tuple[dict[str, None], dict[str, None]] = ({}, {})
        masters = styles_root.find(f"{{{_OFFICE}}}master-styles") if styles_root is not None else None
        body_styles, self.styles = self.styles, master_styles
        try:
            for master in masters if masters is not None else ():
                for part in master:
                    if part.tag in _HEADERS + _FOOTERS and part.get(f"{{{_STYLE}}}display") != "false":
                        text = join_blocks(self._blocks(part, _BODY, 1))
                        if text:
                            found[part.tag in _FOOTERS][text] = None
        finally:
            self.styles = body_styles
        return list(found[0]), list(found[1])

    # ---- Spreadsheet ---------------------------------------------------------------------------------------------

    def sheet_rows(self, table: etree._Element, name: str,
                   across: dict[int, list[tuple[int, int, str]]]) -> Iterator[tuple[str, ...]]:
        """Rows of a sheet as the spreadsheet helpers take them (`_collect_rows`): one tuple of displayed cell text per row, `()`
        for a gap of empty rows. Works like `xml_spreadsheet._xml_sheet_rows`: a vertical merge is repeated in the rows below that have
        content, and a horizontal merge in a row that has text to its left is reported in `across` (index of the yielded row →
        (first column, end column, value)) to be filled after the tables are split.

        Repeats cost nothing unless they hold text: `number-columns-repeated` on empty cells and `number-rows-repeated` on empty
        rows only move the position (every LibreOffice sheet ends with a million-row repeat), a cell with text is repeated up to the
        column limit, and a row with text is written out within `_MAX_REPEAT` and `_REPEAT_BUDGET` with a warning when cut
        (Pinned by: test_odf.py::TestSpreadsheet::test_trailing_repeats_of_empty_rows_and_cells_cost_nothing,
        TestHostileInput::test_repeated_rows_with_text_are_cut_short_with_a_warning).
        """
        limit = min(self._options.max_sheet_cols, _MAX_COLUMNS)
        offset = _first_used_column(table)   # leading empty columns: dropped from the rows and not counted against the limit
        merged_down: dict[int, tuple[str, int, int]] = {}   # column → (value, last sheet row, last column spanned horizontally)
        row_number = yielded = 0
        gap = cut = False
        for row in _rows(table):
            repeat = _repeat(row, _ROWS_REPEATED)
            values: dict[int, str] = {}
            spans: list[tuple[int, int, str]] = []
            column = -offset
            for cell in row:
                tag = cell.tag
                if tag != _CELL and tag != _COVERED:
                    continue
                columns = _repeat(cell, _COLS_REPEATED)
                text = self._sheet_cell(cell, row_number, column + offset) if tag == _CELL and (len(cell) or cell.attrib) else ""
                if column < 0:
                    text = ""   # left of the first used column there is no value; the call above only notes comments and drawings
                if text and column < limit:
                    wanted = min(columns, limit - column)
                    copies = self._copies(wanted, len(text))
                    if copies < wanted:
                        _warn_once(self._extraction.warnings, f"Sheet '{name}': a repeated cell was read fewer times than written.")
                    for copy in range(copies):
                        values[column + copy] = text
                    if copies == 1:
                        labelled = bool(values.keys() - {column}) or any(
                            left < column and last_row >= row_number for left, (_, last_row, _) in merged_down.items())
                        last = min(column + max(decimal_int(cell.get(_COLS_SPANNED), 1), 1), limit) - 1 if labelled else column
                        if last > column:
                            spans.append((column + 1, last + 1, text))
                        row_span = decimal_int(cell.get(_ROWS_SPANNED), 1)
                        if row_span > 1:
                            merged_down[column] = (text, row_number + min(row_span, _FAR) - 1, last)
                    if column + columns > limit:
                        values.setdefault(limit, text)   # text beyond the column limit: only raises the 'columns excluded' warning
                elif text:
                    values.setdefault(limit, text)
                column = min(column + columns, _FAR)

            if not values:
                gap = yielded > 0
                row_number = min(row_number + repeat, _FAR)
                continue
            # The copies of a row are bounded by the cells they add as well as by their text (`_REPEAT_SHEET_CELLS`).
            # The width of a copy includes what merges add to it: a value merged down from an earlier row, and the columns a
            # horizontal merge is repeated over (Pinned by: test_odf.py::TestHostileInput::test_repeated_sheet_rows_are_bounded_by_cell_count).
            width = 1 + max(max(values), max(merged_down, default=0), max((last for _, _, last in merged_down.values()), default=0),
                            max((end for _, end, _ in spans), default=0))
            copies = self._copies(min(repeat, 1 + self._sheet_cells // width), sum(len(text) for text in values.values()))
            self._sheet_cells -= (copies - 1) * width
            if copies < repeat and not cut:
                # Said once per sheet, for the first row that was cut: a warning per row was 500 warnings for 500 row elements.
                cut = True
                self._extraction.warnings.append(f"Sheet '{name}': a row repeated {repeat:,} times was read {copies:,} times only.")
            if gap:
                yield ()
                yielded += 1
                gap = False
            for copy in range(copies):
                filled, row_spans = dict(values), list(spans)
                for merged_column, (text, last_row, last) in list(merged_down.items()):
                    if last_row < row_number + copy:
                        del merged_down[merged_column]
                    elif merged_column not in filled:
                        filled[merged_column] = text
                        if last > merged_column:
                            row_spans.append((merged_column + 1, last + 1, text))
                if row_spans:
                    across[yielded] = row_spans
                yield tuple(filled.get(index, "") for index in range(max(filled) + 1))
                yielded += 1
            row_number = min(row_number + repeat, _FAR)

    def _sheet_cell(self, cell: etree._Element, row: int, column: int) -> str:
        """Text of a sheet cell as displayed: the `text:p` content is the formatted value LibreOffice stored ('$120k', '46.4%',
        '2025-03-14'), so number formats are not interpreted. Comments and drawings anchored to the cell (frames, and shapes with
        text in them) are noted for the end of the sheet."""
        lines: list[str] = []
        displayed = False
        for child in cell:
            tag = child.tag
            if tag == _P:
                displayed = True
                if len(child):
                    lines.extend(self._paragraph(child, _SHEET_CELL, 1))
                elif child.text:   # the usual cell: one paragraph of plain text
                    lines.append(_XML_SPACE.sub(" ", child.text).strip())
            elif tag == _ANNOTATION:
                self._comment(child, _cell_reference(row, column))
            elif isinstance(tag, str) and tag.startswith(_DRAW_PREFIX):
                self.frames.append(child)
        return "\n".join(lines) if displayed else _stored_value(cell)

    def sheet_objects(self, table: etree._Element) -> list[str]:
        """Charts (`[Chart] title`), pictures and the text of shapes of a sheet, after its cells, then the cell comments. A drawing
        is anchored to a cell or to the page (`table:shapes`), and a shape with text is not always a frame — a text box drawn in
        Excel arrives as `draw:custom-shape` (Pinned by: test_odf.py::TestSpreadsheet::test_text_in_a_drawn_shape_follows_the_cells).
        """
        shapes = table.find(f"{{{_TABLE}}}shapes")
        anchored = [child for child in shapes if isinstance(child.tag, str) and child.tag.startswith(_DRAW_PREFIX)] if shapes is not None else []
        blocks = [block for drawing in (*self.frames, *anchored) for block in self._shape(drawing, _PLAIN, 1, chart_values=False)
                  if block.strip()]
        blocks.append("\n".join(self.comments))
        self.frames, self.comments = [], []
        return blocks

    # ---- Presentation --------------------------------------------------------------------------------------------

    def slide(self, page: etree._Element) -> tuple[str | None, str]:
        """One `draw:page` → (title, text). The title is the text of the title placeholder (`presentation:class="title"`) and is not
        repeated in the text. Shapes follow in document order; speaker notes and comments come last."""
        title_frame = next((child for child in page.iterchildren(_FRAME) if child.get(_CLASS) == "title"), None)
        title = " ".join(" ".join(self._frame(title_frame, _TITLE, 1)).split()) if title_frame is not None else ""
        if page.get(f"{{{_DRAW}}}style-name") in self.styles.hidden_pages:
            title = f"{title} (hidden)" if title else "(hidden)"   # the same mark as a hidden PPTX slide

        blocks: list[str] = []
        for child in page:
            tag = child.tag
            if child is title_frame or not isinstance(tag, str):
                continue
            if tag == _ANNOTATION:
                self._comment(child)
            elif tag.startswith(_DRAW_PREFIX) and child.get(_CLASS) not in _SKIPPED_CLASSES:
                blocks.extend(self._shape(child, _BODY, 1))
        notes = self._speaker_notes(page.find(_NOTES)) if self._options.include_speaker_notes else ""
        comments, self.comments = "\n".join(self.comments), []
        return title or None, join_blocks([*blocks, notes, comments])

    def _speaker_notes(self, notes: etree._Element | None) -> str:
        """`> Speaker notes: …`, the form the PPTX extractor writes. The notes are the notes placeholder and any text box added to
        the notes page; the slide thumbnail, page number, header and footer placeholders are not notes. List levels are shown
        with `cell_level_mark` because leading spaces of a quote line are lost."""
        lines: list[str] = []
        for frame in notes if notes is not None else ():
            if frame.tag == _FRAME and frame.get(_CLASS) in (None, "notes"):
                lines.extend(line for block in self._frame(frame, _SPEAKER_NOTES, 1) for line in block.splitlines() if line.strip())
        return "> Speaker notes: " + "\n> ".join(lines) if lines else ""


def _marked(run: _Run, emphasis: bool) -> str:
    """The text of a run inside the temporary marks of `_inline`: script innermost, then strikethrough, emphasis outermost
    (`**~~1,000~~**`, `**H_2O**`), the order docx.py uses."""
    text = mark_script(run.text, run.script) if run.script else run.text
    text = mark_strike(text) if run.strike else text
    return mark_emphasis(text, run.bold, run.italic) if emphasis else text


# ---- Entry points ------------------------------------------------------------------------------------------------

_BODIES = {"text": f"{{{_OFFICE}}}text", "spreadsheet": f"{{{_OFFICE}}}spreadsheet", "presentation": f"{{{_OFFICE}}}presentation"}


def extract_odt(data: bytes, options: ConvertOptions) -> Extraction:
    return _extract(data, options, "text")


def extract_ods(data: bytes, options: ConvertOptions) -> Extraction:
    return _extract(data, options, "spreadsheet")


def extract_odp(data: bytes, options: ConvertOptions) -> Extraction:
    return _extract(data, options, "presentation")


def _extract(data: bytes, options: ConvertOptions, kind: str) -> Extraction:
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise CorruptedDocumentError("The OpenDocument archive structure is corrupted.") from exc

    with archive:
        package = _Package(archive, options.max_uncompressed_bytes)
        # An encrypted package lists the key derivation of every part in the manifest; the parts themselves are then not XML.
        if b"encryption-data" in (package.read("META-INF/manifest.xml") or b""):
            raise EncryptedDocumentError("The OpenDocument file is password-protected. Please remove the password and upload it again.")
        content = package.read("content.xml")
        root = _parse(content) if content is not None else None
        body = root.find(f"{{{_OFFICE}}}body") if root is not None else None
        if body is None:
            raise CorruptedDocumentError("The content of the OpenDocument file (content.xml) is missing or corrupted.")

        extraction = Extraction()
        # Parts outside the body may be broken and the body is still read. Without styles.xml the named styles are unknown: numbered
        # lists come out bulleted, the title is a paragraph and headers and footers are lost — hence the warning
        # (Pinned by: test_odf.py::TestBrokenParts::test_broken_styles_part_keeps_the_body_with_a_warning).
        styles_root = package.xml("styles.xml")
        if styles_root is None and package.has("styles.xml"):
            extraction.warnings.append("The style information (styles.xml) could not be read, so the document was converted "
                                       "without list numbering, title and header/footer information.")
        named = styles_root.find(f"{{{_OFFICE}}}styles") if styles_root is not None else None
        styles = _Styles()
        styles.add(named)
        styles.add(root.find(f"{{{_OFFICE}}}automatic-styles"))
        renderer = _Renderer(package, options, extraction, styles)

        # The body decides, not the mimetype: a text body in a package that calls itself a spreadsheet is still read as text.
        found = next((name for name in (kind, *_BODIES) if body.find(_BODIES[name]) is not None), None)
        if found is None:
            raise CorruptedDocumentError("The OpenDocument file has no text, spreadsheet or presentation content.")
        content_root = body.find(_BODIES[found])
        if found == "text":
            _extract_text(renderer, content_root, styles_root, named, extraction, options)
        elif found == "spreadsheet":
            _extract_sheets(renderer, content_root, extraction, options)
        else:
            _extract_slides(renderer, content_root, extraction, options)
        if renderer.omitted_rows:
            extraction.warnings.append(omitted_rows_warning(renderer.omitted_rows))
        extraction.metadata.update(_metadata(package.xml("meta.xml")))
    return extraction


def _extract_text(renderer: _Renderer, body: etree._Element, styles_root: etree._Element | None, named: etree._Element | None,
                  extraction: Extraction, options: ConvertOptions) -> None:
    blocks: list[str] = []
    headers: list[str] = []
    footers: list[str] = []
    if options.include_headers_footers and styles_root is not None:
        # The automatic styles of styles.xml belong to the master pages; those of content.xml may use the same names for other looks.
        master_styles = _Styles()
        master_styles.add(named)
        master_styles.add(styles_root.find(f"{{{_OFFICE}}}automatic-styles"))
        headers, footers = renderer.headers_and_footers(styles_root, master_styles)
        extraction.count(sum(solid_length(text) for text in (*headers, *footers)), options)

    # Read per top-level block and count characters; once the limit is exceeded the rest of the document is not read. Tracked
    # deletions live in `text:tracked-changes` at the top of the body, which is not a block, so deleted text never comes out
    # (Pinned by: test_odf.py::TestTextDocument::test_tracked_deletion_is_left_out_and_insertion_kept).
    for node in _top_level_blocks(body):
        if extraction.exceeded:
            break
        rendered = renderer.block(node, _BODY)
        blocks.extend(rendered)
        extraction.count(sum(solid_length(block) for block in rendered), options)
    text = join_blocks([*headers, *blocks, *footers, "\n".join(renderer.notes), "\n".join(renderer.comments)])
    extraction.sections.append(Section(SectionKind.DOCUMENT, 1, text))


def _extract_sheets(renderer: _Renderer, body: etree._Element, extraction: Extraction, options: ConvertOptions) -> None:
    for index, table in enumerate(body.iterchildren(_TABLE_ELEMENT), start=1):
        name = table.get(f"{{{_TABLE}}}name") or f"Sheet{index}"
        if table.get(f"{{{_TABLE}}}style-name") in renderer.styles.hidden_tables and not options.include_hidden_sheets:
            extraction.warnings.append(f"Hidden sheet '{name}' was excluded.")
            continue
        spans: dict[int, list[tuple[int, int, str]]] = {}
        rows = _collect_rows(renderer.sheet_rows(table, name, spans), options, name, extraction)
        across = {id(rows[row]): found for row, found in spans.items() if row < len(rows)}
        drawn = renderer.sheet_objects(table)
        _append_sheet(extraction, name, rows, [] if extraction.exceeded else drawn, across)
        if extraction.exceeded:
            break
    extraction.metadata["sheet_count"] = str(len(extraction.sections))


def _extract_slides(renderer: _Renderer, body: etree._Element, extraction: Extraction, options: ConvertOptions) -> None:
    for index, page in enumerate(body.iterchildren(_PAGE), start=1):
        title, text = renderer.slide(page)
        extraction.sections.append(Section(SectionKind.SLIDE, index, text, title=title))
        if extraction.count(solid_length(text), options):
            break
    extraction.metadata["slide_count"] = str(len(extraction.sections))


def _metadata(meta: etree._Element | None) -> dict[str, str]:
    """Title and author from meta.xml (`dc:title`; `meta:initial-creator`, else `dc:creator`). A broken meta.xml gives none."""
    metadata: dict[str, str] = {}
    properties = meta.find(f"{{{_OFFICE}}}meta") if meta is not None else None
    if properties is None:
        return metadata
    for key, tags in (("title", (f"{{{_DC}}}title",)), ("author", (f"{{{_META}}}initial-creator", f"{{{_DC}}}creator"))):
        for tag in tags:
            node = properties.find(tag)
            text = " ".join((node.text or "").split()) if node is not None else ""
            if text:
                metadata[key] = text
                break
    return metadata
