"""DOCX → Markdown.

python-docx is used only to open the package and find parts; the body is read by walking the XML directly. `Paragraph.text` does not
handle text boxes, tracked insertions (w:ins) or footnote references, and `_Row.cells` does not tell horizontal merges from vertical ones.
"""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
import re

from docx.opc.exceptions import PackageNotFoundError
from docx.package import Package
from docx.parts.document import DocumentPart
from lxml import etree

from opendockit.exceptions import CorruptedDocumentError, UnsupportedFormatError
from opendockit.extractors._charts import embedded_table, related_loader, render_chart, render_chartex, render_smartart
from opendockit.extractors._docx_parts import (
    _indent_values,
    _load_numbering,
    _load_styles,
    _number_levels,
    _NumberList,
    _outline_to_heading,
    _Style,
    _styles_root,
)
from opendockit.extractors._docx_runs import (
    _A,
    _CHART,
    _CHARTEX,
    _DIAGRAM,
    _MC,
    _V,
    _children,
    _drawing_alt,
    _drawing_lines,
    _embed_markdown_tables,
    _Inline,
    _inside_fallback,
    _int_attr,
    _is_deleted_paragraph,
    _is_drop_cap,
    _is_on,
    _is_read_box,
    _keep_filled_placeholders,
    _next_is_paragraph,
    _NoteLabels,
    _paragraph_text,
    _placeholder_texts,
    _shows_placeholder,
    _w,
    _word_art_text,
)
from opendockit.extractors._html_mail import is_mhtml
from opendockit.extractors._inline import (
    link_target,
    plain_inline,
    resolve_inline,
)
from opendockit.extractors._lists import HAND_TYPED_BULLET, Numbering, cell_level_mark, list_item_prefix
from opendockit.extractors._notation import (
    COMMENT_TARGET_MAX,
    OLE_PLACEHOLDER,
    TableBudget,
    comment_line,
    note_table,
    omitted_rows_warning,
    unread_ole_warning,
)
from opendockit.extractors._package import core_properties, is_strict_package, open_package, parse_part
from opendockit.extractors.html import extract_html
from opendockit.extractors.text import extract_text
from opendockit.markdown import TableGrid, join_blocks, merged_copy, solid_length
from opendockit.models import ConvertOptions, Extraction, Section, SectionKind, renumber_image_markers


_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


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


# Header/footer kinds: default, first page, even pages
# (Pinned by: test_office.py::TestDocxObjects::test_first_page_and_even_page_headers_are_read).
_HEADER_FOOTER_KINDS = {"header": ("header", "first_page_header", "even_page_header"),
                        "footer": ("footer", "first_page_footer", "even_page_footer")}


_HTML_CHUNK_TYPES = {"text/html", "application/xhtml+xml"}


_MHT_CHUNK_TYPES = {"message/rfc822", "multipart/related", "application/x-mimearchive"}
_TEXT_CHUNK_TYPES = {"text/plain"}


# Character styles whose runs are emphasized: the built-in styles Word names Strong and Emphasis (the name in styles.xml is the
# English one in every language edition) → (bold, italic). Other styles are not followed (`_run_emphasis`).
_EMPHASIS_STYLE_NAMES = {"strong": (True, False), "emphasis": (False, True)}


# Blocks read in order in the body, cells and text boxes: paragraph, table, imported content
_BLOCK_TAGS = (_w("p"), _w("tbl"), _w("altChunk"))


@dataclass(slots=True)
class _ListItem:
    """Preceding item remembered so that the levels of consecutive list items can be aligned by indent (`_DocxRenderer._list_depth`)."""

    num_id: str
    indent: int | None  # left indent on screen (twips). None if it is written nowhere
    depth: int  # the level that was output
    offset: int  # output level − level counted from ilvl. The next item of the same list inherits it


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
        # Footnote / endnote parts (kind → (part, root)) and the references seen in the body with their labels. Only notes with
        # something to write take a label (`_written_notes`)
        self.note_parts = _note_parts(document, self.placeholders) if options.include_footnotes else {}
        self.notes = _NoteLabels(self._written_notes())
        # Tables being rendered. A table inside a cell is handed to the table at the top of the stack (markdown.TableGrid.embed)
        self._grids: list[TableGrid] = []
        # Whether a top-level table being read is a body table, which checks the character limit cell by cell (`_render_table`)
        self._in_body = True
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
        in_body, self._in_body = self._in_body, False
        try:
            with self.in_part(part), self._own_lists():
                return self.render_container(container)
        finally:
            self._in_body = in_body

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
        # is not counted as cell text of the outer table (it undercounts). A header or footer table is not checked: headers are read
        # after the body but written before it, so the body's characters stopped a header table after its first row and the text
        # returned was not the front of the full conversion (Pinned by: test_office_table_char_limit.py::
        # TestCharacterLimitInsideOneTable::test_header_table_is_not_cut_by_the_characters_of_the_body).
        budget = TableBudget(self._extraction, self._options) if not self._grids and self._in_body else None
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
        blob = self._image_blob(element)
        if not blob:
            return ""
        return f" {self._extraction.add_image(blob, alt=_drawing_alt(element))} "

    def _image_blob(self, element: etree._Element) -> bytes | None:
        """Bytes of the picture a:blip (r:embed) / v:imagedata (r:id) points at in the part being read. None when pictures are not
        extracted, the picture is only linked or its relationship is broken."""
        if not self._options.extract_images:
            return None
        rel_id = element.get(f"{{{_R}}}embed") or element.get(f"{{{_R}}}id")
        part = self._part.related_parts.get(rel_id) if rel_id else None
        return getattr(part, "blob", None) or None

    def _written_notes(self) -> set[tuple[str, str]]:
        """The notes (kind, id) that will have content below the body (`_NoteLabels`). Decided with the same walk that writes the
        notes (`_paragraph_text` over the paragraphs `_note_block` reads), with a probe in place of the picture registration: an
        OLE object (its preview or placeholder) and, when pictures are extracted, a picture that can be read count as content.
        Deleted, moved-away and hidden content, compatibility copies and objects that are not OLE objects (an ActiveX control)
        therefore count exactly when they are written — searched for separately, they left a mark with no definition (Pinned by:
        test_office_symbols_fields_notes.py::TestDocxNoteReferenceWithoutContent,
        test_office_robustness.py::TestDocxNoteWithOnlyUnwrittenContent)."""
        def probe(element: etree._Element) -> str:
            if element.tag == _w("object"):
                return OLE_PLACEHOLDER
            return "picture" if self._image_blob(element) else ""

        written = set()
        for kind, (part, root) in self.note_parts.items():
            with self.in_part(part):
                for note in root.iterchildren(_w(kind)):
                    paragraphs = (paragraph for paragraph in note.iter(_w("p")) if not _inside_fallback(paragraph))
                    if any(_paragraph_text(paragraph, None, probe).strip() for paragraph in paragraphs):
                        written.add((kind, note.get(_w("id"), "")))
        return written

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


def _notes(document: object, renderer: _DocxRenderer) -> str:
    """Content of the footnotes/endnotes referenced in the body (`renderer.notes`), in order of reference. Pictures are found through
    the relationships of the footnote part and registered — they were dropped with no placeholder (HWPX footnotes keep pictures.
    Pinned by: test_office_robustness.py::TestDocxFootnoteBoxesAndPictures::
    test_picture_in_a_footnote_is_registered_from_the_footnote_part)."""
    texts: dict[tuple[str, str], str] = {}
    for kind, (part, root) in renderer.note_parts.items():
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


def _note_parts(document: object, placeholders: dict[str, str]) -> dict[str, tuple[object, etree._Element]]:
    """Footnote and endnote parts: kind → (part, parsed root, with filled placeholders kept). An external relationship or a broken
    part skips only that set of notes. Even the body was rejected as a 'corrupted document' (same rule as charts and SmartArt.
    Pinned by: test_office.py::TestOneOddValueDoesNotRejectTheDocument::test_docx_broken_or_external_footnotes_part_keeps_the_body)."""
    found = {}
    for kind in ("footnote", "endnote"):
        part = next((rel.target_part for rel in document.part.rels.values()
                     if not rel.is_external and rel.reltype.endswith(f"/{kind}s")), None)
        root = parse_part(part.blob) if part is not None else None
        if root is not None:
            _keep_filled_placeholders(root, placeholders)
            found[kind] = (part, root)
    return found


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


# Wrappers whose content the body does not show (`_comment_targets`)
_UNSHOWN_WRAPPERS = frozenset((_w("del"), _w("moveFrom")))


def _comment_targets(body: etree._Element) -> dict[str, str]:
    """Comment id → the body range the comment is attached to (the text from commentRangeStart to commentRangeEnd). Per range only
    as much as will be shown (`COMMENT_TARGET_MAX` + 1 characters) is collected, and a range that is full is not looked at again —
    even with many overlapping ranges, each text is not appended to every range. Empty text (`<w:t/>`) does not extend a range and
    is not scanned — with many ranges lacking an end mark, every empty text scanned them all, which took quadratic time (Pinned by:
    test_office_comments.py::TestDocxComments::test_many_open_ranges_over_empty_runs_take_linear_time). What the body does not show
    is left out, as in `_paragraph_text`: deleted text (w:del), the original position of moved text (w:moveFrom), hidden runs
    (w:vanish) and compatibility copies (mc:Fallback) — the moved-away words were quoted as the commented range (Pinned by:
    test_office_comments.py::TestDocxComments::test_moved_away_and_hidden_text_is_not_part_of_the_range)."""
    targets: dict[str, list[str]] = {}
    open_ranges: dict[str, int] = {}  # range being collected → number of characters collected
    fallback = 0  # depth inside compatibility copies: nothing in there counts, range marks included
    hidden = 0  # depth inside deleted, moved-away and hidden content: its text does not count, its range marks do

    for event, element in etree.iterwalk(body, events=("start", "end")):
        tag = element.tag
        if tag == f"{{{_MC}}}Fallback":
            fallback += 1 if event == "start" else -1
        elif tag in _UNSHOWN_WRAPPERS or (tag == _w("r") and _is_on(element.find(f"{_w('rPr')}/{_w('vanish')}"))):
            hidden += 1 if event == "start" else -1
        elif event != "start" or fallback:
            continue
        elif tag == _w("commentRangeStart"):
            comment_id = element.get(_w("id"), "")
            targets.setdefault(comment_id, [])
            open_ranges[comment_id] = 0
        elif tag == _w("commentRangeEnd"):
            open_ranges.pop(element.get(_w("id"), ""), None)
        elif tag in (_w("t"), _w("tab"), _w("br"), _w("p")) and open_ranges and not hidden:
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
