"""DOCX → Markdown.

python-docx is used only to open the package and find parts; the body is read by walking the XML directly. `Paragraph.text` does not
handle text boxes, tracked insertions (w:ins) or footnote references, and `_Row.cells` does not tell horizontal merges from vertical ones.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import re

from docx.opc.exceptions import PackageNotFoundError
from docx.opc.part import Part, PartFactory
from docx.package import Package
from docx.parts.document import DocumentPart
from lxml import etree

from opendockit.exceptions import CorruptedDocumentError, UnsupportedFormatError
from opendockit.extractors._charts import drawing_paragraph_lines, embedded_table, related_loader, render_chart, render_chartex, render_smartart
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
    plain_inline,
    resolve_inline,
    resolve_scripts,
    resolve_strikes,
)
from opendockit.extractors._lists import DOCX_NUMBER_SHAPES, HAND_TYPED_BULLET, Numbering, NumberLevel, cell_level_mark, list_item_prefix
from opendockit.extractors._notation import (
    COMMENT_TARGET_MAX,
    OLE_PLACEHOLDER,
    TableBudget,
    comment_line,
    format_dutmal,
    note_table,
    omitted_rows_warning,
    unread_ole_warning,
)
from opendockit.extractors._omml import render_omml
from opendockit.extractors._package import core_properties, decimal_int, is_strict_package, open_package, parse_part
from opendockit.extractors._symbols import SYMBOL_FONTS, SYMBOL_GLYPHS, symbol_font_text, symbol_glyph
from opendockit.extractors.html import extract_html, is_mhtml
from opendockit.extractors.text import extract_text
from opendockit.markdown import TableGrid, join_blocks, merged_copy, solid_length
from opendockit.models import ConvertOptions, Extraction, Section, SectionKind, renumber_image_markers


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_V = "urn:schemas-microsoft-com:vml"
_O = "urn:schemas-microsoft-com:office:office"
_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_CHART = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_CHARTEX = "http://schemas.microsoft.com/office/drawing/2014/chartex"
_DIAGRAM = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
# Object inside a paragraph → (the r: attribute pointing at the part, renderer). Rendering uses the same `_charts` and `_omml` as PPTX.
_OBJECT_RENDERERS = {f"{{{_CHART}}}chart": ("id", render_chart), f"{{{_CHARTEX}}}chart": ("id", render_chartex),
                     f"{{{_DIAGRAM}}}relIds": ("dm", render_smartart)}
_HEADING_NAME = re.compile(r"^(?:heading|제목)\s*([1-9])$", re.IGNORECASE)
_TITLE_NAMES = {"title", "제목"}
# Word styles such as "List Bullet 2" and "List Number 3" express the level by the digit at the end of the name, not by the level (ilvl).
_LIST_STYLE_LEVEL = re.compile(r"^List (?:Bullet|Number|Continue|Paragraph)\s+(\d)$", re.IGNORECASE)
# A paragraph longer than this, or one containing a line break, is body text even with a heading style or outline level (same value as
# HWP/HWPX. Pinned by: test_office.py::
# TestDocxListDepthCheckBoxesAndLongHeadings::test_long_paragraph_with_an_outline_level_is_not_a_heading).
_HEADING_MAX_CHARS = 120
# Hand-typed list marks without numbering (the continuation text in `_DocxRenderer._render_paragraph`). Only those Markdown reads as a
# list item.
_HAND_TYPED_ITEM = re.compile(r"[-*]\s")
# Indents equal within this much (twips, about 0.09 cm) are the same level. Some documents store the same position as 1599 and 1600.
_INDENT_TOLERANCE = 50
# Character-unit indent (w:leftChars, 1/100 character) to twips: one 10pt character cell = 200 twips. Used only to compare levels.
_TWIPS_PER_CHAR_HUNDREDTH = 2
# Painted box: a □/☐ painted dark with a highlight or shading is a filled square (a chosen option) on screen (`_is_painted_box`).
_FILLED_BOXES = str.maketrans({"□": "■", "☐": "☒"})
_HIGHLIGHT_COLORS = {"black": "000000", "darkBlue": "000080", "darkRed": "800000", "darkMagenta": "800080",
                     "darkGreen": "008000", "darkCyan": "008080", "darkYellow": "808000", "darkGray": "808080"}
# Characters of a symbol font (w:sym, the w:t of a symbol-font run) are converted straight to Unicode with the per-font table
# (`_symbols.SYMBOL_GLYPHS`). Only w:sym characters missing from the table are moved to the private use area (U+F000 + code) and left
# to the symbol mapping of markdown.clean_text (removed if not there either). When everything was moved to the private use area,
# Symbol's ≥ ± ° μ and Wingdings' ☒ disappeared and Symbol's ◊ ¬ ♦ came out as Wingdings' → ➢ □
# (Pinned by: test_office.py::TestDocxObjects::test_symbol_font_check_boxes_are_kept, TestDocxStruckDeletedAndSymbolRuns).
# Run fonts (w:rFonts) are consulted in this order. Private-use-area characters are usually drawn in the ascii/hAnsi font.
_RUN_FONT_SLOTS = ("ascii", "hAnsi", "eastAsia", "cs")
# Header/footer kinds: default, first page, even pages
# (Pinned by: test_office.py::TestDocxObjects::test_first_page_and_even_page_headers_are_read).
_HEADER_FOOTER_KINDS = {"header": ("header", "first_page_header", "even_page_header"),
                        "footer": ("footer", "first_page_footer", "even_page_footer")}
# A macro-enabled document (.docm) and templates (.dotx, .dotm) differ from .docx only in the content type name of the main part; the
# structure is the same. python-docx reads only the .docx type name as a document part, so the others are registered with the same
# class (the same method python-docx uses to register its own types; it is a process-wide registration — deliberate).
# Pinned by: test_office.py::test_macro_enabled_document_is_read_like_a_plain_one.
for _content_type in ("application/vnd.ms-word.document.macroEnabled.main+xml",
                      "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml",
                      "application/vnd.ms-word.template.macroEnabledTemplate.main+xml"):
    PartFactory.part_type_for[_content_type] = DocumentPart
# Part referenced by imported content (w:altChunk). The extractor to read it with is chosen by content type
# (`_DocxRenderer._chunk_blocks`).
_ALT_CHUNK = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/aFChunk"
_HTML_CHUNK_TYPES = {"text/html", "application/xhtml+xml"}
# Markdown tables of imported content (`_embed_markdown_tables`): a row line, the rule line under the header row, a cell border (a `\|`
# neutralized with a backslash is cell text).
_TABLE_ROW = re.compile(r"\|.*\|")
_TABLE_RULE = re.compile(r"\|(?: ?:?-{3,}:? ?\|)+")
_TABLE_CELL_BORDER = re.compile(r"(?<!\\)\|")
_MHT_CHUNK_TYPES = {"message/rfc822", "multipart/related", "application/x-mimearchive"}
_TEXT_CHUNK_TYPES = {"text/plain"}
_library_part_class = PartFactory.part_class_selector


def _select_part_class(content_type: str, reltype: str) -> type[Part] | None:
    """An imported-content part is kept as raw bytes whatever it holds. The content type of an embedded Word document chunk has the
    same name as the main document (wordprocessingml.document.main+xml), so python-docx read the ZIP bytes as XML and rejected the whole
    document (a file whose documents were merged with the OpenXML SDK). It is a process-wide registration that wraps python-docx's
    selector function (the same method as the type name registration above — deliberate.
    Pinned by: test_office.py::TestDocxImportedChunks::test_chunk_of_an_unsupported_type_is_reported_with_a_count)."""
    if reltype == _ALT_CHUNK:
        return Part
    return _library_part_class(content_type, reltype) if _library_part_class is not None else None


PartFactory.part_class_selector = _select_part_class


def _w(tag: str) -> str:
    return f"{{{_W}}}{tag}"


# Text elements that are not emitted when on the instruction side of a complex field (`_paragraph_text`).
_FIELD_CODE_TEXT = {_w(name) for name in ("t", "tab", "ptab", "br", "cr", "noBreakHyphen", "sym")}
# Reading the instruction of an EQ field (Word 'Enclose Characters', overstrike) (`_equation_field_text`). Switches look like `\o`,
# `\ac`, `\up 1`, `\co2`; arguments are split inside the parentheses at ',' (';' in locales whose list separator is ';'). A long
# instruction, or one with many parentheses, is not parsed for structure; only its text is kept.
_EQ_FIELD = re.compile(r"\s*eq(?![A-Za-z])", re.IGNORECASE)
_EQ_SWITCH = re.compile(r"\\([A-Za-z]+)[ \t]*(?:-?\d+[ \t]*)?")
_EQ_SYNTAX = re.compile(r"\\[A-Za-z]+[ \t]*(?:-?\d+)?|[(),;\\]")
_EQ_MAX_CHARS = 2_000
_EQ_MAX_GROUPS = 32
_EQ_CIRCLES = {"○", "◯", "〇"}
_EQ_SQUARES = {"□", "☐", "◻"}
_EQ_CHECKS = {"✓", "✔", "√", "v", "V"}
_EQ_CROSSES = {"×", "✗", "✘", "x", "X"}
# Circled-number ranges: (first number, last number, first character). ①~⑳, ㉑~㉟, ㊱~㊿.
_CIRCLED_NUMBERS = ((1, 20, 0x2460), (21, 35, 0x3251), (36, 50, 0x32B1))
_CIRCLED_HANGUL = {**dict(zip("가나다라마바사아자차카타파하", map(chr, range(0x326E, 0x327C)), strict=True)),
                   **dict(zip("ㄱㄴㄷㄹㅁㅂㅅㅇㅈㅊㅋㅌㅍㅎ", map(chr, range(0x3260, 0x326E)), strict=True))}
# Character styles whose runs are emphasized: the built-in styles Word names Strong and Emphasis (the name in styles.xml is the
# English one in every language edition) → (bold, italic). Other styles are not followed (`_run_emphasis`).
_EMPHASIS_STYLE_NAMES = {"strong": (True, False), "emphasis": (False, True)}
# A HYPERLINK field: `HYPERLINK "address" \l "bookmark" \o "tip"`. The address may be unquoted; a first argument that is a
# switch (`\l "bookmark"` alone) is a jump inside the document and is not a link.
_HYPERLINK_FIELD = re.compile(r'\s*HYPERLINK\s+(?:"([^"]*)"|([^\s"\\]\S*))', re.IGNORECASE)
_HYPERLINK_BOOKMARK = re.compile(r'\\l\s+"([^"]*)"', re.IGNORECASE)
# Blocks read in order in the body, cells and text boxes: paragraph, table, imported content
_BLOCK_TAGS = (_w("p"), _w("tbl"), _w("altChunk"))


@dataclass(slots=True)
class _Style:
    style_id: str
    name: str
    outline_level: int | None
    num_id: str | None
    ilvl: int
    based_on: str | None
    indent: dict[str, int]  # left indent written directly on the style (`_indent_values`)
    size: int | None  # font size written directly on the style (w:sz, half-points). The paragraph size follows basedOn (`_paragraph_size`)
    default: bool  # the default paragraph style (w:default), used by paragraphs with no paragraph style set


# Number level definition w:lvl → (numFmt, lvlText, start number, legal-style numbering w:isLgl, restart w:lvlRestart — None means the
# default). `_level_definition`
type _LevelDefinition = tuple[str, str, int, bool, int | None]


@dataclass(slots=True)
class _NumberList:
    """One numbered list (w:num). Its level definitions are those of the number definition (w:abstractNum) it points at."""

    abstract_id: str
    levels: dict[int, _LevelDefinition]  # level → level definition. Includes levels replaced by w:lvlOverride/w:lvl
    style_levels: dict[str, int]  # paragraph style id → the level bound to that style (w:lvl/w:pStyle)
    restarts: dict[int, int]  # start-number overrides (the w:startOverride of w:lvlOverride): level → start number
    indents: dict[int, dict[str, int]]  # level → left indent of that level definition (w:lvl/w:pPr/w:ind) (`_indent_values`)


@dataclass(slots=True)
class _ListItem:
    """Preceding item remembered so that the levels of consecutive list items can be aligned by indent (`_DocxRenderer._list_depth`)."""

    num_id: str
    indent: int | None  # left indent on screen (twips). None if it is written nowhere
    depth: int  # the level that was output
    offset: int  # output level − level counted from ilvl. The next item of the same list inherits it


@dataclass(frozen=True, slots=True)
class _Inline:
    """What `_paragraph_text` marks inside a paragraph besides its text (`ConvertOptions.keep_links`, `keep_emphasis`)."""

    link: Callable[[etree._Element], str | None] | None  # w:hyperlink → vetted address, None when links are not kept
    emphasis: bool
    styles: dict[str, tuple[bool, bool]]  # character style id → (bold, italic) (`_EMPHASIS_STYLE_NAMES`)


class _DocxRenderer:
    def __init__(self, document: object, options: ConvertOptions, extraction: Extraction) -> None:
        self._options = options
        self._part = document.part
        self._main_part = document.part
        self._extraction = extraction
        self.unread_chunks = 0  # count of imported content no extractor can read (RTF, embedded Word documents …). Reported as a warning
        self.unread_ole = 0  # count of OLE objects (w:object) left as a bare placeholder with no preview picture. Reported as a warning
        self.omitted_rows = 0  # count of rows dropped by the table grid cell limit (`TableGrid.omitted_rows`). Reported as a warning
        self.placeholders = _placeholder_texts(self._part)  # glossary docPart name → placeholder text (`_keep_filled_placeholders`)
        self._styles = _load_styles(document)
        self._default_style = next((style for style in self._styles.values() if style.default), None)
        styles_root = _styles_root(document)
        default_size = (styles_root.find(f"{_w('docDefaults')}/{_w('rPrDefault')}/{_w('rPr')}/{_w('sz')}")
                        if styles_root is not None else None)
        self._default_size = _int_attr(default_size, 0) or 20  # document default font size. If absent, Word uses 10pt (20 half-points)
        self._number_lists = _load_numbering(document)
        # There is one counter per number definition (abstractNum). Numbered lists (numId) already seen in the body are noted separately.
        self._numberings: dict[str, Numbering] = {}
        self._started_lists: set[str] = set()
        self.notes = _NoteLabels()  # footnote/endnote references seen in the body, and their labels
        # Tables being rendered. A table inside a cell is handed to the table at the top of the stack (markdown.TableGrid.embed)
        self._grids: list[TableGrid] = []
        self._chunks: dict[object, Extraction] = {}  # imported-content part → its conversion (`_chunk_blocks`)
        self._chart_titles: dict[object, str] = {}  # chart part already written → its `[Chart] title` line (`_object_block`)
        self._drop_cap: tuple[etree._Element, str] | None = None  # (drop-cap paragraph, its text). Prepended to the very next paragraph
        # Preceding list items not yet closed. Cells, headers and text boxes count separately (`_list_depth`)
        self._list_items: list[_ListItem] = []
        self._list_offsets: dict[str, int] = {}  # list (numId) → the offset the last item of that list used (`_ListItem.offset`)
        # Links and emphasis to keep. None when both options are off — the paragraph text is then read exactly as before
        self.inline: _Inline | None = None
        if options.keep_links or options.keep_emphasis:
            emphasis_styles = {style_id: _EMPHASIS_STYLE_NAMES[style.name.strip().lower()] for style_id, style in self._styles.items()
                               if style.name.strip().lower() in _EMPHASIS_STYLE_NAMES}
            self.inline = _Inline(self._hyperlink_target if options.keep_links else None, options.keep_emphasis, emphasis_styles)

    def _hyperlink_target(self, hyperlink: etree._Element) -> str | None:
        """Address of a w:hyperlink: the target of its external relationship in the part being read (body, header, footnote …),
        with the bookmark (w:anchor) appended as the fragment. None for a jump inside the document (no r:id), a broken or internal
        relationship, and an address that is not kept (`_inline.link_target`)."""
        relation = hyperlink.get(f"{{{_R}}}id")
        relationship = self._part.rels.get(relation) if relation else None
        if relationship is None or not relationship.is_external:
            return None
        address = relationship.target_ref
        bookmark = hyperlink.get(_w("anchor"))
        return link_target(f"{address}#{bookmark}" if bookmark and "#" not in address else address)

    def render_container(self, container: etree._Element) -> list[str]:
        return [block for element in _children(container, *_BLOCK_TAGS) for block in self.render_block(element)]

    def render_story(self, part: object, container: etree._Element) -> list[str]:
        """Content of a part other than the body, such as a header or footer. Relationship ids of pictures and charts are looked up in
        that part. Looked up in the body part, a different part with the same id was registered as the picture
        (Pinned by: test_office.py::TestDocxObjects::test_header_picture_comes_from_the_header_part)."""
        with self.in_part(part), self._own_lists():
            return self.render_container(container)

    @contextmanager
    def in_part(self, part: object) -> Iterator[None]:
        """While relationship ids of pictures and charts are looked up in `part` (a header or footnote part)."""
        previous, self._part = self._part, part
        try:
            yield
        finally:
            self._part = previous

    @contextmanager
    def _own_lists(self) -> Iterator[None]:
        """While rendering content that counts list levels separately from the body (cells, headers, text boxes), the body's preceding
        items are set aside. When a margin paragraph in there closed the body's preceding items, the level of the body items after it
        was lost (Pinned by: test_office.py::
        TestDocxListDepthCheckBoxesAndLongHeadings::test_text_box_in_a_list_item_does_not_close_the_items_before_it)."""
        saved = self._list_items, self._list_offsets
        self._list_items, self._list_offsets = [], {}
        try:
            yield
        finally:
            self._list_items, self._list_offsets = saved

    def render_block(self, element: etree._Element) -> list[str]:
        """One paragraph, table or imported content (w:altChunk)."""
        if element.tag == _w("p"):
            return self._render_paragraph(element)
        if element.tag == _w("altChunk"):
            return self._chunk_blocks(element)
        return [self._render_table(element)]

    def _chunk_blocks(self, chunk: etree._Element) -> list[str]:
        """Read the part referenced by imported content (w:altChunk) with the extractor matching its content type and put the result in
        place: the HTML extractor for HTML, XHTML and MHT, the text extractor for plain text. Only paragraphs and tables were walked,
        so it was dropped without a warning, and a document from a generator (html-docx-js and the like) whose whole body is one
        afchunk.mht came out empty. Types no extractor can read (RTF, embedded Word documents) are counted and reported as a warning.
        Pictures of the chunk are registered again as pictures of this document (Pinned by: test_office.py::TestDocxImportedChunks)."""
        relation = chunk.get(f"{{{_R}}}id")
        part = self._part.related_parts.get(relation) if relation else None
        blob = getattr(part, "blob", None)
        if not blob:  # broken relationship or empty part
            return []

        content_type = part.content_type.split(";")[0].strip().lower()
        # A part is converted once however many w:altChunk point at it; each reference still writes its content. It was
        # converted again per reference, and a chunk that gives no text never moves the character count: 20 references to 2 MB
        # of empty <div> took 60 s (Pinned by: test_audit_office.py::TestDocxChunkPartReferencedManyTimes).
        if part in self._chunks:
            imported = self._chunks[part]
        elif content_type in _HTML_CHUNK_TYPES or (content_type in _MHT_CHUNK_TYPES and is_mhtml(blob)):
            imported = extract_html(blob, self._options)
        elif content_type in _TEXT_CHUNK_TYPES:
            imported = extract_text(blob, self._options)
        else:
            self.unread_chunks += 1
            return []
        self._chunks[part] = imported

        # The chunk's placeholders carry numbers local to the chunk. They must be rewritten in one pass so that a rewritten number is
        # not rewritten again.
        markers = {image.id: self._extraction.add_image(image.data, kind=image.kind, alt=image.alt) for image in imported.images}
        text = imported.sections[0].text if imported.sections else ""
        text = renumber_image_markers(text, markers)
        self._extraction.warnings.extend(warning for warning in imported.warnings if warning not in self._extraction.warnings)
        if self._grids:
            # Inside a table cell: a Markdown table of the chunk is handed to the outer table, like a chart inside a cell. The table
            # text went into the cell and became a mess of `\|` and `<br>` (Pinned by:
            # test_office.py::TestDocxImportedChunks::test_table_of_an_html_chunk_inside_a_table_cell_keeps_its_rows_and_columns).
            text = _embed_markdown_tables(text, embedded_table(self._grids[-1]))
        return [text] if text.strip() else []

    def _render_paragraph(self, paragraph: etree._Element) -> list[str]:
        # A paragraph wrapped by an empty content control is placeholder text ("여기를 클릭하세요", "Click here"). Some wrap a cell, so
        # it is left out here rather than in `_children` — leaving out the whole cell shifts the columns (Pinned by:
        # test_office.py::TestDocxStruckDeletedAndSymbolRuns::test_placeholder_text_of_an_empty_content_control_is_left_out). If it is
        # a numbered paragraph its number is still counted (the same as a numbered paragraph with empty text). Not counting it pulled
        # the following numbers forward (Pinned by: test_office_revisions_and_numbering.py::TestDocxPlaceholderNumberedParagraph).
        if any(_shows_placeholder(ancestor) for ancestor in paragraph.iterancestors(_w("sdt"))):
            if not _is_deleted_paragraph(paragraph):
                self._list_prefix(paragraph, self._paragraph_style(paragraph))
            return []

        objects: list[str] = []

        def add_object(element: etree._Element) -> bool:
            objects.append(self._object_block(element))
            return bool(objects[-1])

        # When footnotes are not included, the reference marks in the body are left out too. Only `[^1]` remained, with no content to
        # point at — HWP/HWPX leave them out too (Pinned by:
        # test_office.py::TestOfficeReadingOrderAndListMarks::test_note_marks_are_left_out_when_footnotes_are_not_included).
        notes = self.notes if self._options.include_footnotes else None
        marked = _paragraph_text(paragraph, notes, self.image_marker, add_object, self._paragraph_size(paragraph), self.inline).strip()
        marked = self._after_drop_cap(paragraph, marked)
        # `marked` still holds the temporary link and emphasis marks. Everything that decides what the paragraph is (empty, a
        # heading, too long for a heading) looks at the text without them, so those decisions are the same with the options off.
        text = plain_inline(marked).strip() if self.inline is not None else marked
        # Charts, SmartArt and text boxes go after the paragraph text as separate blocks.
        after = [block for block in objects if block]
        with self._own_lists():
            after.extend(
                block
                for box in paragraph.iter(_w("txbxContent"))
                if _is_read_box(box, paragraph)
                for block in self.render_container(box)
            )
        # The text of a shape pasted from PowerPoint (lc:lockedCanvas) is not w:t but the a:t of a:txBody. It goes after the paragraph
        # like a text box (Pinned by: test_office.py::TestDocxFieldsAndRuns::test_text_of_a_pasted_drawing_shape_is_kept).
        after.extend(
            line
            for body in paragraph.iter(f"{{{_A}}}txBody")
            if _is_read_box(body, paragraph)
            for line in _drawing_lines(body)
        )
        # The text of legacy WordArt (VML v:textpath) is not element text but the string attribute. It is read only in the body — the
        # same shape in a header is a watermark laid on every page ('초안' (draft), '대외비' (confidential))
        # (Pinned by: test_office.py::TestDocxLegacyWordArt).
        if self._part is self._main_part:
            arts = (_word_art_text(path) for path in paragraph.iter(f"{{{_V}}}textpath") if _is_read_box(path, paragraph))
            after.extend(art for art in arts if art)

        # A numbered paragraph with empty text still counts its number (no item is emitted). Otherwise the following numbers are pulled
        # forward. A paragraph deleted by tracked changes and the original position of a moved paragraph (w:moveFrom) are not in the
        # final version and are not counted (Pinned by: test_office.py::TestDocxNumbering::
        # test_empty_numbered_paragraph_still_counts, test_numbered_paragraph_moved_away_by_tracked_change_does_not_count).
        style = self._paragraph_style(paragraph)
        list_prefix = None if not text and _is_deleted_paragraph(paragraph) else self._list_prefix(paragraph, style)
        if not text:
            return after
        if _is_drop_cap(paragraph) and _next_is_paragraph(paragraph):
            self._drop_cap = (paragraph, marked)
            return after

        # A heading-style paragraph inside a table cell gets no heading mark. The cell text became '## 추진 배경' (a '## ' heading) —
        # HWP/HWPX do not treat it as a heading inside a cell either (Pinned by: test_office.py::TestOfficeReadingOrderAndListMarks::
        # test_heading_style_inside_a_table_cell_gets_no_heading_mark).
        # Body text longer than 120 characters with a heading style or outline level is not treated as a heading. The whole of it
        # became '## ' (Pinned by: test_office.py::
        # TestDocxListDepthCheckBoxesAndLongHeadings::test_long_paragraph_with_an_outline_level_is_not_a_heading). Line breaks (w:br)
        # are not considered (unlike HWP/HWPX, deliberate) — hand-inserted line breaks are common in cover titles and chapter titles,
        # and real headings fell to body text (test_title_and_heading_with_a_manual_line_break_stay_headings). Line breaks in heading
        # text are joined with a space below.
        is_body = self._grids or len(text) > _HEADING_MAX_CHARS
        heading = None if is_body else self._heading_level(paragraph, style)
        if self.inline is not None:
            # A heading keeps its links but is not emphasized — the heading styles are bold themselves, and a title typed in bold
            # is a heading, not an emphasis. Bold or italic over a whole paragraph (a label line, a header cell) is how the
            # paragraph is styled and is left unmarked (`_inline.resolve_inline`. Pinned by:
            # test_inline_formatting.py::TestDocxEmphasis::test_heading_is_not_emphasized, TestWholeBlocks).
            text = resolve_inline(marked, emphasis=not heading).strip()
        if heading:
            self._list_items.clear()
            # A heading with an auto number keeps the number in front of the heading text (a bullet is dropped). Returning right at
            # the heading decision dropped the number, and the number was not counted either, so the following numbers shifted
            # (Pinned by: test_office.py::TestReportedGaps::test_docx_numbered_heading_keeps_its_number).
            number = (list_prefix or "").strip()
            number = "" if number == "-" else number.removeprefix("- ")
            return [f"{'#' * heading} {number + ' ' if number else ''}{' '.join(text.split())}", *after]
        if list_prefix:
            return [self._list_item_lines(list_prefix, text), *after]

        # A paragraph with a hand-typed '- ' that is continuation text not closing the preceding items is emitted as a list line one
        # level below the remaining item. It came out at column 0 and read like a top-level item right after a deep item (Pinned by:
        # test_office.py::TestDocxListDepthCheckBoxesAndLongHeadings::
        # test_hand_typed_dash_paragraph_continuing_an_item_is_indented_under_it). '- ' lines joined by a line break (w:br) are each
        # indented as well and the other lines are left as they are (continuation lines of the preceding item). Deliberately only
        # '- ' and '* ' lines — for other lines `clean_text` removes the leading spaces (to prevent a code block), and Markdown does
        # not read them as a list anyway.
        self._close_list_items(self._paragraph_indent(paragraph, style) or 0)
        if self._list_items and not self._grids and _HAND_TYPED_ITEM.match(text):
            indent = "  " * (self._list_items[-1].depth + 1)
            lines = (line.strip() for line in text.split("\n"))
            text = "\n".join(indent + line if _HAND_TYPED_ITEM.match(line) else line for line in lines)
        return [text, *after]

    def _list_item_lines(self, prefix: str, text: str) -> str:
        """Text of a list-item paragraph. Lines separated by a line break (w:br) are not joined with spaces.

        Hand-typed sub-bullet lines were merged into one line and became a 600-character item (a real review request form). The first
        line is the item; a line starting with a bullet such as '- ', '* ', '·', '○' is an item one level down ('- ' is prepended if
        missing — for the same reason as `list_item_prefix`); the other lines are left as lines continuing the line before (leading
        spaces are removed by `clean_text` — the same as line breaks in a paragraph outside a list). Empty lines break the list, so
        they are dropped. Inside a cell the lines become `<br>` and sub-items are marked with `cell_level_mark` (Pinned by:
        test_office.py::TestDocxListDepthCheckBoxesAndLongHeadings::test_line_breaks_inside_a_list_item_keep_their_lines).
        """
        lines = [" ".join(line.split()) for line in text.split("\n") if line.strip()]
        if self._grids:
            sub = cell_level_mark(len(prefix) - len(prefix.lstrip("└─")) + 1)  # length of the level mark heading the item = level
        else:
            sub = " " * (len(prefix) - len(prefix.lstrip(" ")) + 2)

        rendered = [prefix + lines[0]]
        for line in lines[1:]:
            if _HAND_TYPED_ITEM.match(line):
                line = sub + line
            elif HAND_TYPED_BULLET.match(line):
                line = f"{sub}- {line}"
            rendered.append(line)
        return "\n".join(rendered)

    def _after_drop_cap(self, paragraph: etree._Element, text: str) -> str:
        """If the paragraph right before is a drop cap, prepend its text to this paragraph's text.

        A drop cap (w:framePr whose w:dropCap is drop or margin) is stored as a separate paragraph holding only the first letter.
        Emitted separately, '올' and '해 우리 회사는 …' (one word split after its first letter) became different paragraphs
        (Pinned by: test_office.py::TestOfficeReadingOrderAndListMarks::test_drop_cap_is_joined_to_the_paragraph_it_starts).
        """
        pending, self._drop_cap = self._drop_cap, None
        if pending is None or paragraph.getprevious() is not pending[0]:
            return text
        return pending[1] + text

    def _render_table(self, table: etree._Element) -> str:
        """For a vertical merge, the continuation cells (vMerge continue) are counted and passed on as the row span of the starting
        cell. TableGrid renders by the same rules as the other formats — Markdown repeats only the text on every row and puts picture
        placeholders only in the top cell (Pinned by: test_office.py::TestReportedGaps::
        test_docx_picture_in_a_vertically_merged_cell_is_placed_once), an HTML table uses rowspan
        (test_docx_table_with_horizontal_and_vertical_merges_keeps_both_spans). Repeating the text of the cell above directly put the
        repeated value into the HTML table instead of a rowspan."""
        # (row, column, column span, whether it is a continuation cell, tc)
        cells: list[tuple[int, int, int, bool, etree._Element]] = []
        for row_index, row in enumerate(_children(table, _w("tr"))):
            # A span below 1 and a negative number of skipped columns are not used as they are: gridSpan="0" put the next cell on
            # the same column (its text was lost) and a negative value moved cells to the left of their row
            # (Pinned by: test_audit_office.py::TestDocxGridSpanBelowOne).
            col = max(_int_attr(row.find(f"{_w('trPr')}/{_w('gridBefore')}"), 0), 0)
            for cell in _children(row, _w("tc")):
                properties = cell.find(_w("tcPr"))
                span = max(_int_attr(properties.find(_w("gridSpan")) if properties is not None else None, 1), 1)
                # A continuation cell of the legacy horizontal merge (w:hMerge) is added to the column span of the cell before it.
                # Kept separate, the merge became an empty cell (Pinned by:
                # test_office.py::TestDocxStruckDeletedAndSymbolRuns::test_legacy_horizontal_merge_extends_the_starting_cell).
                merge = properties.find(_w("hMerge")) if properties is not None else None
                if merge is not None and merge.get(_w("val"), "continue") == "continue" and cells and cells[-1][0] == row_index:
                    cells[-1] = (*cells[-1][:2], cells[-1][2] + span, *cells[-1][3:])
                    col += span
                    continue
                merge = properties.find(_w("vMerge")) if properties is not None else None
                cells.append((row_index, col, span, merge is not None and merge.get(_w("val"), "continue") == "continue", cell))
                col += span

        # Continuation cells are tied into a row span only when they are as wide as the starting cell. Tying cells of a different
        # width made the span go down at the starting cell's width and cover the neighbouring cell
        # (test_docx_vertical_merge_whose_width_changes_keeps_the_neighbour_cell). A cell that could not be tied repeats the text of
        # the cell above.
        grid = TableGrid()
        # The top-level table checks the character limit while reading its cells (`_notation.TableBudget`). The text of an inner table
        # is not counted as cell text of the outer table (it undercounts).
        budget = None if self._grids else TableBudget(self._extraction, self._options)
        outer = self._grids[-1] if self._grids else None
        self._grids.append(grid)
        try:
            self._place_cells(grid, cells, budget)
        finally:
            self._grids.pop()
        # A table inside a table is rendered in place by the outer table
        # (Pinned by: test_quality.py::test_docx_nested_table_keeps_its_rows_and_columns).
        if outer is not None:
            return outer.embed(grid)
        rendered = grid.render()
        self.omitted_rows += grid.omitted_rows()
        return rendered

    def _place_cells(self, grid: TableGrid, cells: list[tuple[int, int, int, bool, etree._Element]],
                     budget: TableBudget | None) -> None:
        """Once `budget` says the limit is exceeded, the remaining cells are not read — the document is over the limit anyway, so the
        character-count verdict is the same."""
        continuing = {(row, col): span for row, col, span, is_continuation, _ in cells if is_continuation}
        # (row, column) → text shown in that cell (merged_copy). A continuation cell has that of the cell right above
        shown: dict[tuple[int, int], str] = {}
        for row, col, span, is_continuation, cell in cells:
            if is_continuation:
                # A covered cell is inside the table too — the last row is not removed even when all of it is covered (TableGrid.reach)
                grid.reach(row, col)
                if (row, col) not in shown:
                    shown[row, col] = shown.get((row - 1, col), "")
                    grid.place(row, col, shown[row, col], col_span=span)
                    if budget is not None:
                        # Only takes up the slot. The copy of the upper cell's text is not counted (it undercounts)
                        budget.add(row, col, "", col_span=span)
                continue

            text = self._cell_text(cell)
            row_span = 1
            shown[row, col] = merged_copy(text)
            while continuing.get((row + row_span, col)) == span:
                shown[row + row_span, col] = shown[row, col]
                row_span += 1
            grid.place(row, col, text, row_span=row_span, col_span=span)
            if budget is not None and budget.add(row, col, text, row_span=row_span, col_span=span):
                break

    def _cell_text(self, cell: etree._Element) -> str:
        # A table inside a table comes back as a placeholder handed to the outer table (`_render_table`). List levels inside a cell
        # are counted per cell.
        with self._own_lists():
            lines = [line for element in _children(cell, *_BLOCK_TAGS) for line in self.render_block(element)]
        return "\n".join(line for line in lines if line.strip())

    def image_marker(self, element: etree._Element) -> str:
        """Register the image part referenced by a:blip (r:embed) / v:imagedata (r:id). Relationships of other parts such as headers
        are skipped. An OLE object (w:object) gets its preview picture, or a placeholder if that cannot be registered (`_ole_marker`)."""
        if element.tag == _w("object"):
            return self._ole_marker(element)
        if not self._options.extract_images:
            return ""
        rel_id = element.get(f"{{{_R}}}embed") or element.get(f"{{{_R}}}id")
        part = self._part.related_parts.get(rel_id) if rel_id else None
        blob = getattr(part, "blob", None)
        if not blob:
            return ""
        return f" {self._extraction.add_image(blob, alt=_drawing_alt(element))} "

    def _ole_marker(self, element: etree._Element) -> str:
        """Preview picture (v:imagedata / a:blip) of an OLE object (a pasted Excel sheet or document). When image extraction is off or
        there is no preview, `[OLE object]` is left and counted, as for HWP/HWPX. It vanished with neither a trace nor a warning
        (Pinned by: test_office_chart_units_and_ole.py::TestOfficeOleObjectPlaceholders). The object's content is not read."""
        preview = next(element.iter(f"{{{_A}}}blip", f"{{{_V}}}imagedata"), None)
        marker = self.image_marker(preview) if preview is not None else ""
        if marker.strip():
            return marker
        self.unread_ole += 1
        return f" {OLE_PLACEHOLDER} "

    def _object_block(self, element: etree._Element) -> str:
        """Chart (c:chart, cx:chart) or SmartArt (dgm:relIds) inside a paragraph → a separate block. An empty string if the part
        cannot be read."""
        attribute, render = _OBJECT_RENDERERS[element.tag]
        relation = element.get(f"{{{_R}}}{attribute}")
        part = self._part.related_parts.get(relation) if relation else None
        # A chart part is read once however many frames point at it, and its values are written at the first of them only; the
        # later frames give `[Chart] title` (the rule for ODF objects). The part was parsed and rendered again for every frame
        # (Pinned by: test_audit_office.py::TestChartPartReferencedByManyFrames).
        if part in self._chart_titles:
            return self._chart_titles[part]
        blob = getattr(part, "blob", None)
        root = parse_part(blob) if blob else None
        if root is None:
            return ""
        # Text boxes inside a chart (c:userShapes) are found through the chart part's relationships
        # (Pinned by: test_office_chart_units_and_ole.py::TestChartUserShapes).
        extra = {"load_related": related_loader(part)} if render is render_chart else {}
        if not self._grids:
            return self._first_frame(part, render, render(root, **extra))

        # Inside a table cell: a chart's value table is handed to the outer table (like a table inside a table) and SmartArt levels
        # are shown with `cell_level_mark`. The Markdown table text went into the cell and became a mess of `\|` and `<br>`, and the
        # level indentation was removed (Pinned by: test_office_cells_and_notes.py::
        # TestDocxObjectsInsideTableCells).
        if render is render_smartart:
            return render_smartart(root, in_cell=True)
        return self._first_frame(part, render, render(root, embedded_table(self._grids[-1]), **extra))

    def _first_frame(self, part: object, render: Callable[..., str], text: str) -> str:
        """Remember the title line of a chart written at its first frame (`_object_block`). SmartArt is not remembered."""
        if render is not render_smartart:
            self._chart_titles[part] = text.split("\n", 1)[0]
        return text

    def _paragraph_style(self, paragraph: etree._Element) -> _Style | None:
        style_ref = paragraph.find(f"{_w('pPr')}/{_w('pStyle')}")
        style_id = style_ref.get(_w("val")) if style_ref is not None else None
        return self._styles.get(style_id) if style_id else None

    def _paragraph_size(self, paragraph: etree._Element) -> int:
        """Paragraph font size (half-points): found from the paragraph style (the default paragraph style if there is none) by
        following basedOn; otherwise the document default."""
        style = self._paragraph_style(paragraph) or self._default_style
        seen = 0
        while style is not None and seen < 10:
            if style.size:
                return style.size
            style = self._styles.get(style.based_on) if style.based_on else None
            seen += 1
        return self._default_size

    def _heading_level(self, paragraph: etree._Element, style: _Style | None) -> int | None:
        direct = paragraph.find(f"{_w('pPr')}/{_w('outlineLvl')}")
        if direct is not None:
            return _outline_to_heading(_int_attr(direct, 9))

        seen = 0
        while style is not None and seen < 10:
            match = _HEADING_NAME.match(style.name.strip())
            if match:
                return min(int(match.group(1)), 6)
            if style.name.strip().lower() in _TITLE_NAMES:
                return 1
            if style.outline_level is not None:
                return _outline_to_heading(style.outline_level)
            style = self._styles.get(style.based_on) if style.based_on else None
            seen += 1
        return None

    def _list_reference(self, paragraph: etree._Element, style: _Style | None) -> tuple[str | None, int]:
        """Numbered list (numId) and level of a paragraph. What is written directly on the paragraph comes first; otherwise the style's.

        If the paragraph's w:numPr has only a level (ilvl) and no numId, the list is the style's and the level the paragraph's
        (Pinned by: test_office.py::TestDocxNumbering::test_list_level_without_a_list_id_takes_the_list_of_the_paragraph_style).
        The numbering of a style is looked up through the inherited styles (basedOn) as well. The level of a style's numbering is not
        the ilvl written on the style but the level in the number definition that points at that style (w:lvl/w:pStyle) (Word's
        heading numbering). Without that lookup the number is dropped and the following numbers shift
        (Pinned by: test_office.py::TestDocxNumbering::test_style_numbering_is_inherited_through_based_on).
        """
        num_pr = paragraph.find(f"{_w('pPr')}/{_w('numPr')}")
        num_id_element = num_pr.find(_w("numId")) if num_pr is not None else None
        if num_id_element is not None:
            return num_id_element.get(_w("val")), _int_attr(num_pr.find(_w("ilvl")), 0)
        direct_level = num_pr.find(_w("ilvl")) if num_pr is not None else None

        seen = 0
        while style is not None and seen < 10:
            if style.num_id:
                number_list = self._number_lists.get(style.num_id)
                level = (number_list.style_levels if number_list else {}).get(style.style_id, style.ilvl)
                return style.num_id, _int_attr(direct_level, level)
            style = self._styles.get(style.based_on) if style.based_on else None
            seen += 1
        return None, 0

    def _numbering(self, num_id: str, number_list: _NumberList) -> Numbering:
        """Counter of a numbered list. Lists pointing at the same number definition (abstractNum) share one counter and count on.

        Word restarts a level only when a list with a start-number override (startOverride) first appears. With a separate counter per
        numId, lists without an override restarted from 1 as well
        (Pinned by: test_office.py::TestDocxNumbering::test_lists_sharing_a_definition_continue_counting_until_a_start_override).
        The number text and number format are swapped in per paragraph for those of its list (w:num) — a list that overrides a level
        definition wholesale (w:lvlOverride/w:lvl) shares only the counter and keeps its own look. The start number is left at the
        value already reflected in the counter
        (Pinned by: test_office.py::TestDocxNumbering::test_level_override_with_its_own_definition_replaces_the_level).
        """
        if number_list.abstract_id not in self._numberings:
            self._numberings[number_list.abstract_id] = Numbering(_number_levels(number_list.levels), mark="%")
        numbering = self._numberings[number_list.abstract_id]
        numbering.levels = [replace(own, start=current.start)
                            for own, current in zip(_number_levels(number_list.levels), numbering.levels, strict=True)]
        if num_id not in self._started_lists:
            self._started_lists.add(num_id)
            for level, start in number_list.restarts.items():
                numbering.levels[level] = replace(numbering.levels[level], start=start)
                for restarted in [key for key in numbering.counters if key >= level]:
                    del numbering.counters[restarted]
        return numbering

    def _list_prefix(self, paragraph: etree._Element, style: _Style | None) -> str | None:
        num_id, ilvl = self._list_reference(paragraph, style)
        if not num_id or num_id == "0":
            return None
        # When there is no number definition (no numbering.xml, no w:num matching the numId) or the level is not in the definition,
        # Word draws no number. It was treated as a bullet and an ordinary paragraph became a `- ` item (Pinned by:
        # test_office_revisions_and_numbering.py::TestDocxNumberReferenceWithoutADefinition).
        number_list = self._number_lists.get(num_id)
        ilvl = min(max(ilvl, 0), 8)
        if number_list is None or ilvl not in number_list.levels:
            return None

        # The number format is taken from the actual level (ilvl) of the number definition; the indentation also reflects the level
        # in the style name.
        depth = ilvl
        level_match = _LIST_STYLE_LEVEL.match(style.name.strip()) if style is not None else None
        if level_match:
            depth = min(max(depth, int(level_match.group(1)) - 1), 8)

        depth = self._list_depth(num_id, depth, self._paragraph_indent(paragraph, style, number_list, ilvl))
        # Leading spaces of a line inside a cell are removed when the table is built (`markdown._clean_cell`). The level is marked
        # with `└ ` / `└─ ` as in HWP/HWPX (Pinned by:
        # test_office.py::TestDocxListDepthCheckBoxesAndLongHeadings::test_list_levels_inside_a_table_cell_are_marked_like_hwp).
        indent = cell_level_mark(depth) if self._grids else "  " * depth

        # Bullet and no-number levels pass through the counter too, so that deeper levels restart from 1. Skipping them made a number
        # under a bullet item continue from the number of the earlier item (Pinned by: test_office.py::TestDocxNumbering::
        # test_bullet_at_a_shallower_level_restarts_the_deeper_numbers).
        number_format = number_list.levels[ilvl][0]
        number = self._numbering(num_id, number_list).advance(ilvl)
        if number_format == "bullet":
            return f"{indent}- "
        # A no-number level (numFmt none) has an empty number for that level (`format_number` "NONE"), so when nothing remains in
        # the number text it is an unmarked paragraph. It was treated as a bullet and an explanatory paragraph under a number became
        # a `- ` item (Pinned by: test_office.py::TestDocxNumbering::
        # test_level_without_a_number_format_is_a_plain_paragraph).
        if not number:
            return None
        return f"{indent}{number} " if self._grids else list_item_prefix(number, depth)

    def _list_depth(self, num_id: str, depth: int, indent: int | None) -> int:
        """Output level of a list item. While the same list (numId) continues it is the level counted from ilvl as is (shifted by
        the same amount as the preceding item); where the list changes, the item is slotted in among the preceding items by its
        on-screen indent: the same level as a preceding item with the same indent, one level below it for a more indented item.

        A document that builds sub-items as a different numbered list (bullet items of numId2 under '1.1 지표명' (indicator name) of
        numId1) has ilvl 0 on those items, so they came out as top-level items and the hierarchy seen in Word was lost (real
        agreement proposals and review request forms. Pinned by: test_office.py::
        TestDocxListDepthCheckBoxesAndLongHeadings::test_item_of_another_list_nests_under_the_item_it_is_indented_from).
        Within one list the indent is not consulted, so the output of a single list does not change. If the indent is written
        nowhere, only the level is used.

        A list that comes back beside an item of another list with the same indent returns to the offset that list used before (it
        never gets deeper than the item beside it). Merely inheriting the level of the item beside it made a numbered list and a
        bullet list alternating at the same indent get one level deeper with every chapter (Pinned by:
        test_alternating_lists_at_the_same_indent_do_not_staircase).
        """
        items = self._list_items
        previous = items[-1] if items else None
        while indent is not None and items and items[-1].indent is not None and items[-1].indent > indent + _INDENT_TOLERANCE:
            items.pop()
        anchor = items[-1] if items and items[-1].indent is not None and indent is not None else None
        is_beside = anchor is not None and abs(anchor.indent - indent) <= _INDENT_TOLERANCE

        shown = depth
        if previous is not None and previous.num_id == num_id:
            shown = depth + previous.offset
        elif is_beside and num_id in self._list_offsets:
            shown = min(depth + self._list_offsets[num_id], anchor.depth)
        elif anchor is not None:
            shown = anchor.depth if is_beside else anchor.depth + 1
        shown = min(max(shown, 0), 8)

        if is_beside:
            items.pop()
        items.append(_ListItem(num_id, indent, shown, shown - depth))
        self._list_offsets[num_id] = shown - depth
        return shown

    def _close_list_items(self, indent: int) -> None:
        """Preceding items closed by a non-list paragraph: the items indented further than that paragraph. A paragraph indented to
        line up with an item's text is continuation text of that item and does not close it — closing it moved the next sub-item up a
        level (hand-typed '- …' paragraphs in real documents). Body text at the margin closes everything
        (Pinned by: test_office.py::TestDocxListDepthCheckBoxesAndLongHeadings::
        test_indentation_counts_the_paragraph_and_style_indents_and_body_text_ends_the_nesting). An item with indent 0 is closed by a
        margin paragraph too — compared by indent it could not be closed, and a hand-typed '- ' paragraph at the margin became an item
        under it (Pinned by: test_margin_dash_paragraph_after_a_list_without_indent_stays_at_column_0)."""
        items = self._list_items
        if indent <= 0:
            items.clear()
        while items and (items[-1].indent is None or items[-1].indent > indent + _INDENT_TOLERANCE):
            items.pop()

    def _paragraph_indent(self, paragraph: etree._Element, style: _Style | None, number_list: _NumberList | None = None,
                          ilvl: int = 0) -> int | None:
        """On-screen left indent of a paragraph (twips). Each attribute is overridden in the order style (inherited ones first) →
        number level definition → paragraph. A character-unit value (leftChars), when not 0, takes precedence — documents in which
        the paragraph's leftChars="0" turns off the leftChars of the List Paragraph style are common. None if it is written nowhere."""
        chain = []
        while style is not None and len(chain) < 10:
            chain.append(style.indent)
            style = self._styles.get(style.based_on) if style.based_on else None

        values: dict[str, int] = {}
        for indent in reversed(chain):
            values.update(indent)
        if number_list is not None:
            values.update(number_list.indents.get(ilvl, {}))
        values.update(_indent_values(paragraph.find(f"{_w('pPr')}/{_w('ind')}")))
        if values.get("chars"):
            return values["chars"] * _TWIPS_PER_CHAR_HUNDREDTH
        return values.get("left")


def _reject_strict(data: bytes) -> None:
    if is_strict_package(data):
        raise UnsupportedFormatError("Word documents in the Strict Open XML format are not supported. "
                                     "Please save the file again in Word as 'Word Document (.docx)'.")


def extract_docx(data: bytes, options: ConvertOptions) -> Extraction:
    # If opening fails because of one part that is missing or is not XML, only that part is detached and the package is opened again
    # (`_package.open_package`, one warning).
    # A Strict Open XML document fails here because its main part is not found. It is not corruption; the message asks to save it again
    # (`_package.is_strict_package`).
    extraction = Extraction()
    try:
        part = open_package(Package.open, data, extraction.warnings).main_document_part
    except (PackageNotFoundError, KeyError, ValueError) as exc:
        _reject_strict(data)
        raise CorruptedDocumentError("The DOCX document structure could not be read.") from exc
    if not isinstance(part, DocumentPart):
        _reject_strict(data)
        raise CorruptedDocumentError("The DOCX document structure could not be read.")

    document = part.document
    # A main part whose root is not w:document is not a Word document. A w:document without w:body (optional in the schema) is an
    # empty document, as a deck without a slide list is an empty deck: it is given an empty body in memory. Both raised from
    # inside the body walk (Pinned by: test_audit_office.py::TestDocxMainPartWithoutABody).
    if document.element.tag != _w("document"):
        _reject_strict(data)
        raise CorruptedDocumentError("The DOCX document structure could not be read.")
    if document.element.body is None:
        etree.SubElement(document.element, _w("body"))
    renderer = _DocxRenderer(document, options, extraction)
    _keep_filled_placeholders(document.element.body, renderer.placeholders)
    # The body is read paragraph by paragraph and table by table while counting characters. Once the limit is exceeded, the rest of
    # the body is not read.
    body: list[str] = []
    for element in _children(document.element.body, *_BLOCK_TAGS):
        rendered = renderer.render_block(element)
        body.extend(rendered)
        if extraction.count(sum(solid_length(block) for block in rendered), options):
            break

    blocks = []
    if options.include_headers_footers:
        blocks.append(_header_footer_text(document, renderer, "header"))
    blocks.append(join_blocks(body))
    if options.include_headers_footers:
        blocks.append(_header_footer_text(document, renderer, "footer"))
    if options.include_footnotes and renderer.notes.labels:
        blocks.append(_notes(document, renderer))
    blocks.append(_comments(document))
    if renderer.unread_chunks:
        extraction.warnings.append(f"{renderer.unread_chunks} imported content item(s) (altChunk: RTF, embedded Word documents, etc.) "
                                   "could not be converted.")
    if renderer.unread_ole:
        extraction.warnings.append(unread_ole_warning(renderer.unread_ole))
    if renderer.omitted_rows:
        extraction.warnings.append(omitted_rows_warning(renderer.omitted_rows))

    extraction.metadata.update(core_properties(part.package))
    extraction.sections.append(Section(SectionKind.DOCUMENT, 1, join_blocks(blocks)))
    return extraction


def _children(container: etree._Element, *tags: str) -> Iterator[etree._Element]:
    """Children whose tag is in `tags`, in document order. When a content control (w:sdt) or custom XML (w:customXml) wraps them, its
    inside is searched.

    Form and repeating-section documents wrap not only paragraphs and tables but also table rows and cells in content controls.
    Looking only at direct children makes those rows and cells vanish without a warning
    (Pinned by: test_office.py::test_table_row_wrapped_in_content_control_is_kept).
    """
    for child in container:
        if child.tag in tags:
            yield child
        elif child.tag == _w("sdt"):
            content = child.find(_w("sdtContent"))
            if content is not None:
                yield from _children(content, *tags)
        elif child.tag == _w("customXml"):
            yield from _children(child, *tags)


def _paragraph_text(paragraph: etree._Element, notes: "_NoteLabels | None",
                    image_marker: Callable[[etree._Element], str] | None = None,
                    on_object: Callable[[etree._Element], bool] | None = None, body_size: int | None = None,
                    inline: "_Inline | None" = None) -> str:
    """Collect the paragraph text in document order.

    Text boxes (w:txbxContent) are skipped because they are emitted as separate blocks, and mc:Fallback because it is a compatibility
    copy of the same shape. Field codes (w:instrText) have a different tag and drop out naturally. The original position of moved
    text (w:moveFrom) keeps its characters as plain w:t, so it is skipped explicitly — the same text is at the new position
    (w:moveTo). Tracked deletions (w:del) are skipped wholesale as well — the text (w:delText) drops out by its different tag, but
    footnote references and pictures inside came out as if they were final content. Hidden text (w:vanish on the run) is not on
    screen and is left out — text hidden through a style is not chased
    (Pinned by: test_office.py::TestDocxFieldsAndRuns::test_hidden_run_is_left_out).
    Struck-through runs (w:strike, w:dstrike) are marked as `~~text~~` — only what is set directly on the run is considered; what
    is inherited from a style is not chased. Placeholder text of an empty content control (w:showingPlcHdr) is left out
    (Pinned by: test_office.py::TestDocxStruckDeletedAndSymbolRuns).
    Charts and SmartArt are passed to `on_object` and emitted as separate blocks (True if a block was emitted). When `notes` is
    None, footnote/endnote reference marks are not emitted (the option that excludes footnotes). `body_size` is the paragraph font
    size (half-points, `_run_script`) — None if unknown.

    With `inline`, hyperlinks (w:hyperlink with an external relationship, HYPERLINK fields) and bold / italic runs are announced
    with temporary marks, and the returned text **still holds those marks**: the caller decides what the paragraph is from
    `_inline.plain_inline` and writes the Markdown with `_inline.resolve_inline` (a heading is not emphasized). Without `inline`
    the text is exactly what it was before those options existed.
    """
    parts: list[str] = []

    # For each complex field (w:fldChar begin … separate … end), True while on the instruction side (before separate). The result of
    # a field nested in an instruction is part of the outer field's instruction and not text —
    # `{ IF { NUMPAGES } > 1 "다음 쪽에 계속" "" }` became '4다음 쪽에 계속' ("continued on the next page") (Pinned by: test_office.py::
    # TestOfficeLineBreaksNestedFieldsAndMetadata::test_docx_result_of_a_field_nested_in_an_instruction_is_left_out). A field that
    # started outside the paragraph (a table of contents spanning several paragraphs) is left unknown — an unmatched end is ignored.
    # An EQ field without result text (Word 'Enclose Characters') has its instruction read and emitted as text
    # (`_equation_field_text`).
    fields: list[_Field] = []
    # Number of fields on the instruction side. Every text piece scanned the whole field stack, which took the square of the nesting
    # depth (32,000 levels took 49 seconds. Pinned by:
    # test_office_scaling_and_note_order.py::TestDocxLongRunsAndDeepFields::test_deeply_nested_fields_take_linear_time).
    instruction_fields = 0

    # visible[i] = number of non-blank pieces in parts[:i]. It replaces joining all the pieces after a field every time one ended.
    # Where pieces were swapped (script runs, ruby), `forget_from` discards the counts from there on.
    visible = [0]

    def has_text_since(start: int) -> bool:
        for piece in parts[len(visible) - 1:]:
            visible.append(visible[-1] + bool(piece.strip()))
        return visible[-1] > visible[start]

    def forget_from(start: int) -> None:
        del visible[start + 1:]

    def collapse_from(start: int) -> None:
        """Called right before the pieces from `start` on are swapped for one piece (script runs, ruby). A field that began in
        there keeps a piece index that no longer exists: whether it has shown text so far is settled now, and its result is
        counted from after the swapped piece. The stale index raised IndexError (the whole document was rejected) or gave the
        wrong 'no result text' verdict (Pinned by: test_audit_office.py::TestDocxFieldStartedInsideASwappedRun)."""
        for open_field in reversed(fields):
            if open_field.start <= start:
                break
            open_field.shown = open_field.shown or has_text_since(open_field.start)
            open_field.start = start + 1
            open_field.hidden = [(min(index, start + 1), text) for index, text in open_field.hidden]

    # Run → the look of that run (`_RunLook`). Looking up the run properties for every w:t (scanning all children when there is no
    # w:rPr) and joining the whole run's text again to see whether the run holds only a box took the square of the number of w:t
    # (a 38 KB document took 312 seconds. Pinned by: test_office_scaling_and_note_order.py::
    # TestDocxLongRunsAndDeepFields::test_run_with_many_text_pieces_takes_linear_time).
    run_looks: dict[etree._Element, _RunLook] = {}

    def walk(element: etree._Element) -> None:
        nonlocal instruction_fields
        for child in element:
            tag = child.tag
            if tag in (_w("txbxContent"), f"{{{_MC}}}Fallback", _w("pPr"), _w("rPr"), _w("moveFrom"), _w("del")):
                continue
            if tag in _FIELD_CODE_TEXT and instruction_fields:
                if tag == _w("t"):
                    fields[-1].hidden.append((len(parts), child.text or ""))
                continue
            if tag == _w("r") and _is_on(child.find(f"{_w('rPr')}/{_w('vanish')}")):
                continue
            if tag == _w("sdt") and _shows_placeholder(child):
                continue
            if tag == _w("t"):
                look = run_looks.get(element) or run_looks.setdefault(element, _RunLook.of(element, body_size, inline))
                text = symbol_font_text(look.symbol_font, child.text or "", ascii_font=look.ascii_font, latin_font=look.latin_font)
                if look.painted_box:
                    text = text.translate(_FILLED_BOXES)
                # Emphasis is the outer mark and strikethrough the inner one: `**~~text~~**`
                parts.append(mark_emphasis(mark_strike(text) if look.struck else text, look.bold, look.italic))
            elif tag in (_w("tab"), _w("ptab")):
                parts.append("\t")
            elif tag in (_w("br"), _w("cr")):
                parts.append("\n")
            elif tag == _w("noBreakHyphen"):
                parts.append("-")
            elif tag in (f"{{{_A}}}blip", f"{{{_V}}}imagedata") or (tag == _w("object") and _is_ole_object(child)):
                if image_marker is not None:
                    parts.append(image_marker(child))
            elif tag in (f"{{{_CHART}}}chart", f"{{{_DIAGRAM}}}relIds"):
                if on_object is not None:
                    on_object(child)
            elif tag == f"{{{_MC}}}AlternateContent" and next(child.iter(f"{{{_CHARTEX}}}chart"), None) is not None:
                # A new chart kind (chartex: waterfall, treemap …) is emitted as a table when its values can be read; otherwise the
                # pre-rendered picture of the fallback (mc:Fallback) is used. If there is none either, a placeholder is left. The
                # fallback was always skipped, so the chart vanished without a trace (Pinned by:
                # test_office.py::TestDocxFieldsAndRuns::test_chartex_falls_back_to_its_picture_or_a_placeholder).
                if on_object is None or not on_object(next(child.iter(f"{{{_CHARTEX}}}chart"))):
                    start = len(parts)
                    fallback = child.find(f"{{{_MC}}}Fallback")
                    if fallback is not None:
                        walk(fallback)
                    if not has_text_since(start):
                        parts.append("[Chart]")
            elif tag == _w("sym"):
                symbol = _symbol_char(child)
                if inline is not None and inline.emphasis and element.tag == _w("r"):
                    look = run_looks.get(element) or run_looks.setdefault(element, _RunLook.of(element, body_size, inline))
                    symbol = mark_emphasis(symbol, look.bold, look.italic)
                parts.append(symbol)
            elif tag == _w("fldChar"):
                kind = child.get(_w("fldCharType"))
                if not instruction_fields:
                    parts.append(_form_field_value(child))
                if kind == "begin":
                    fields.append(_Field(start=len(parts)))
                    instruction_fields += 1
                elif kind == "separate" and fields:
                    if fields[-1].instruction_side:
                        instruction_fields -= 1
                    fields[-1].instruction_side = False
                    # The result of a HYPERLINK field is the link text. The link is closed where the field ends; a field that
                    # never ends leaves an unclosed mark, which is not written
                    # (Pinned by: test_inline_formatting.py::TestDocxLinks::test_hyperlink_field_code_is_a_link).
                    if inline is not None and inline.link is not None and not instruction_fields and fields[-1].link is None:
                        fields[-1].link = _field_link("".join(fields[-1].instruction))
                        if fields[-1].link:
                            parts.append(LINK_OPEN)
                    fields[-1].start = len(parts)
                    fields[-1].shown = False
                elif kind == "end" and fields:
                    ended = fields.pop()
                    if ended.instruction_side:
                        instruction_fields -= 1
                    if not instruction_fields and not ended.shown and not has_text_since(ended.start):
                        parts.append(_equation_field_text("".join(ended.instruction)))
                    if ended.link:
                        parts.append(link_close(ended.link))
            elif tag == _w("instrText"):
                if fields and fields[-1].instruction_side:
                    fields[-1].instruction.append(child.text or "")
            elif tag == _w("fldSimple"):
                link = None
                if inline is not None and inline.link is not None and not instruction_fields:
                    link = _field_link(child.get(_w("instr"), ""))
                if link:
                    parts.append(LINK_OPEN)
                start = len(parts)
                walk(child)
                if not instruction_fields and not has_text_since(start):
                    parts.append(_equation_field_text(child.get(_w("instr"), "")))
                if link:
                    parts.append(link_close(link))
            elif tag == _w("hyperlink"):
                # Only a link out of the document (external relationship) is kept; a jump to a bookmark stays text. On the
                # instruction side of a field nothing is emitted, so the marks would hold no text and are not written at all.
                link = inline.link(child) if inline is not None and inline.link is not None else None
                if link:
                    parts.append(LINK_OPEN)
                walk(child)
                if link:
                    parts.append(link_close(link))
            elif tag == _w("ruby"):
                # Ruby stores the annotation (w:rt) before the base text (w:rubyBase). Read in order it became '한국韓國'. It is
                # emitted in the same shape as HWP ruby text
                # (Pinned by: test_office.py::TestDocxFieldsAndRuns::test_ruby_is_written_as_base_then_annotation).
                start = len(parts)
                texts = []
                for name in ("rubyBase", "rt"):
                    first = len(parts)
                    for piece in child.iterchildren(_w(name)):
                        walk(piece)
                    texts.append("".join(parts[first:]))
                collapse_from(start)
                parts[start:] = [format_dutmal(*texts)]
                forget_from(start)
            elif tag == _w("r") and _run_script(child, body_size) is not None:
                # Superscript/subscript run. Appended as plain text, 10⁶ became 106
                # (Pinned by: test_office.py::TestSuperscriptAndTextBoxes::test_docx_superscript_and_subscript_runs_are_marked).
                start = len(parts)
                walk(child)
                text = mark_script("".join(parts[start:]), _run_script(child, body_size))
                text = mark_strike(text) if _is_struck(child) else text
                if inline is not None and inline.emphasis:
                    # The emphasis mark goes around the script mark, so a bold formula stays one span: `**H_2O**`
                    text = mark_emphasis(text, *_run_emphasis(child, inline.styles))
                collapse_from(start)
                parts[start:] = [text]
                forget_from(start)
            elif tag == f"{{{_M}}}oMath":
                parts.append(render_omml(child))
            elif tag in (_w("footnoteReference"), _w("endnoteReference")):
                if notes is None:
                    continue
                kind = "footnote" if tag == _w("footnoteReference") else "endnote"
                parts.append(f"[^{notes.label(kind, child.get(_w('id'), ''))}]")
            else:
                walk(child)

    walk(paragraph)
    # For a field still on the instruction side when the paragraph ends (a corrupted file without separate/end), the text after the
    # instruction is the result. Removing it made the rest of the paragraph vanish
    # (Pinned by: test_office_symbols_fields_notes.py::TestDocxFieldWithoutAnEnd)
    unclosed = sorted((piece for open_field in fields if open_field.instruction_side for piece in open_field.hidden),
                      key=lambda piece: piece[0])
    if unclosed:
        merged, taken = [], 0
        for index, text in unclosed:
            merged += parts[taken:index]
            merged.append(text)
            taken = max(taken, index)
        parts = merged + parts[taken:]
    text = "".join(parts)
    if inline is not None:
        text = merge_emphasis_marks(text)
    return resolve_scripts(resolve_strikes(text))


def _field_link(instruction: str) -> str | None:
    """Vetted address of a HYPERLINK field instruction, None for any other field and for an address that is not kept."""
    match = _HYPERLINK_FIELD.match(instruction)
    if match is None:
        return None
    address = match.group(1) if match.group(1) is not None else match.group(2)
    bookmark = _HYPERLINK_BOOKMARK.search(instruction, match.end())
    return link_target(f"{address}#{bookmark.group(1)}" if bookmark and bookmark.group(1) and "#" not in address else address)


def _run_emphasis(run: etree._Element, styles: dict[str, tuple[bool, bool]]) -> tuple[bool, bool]:
    """(bold, italic) of a run: w:b / w:i set directly on the run (`w:val="0"` / `false` switches it off), otherwise what its
    character style gives when that style is Strong or Emphasis.

    Deliberate: the paragraph style and other character styles are not followed. A heading or title style is bold as a whole,
    which says what the paragraph is and not that its words are stressed; following every style would mark whole documents whose
    body style happens to be bold. Complex-script bold (w:bCs / w:iCs) is not read either
    (Pinned by: test_inline_formatting.py::TestDocxEmphasis).
    """
    properties = run.find(_w("rPr"))
    if properties is None:
        return False, False
    style = properties.find(_w("rStyle"))
    bold, italic = styles.get(style.get(_w("val"), ""), (False, False)) if style is not None else (False, False)
    direct_bold, direct_italic = properties.find(_w("b")), properties.find(_w("i"))
    return (_is_on(direct_bold) if direct_bold is not None else bold,
            _is_on(direct_italic) if direct_italic is not None else italic)


def _is_ole_object(element: etree._Element) -> bool:
    """Whether a w:object is an OLE object (o:OLEObject, or w:objectEmbed / w:objectLink). One holding only an ActiveX control
    (w:control) is not."""
    return next(element.iter(f"{{{_O}}}OLEObject", _w("objectEmbed"), _w("objectLink")), None) is not None


def _is_drop_cap(paragraph: etree._Element) -> bool:
    """Whether the paragraph frame (w:framePr) is a drop cap (w:dropCap is drop or margin)."""
    frame = paragraph.find(f"{_w('pPr')}/{_w('framePr')}")
    return frame is not None and frame.get(_w("dropCap")) in ("drop", "margin")


def _next_is_paragraph(paragraph: etree._Element) -> bool:
    following = paragraph.getnext()
    return following is not None and following.tag == _w("p")


def _is_on(element: etree._Element | None) -> bool:
    """On/off element such as w:vanish or w:checked. On when the element is present and has no `w:val`."""
    return element is not None and element.get(_w("val"), "1") not in ("0", "false", "off")


def _is_struck(run: etree._Element) -> bool:
    """Strikethrough (w:strike) or double strikethrough (w:dstrike) set directly on the run."""
    properties = run.find(_w("rPr"))
    return properties is not None and (_is_on(properties.find(_w("strike"))) or _is_on(properties.find(_w("dstrike"))))


@dataclass(frozen=True)
class _RunLook:
    """Look of a run, used when reading w:t. Built once per run (run_looks in `_paragraph_text`)."""

    # Strikethrough. For a script run it is applied outside the script mark, in the script branch (inside, it breaks the notation)
    struck: bool
    symbol_font: str | None
    ascii_font: str | None
    latin_font: str | None
    painted_box: bool  # a run holding only a box painted dark (`_is_painted_box`)
    # Bold / italic to mark (`_run_emphasis`). Not for the text of a script run: the script notation (`10^6`) is built from the
    # bare text, and the run as a whole is marked where the script mark is put (`_paragraph_text`)
    bold: bool = False
    italic: bool = False

    @classmethod
    def of(cls, run: etree._Element, body_size: int | None, inline: "_Inline | None" = None) -> "_RunLook":
        is_script = _run_script(run, body_size) is not None
        emphasis = (False, False)
        if inline is not None and inline.emphasis and not is_script and run.tag == _w("r"):
            emphasis = _run_emphasis(run, inline.styles)
        return cls(_is_struck(run) and not is_script, _symbol_font(run), *_plain_fonts(run), _is_painted_box(run), *emphasis)


def _is_painted_box(run: etree._Element) -> bool:
    """Whether the run holds only a box painted dark (the □/☐ of such a run is emitted as ■/☒). A run that paints only a box with a
    black highlight (w:highlight) or dark shading (w:shd) is a filled square on screen, i.e. a chosen option, yet it came out as □,
    the same as an option not chosen ('□ 비용형' in a real agreement proposal). ■ is the character the same documents use for chosen
    options. Changed only when the run's text is nothing but the box — a run whose text is painted too is inverse emphasis. A box
    with a light text color set is left alone, since its meaning cannot be determined. Only what is set directly on the run is
    considered (Pinned by: test_office.py::TestDocxListDepthCheckBoxesAndLongHeadings::
    test_box_painted_with_a_black_highlight_is_a_filled_box).
    """
    properties = run.find(_w("rPr"))
    if run.tag != _w("r") or properties is None:
        return False

    highlight, shading, color = properties.find(_w("highlight")), properties.find(_w("shd")), properties.find(_w("color"))
    backgrounds = [_HIGHLIGHT_COLORS.get(highlight.get(_w("val"), "")) if highlight is not None else None]
    if shading is not None:
        # The 'solid' pattern paints with the pattern color (w:color, auto is black). Otherwise it is the background color (w:fill,
        # auto paints nothing).
        solid = shading.get(_w("val")) == "solid"
        backgrounds.append(shading.get(_w("color"), "auto").replace("auto", "000000") if solid else shading.get(_w("fill")))
    is_dark = any(level is not None and level < 0.25 for level in map(_luminance, backgrounds))
    glyph = _luminance(color.get(_w("val")) if color is not None else None)
    is_light_glyph = glyph is not None and glyph > 0.6
    return is_dark and not is_light_glyph and _is_box_only(run)


def _is_box_only(run: etree._Element) -> bool:
    """Whether the run's text (all of its w:t) is only boxes (□, ☐) and spaces."""
    return not "".join(node.text or "" for node in run.iterchildren(_w("t"))).strip(" □☐")


def _luminance(color: str | None) -> float | None:
    """Brightness (0–1) of an RRGGBB color. None if it is not a color (auto, missing, etc.)."""
    if color is None or not re.fullmatch(r"[0-9A-Fa-f]{6}", color):
        return None
    red, green, blue = (int(color[index:index + 2], 16) for index in (0, 2, 4))
    return (0.299 * red + 0.587 * green + 0.114 * blue) / 255


def _shows_placeholder(control: etree._Element) -> bool:
    """Whether it is a content control that is empty and shows placeholder text (the w:sdtPr/w:showingPlcHdr of w:sdt)."""
    return _is_on(control.find(f"{_w('sdtPr')}/{_w('showingPlcHdr')}"))


def _placeholder_texts(part: Part) -> dict[str, str]:
    """docPart name of the glossary document (glossaryDocument relationship) → its text with whitespace removed. An empty dict if
    there is no glossary document or it is broken."""
    glossary = next((rel.target_part for rel in part.rels.values()
                     if not rel.is_external and rel.reltype.endswith("/glossaryDocument")), None)
    root = parse_part(glossary.blob) if glossary is not None else None
    if root is None:
        return {}
    texts = {}
    for entry in root.iter(_w("docPart")):
        name = entry.find(f"{_w('docPartPr')}/{_w('name')}")
        body = entry.find(_w("docPartBody"))
        if name is not None and body is not None:
            texts[name.get(_w("val"), "")] = "".join("".join(text.text or "" for text in body.iter(_w("t"))).split())
    return texts


def _keep_filled_placeholders(root: etree._Element, placeholders: dict[str, str]) -> None:
    """A content control that still carries showingPlcHdr but whose content differs from the placeholder text it points at (the text
    of the glossary docPart) has the flag removed — it is real input. Input values of a document whose text alone was filled in by a
    program (Apache POI 55966) were taken for placeholder text and dropped (Pinned by:
    test_office_revisions_and_numbering.py::TestDocxPlaceholderFlagOnRealInput). If the referenced docPart does not exist there is
    nothing to compare with, so the flag is kept (left out as placeholder text — there are placeholders with data binding only and
    no docPart, like '[Address 1]' in Word's résumé template)."""
    if not placeholders:
        return
    for control in root.iter(_w("sdt")):
        flag = control.find(f"{_w('sdtPr')}/{_w('showingPlcHdr')}")
        reference = control.find(f"{_w('sdtPr')}/{_w('placeholder')}/{_w('docPart')}")
        content = control.find(_w("sdtContent"))
        guide = placeholders.get(reference.get(_w("val"), "")) if reference is not None else None
        if not _is_on(flag) or guide is None or content is None:
            continue
        text = "".join("".join(node.text or "" for node in content.iter(_w("t"))).split())
        if text and text != guide:
            flag.getparent().remove(flag)


@dataclass
class _Field:
    """One complex field inside a paragraph: whether on the instruction side, the instruction text, the index of the piece where the
    result text starts (the begin position until separate), the text left out."""

    start: int
    instruction_side: bool = True
    instruction: list[str] = field(default_factory=list)
    hidden: list[tuple[int, str]] = field(default_factory=list)  # text pieces left out on the instruction side (insert position, text)
    link: str | None = None  # address of a HYPERLINK field whose result is being read ('' when it was looked at and is not a link)
    shown: bool = False  # result text seen in pieces that were swapped for one piece afterwards (`collapse_from` of `_paragraph_text`)


def _equation_field_text(instruction: str) -> str:
    """Visible text if the instruction of a field that ended without result text is an EQ field, otherwise ''.

    Word's 'Enclose Characters' (Home → Enclose Characters) stores only the instruction `eq \\o\\ac(○,21)` and no result run (Word
    overstrikes when drawing). The instruction was not read, so '제㉑조' became '제조' and '☑ 신규지표' became '신규지표'. Overstrike
    (\\o) turns a circle + a number or one letter into an enclosed character (①~㊿, ⓐ, ㉮, ㉠), a square + ✓/× into ☑/☒, other
    circle/square text into '(주)' / '[확인]', and otherwise joins the overstruck characters. A fraction (\\f) is '1/2'; an unknown
    command joins its argument text with spaces (nothing is removed). **Checked only with synthetic XML, without a real Word file**
    (Pinned by: test_office_symbols_fields_notes.py::TestDocxEnclosedCharacters).
    """
    match = _EQ_FIELD.match(instruction)
    if match is None:
        return ""
    source = instruction[match.end():]
    # Keep only the text so that deep nesting does not exceed the recursion limit
    if len(source) > _EQ_MAX_CHARS or source.count("(") > _EQ_MAX_GROUPS:
        return " ".join(_EQ_SYNTAX.sub(" ", source).split())
    return _eq_expression(source, 0, "")[0].strip()


def _eq_expression(source: str, index: int, stops: str) -> tuple[str, int]:
    """Read one expression of an EQ instruction up to a character of `stops` (outside parentheses) → (visible text, stop position)."""
    out = []

    while index < len(source) and source[index] not in stops:
        char = source[index]
        switch = _EQ_SWITCH.match(source, index) if char == "\\" else None
        if char == "\\" and switch is None:  # a character escaped with a backslash (\,·\(·\\)
            out.append(source[index + 1:index + 2])
            index += 2
        elif switch is not None:
            index = switch.end()
            while (option := _EQ_SWITCH.match(source, index)) is not None:  # decoration switches, as in \o\ac and \s\up 1
                index = option.end()
            arguments = []
            if source[index:index + 1] == "(":
                while source[index:index + 1] not in (")", ""):
                    argument, index = _eq_expression(source, index + 1, ",;)")
                    arguments.append(argument.strip())
                index += 1
            out.append(_eq_command(switch.group(1).lower(), arguments))
        elif char == "(":
            inner, index = _eq_expression(source, index + 1, ")")
            out.append(f"({inner})")
            index += 1
        else:
            out.append(char)
            index += 1

    return "".join(out), index


def _eq_command(command: str, arguments: list[str]) -> str:
    """Visible text of one EQ command. Commands other than overstrike (o) and fraction (f) (s script, a array, x box …) join their
    argument text with spaces."""
    pieces = [argument for argument in arguments if argument]
    if command == "f" and len(pieces) == 2:
        return f"{pieces[0]}/{pieces[1]}"
    if command != "o":
        return " ".join(pieces)

    if len(pieces) == 2:
        for shape, inner in (pieces, pieces[::-1]):
            if shape in _EQ_CIRCLES:
                return _circled(inner) or f"({inner})"
            if shape in _EQ_SQUARES:
                return "☑" if inner in _EQ_CHECKS else "☒" if inner in _EQ_CROSSES else f"[{inner}]"
    return "".join(pieces)


def _circled(text: str) -> str | None:
    """Text inside a circle → enclosed character (0–50, one Latin letter, 가~하, ㄱ~ㅎ). None if there is none."""
    number = int(text) if text.isdecimal() and len(text) <= 2 else -1  # int() raises ValueError on a long digit string
    if number == 0:
        return "⓪"
    for first, last, start in _CIRCLED_NUMBERS:
        if first <= number <= last:
            return chr(start + number - first)
    if len(text) == 1 and text.isascii() and text.isalpha():
        return chr((0x24D0 if text.islower() else 0x24B6) + ord(text.lower()) - ord("a"))
    return _CIRCLED_HANGUL.get(text)


def _form_field_value(field_char: etree._Element) -> str:
    """Check box state and chosen drop-down entry of a legacy form field (the w:ffData of w:fldChar). They are attributes, not
    characters, so they vanished.

    A check box is emitted with the same characters as a content-control check box (☒, ☐). The state is w:checked, or the default
    (w:default) if absent. A drop-down is the w:result-th entry (w:default if absent). The text of a text input is in the field's
    result run and is read separately
    (Pinned by: test_office.py::TestDocxFieldsAndRuns::test_legacy_form_field_states_are_kept).
    """
    check_box = field_char.find(f"{_w('ffData')}/{_w('checkBox')}")
    if check_box is not None:
        state = check_box.find(_w("checked"))
        return "☒" if _is_on(state if state is not None else check_box.find(_w("default"))) else "☐"
    drop_down = field_char.find(f"{_w('ffData')}/{_w('ddList')}")
    if drop_down is not None:
        entries = [entry.get(_w("val"), "") for entry in drop_down.iterchildren(_w("listEntry"))]
        selected = _int_attr(drop_down.find(_w("result")), _int_attr(drop_down.find(_w("default")), 0))
        return entries[selected] if 0 <= selected < len(entries) else ""
    return ""


def _drawing_lines(body: etree._Element) -> list[str]:
    """Lines of DrawingML shape text (a:txBody). A line break inside a paragraph (a:br) splits lines too
    (`_charts.drawing_paragraph_lines`). Empty lines are dropped."""
    return [line for paragraph in body.iter(f"{{{_A}}}p") for line in drawing_paragraph_lines(paragraph)]


def _run_script(run: etree._Element, body_size: int | None = None) -> str | None:
    """Superscript/subscript mark of a run (`^` / `_`). None if it is not a script.

    Word writes it with w:vertAlign; LibreOffice writes it with raise/lower (w:position) and a small font size (w:sz) — the mark was
    missing and the text became 'H2O'. Only when the paragraph font size (`body_size`, half-points) is known, a run whose directly
    written size is at most 0.8× of it and that is raised by at least 0.2× or lowered by at least 0.1× of the run size is treated
    as a script (the same criteria as the PDF position check `pdf._scripted_text`. Pinned by:
    test_office.py::TestSuperscriptAndTextBoxes::test_docx_raised_and_lowered_small_runs_from_libreoffice_are_marked). Decorative
    text shifted at the same size stays as it is.
    """
    properties = run.find(_w("rPr"))
    if properties is None:
        return None
    align = properties.find(_w("vertAlign"))
    if align is not None:
        return {"superscript": SUPERSCRIPT, "subscript": SUBSCRIPT}.get(align.get(_w("val")))

    size = _int_attr(properties.find(_w("sz")), 0)
    shift = _int_attr(properties.find(_w("position")), 0)
    if not body_size or size <= 0 or size > 0.8 * body_size:
        return None
    if shift >= 0.2 * size:
        return SUPERSCRIPT
    return SUBSCRIPT if -shift >= 0.1 * size else None


def _is_read_box(box: etree._Element, paragraph: etree._Element) -> bool:
    """Whether this is a text box (or shape text / legacy WordArt text path) to be read separately after the paragraph text.

    mc:Fallback is a compatibility copy of the same shape. One inside another text box is read when the paragraphs of the outer text
    box are rendered — reading it here too made it appear twice
    (Pinned by: test_office.py::TestSuperscriptAndTextBoxes::test_docx_text_box_inside_a_text_box_is_read_once).
    What sits inside a tracked deletion (w:del), the original position of moved text (w:moveFrom) or a hidden run (w:vanish) — all
    skipped by the paragraph text (`_paragraph_text`) — is not in the final version either. Text boxes are found and read separately
    from the paragraph text, so the price in a deleted box and a hidden box came out as valid content, and a moved box twice
    (Pinned by: test_office_revisions_and_numbering.py::TestDocxBoxesOutsideTheFinalVersion).
    """
    if _inside_fallback(box):
        return False
    for ancestor in box.iterancestors():
        if ancestor is paragraph:
            return True
        if ancestor.tag in (_w("txbxContent"), _w("del"), _w("moveFrom")):
            return False
        if ancestor.tag == _w("r") and _is_on(ancestor.find(f"{_w('rPr')}/{_w('vanish')}")):
            return False
    return True


def _is_deleted_paragraph(paragraph: etree._Element) -> bool:
    """Whether the paragraph mark (w:pPr/w:rPr) was deleted by tracked changes or is the original position of a moved paragraph
    (w:moveFrom) — a paragraph that is not in the final version."""
    mark = paragraph.find(f"{_w('pPr')}/{_w('rPr')}")
    return mark is not None and (mark.find(_w("del")) is not None or mark.find(_w("moveFrom")) is not None)


def _symbol_font(run: etree._Element) -> str | None:
    """The font set directly on the run (w:rFonts) that has a symbol-font table. Style and theme fonts are not chased (same as
    strikethrough).

    Word's Insert Symbol is w:sym, but pasted text and other generators store characters of a Wingdings run (U+F0FD etc.). The shared
    mapping had only □, so ☒ was removed and only □ was left, and the chosen option read the other way round
    (Pinned by: test_office_symbols_fields_notes.py::TestSymbolFontCharactersInText).
    """
    fonts = run.find(f"{_w('rPr')}/{_w('rFonts')}")
    if fonts is None:
        return None
    return next((font for slot in _RUN_FONT_SLOTS if (font := fonts.get(_w(slot))) and font.strip().lower() in SYMBOL_GLYPHS),
                None)


def _embed_markdown_tables(text: str, table: Callable[[list[list[str]]], str]) -> str:
    """Read the Markdown table blocks in imported content (the result text of the HTML extractor) back into row lists and replace
    them with `table` (the `embedded_table` that hands them to the outer table). The `\\|` and `<br>` in cell text were added by
    `markdown.render_table` and are undone. Known accepted gap: a chunk table that came out as an HTML table because horizontal
    merges remain is not read back (the table tags go into the cell text neutralized)."""
    blocks = []
    for block in text.split("\n\n"):
        lines = block.strip().split("\n")
        if len(lines) < 2 or not _TABLE_RULE.fullmatch(lines[1]) or not all(_TABLE_ROW.fullmatch(line) for line in lines):
            blocks.append(block)
            continue
        rows = [[cell.strip().replace("\\|", "|").replace("<br>", "\n") for cell in _TABLE_CELL_BORDER.split(line[1:-1])]
                for index, line in enumerate(lines) if index != 1]
        blocks.append(table(rows))
    return "\n\n".join(blocks)


def _plain_fonts(run: etree._Element) -> tuple[str | None, str | None]:
    """Fonts for ordinary characters set directly on the run: (w:ascii for ASCII characters, w:hAnsi for the other Latin-1
    characters). A theme font (w:asciiTheme etc.), when present, wins, so the slot counts as absent (the Symbol mapping of ordinary
    characters in `symbol_font_text`)."""
    fonts = run.find(f"{_w('rPr')}/{_w('rFonts')}")
    if fonts is None:
        return None, None
    return tuple(None if fonts.get(_w(f"{slot}Theme")) else fonts.get(_w(slot)) for slot in ("ascii", "hAnsi"))


def _symbol_char(symbol: etree._Element) -> str:
    try:
        code = int(symbol.get(_w("char"), ""), 16)
    except ValueError:
        return ""
    font = symbol.get(_w("font"), "").strip().lower()
    glyph = symbol_glyph(font, code)
    if glyph is not None:
        return glyph
    if code < 0x100 and font.startswith(SYMBOL_FONTS):
        code += 0xF000
    return chr(code) if 0x20 <= code <= 0x10FFFF and not 0xD800 <= code <= 0xDFFF else ""


def _word_art_text(path: etree._Element) -> str:
    """Text of a v:textpath (string). One whose text path is turned off (on="f") and the template of a shape definition
    (v:shapetype) are left out."""
    if path.get("on", "t").lower() in ("f", "false") or any(ancestor.tag == f"{{{_V}}}shapetype" for ancestor in path.iterancestors()):
        return ""
    return " ".join(path.get("string", "").split())



def _drawing_alt(element: etree._Element) -> str | None:
    """Alt text of a picture: the wp:docPr descr (title if absent) of the nearest wp:inline / wp:anchor."""
    for ancestor in element.iterancestors(f"{{{_WP}}}inline", f"{{{_WP}}}anchor"):
        doc_pr = ancestor.find(f"{{{_WP}}}docPr")
        if doc_pr is not None:
            return doc_pr.get("descr") or doc_pr.get("title")
    return None


def _inside_fallback(element: etree._Element) -> bool:
    return any(ancestor.tag == f"{{{_MC}}}Fallback" for ancestor in element.iterancestors())


def _int_attr(element: etree._Element | None, default: int) -> int:
    if element is None:
        return default
    value = element.get(_w("val"))
    return _int_value(value, default)


def _outline_to_heading(level: int) -> int | None:
    """w:outlineLvl starts at 0, and 9 is body text."""
    return min(level + 1, 6) if 0 <= level < 9 else None


def _raw_part_root(part: object, kind: str) -> etree._Element | None:
    """Root of the styles or numbering part (`kind`) parsed from its bytes, None when it is not XML. A part declared with a generic
    content type (application/xml) comes back from python-docx as a plain `Part` with no parsed tree, and asking it for its styles
    or element raised AttributeError, which rejected the whole document. It is read the way the footnotes and comments parts are
    (Pinned by: test_audit_office.py::TestDocxStylesAndNumberingWithAGenericContentType)."""
    related = next((rel.target_part for rel in part.rels.values() if not rel.is_external and rel.reltype.endswith(f"/{kind}")), None)
    blob = getattr(related, "blob", None)
    return parse_part(blob) if blob else None


def _styles_root(document: object) -> etree._Element | None:
    try:
        return document.styles.element
    except AttributeError:
        return _raw_part_root(document.part, "styles")


def _load_styles(document: object) -> dict[str, _Style]:
    styles: dict[str, _Style] = {}
    root = _styles_root(document)
    for style in root.iterchildren(_w("style")) if root is not None else ():
        style_id = style.get(_w("styleId"))
        if not style_id:
            continue
        name = style.find(_w("name"))
        outline = style.find(f"{_w('pPr')}/{_w('outlineLvl')}")
        num_pr = style.find(f"{_w('pPr')}/{_w('numPr')}")
        num_id = num_pr.find(_w("numId")) if num_pr is not None else None
        based_on = style.find(_w("basedOn"))
        styles[style_id] = _Style(
            style_id=style_id,
            name=name.get(_w("val"), "") if name is not None else "",
            outline_level=_int_attr(outline, 9) if outline is not None else None,
            num_id=num_id.get(_w("val")) if num_id is not None else None,
            ilvl=_int_attr(num_pr.find(_w("ilvl")) if num_pr is not None else None, 0),
            based_on=based_on.get(_w("val")) if based_on is not None else None,
            indent=_indent_values(style.find(f"{_w('pPr')}/{_w('ind')}")),
            size=_int_attr(style.find(f"{_w('rPr')}/{_w('sz')}"), 0) or None,
            default=style.get(_w("type")) == "paragraph" and style.get(_w("default")) in ("1", "true", "on"),
        )
    return styles


def _load_numbering(document: object) -> dict[str, _NumberList]:
    """numId → numbered list. Used to tell bullets from numbered lists and to build number strings such as "제%1조".

    A number definition that points at a numbering style (w:numStyleLink) has empty level definitions. It is read as the number
    definition that defines that style (w:styleLink) instead — left as it was, it became bullets
    (Pinned by: test_office.py::TestDocxNumbering::test_definition_linked_to_a_numbering_style_uses_that_style_levels).
    The start-number override of a w:num (w:lvlOverride/w:startOverride) is kept in `_NumberList.restarts`
    (Pinned by: test_office.py::TestDocxObjects::test_numbering_uses_start_and_level_text).
    When a w:lvlOverride holds a whole level definition (w:lvl), the look of that level (number text, format) changes for that
    list only. With only startOverride read, '제3조' came out as '3.'. The start number of the definition (w:start) does not restart
    the count — only startOverride does. Restarting on a definition alone turned 'B', 'C', '4' of a file saved by Word (Tika
    testWORD_override_list_numbering.docx 'Test 4') into 'A', 'B', '3'
    (Pinned by: test_office.py::TestDocxNumbering::test_level_override_with_its_own_definition_replaces_the_level,
    test_level_override_restarts_only_with_a_start_override).
    """
    try:
        numbering = document.part.numbering_part.element
    except (KeyError, NotImplementedError):
        return {}
    except AttributeError:
        numbering = _raw_part_root(document.part, "numbering")
        if numbering is None:
            return {}

    abstract_levels: dict[str, dict[int, _LevelDefinition]] = {}
    abstract_indents: dict[str, dict[int, dict[str, int]]] = {}
    style_levels: dict[str, dict[str, int]] = {}
    style_definitions: dict[str, str] = {}  # numbering style name → the abstractNumId that defines that style
    style_links: dict[str, str] = {}  # abstractNumId → the numbering style name it points at
    for abstract in numbering.iterchildren(_w("abstractNum")):
        abstract_id = abstract.get(_w("abstractNumId"), "")
        levels, linked_styles, indents = {}, {}, {}
        for level in abstract.iterchildren(_w("lvl")):
            index = _int_value(level.get(_w("ilvl")), 0)
            levels[index] = _level_definition(level, index)
            indents[index] = _indent_values(level.find(f"{_w('pPr')}/{_w('ind')}"))
            style = level.find(_w("pStyle"))
            if style is not None:
                linked_styles[style.get(_w("val"), "")] = index
        abstract_levels[abstract_id], style_levels[abstract_id], abstract_indents[abstract_id] = levels, linked_styles, indents
        definition, link = abstract.find(_w("styleLink")), abstract.find(_w("numStyleLink"))
        if definition is not None:
            style_definitions[definition.get(_w("val"), "")] = abstract_id
        if link is not None:
            style_links[abstract_id] = link.get(_w("val"), "")

    lists = {}
    for num in numbering.iterchildren(_w("num")):
        abstract_ref = num.find(_w("abstractNumId"))
        if abstract_ref is None:
            continue
        abstract_id = abstract_ref.get(_w("val"), "")
        abstract_id = style_definitions.get(style_links.get(abstract_id, ""), abstract_id)
        levels, restarts, indents = abstract_levels.get(abstract_id, {}), {}, abstract_indents.get(abstract_id, {})
        for override in num.iterchildren(_w("lvlOverride")):
            index = _int_value(override.get(_w("ilvl")), -1)
            if not 0 <= index <= 8:
                continue
            level, start = override.find(_w("lvl")), override.find(_w("startOverride"))
            if level is not None:
                levels = {**levels, index: _level_definition(level, index)}
                indents = {**indents, index: _indent_values(level.find(f"{_w('pPr')}/{_w('ind')}"))}
            if start is not None:
                restarts[index] = _int_attr(start, 1)
        lists[num.get(_w("numId"), "")] = _NumberList(abstract_id, levels, style_levels.get(abstract_id, {}), restarts, indents)
    return lists


def _level_definition(level: etree._Element, index: int) -> _LevelDefinition:
    """w:lvl → (numFmt, lvlText, start number, legal-style numbering w:isLgl, restart w:lvlRestart). Shared by number definitions
    (w:abstractNum) and the level overrides of a list (w:lvlOverride).

    w:lvlRestart was not read, so the count restarted from 1 whenever a higher level appeared — '제N조' (Article N) running on across
    chapters was '제1조' in every chapter. The value is the deepest higher level that restarts the count (from 1); 0 means never
    restart. A missing or unreadable value is the default (the level right above)
    (Pinned by: test_office_revisions_and_numbering.py::TestDocxLevelRestart)."""
    number_format, text, restart = level.find(_w("numFmt")), level.find(_w("lvlText")), level.find(_w("lvlRestart"))
    restart_after = _int_attr(restart, -1) if restart is not None else -1
    return (number_format.get(_w("val"), "decimal") if number_format is not None else "decimal",
            text.get(_w("val"), "") if text is not None else f"%{index + 1}.",
            _int_attr(level.find(_w("start")), 1),
            _is_on(level.find(_w("isLgl"))),
            restart_after if 0 <= restart_after <= index else None)


def _indent_values(indent: etree._Element | None) -> dict[str, int]:
    """Left indent of a w:ind: "left" (w:left / w:start, twips) and "chars" (w:leftChars / w:startChars, 1/100 character). Only what
    is written is included — each attribute is overridden in the order paragraph → number level → style
    (`_DocxRenderer._paragraph_indent`)."""
    values: dict[str, int] = {}
    if indent is None:
        return values
    for key, names in (("left", ("left", "start")), ("chars", ("leftChars", "startChars"))):
        value = next((indent.get(_w(name)) for name in names if indent.get(_w(name)) is not None), None)
        number = decimal_int(value, None, signed=True)
        if number is not None:
            values[key] = number
    return values


def _number_levels(levels: dict[int, _LevelDefinition]) -> list[NumberLevel]:
    """Number definitions of levels 0–8. A missing level is "%n." in Arabic digits. An unknown number format is written in Arabic
    digits too."""
    defined = (levels.get(index, ("decimal", f"%{index + 1}.", 1, False, None)) for index in range(9))
    return [NumberLevel(text, DOCX_NUMBER_SHAPES.get(number_format, "DIGIT"), start, legal, restart_after)
            for number_format, text, start, legal, restart_after in defined]


def _int_value(value: str | None, default: int) -> int:
    """Integer attribute value. '²' and numbers of more than 4,300 digits give `default` (`_package.decimal_int`. Pinned by:
    test_office.py::TestOneOddValueDoesNotRejectTheDocument::test_docx_odd_grid_span_is_read_as_one_column)."""
    return decimal_int(value, default, signed=True)


def _header_footer_text(document: object, renderer: _DocxRenderer, kind: str) -> str:
    seen: list[str] = []
    # A part referenced by several sections is rendered once. Rendering it per section and dropping the copies by their text kept
    # every copy whose text differed: a numbered paragraph counted on ('1. Company', '2. Company' …) and a picture was registered
    # once per section (Pinned by: test_audit_office.py::TestDocxHeaderSharedBySections).
    rendered: set[object] = set()
    for section in document.sections:
        for attribute in _HEADER_FOOTER_KINDS[kind]:
            story = getattr(section, attribute)
            if story.is_linked_to_previous:
                continue
            try:
                part = story.part
            except KeyError:  # a header detached because its part is missing (`_package.repair_package`). Only that header is skipped
                continue
            # A reference whose relationship points at a part that is not a header (a picture) has no content to read. Asking for
            # it raised AttributeError and rejected the whole document (Pinned by:
            # test_audit_office.py::TestDocxHeaderReferenceToAPartThatIsNotAHeader::test_only_that_header_is_skipped).
            element = getattr(part, "element", None)
            if element is None or part in rendered:
                continue
            rendered.add(part)
            _keep_filled_placeholders(element, renderer.placeholders)
            text = join_blocks(renderer.render_story(part, element))
            if text and text not in seen:
                seen.append(text)
    return join_blocks(seen)


class _NoteLabels:
    """Footnotes/endnotes referenced in the body (kind, id) → label. Each kind is numbered from 1 in order of first reference (same
    as HWP/HWPX). Footnotes and endnotes are numbered separately and would collide, so endnotes get the prefix 'endnote' to tell
    them apart. With the XML id used as is, the first footnote of a document saved by LibreOffice (the separators take ids 0 and 1)
    was `[^2]` (Pinned by: test_office_scaling_and_note_order.py::TestDocxFootnoteNumbersFollowTheOrderOfReference).
    """

    def __init__(self) -> None:
        self.labels: dict[tuple[str, str], str] = {}  # in order of first reference
        self._counts = {"footnote": 0, "endnote": 0}

    def label(self, kind: str, note_id: str) -> str:
        if (kind, note_id) not in self.labels:
            self._counts[kind] += 1
            number = self._counts[kind]
            self.labels[kind, note_id] = str(number) if kind == "footnote" else f"endnote{number}"
        return self.labels[kind, note_id]


def _notes(document: object, renderer: _DocxRenderer) -> str:
    """Content of the footnotes/endnotes referenced in the body (`renderer.notes`), in order of reference. Pictures are found through
    the relationships of the footnote part and registered — they were dropped with no placeholder (HWPX footnotes keep pictures.
    Pinned by: test_office_robustness.py::TestDocxFootnoteBoxesAndPictures::
    test_picture_in_a_footnote_is_registered_from_the_footnote_part)."""
    texts: dict[tuple[str, str], str] = {}
    for kind in ("footnote", "endnote"):
        # An external relationship or a broken part skips only that set of notes. Even the body was rejected as a 'corrupted
        # document' (same rule as charts and SmartArt. Pinned by:
        # test_office.py::TestOneOddValueDoesNotRejectTheDocument::test_docx_broken_or_external_footnotes_part_keeps_the_body).
        part = next((rel.target_part for rel in document.part.rels.values()
                     if not rel.is_external and rel.reltype.endswith(f"/{kind}s")), None)
        root = parse_part(part.blob) if part is not None else None
        if root is None:
            continue
        _keep_filled_placeholders(root, renderer.placeholders)
        with renderer.in_part(part):
            for note in root.iterchildren(_w(kind)):
                # A note the body never references is not written, so it is not rendered either: its picture was registered as a
                # picture of the document with no placeholder anywhere (Pinned by: test_audit_office.py::
                # TestDocxFootnoteNobodyReferences::test_picture_in_an_unreferenced_footnote_is_not_registered).
                if (kind, note.get(_w("id"), "")) not in renderer.notes.labels:
                    continue
                body = " ".join(_note_block(block, renderer.image_marker, renderer.inline)
                                for block in _children(note, _w("p"), _w("tbl")))
                texts[kind, note.get(_w("id"), "")] = body.strip()
    return "\n".join(f"[^{label}]: {texts[ref]}" for ref, label in renderer.notes.labels.items() if texts.get(ref))


_W14_PARA_ID = "{http://schemas.microsoft.com/office/word/2010/wordml}paraId"
_W15 = "http://schemas.microsoft.com/office/word/2012/wordml"


def _comments(document: object) -> str:
    """Comments (word/comments.xml) in part order as `> Comment(author, on "range"): text` lines (`_notation.comment_line`). A reply
    (a comment with a paraIdParent in commentsExtended) is `> Reply(author): text`. Comments were dropped wholesale without a warning
    (Pinned by: test_office_comments.py::TestDocxComments). A broken or external part skips only the comments (same rule as
    footnotes)."""
    related = {rel.reltype.rsplit("/", 1)[-1]: rel.target_part for rel in document.part.rels.values() if not rel.is_external}
    root = parse_part(related["comments"].blob) if "comments" in related else None
    if root is None:
        return ""
    extended = parse_part(related["commentsExtended"].blob) if "commentsExtended" in related else None
    replies = {entry.get(f"{{{_W15}}}paraId") for entry in extended.iter(f"{{{_W15}}}commentEx")
               if entry.get(f"{{{_W15}}}paraIdParent")} if extended is not None else set()
    targets = _comment_targets(document.element.body)

    lines = []

    for comment in root.iterchildren(_w("comment")):
        paragraphs = [paragraph for paragraph in comment.iter(_w("p")) if not _inside_fallback(paragraph)]
        text = " ".join(_paragraph_text(paragraph, None) for paragraph in paragraphs)
        if not text.strip():
            continue
        reply = bool(paragraphs) and paragraphs[-1].get(_W14_PARA_ID) in replies
        target = "" if reply else targets.get(comment.get(_w("id"), ""), "")
        lines.append(comment_line(comment.get(_w("author"), ""), text, target, reply=reply))
    return "\n".join(lines)


def _comment_targets(body: etree._Element) -> dict[str, str]:
    """Comment id → the body range the comment is attached to (the text from commentRangeStart to commentRangeEnd). Per range only
    as much as will be shown (`COMMENT_TARGET_MAX` + 1 characters) is collected, and a range that is full is not looked at again —
    even with many overlapping ranges, each text is not appended to every range. Empty text (`<w:t/>`) does not extend a range and
    is not scanned — with many ranges lacking an end mark, every empty text scanned them all, which took quadratic time (Pinned by:
    test_office_comments.py::TestDocxComments::test_many_open_ranges_over_empty_runs_take_linear_time). Deleted text (w:delText)
    and compatibility copies (mc:Fallback) are left out."""
    targets: dict[str, list[str]] = {}
    open_ranges: dict[str, int] = {}  # range being collected → number of characters collected
    fallback = 0

    for event, element in etree.iterwalk(body, events=("start", "end")):
        tag = element.tag
        if tag == f"{{{_MC}}}Fallback":
            fallback += 1 if event == "start" else -1
        elif event != "start" or fallback:
            continue
        elif tag == _w("commentRangeStart"):
            comment_id = element.get(_w("id"), "")
            targets.setdefault(comment_id, [])
            open_ranges[comment_id] = 0
        elif tag == _w("commentRangeEnd"):
            open_ranges.pop(element.get(_w("id"), ""), None)
        elif tag in (_w("t"), _w("tab"), _w("br"), _w("p")) and open_ranges:
            piece = element.text or "" if tag == _w("t") else " "
            if not piece:
                continue
            for comment_id in list(open_ranges):
                targets[comment_id].append(piece)
                open_ranges[comment_id] += len(piece)
                if open_ranges[comment_id] > COMMENT_TARGET_MAX:
                    del open_ranges[comment_id]
    return {comment_id: "".join(pieces) for comment_id, pieces in targets.items()}


def _note_block(block: etree._Element, image_marker: Callable[[etree._Element], str], inline: _Inline | None = None) -> str:
    """A paragraph inside a footnote (including the paragraphs of text boxes in it) or a table → one line. A table puts ' / '
    between cells and '; ' between rows (`_notation.note_table`, same as HWP/HWPX). Joining all cell paragraphs with spaces produced
    '출처: 환경부 구분 값 A 10' (Pinned by: test_office_symbols_fields_notes.py::TestDocxTableInsideFootnote). The compatibility
    copy of a text box (the VML in mc:Fallback) is skipped as in the body (`_inside_fallback`) — the same text appeared twice
    (Pinned by: test_office_robustness.py::TestDocxFootnoteBoxesAndPictures::test_text_box_in_a_footnote_is_read_once)."""
    def text(container: etree._Element) -> str:
        # Links and emphasis are kept as in the body (sources are usually cited with a link in a footnote)
        return " ".join(" ".join(resolve_inline(_paragraph_text(p, None, image_marker, inline=inline)).split())
                        for p in container.iter(_w("p")) if not _inside_fallback(p))

    if block.tag == _w("tbl"):
        return note_table([text(cell) for cell in _children(row, _w("tc"))] for row in _children(block, _w("tr")))
    return text(block)
