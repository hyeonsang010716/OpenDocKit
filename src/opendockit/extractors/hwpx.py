"""HWPX (OWPML, KS X 6101) → Markdown.

HWPX is XML inside a ZIP. The body in `Contents/section*.xml` is structured as `hp:p` (paragraph) → `hp:run` → `hp:t` (text), `hp:tbl`
(table) and shapes (text boxes).
Elements are compared **by local name only**: the namespace URI (2011/2016) differs between Hangul versions.
"""

from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from io import BytesIO
from itertools import islice
import re
import unicodedata
import zipfile

from lxml import etree

from opendockit.exceptions import CorruptedDocumentError, EncryptedDocumentError
from opendockit.extractors._charts import embedded_table, package_loader, render_chart
from opendockit.extractors._equation import render_equation
from opendockit.extractors._hancom_symbols import framed_number, hancom_symbols
from opendockit.extractors._hangul import (
    FRAME_MAX_CELLS,
    CommentRanges,
    ParagraphPieces,
    SizeHeadings,
    bottom_of_the_lines,
    click_here_guide,
    command_link,
    decorated_text,
    drop_unfilled_guide,
    emphasis_pieces,
    form_caption,
    has_text,
    header_by_default,
    is_frame,
    link_end,
    link_start,
    preview_has_words,
)
from opendockit.extractors._inline import SUBSCRIPT, SUPERSCRIPT, mark_script, mark_strike
from opendockit.extractors._lists import BulletNesting, Numbering, NumberLevel, format_number
from opendockit.extractors._notation import (
    TableBudget,
    chars_left,
    comment_line,
    format_dutmal,
    note_table,
    omitted_rows_warning,
)
from opendockit.extractors._package import ZIP_MEMBER_ERRORS, decimal_int, parse_part
from opendockit.extractors._xml import thread_parser
from opendockit.markdown import TableGrid, join_blocks, render_table, solid_length
from opendockit.models import ConvertOptions, Extraction, Section, SectionKind


_SECTION_NAME = re.compile(r"^Contents/section(\d+)\.xml$")
# Spine entries (itemref of content.hpf) looked at, the same number as for an EPUB (`_section_order`).
_MAX_SPINE_ITEMS = 5_000
_xml_parser = thread_parser(resolve_entities=False, no_network=True, remove_comments=True)
# Pictures, OLE objects and videos have no text, and a picture's description (shapeComment) is the "그림입니다. 원본 그림의 이름…" text
# that Hangul inserts automatically.
# The caption of an object (hp:caption) is read separately by `HwpxRenderer._caption`.
_SKIP_OBJECTS = {"pic", "ole", "video", "chart", "linesegarray", "secPr", "shapeComment"}
# The marks that stand for a character: a tab, a forced line break, the fixed-width and the non-breaking space. Hangul writes
# them inside hp:t (`_t_text`); other writers of HWPX put them beside it, straight under hp:run (`_render_object`).
_TEXT_MARKS = {"tab": "\t", "lineBreak": "\n", "nbSpace": " ", "fwSpace": " "}
# Sides where the caption comes before the object (hp:caption side)
_CAPTION_BEFORE = {"TOP", "LEFT"}
_CAPTION_AFTER = {"BOTTOM", "RIGHT"}
# Line break marks in the text attribute of text art (hp:textart). Hangul writes control picture symbols (␍␊) instead of CR/LF.
_TEXTART_BREAKS = re.compile("␍␊|␍|␊")
# Auto-number kinds (hp:autoNum numType) that write the number in place in the text — the number in a caption's "표 1" ("Table 1").
# Page numbers (PAGE, TOTAL_PAGE) are not document content,
# and footnote/endnote numbers (FOOTNOTE, ENDNOTE) are replaced by `[^n]`, so they are not written (deliberate. Pinned by:
# test_hwp.py::TestAutoNumber).
_AUTO_NUMBER_KINDS = {"PICTURE", "TABLE", "EQUATION"}
# hh:strikeout shapes that count as strikethrough — the line types of the standard (OWPML LineType2). `NONE` and values outside the
# standard are not strikethrough: in real press releases without strikethrough the body character shape was `3D`. The HWP side
# (`hwp._STRIKEOUT_LINE_SHAPES`) looks at the same line types only (deliberate.
# Pinned by: test_hwp.py::TestStrikethrough).
_STRIKEOUT_SHAPES = frozenset({"SOLID", "DOT", "DASH", "DASH_DOT", "DASH_DOT_DOT", "LONG_DASH", "CIRCLE", "DOUBLE_SLIM",
                               "SLIM_THICK", "THICK_SLIM", "SLIM_THICK_SLIM"})
# Form objects whose displayed value is written. Check boxes use ☒ and ☐, the same as DOCX form fields and HTML. Command buttons (hp:btn)
# are left out like an HTML `<button>`
# (deliberate. Pinned by: test_hwp.py::TestHwpxFormObjects). The structure is from public hwpxlib samples (SimpleButtons, SimpleComboBox,
# SimpleEdit).
_FORM_OBJECTS = {"checkBtn", "radioBtn", "comboBox", "listBox", "edit", "btn"}
_CHECKED, _UNCHECKED = "☒", "☐"
# What reading one archive member can raise when that member is broken: besides bad compression or CRC (BadZipFile, zlib.error,
# EOFError), an unknown compression method (NotImplementedError), the ZIP encryption flag (RuntimeError) and the like. Only that
# member is skipped; catching the first three alone let one such picture, header.xml or section reject the whole document
# (Pinned by: test_hwp_robustness.py::TestHwpxUnreadableMembers). The set is shared with DOCX/PPTX (`_package.ZIP_MEMBER_ERRORS`).
_MEMBER_ERRORS = ZIP_MEMBER_ERRORS
# Placeholder left in the body by objects whose content is not read, and the warning text (the count is filled in).
_UNREAD_OBJECTS = {
    "ole": ("[OLE object]", "The contents of {} OLE object(s) (pasted tables, charts, etc.) could not be converted."),
    "video": ("[Video]", "{} video(s) could not be converted."),
}


@dataclass(slots=True)
class ParagraphHeads:
    """Paragraph head definitions from header.xml: paragraph shape id → (kind, numbering/bullet id, level) and left margin, paragraph
    numbering id → per-level definitions,
    and bullet ids."""

    shapes: dict[str, tuple[str, str, int]] = field(default_factory=dict)
    margins: dict[str, int] = field(default_factory=dict)
    numberings: dict[str, list[NumberLevel]] = field(default_factory=dict)
    bullets: set[str] = field(default_factory=set)
    # Check bullet id → (unchecked character, checked character), and the checked paragraph shape ids (hh:paraPr checked="1")
    check_bullets: dict[str, tuple[str, str]] = field(default_factory=dict)
    checked: set[str] = field(default_factory=set)


# The tag of an element → its name without the namespace, for the tags seen so far. An HWPX document is read by the names of
# its elements, one at a time and millions of times (a hundred or so different tags), and splitting the tag each time was
# the largest single cost of the reader. Tags are names of the format, not text of a document; still, only short ones are
# kept, and only so many, so that a file of made-up element names leaves nothing large behind
# (Pinned by: test_hwp.py::TestLocalNames).
_LOCAL_NAMES: dict[object, str] = {}
_LOCAL_NAMES_MAX = 1024
_LOCAL_NAME_TAG_MAX = 128


def _local(element: etree._Element) -> str:
    """The name of an element without its namespace; "" for a comment or a processing instruction (whose tag is no string)."""
    tag = element.tag
    try:
        return _LOCAL_NAMES[tag]
    except KeyError:
        name = tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""
        if len(_LOCAL_NAMES) < _LOCAL_NAMES_MAX and (not isinstance(tag, str) or len(tag) <= _LOCAL_NAME_TAG_MAX):
            _LOCAL_NAMES[tag] = name
        return name


def _offset_under_the_paragraph(table: etree._Element) -> int | None:
    """How far under the top of its paragraph a table starts, when it is placed from there and takes the whole width (text
    stands above and below it, not beside it); None for any other table, the usual one that is treated as a character first."""
    if table.get("textWrap") != "TOP_AND_BOTTOM":
        return None
    position = _child(table, "pos")
    if (position is None or position.get("treatAsChar") != "0" or position.get("vertRelTo") != "PARA"
            or position.get("vertAlign") != "TOP"):
        return None
    try:
        offset = int(position.get("vertOffset", ""))
    except ValueError:
        return None
    return offset - (1 << 32) if offset >= 1 << 31 else offset  # written unsigned: 4294967234 is 62 above the top


def _bottom_of_the_lines(paragraph: etree._Element) -> int | None:
    """`_hangul.bottom_of_the_lines` of the line segments Hangul stored with a paragraph; None without them."""
    segments = _child(paragraph, "linesegarray")
    if segments is None:
        return None
    try:
        return bottom_of_the_lines([(int(segment.get("vertpos", "")), int(segment.get("vertsize", "")))
                                    for segment in _children(segments, "lineseg")])
    except ValueError:
        return None


def _bottom_where_tables_stand_under(paragraph: etree._Element) -> int | None:
    """The bottom of the lines of a paragraph, when a table that starts at or under it is drawn under its text; None when
    that cannot be said. It cannot when the paragraph holds any other object that is not treated as a character - a picture,
    a shape, a table that starts higher: such an object pushes the first line down, and the stored position of that line is
    then not the top the offsets are measured from. Objects inside an hp:switch are not looked at, so one keeps the order
    of the file too."""
    bottom = _bottom_of_the_lines(paragraph)
    if bottom is None:
        return None
    for run in _children(paragraph, "run"):
        for node in run:
            name = _local(node)
            if name == "t":
                continue
            if name == "switch":
                return None
            position = _child(node, "pos")
            if position is None or position.get("treatAsChar") == "1":
                continue
            offset = _offset_under_the_paragraph(node) if name == "tbl" else None
            if offset is None or offset < bottom:
                return None
    return bottom


def _children(element: etree._Element, name: str) -> Iterator[etree._Element]:
    return (child for child in element if _local(child) == name)


def _child(element: etree._Element, name: str) -> etree._Element | None:
    return next(_children(element, name), None)


def parse_xml(data: bytes) -> etree._Element:
    try:
        return etree.fromstring(data, parser=_xml_parser())
    except etree.XMLSyntaxError as exc:
        raise CorruptedDocumentError("The XML of the HWPX document is corrupted.") from exc


class HwpxRenderer:
    def __init__(self, styles: dict[str, str], options: ConvertOptions, extraction: Extraction,
                 load_binary: Callable[[str], bytes | None], heads: ParagraphHeads | None = None,
                 load_part: Callable[[str], bytes | None] | None = None, scripts: dict[str, str] | None = None,
                 struck: frozenset[str] = frozenset(), sizes: dict[str, int] | None = None,
                 emphasis: dict[str, tuple[bool, bool]] | None = None,
                 boxed_fills: Callable[[], dict[str, bool]] | None = None) -> None:
        self._styles = styles
        # Border fill id → whether it draws all four sides of a cell, read from the header the first time a table is looked
        # at as a frame (`_is_frame`): most documents never ask
        self._load_boxed_fills = boxed_fills
        self._boxed_fills: dict[str, bool] | None = None
        # Character shape id → (bold, italic) for the shapes that have either (`_load_emphasis`; empty without `keep_emphasis`),
        # and that of the run being read
        self._emphasis = emphasis or {}
        self._bold_italic = (False, False)
        # Character shape id → size (hh:charPr height, 1/100 pt), the size of the run being read, the headings told by size
        # (`_hangul.SizeHeadings`; the body blocks go through `size_headings.resolve` once everything is read), and how deep in
        # inner paragraph lists (cells, notes, text boxes, captions, headers) the paragraph being read is
        self._sizes = sizes or {}
        self._size = 0
        self.size_headings = SizeHeadings()
        self._inner = 0
        # Character shape id → super/subscript mark. Looked up by the charPrIDRef of the run (hp:run).
        self._scripts = scripts or {}
        self._script: str | None = None
        # Character shape ids that are struck through, and whether the run being read is struck through
        self._struck_shapes = struck
        self._struck = False
        # Number of objects whose content was not read and only a placeholder was left (hp:ole, hp:video). Reported as warnings after
        # everything is read (`unread_warnings`).
        self._unread: dict[str, int] = {}
        self._heads = heads or ParagraphHeads()
        # Margin levels of bullet paragraphs (`BulletNesting`). The top-level body is [0]; a new one is pushed for each inner paragraph
        # list (cells, footnotes, text boxes, captions).
        self._bullets = [BulletNesting()]
        self._load_part = load_part
        self._charts: dict[str, str] = {}  # chart part name → the `[Chart] title` line later references to it give (`_render_chart`)
        # Number counters: (kind, numbering id). Outline numbers and paragraph numbers are counted separately. The outline numbering id
        # is set by the section properties (secPr).
        self._numberings: dict[tuple[str, str], Numbering] = {}
        # While the branches of an hp:switch are read, innermost last: the counters of each numbering as they were before the branch
        # first advanced it (None: not counted yet), to be put back when the branch is passed over (`_render_switch`)
        self._numbering_undo: list[dict[tuple[str, str], dict[int, int] | None]] = []
        self._outline_numbering = "1"
        self._options = options
        self._extraction = extraction
        self._load_binary = load_binary
        self._note_count = 0
        self._cell_depth = 0
        # The paragraphs of each cell of the layout box being read — a one-column table of the top-level body — and None outside
        # one (`_render_table`, `_hangul.SizeHeadings.box`)
        self._box_cells: list[list[str]] | None = None
        self._note_depth = 0  # 1 or more while footnote/endnote text is rendered. A table inside it becomes one line (`note_table`)
        self._plain_header = False  # while a header written without `include_headers_footers` is rendered (no headings)
        # Tables being rendered (an inner table is handed to the top-level table). None is a place where a table cannot be handed over
        # (a footnote or header inside a cell).
        self._grids: list[TableGrid | None] = []
        self.notes: list[str] = []
        # Header and footer texts in order of first appearance, each once per kind. Looked up in a list, 60,000 different headers took
        # 11 s (Pinned by: test_audit_hangul.py::TestManyDistinctHeaders). Kept apart so headers go before the body and footers after
        # it, as in DOCX and ODT (Pinned by: test_hwp.py::TestHeaderAndFooterPlacement).
        self.headers: dict[str, None] = {}
        self.footers: dict[str, None] = {}
        # Comments (review remarks): (author, text, pieces of the body range the comment is attached to). The range is the text between
        # fieldBegin and the fieldEnd with the same id
        self.memos: list[tuple[str, str, list[str]]] = []
        self._memo_ids: list[str] = []  # the id of each comment above, to take back those of a passed-over hp:switch branch
        self._memo_ranges = CommentRanges()  # the ranges of the comments not yet closed by fieldEnd, by comment id
        self._memo_depth = 0  # 1 or more while comment text is rendered. That text is not the range of another comment
        self._open_links: dict[str, str] = {}  # hyperlink field id not yet closed by fieldEnd → its address (`_open_link`)
        # click-here field id not yet closed → (the pieces it was opened in, where its text starts there, its guide text, how many
        # click-here fields had been opened when it was)
        self._open_clicks: dict[str, tuple[list[str], int, str | None, int]] = {}
        self._clicks_opened = 0
        self.omitted_rows = 0  # rows dropped by the table grid cell limit (`TableGrid.omitted_rows`). Reported as a warning
        # Paragraph being read that holds a table placed from its top → (how far down its lines end, its tables drawn under
        # them, which are written after its text). Empty for nearly every paragraph (`_is_set_under_its_paragraph`)
        self._under: dict[etree._Element, tuple[int | None, list[etree._Element]]] = {}

    def render_paragraphs(self, container: etree._Element) -> list[str]:
        """An inner paragraph list (hp:subList etc.). The top-level body calls `render_paragraph` per paragraph."""
        blocks = []
        self._bullets.append(BulletNesting())
        self._inner += 1
        try:
            for paragraph in _children(container, "p"):
                blocks.extend(self.render_paragraph(paragraph))
        finally:
            self._inner -= 1
            self._bullets.pop()
        return blocks

    def render_paragraph(self, paragraph: etree._Element) -> list[str]:
        self._note_memo_target(" ")  # between paragraphs of a comment range that spans several paragraphs
        # The head goes on the first text piece only, and a paragraph without text keeps its number (`_hangul.ParagraphPieces`).
        pieces = ParagraphPieces(lambda text, plain, heading: self._decorate(paragraph, text, plain, heading))
        blocks, buffer, flush = pieces.blocks, pieces.buffer, pieces.flush

        for run in _children(paragraph, "run"):
            script = self._scripts.get(run.get("charPrIDRef", ""))
            struck = run.get("charPrIDRef", "") in self._struck_shapes
            size = self._sizes.get(run.get("charPrIDRef", ""), 0)
            bold_italic = self._emphasis.get(run.get("charPrIDRef", ""), (False, False))
            for node in run:
                # Reading inner paragraphs (table, text box) changes these by the time it returns, so set them again per node
                self._script, self._struck, self._size, self._bold_italic = script, struck, size, bold_italic
                self._render_object(node, buffer, blocks, flush)
        if self._under:
            # The tables drawn under the lines of this paragraph, after its text (`_is_set_under_its_paragraph`)
            for table in self._under.pop(paragraph, (None, ()))[1]:
                flush()
                blocks.append(self._render_table(table))
        return pieces.finish(lambda: self._paragraph_head(paragraph), in_cell=bool(self._cell_depth))

    def _is_set_under_its_paragraph(self, table: etree._Element) -> bool:
        """Is this table, a child of a run of the paragraph being read, drawn under every line of that paragraph
        (`_hangul.bottom_of_the_lines`)? It is then kept for the end of the paragraph. The lines are read once for a paragraph."""
        offset = _offset_under_the_paragraph(table)
        if offset is None:
            return False
        run = table.getparent()
        paragraph = run.getparent() if run is not None else None
        if paragraph is None or _local(run) != "run" or _local(paragraph) != "p":
            return False  # inside an hp:switch or a group: read where it stands
        kept = self._under.get(paragraph)
        if kept is None:
            kept = self._under[paragraph] = (_bottom_where_tables_stand_under(paragraph), [])
        bottom, tables = kept
        if bottom is None or offset < bottom:
            return False
        tables.append(table)
        return True

    def _render_object(self, node: etree._Element, buffer: list[str], blocks: list[str],
                       flush: Callable[[], None]) -> None:
        name = _local(node)
        if name == "t":
            # A run with a super/subscript character shape. Appended as plain text, 10⁶ became 106 (Pinned by: test_hwp.py::TestSuperscript).
            plain = hancom_symbols(_t_text(node))  # Hancom's own symbols as text (`_hancom_symbols`)
            if not self._note_depth:
                self.size_headings.count(self._size, plain, body=not self._inner and not self._cell_depth)
            text = mark_script(plain, self._script) if self._script else plain
            self._note_memo_target(plain)
            # A run with a strikethrough character shape. Appended as plain text, a deleted value read as a valid one
            # (Pinned by: test_hwp.py::TestStrikethrough).
            buffer.extend(emphasis_pieces(mark_strike(text) if self._struck else text, *self._bold_italic))
        elif name == "tbl":
            if self._is_set_under_its_paragraph(node):
                return  # written once the text of the paragraph is (`render_paragraph`)
            flush()
            blocks.append(self._render_table(node))
        elif name in _TEXT_MARKS:
            # A mark beside hp:t, as writers other than Hangul leave it: read as a shape it left nothing, and the two lines it
            # stood between became one (Pinned by: test_hwp.py::TestHwpxMarksBesideTheText).
            self._note_memo_target(_TEXT_MARKS[name])
            buffer.append(_TEXT_MARKS[name])
        elif name == "ctrl":
            self._render_control(node, buffer)
        elif name == "equation":
            script = _child(node, "script")
            text = render_equation(script.text or "") if script is not None else ""
            buffer.append(self._with_caption(node, text))
        elif name == "pic":
            self._with_caption_blocks(node, self._picture_marker(node), buffer, blocks, flush)
        elif name in _UNREAD_OBJECTS:
            # OLE object (pasted Excel sheet or chart) and video: the content is not read and only a placeholder is left. They used to
            # vanish without a trace, so there was no telling what was missing
            # (Pinned by: test_hwp.py::TestEmbeddedObjects).
            self._unread[name] = self._unread.get(name, 0) + 1
            self._with_caption_blocks(node, f" {_UNREAD_OBJECTS[name][0]} ", buffer, blocks, flush)
        elif name == "chart":
            flush()
            caption, before = self._caption(node)
            chart = self._render_chart(node)
            blocks.extend(block for block in ((caption, chart) if before else (chart, caption)) if block)
        elif name == "textart":
            # Text art: the characters are in the text attribute, not the element text
            # (Pinned by: test_hwp.py::TestHwpxObjects::test_text_art_is_kept). Its caption is a block beside it, as for a chart
            # (Pinned by: test_hwp.py::TestHwpxObjects::test_text_art_caption_is_kept).
            flush()
            caption, before = self._caption(node)
            art = _TEXTART_BREAKS.sub("\n", node.get("text", "")).strip()
            blocks.extend(block for block in ((caption, art) if before else (art, caption)) if block)
        elif name == "dutmal":
            # Ruby text (dutmal): the main text (hp:mainText) is also inside this element, not in hp:t, so even the main text was dropped
            # (Pinned by: test_hwp.py::TestDutmal).
            main, annotation = (_child(node, part) for part in ("mainText", "subText"))
            buffer.append(format_dutmal(*("".join(part.itertext()).strip() if part is not None else ""
                                          for part in (main, annotation))))
        elif name == "compose":
            # Composed characters: the overlapped characters are in the composeText attribute and were dropped
            # (Pinned by: test_hwp.py::TestComposedCharacters).
            # A number in a frame is private-use pieces of its digits there too, as in HWP (`hwp._composed_text`)
            text = node.get("composeText", "")
            number = framed_number(text.strip())
            buffer.append(number if number is not None else hancom_symbols(text))
        elif name in _FORM_OBJECTS:
            # Form objects: the value is in attributes and children and was dropped (Pinned by: test_hwp.py::TestHwpxFormObjects).
            value = _form_value(node)
            buffer.append(f" {value} " if value else "")
        elif name == "secPr":
            self._outline_numbering = node.get("outlineShapeIDRef", self._outline_numbering)
        elif name == "switch":
            self._render_switch(node, buffer, blocks, flush)
        elif name == "container":
            # Group object: handle the pictures and text boxes inside in order.
            for child in node:
                self._render_object(child, buffer, blocks, flush)
        elif name not in _SKIP_OBJECTS:
            # Shapes such as text boxes: the picture fill first, then the inner paragraph lists (subList) as separate blocks after it.
            buffer.append(self._picture_fill(node))
            sublists = list(_top_sublists(node))
            if sublists:
                flush()
            for sublist in sublists:
                blocks.extend(self.render_paragraphs(sublist))

    def _render_switch(self, switch: etree._Element, buffer: list[str], blocks: list[str], flush: Callable[[], None]) -> None:
        """Compatibility switch: read the newer object (hp:case, e.g. an OOXML chart) first, and if no text comes out read the
        fallback object (hp:default). A branch that is passed over for the next one leaves no text behind — an empty block, such as
        an empty table, is not text — and a comment it opened is taken back only when a later branch holds that comment too (the
        same id; it was registered twice). A comment only the passed-over branch opens stays, and stays open: its range goes on
        through the fallback text to its hp:fieldEnd after the switch — taken back with the rest, the remark was lost
        (Pinned by: test_hwp.py::TestHwpxObjects::test_switch_branch_without_text_leaves_nothing_behind,
        test_comment_opened_only_in_a_passed_over_switch_branch_is_kept).

        The notes of a passed-over branch (a note needs a reference in the text, so these are notes inside its comments) are
        taken back with their numbers when every comment of the branch is. So are the paragraph numbers it took: an empty numbered
        paragraph in a table of both branches was counted twice, and the next item came one number too high (Pinned by:
        test_hwp_heads_and_shapes.py::TestEmptyNumberedParagraph::test_hwpx_in_a_passed_over_switch_branch).
        Known accepted gap: when only some of its comments are repeated later, the notes of the ones taken back stay as definitions
        without a reference.

        A text-box or table in the branch flushes the paragraph text before the switch into a block of its own; that block is not the
        branch's, so `flush` is wrapped to tell the two apart."""
        branches = [branch for branch in (_child(switch, "case"), _child(switch, "default")) if branch is not None]
        for index, branch in enumerate(branches):
            start, own = len(buffer), len(blocks)   # where this branch's pieces and blocks begin
            notes, note_count, memos = len(self.notes), self._note_count, len(self.memos)
            produced = False

            def branch_flush() -> None:
                nonlocal start, own, produced
                produced = produced or has_text(buffer[start:])
                flush()
                start, own = 0, len(blocks)

            self._numbering_undo.append({})
            try:
                for child in branch:
                    self._render_object(child, buffer, blocks, branch_flush)
            finally:
                undo = self._numbering_undo.pop()
            if index == len(branches) - 1 or produced or has_text(buffer[start:]) or any(block.strip() for block in blocks[own:]):
                if self._numbering_undo:   # kept, inside a branch of an outer switch: the outer branch may still be passed over
                    for key, counters in undo.items():
                        self._numbering_undo[-1].setdefault(key, counters)
                return
            for key, counters in undo.items():
                if counters is None:
                    del self._numberings[key]
                else:
                    self._numberings[key].counters = counters
            del buffer[start:], blocks[own:]
            later = {field.get("id", "") for other in branches[index + 1:] for field in other.iter()
                     if _local(field) == "fieldBegin" and field.get("type") == "MEMO"}
            repeated = [at for at in range(memos, len(self.memos)) if self._memo_ids[at] in later]
            if len(repeated) == len(self.memos) - memos:
                del self.notes[notes:]
                self._note_count = note_count
            for at in reversed(repeated):
                del self.memos[at], self._memo_ids[at]

    def unread_warnings(self) -> list[str]:
        """Warnings per kind for objects left as placeholders only (OLE, video)."""
        return [warning.format(self._unread[name]) for name, (_, warning) in _UNREAD_OBJECTS.items() if name in self._unread]

    def _caption(self, node: etree._Element) -> tuple[str, bool]:
        """Caption (hp:caption) text of a picture, equation, OLE object or chart, and whether it goes before the object (top and left
        captions). The caption of a table is read by `_render_table`
        (Pinned by: test_hwp.py::TestHwpxObjects::test_picture_and_equation_captions_are_kept)."""
        caption = _child(node, "caption")
        if caption is None:
            return "", False
        text = " ".join(" ".join(block.split()) for sub in _top_sublists(caption) for block in self.render_paragraphs(sub))
        return text, caption.get("side", "BOTTOM") in _CAPTION_BEFORE

    def _with_caption_blocks(self, node: etree._Element, marker: str, buffer: list[str], blocks: list[str],
                             flush: Callable[[], None]) -> None:
        """A picture or an object that is not read, and its caption: the paragraphs of the caption are blocks of their own,
        above the object for a top or left caption and under it otherwise, as the HWP reader writes them. Set inside the
        paragraph the picture hangs on, the caption ran into the sentence before it ('… 순서였음. 그림 32. EU 국가의 …') and
        the picture came after both (Pinned by: test_hwp_captions.py)."""
        caption = _child(node, "caption")
        captions = [block for sub in _top_sublists(caption) for block in self.render_paragraphs(sub)] if caption is not None else []
        if not captions:
            buffer.append(marker)
        elif caption.get("side", "BOTTOM") in _CAPTION_BEFORE:
            flush()
            blocks.extend(captions)
            buffer.append(marker)
        else:
            buffer.append(marker)
            flush()
            blocks.extend(captions)

    def _with_caption(self, node: etree._Element, content: str) -> str:
        """Attach the caption before or after an object inside a paragraph (an equation)."""
        text, before = self._caption(node)
        if not text:
            return content
        return f" {text} {content} " if before else f" {content} {text} "

    def _render_chart(self, chart: etree._Element) -> str:
        """The chartIDRef of hp:chart is an OOXML chart inside the archive (Chart/chart1.xml). The same renderer as PPTX and DOCX is used
        (Pinned by: test_hwp.py::TestHwpxObjects::test_chart_data_is_kept)."""
        name = chart.get("chartIDRef", "")
        # A chart part is read, parsed and rendered once however many hp:chart point at it, and its values are written at the
        # first of them only; later ones give the `[Chart] title` line (same rule as an ODF embedded object): 100 references
        # in one paragraph to a part of 300,000 empty elements (a 7.8 KB file) took 47 s
        # (deliberate. Pinned by: test_audit_hangul.py::TestHwpxChartReferencedManyTimes).
        if name in self._charts:
            return self._charts[name]
        self._charts[name] = ""
        data = self._load_part(name) if self._load_part is not None else None
        root = parse_part(data) if data else None
        if root is None:
            return ""

        # A chart inside a table cell hands its value table to the outer table (same as a table inside a table. Pinned by:
        # test_hwp_heads_and_shapes.py::TestChartInsideATableCell).
        # A text box inside the chart (c:userShapes) is found through the relationships file of the chart part
        # (Chart/_rels/chart1.xml.rels) (Pinned by:
        # test_office_chart_units_and_ole.py::TestChartUserShapes::test_hwpx_chart_text_box_is_kept).
        outer = self._grids[-1] if self._grids else None
        table = embedded_table(outer) if outer is not None else render_table
        # The value rows stop once their text passes the characters the output can still take (`_charts._value_rows`, as in DOCX
        # and PPTX). A chart in a note, header or footer (a None in `_grids`) is checked against its own characters only, as a
        # table there is not counted while it is read: headers and footers are written before the body but read where they stand
        # in it, so charged with the body's characters the chart stopped short and the cut text was not the front of the full
        # conversion (Pinned by: test_office_table_char_limit.py::TestChartValuesPastTheLimit::test_hwpx_chart_stops_at_the_limit,
        # test_hwpx_header_chart_is_not_cut_by_the_characters_of_the_body).
        own = self._note_depth or any(grid is None for grid in self._grids)
        room = chars_left(Extraction() if own else self._extraction, self._options)
        rendered = render_chart(root, table, package_loader(self._load_part, name), room, self._extraction.warnings)
        self._charts[name] = rendered.split("\n", 1)[0]
        return rendered

    def _paragraph_head(self, paragraph: etree._Element) -> tuple[str, int]:
        """Paragraph head of the paragraph shape (hh:heading) → (number "1." / "가." or bullet "-", level). ("", 0) if the definition is
        missing or of an unknown kind.
        The number counters are advanced here (Pinned by: test_hwp.py::TestHwpxObjects::test_paragraph_numbers_and_bullets_are_kept)."""
        head_type, numbering_id, level = self._heads.shapes.get(paragraph.get("paraPrIDRef", ""), ("NONE", "", 0))
        if head_type == "BULLET":
            return ("-", level) if numbering_id in self._heads.bullets else ("", 0)
        if head_type == "OUTLINE":
            numbering_id = self._outline_numbering
        elif head_type != "NUMBER":
            return "", 0
        if numbering_id not in self._heads.numberings:
            return "", 0
        key = (head_type, numbering_id)
        if self._numbering_undo and key not in self._numbering_undo[-1]:
            self._numbering_undo[-1][key] = dict(self._numberings[key].counters) if key in self._numberings else None
        if key not in self._numberings:
            self._numberings[key] = Numbering(self._heads.numberings[numbering_id])
        return self._numberings[key].advance(level), level

    def _picture_marker(self, picture: etree._Element) -> str:
        """binaryItemIDRef of hc:img → content.hpf manifest → file under BinData/."""
        if not self._options.extract_images:
            return ""
        image = next((node for node in picture.iter() if _local(node) == "img"), None)
        data = self._load_binary(image.get("binaryItemIDRef", "")) if image is not None else None
        if not data:
            return ""
        comment = _child(picture, "shapeComment")
        alt = " ".join((comment.text or "").split()) if comment is not None else None
        return f" {self._extraction.add_image(data, alt=alt)} "

    def _picture_fill(self, shape: etree._Element) -> str:
        """Picture placeholder for a shape filled with a picture (hc:fillBrush/hc:imgBrush/hc:img). Not being a picture object, it was
        dropped without a trace — it is registered like PPTX `_picture_fill`, and a tiled fill (mode TILE…) is a pattern so it is not
        registered (deliberate. Pinned by:
        test_hwp_heads_and_shapes.py::TestShapeFilledWithAPicture)."""
        brush = _child(shape, "fillBrush")
        image_brush = _child(brush, "imgBrush") if brush is not None else None
        image = _child(image_brush, "img") if image_brush is not None else None
        if image is None or not self._options.extract_images or image_brush.get("mode", "").startswith("TILE"):
            return ""
        data = self._load_binary(image.get("binaryItemIDRef", ""))
        return f" {self._extraction.add_image(data)} " if data else ""

    def _render_control(self, control: etree._Element, buffer: list[str]) -> None:
        """Footnotes/endnotes, headers/footers, auto numbers and comments of hp:ctrl. Anything else is not read even if it has inner
        paragraphs. A comment (the subList of hp:fieldBegin
        type="MEMO") goes not in place in the body but to the comment lines at the end of the document (`_open_memo`)."""
        for node in control:
            name = _local(node)
            if name == "fieldBegin" and node.get("type") == "MEMO":
                self._open_memo(node)
            elif name == "fieldBegin" and node.get("type") == "HYPERLINK":
                self._open_link(node, buffer)
            elif name == "fieldBegin" and node.get("type") == "CLICK_HERE":
                # Its guide is dropped at its end only when no other click-here field was opened inside it: checked at every end, 40,000
                # fields opened in one paragraph and then ended joined the text from each start again, 19 s (Pinned by:
                # test_hwp.py::TestClickHereFields::test_fields_opened_inside_each_other_take_linear_time)
                self._clicks_opened += 1
                self._open_clicks[node.get("id", "")] = (buffer, len(buffer), click_here_guide(_field_command(node)), self._clicks_opened)
            elif name == "fieldEnd":
                self._close_memo(node.get("beginIDRef", ""))
                opened = self._open_clicks.pop(node.get("beginIDRef", ""), None)
                if opened is not None and opened[0] is buffer and opened[3] == self._clicks_opened:
                    drop_unfilled_guide(buffer, opened[1], opened[2])
                url = self._open_links.pop(node.get("beginIDRef", ""), None)
                if url is not None:
                    buffer.append(link_end(url))
            elif name in ("footNote", "endNote") and self._options.include_footnotes and not self._note_depth:
                self._render_note(node, buffer)
            # Without `include_headers_footers` a header that holds a word is still kept (`_hangul.header_by_default`)
            elif name == "header" or (name == "footer" and self._options.include_headers_footers):
                pictures, notes, note_count, memos = len(self._extraction.images), len(self.notes), self._note_count, len(self.memos)
                unread = dict(self._unread)
                self._grids.append(None)
                self._plain_header = not self._options.include_headers_footers
                try:
                    with self._own_numbering():
                        text = join_blocks(block for sub in _top_sublists(node) for block in self.render_paragraphs(sub))
                finally:
                    self._grids.pop()
                    self._plain_header = False
                if not self._options.include_headers_footers:
                    # Only the words of the header: the pictures it registered are taken back with their placeholders
                    del self._extraction.images[pictures:]
                    # and so are its OLE objects and videos, which would warn of contents not converted that are not written
                    # (Pinned by: test_hwp_headers.py::TestHeadersByDefault::test_object_of_a_header_not_written_does_not_warn)
                    self._unread = unread
                    text = header_by_default(text)
                    # A header that is not written takes back its notes and comments, which would be left without a reference
                    if not text or text in (self.headers if name == "header" else self.footers):
                        del self.notes[notes:], self.memos[memos:], self._memo_ids[memos:]
                        self._note_count = note_count
                elif text in (self.headers if name == "header" else self.footers):
                    # A header written once has its comments once: repeated, its comment lines came out once per copy
                    # (Pinned by: test_hwp.py::TestHwpMemo::test_comment_of_a_repeated_header_is_written_once)
                    del self.memos[memos:], self._memo_ids[memos:]
                found = self.headers if name == "header" else self.footers
                if text and text not in found:
                    found[text] = None
                    # Every header is written before the body, so it is counted when first seen; footers after the body.
                    if found is self.headers:
                        self._extraction.count(solid_length(text), self._options)
            elif name == "autoNum":
                buffer.append(_auto_number(node))

    def _render_note(self, note: etree._Element, buffer: list[str]) -> None:
        """A footnote or endnote: `[^n]` in place and `[^n]: text` at the end. The number is taken once the text is known, so a
        note without text leaves neither reference nor definition and the notes after it stay consecutive; a note inside a note is
        left out (the caller checks `_note_depth`), as in RTF and ODF — Markdown cannot reference it from a definition, and numbering
        it first gave the outer note's number to both. A note inside a comment is numbered the same way
        (Pinned by: test_hwp.py::TestNotesInsideNotes)."""
        self._grids.append(None)  # footnote text is not cell text — handing its table to the outer cell leaks the placeholder to the note
        self._note_depth += 1
        try:
            with self._own_numbering():
                text = " ".join(" ".join(block.split()) for sub in _top_sublists(note) for block in self.render_paragraphs(sub))
        finally:
            self._note_depth -= 1
            self._grids.pop()
        if not text.strip():
            return
        self._note_count += 1
        buffer.append(f"[^{self._note_count}]")
        self.notes.append(f"[^{self._note_count}]: {text}")

    @contextmanager
    def _own_numbering(self) -> Iterator[None]:
        """Paragraph numbers of a header, footer, note or comment counted on their own: a numbered paragraph there advanced the
        body's counters, so the body's next item skipped a number (Pinned by: test_hwp.py::TestNumberingOutsideTheBody)."""
        saved, self._numberings = self._numberings, {}
        undo, self._numbering_undo = self._numbering_undo, []
        try:
            yield
        finally:
            self._numberings, self._numbering_undo = saved, undo

    def _open_memo(self, field: etree._Element) -> None:
        """Record one comment and start gathering its range. The author is the Author parameter; the text is the subList paragraphs on one line.

        Comments were dropped without a warning, so review remarks could not be seen. Like DOCX and PPTX comments they go at the end of
        the document as `> Comment(author, on "range"): text`
        (`_notation.comment_line`, Pinned by: test_hwp.py::TestMemo). Table and picture placeholders inside a comment are not cell text, so
        they are not handed to the outer table.
        """
        author = next((" ".join((param.text or "").split()) for param in field.iter()
                       if _local(param) == "stringParam" and param.get("name") == "Author"), "")
        self._grids.append(None)
        self._memo_depth += 1
        try:
            with self._own_numbering():
                text = " ".join(" ".join(block.split()) for sub in _top_sublists(field) for block in self.render_paragraphs(sub))
        finally:
            self._memo_depth -= 1
            self._grids.pop()
        self.memos.append((author, text, self._memo_ranges.open(field.get("id", ""))))
        self._memo_ids.append(field.get("id", ""))

    def _open_link(self, field: etree._Element, buffer: list[str]) -> None:
        """A hyperlink field: the text up to the hp:fieldEnd with its id becomes `[text](address)` under `keep_links`, with the
        address from the `Command` parameter (`_hangul.command_link`: http, https and mailto only). The text of a comment is plain,
        as in DOCX (Pinned by: test_hwp.py::TestHyperlinks)."""
        if not self._options.keep_links or self._memo_depth:
            return
        url = command_link(_field_command(field))
        if url is not None:
            self._open_links[field.get("id", "")] = url
            buffer.append(link_start())

    def _note_memo_target(self, text: str) -> None:
        """Add a piece of body text to the ranges of the open comments (`_hangul.CommentRanges`); the text of a comment itself is
        not the range of another."""
        if not self._memo_depth:
            self._memo_ranges.add(text)

    def _close_memo(self, memo_id: str) -> None:
        self._memo_ranges.close(memo_id)

    def memo_lines(self) -> str:
        return "\n".join(comment_line(author, text, "".join(target).strip()) for author, text, target in self.memos if text)

    def _decorate(self, paragraph: etree._Element, text: str, plain: str, heading: Callable[[], str]) -> str:
        """Attach the paragraph head (`_lists.paragraph_head`) and, for an outline style or head, the heading mark to the paragraph's
        first text piece. A top-level body paragraph (not in a cell, note, header, text box or caption) passes its smallest
        character size, to become a heading by size (`_hangul.SizeHeadings`)."""
        mark, level = self._paragraph_head(paragraph)
        style_name = self._styles.get(paragraph.get("styleIDRef", ""), "")
        margin = self._heads.margins.get(paragraph.get("paraPrIDRef", ""), 0)
        outline = self._heads.shapes.get(paragraph.get("paraPrIDRef", ""), ("NONE", "", 0))[0] == "OUTLINE"
        top = not self._inner and not self._cell_depth and not self._note_depth
        boxed = self._in_box()
        return decorated_text(text, mark, level, style_name, margin, self._bullets[-1], in_cell=bool(self._cell_depth),
                              check=self._check_mark(paragraph), plain=plain, outline=outline,
                              size=self._smallest_text_size(paragraph) if top or boxed else 0, size_headings=self.size_headings,
                              heading_text=heading, top=top, in_note=bool(self._note_depth), no_heading=self._plain_header,
                              boxed=boxed)

    def _in_box(self) -> bool:
        """Whether the paragraph being read is one of a cell of a layout box (`_render_table`), not one of a text box or a
        table inside that cell."""
        return self._box_cells is not None and self._cell_depth == 1 and self._inner == 1 and not self._note_depth

    def _smallest_text_size(self, paragraph: etree._Element) -> int:
        """The smallest size among the runs of the paragraph that hold visible text (hp:t); 0 when one of them has no known size.
        Only the first few runs are looked at: a paragraph of more is not a heading anyway (`_hangul.SizeHeadings`)."""
        smallest = 0
        for count, run in enumerate(_children(paragraph, "run")):
            if count >= _SIZE_SCAN_RUNS:
                return 0
            if any(_t_text(node).strip() for node in _children(run, "t")):
                size = self._sizes.get(run.get("charPrIDRef", ""), 0)
                if size <= 0:
                    return 0
                smallest = min(smallest, size) if smallest else size
        return smallest

    def _check_mark(self, paragraph: etree._Element) -> str:
        """The check state character (☑ or ☐) of a check bullet paragraph, plus a trailing space. Empty text if it is not a check bullet
        or the character is in the private use area (it gets removed anyway). Only '- ' was left and the done/not-done state of
        checklists was lost (Pinned by: test_hwp_heads_and_shapes.py::TestHwpxCheckBullets)."""
        shape = paragraph.get("paraPrIDRef", "")
        head_type, bullet_id, _ = self._heads.shapes.get(shape, ("NONE", "", 0))
        chars = self._heads.check_bullets.get(bullet_id) if head_type == "BULLET" else None
        if chars is None:
            return ""
        char = chars[1] if shape in self._heads.checked else chars[0]
        return f"{char} " if char and unicodedata.category(char[0]) != "Co" else ""

    def _render_table(self, table: etree._Element) -> str:
        caption = _child(table, "caption")
        # The caption stands above the table unless the document sets it under or right of it (`side`), and is read where it
        # is written, so that the notes and numbers in it count in the order of the output
        # (Pinned by: test_hwp_captions.py::TestTableCaptions).
        caption_first = caption is None or caption.get("side", "TOP") not in _CAPTION_AFTER

        def read_caption() -> str:
            return join_blocks(block for sub in _top_sublists(caption) for block in self.render_paragraphs(sub)) \
                if caption is not None else ""

        caption_text = read_caption() if caption_first else ""

        grid = TableGrid()
        # The top-level table checks the character limit while reading cells (`_notation.TableBudget`). Once exceeded, the remaining rows
        # are not read.
        budget = None if self._grids or self._cell_depth or self._note_depth else TableBudget(self._extraction, self._options)
        outer = self._grids[-1] if self._grids else None
        # A table of one column in the top-level body is a layout box: its paragraphs are kept as they are read, so that one
        # set as a title can become a heading by its size (`_hangul.SizeHeadings.box`)
        layout = (caption is None and not self._grids and not self._cell_depth and not self._inner
                  and not self._note_depth and not self._plain_header)
        box = layout and table.get("colCnt") == "1"
        # ... and a table of one row sets things beside each other, and is one line of text (`SizeHeadings.row`)
        one_row = layout and not box and table.get("rowCnt") == "1"
        # ... and a table that only frames text, one cell with content to a row, is one column of text: its cells with text are
        # read as the cells of a one-column table (`_hangul.is_frame`, `_place_frame_row`). No paragraph of it is a heading by
        # its size, as none was while it was drawn as a table
        frame = (layout and not box and not one_row
                 and _int(table.get("rowCnt"), 0) * _int(table.get("colCnt"), 0) <= FRAME_MAX_CELLS and self._is_frame(table))
        placed = 0   # the cells of a frame placed so far
        self._grids.append(grid)
        self._cell_depth += 1
        box_cells = self._box_cells = [] if box or one_row else self._box_cells
        try:
            for row_index, row in enumerate(_children(table, "tr")):
                if frame:
                    placed, over = self._place_frame_row(grid, row, placed, budget)
                    if over:
                        break
                    continue
                grid.reach(row_index, 0)  # a row (hp:tr) whose cells are all covered by vertical spans from above is in the table (TableGrid.reach)
                if self._place_row(grid, row, row_index, budget):
                    break
        finally:
            self._cell_depth -= 1
            self._grids.pop()
            if box or one_row:
                self._box_cells = None

        # A table inside a footnote (and tables inside it) is flattened to one line (`note_table`: cells ' / ', rows '; '). Rendering a
        # Markdown table gave '| 연도 | 값 | | --- |' when the footnote was joined into one line
        # (Pinned by: test_hwp.py::TestTableInsideFootnote). A table inside a table is drawn in place by the outer table
        # (markdown.TableGrid.embed). Where it cannot be handed over, as in a header inside a cell, it is flattened to 'cell / cell'
        # (Pinned by: test_hwp.py::TestNestedTables).
        # The caption stays with the table wherever it is. Dropping it loses '표 2' ("Table 2"), and a picture in the caption was only
        # registered with no placeholder, costing a description for nothing
        # (Pinned by: test_hwp.py::TestNestedTables::test_caption_of_an_inner_table_is_kept_with_it).
        if not caption_first:
            caption_text = read_caption()

        def captioned(body: str) -> str:
            return join_blocks([caption_text, body] if caption_first else [body, caption_text])

        if self._note_depth:
            return captioned(note_table(grid.rows(), self._options.max_chars))
        if outer is not None:
            return captioned(outer.embed(grid))
        if self._cell_depth:
            return captioned(grid.flatten(self._options.max_chars))
        # A table without a budget (in a header or footer) is drawn with the whole limit as room: what it writes past that is
        # over the limit whatever comes before it (Pinned by:
        # test_office_table_char_limit.py::TestMergedCopiesPastTheLimit::test_hangul_header_table_stops_at_the_limit)
        rendered = grid.render(room=budget.room if budget is not None else self._options.max_chars)
        self.omitted_rows += grid.omitted_rows()
        if box:
            return self.size_headings.box(rendered, box_cells, grid)
        if one_row:
            return self.size_headings.row(rendered, box_cells, grid)
        return captioned(rendered)

    def _is_frame(self, table: etree._Element) -> bool:
        """Whether a table of the top-level body only frames text (`_hangul.is_frame`): its cells by their address, an empty
        cell being one that holds nothing but empty paragraphs (`_holds_content`)."""
        # The first row of nearly every table holds two texts, and a table that starts so is none: told from its first two
        # cells alone, before any address is read, since the question is put to every table of the top level
        first = _child(table, "tr")
        if first is not None:
            head = list(islice(_children(first, "tc"), 2))
            if len(head) == 2 and _holds_content(head[0]) and _holds_content(head[1]):
                return False

        def cells() -> Iterator[tuple[int, int, bool, etree._Element] | None]:
            for row in _children(table, "tr"):
                for cell in _children(row, "tc"):
                    address, span = _child(cell, "cellAddr"), _child(cell, "cellSpan")
                    row_at = _int(address.get("rowAddr"), -1) if address is not None else -1
                    content = _holds_content(cell)
                    # a cell without an address, or with a table inside it, settles the answer
                    yield None if row_at < 0 or (content and next(cell.iter("{*}tbl"), None) is not None) else (
                        row_at, _int(span.get("rowSpan") if span is not None else None, 1), content, cell)

        def boxed(cell: etree._Element) -> bool | None:
            if self._boxed_fills is None:
                self._boxed_fills = self._load_boxed_fills() if self._load_boxed_fills is not None else {}
            return self._boxed_fills.get(cell.get("borderFillIDRef", ""))

        return is_frame(cells(), boxed)

    def _place_frame_row(self, grid: TableGrid, row: etree._Element, placed: int,
                         budget: TableBudget | None) -> tuple[int, bool]:
        """Place the cells of a row of a frame (`_is_frame`): each cell that gives text is the next cell of one column, in
        the order of the document, after the `placed` before it, and a cell that gives none is left out. A cell taken for
        empty that gives text after all (the number of an empty numbered paragraph) is a cell of the column like the others,
        so nothing read is dropped. Returns the cells placed so far, and whether `budget` says the limit is exceeded."""
        for cell in _children(row, "tc"):
            text = "\n".join(block for sub in _children(cell, "subList") for block in self.render_paragraphs(sub))
            if not text.strip():
                continue
            grid.place(placed, 0, text)
            placed += 1
            if budget is not None and budget.add(placed - 1, 0, text):
                return placed, True
        return placed, False

    def _place_row(self, grid: TableGrid, row: etree._Element, row_index: int, budget: TableBudget | None) -> bool:
        """Place the cells of a table row (hp:tr) on the grid. True if `budget` says the limit is exceeded — the remaining cells and rows are not
        read."""
        for cell in _children(row, "tc"):
            # the parts of the cell in one pass over its children: looked up one name at a time, they were gone over three
            # times for every cell of every table
            sublists, address, span = [], None, None
            for part in cell:
                name = _local(part)
                if name == "subList":
                    sublists.append(part)
                elif name == "cellAddr":
                    address = part if address is None else address
                elif name == "cellSpan":
                    span = part if span is None else span
            blocks = [block for sub in sublists for block in self.render_paragraphs(sub)]
            if self._box_cells is not None and self._cell_depth == 1 and self._inner == 0:
                self._box_cells.append(blocks)
            text = "\n".join(blocks)
            row_span = _int(span.get("rowSpan") if span is not None else None, 1)
            col_span = _int(span.get("colSpan") if span is not None else None, 1)
            if address is None:
                grid.place_next(row_index, text, row_span=row_span, col_span=col_span)
                if budget is not None:
                    budget.forget()
                continue

            row_at, col_at = _int(address.get("rowAddr"), row_index), _int(address.get("colAddr"), 0)
            row_span, col_span = grid.place_or_next(row_at, col_at, text, row_span=row_span, col_span=col_span)
            if budget is not None and budget.add(row_at, col_at, text, row_span=row_span, col_span=col_span):
                return True
        return False


def extract_hwpx(data: bytes, options: ConvertOptions) -> Extraction:
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise CorruptedDocumentError("The HWPX archive structure is corrupted.") from exc

    with archive:
        # The member names in archive order, looked up in constant time: searched in the name list, each picture and chart a
        # section refers to took time in proportion to the members, 40,000 of each in a 3.7 MB file 5 s
        # (Pinned by: test_hwp_robustness.py::TestManyMembersAndReferences).
        names: Collection[str] = dict.fromkeys(archive.namelist())
        if b"encryption-data" in (_read_member(archive, names, "META-INF/manifest.xml") or b""):
            raise EncryptedDocumentError("The HWP document is password-protected. Please remove the password and upload it again.")

        # Parts outside the body (styles header.xml, order content.hpf, manifest, preview) may be broken and the body is still read. A
        # single XML error or compression CRC error used to reject a whole document whose body was fine
        # (Pinned by: test_hwp_robustness.py::TestHwpxPartsOutsideTheBody). Without styles, headings, numbering, super/subscripts and
        # strikethrough are lost, so a warning is given. If the manifest cannot be read, the document is read without knowing whether
        # it is encrypted — an encrypted body is not XML
        # and gets rejected as corrupted anyway.
        header = _parse_member(archive, names, "Contents/header.xml")
        styles = _load_styles(header)
        package = _content_package(archive, names)
        binaries = _binary_items(package, names)
        loaded: dict[str, bytes | None] = {}

        def load_binary(item_id: str) -> bytes | None:
            """A picture file is decompressed only once. Decompressing again per reference, a 43KB document that referenced one 40MB
            picture 80 times used 3.4GB.
            The same bytes object is passed on, so the converter also decodes it once. A picture whose compression is broken skips just
            that picture (same rule as HWP)
            (Pinned by: test_image.py::TestResourceBounds::test_hwpx_picture_referenced_many_times_is_read_once)."""
            href = binaries.get(item_id)
            if href not in loaded:
                try:
                    loaded[href] = archive.read(href) if href in names else None
                except _MEMBER_ERRORS:
                    loaded[href] = None
            return loaded[href]

        def load_part(path: str) -> bytes | None:
            """A part XML inside the archive, such as a chart. If broken, just that object is skipped."""
            try:
                return archive.read(path) if path in names else None
            except _MEMBER_ERRORS:
                return None

        extraction = Extraction()
        if header is None and "Contents/header.xml" in names:
            extraction.warnings.append("The document style information (header.xml) could not be read, so the document was converted "
                                       "without headings, numbering and character formatting.")
        renderer = HwpxRenderer(styles, options, extraction, load_binary, _load_paragraph_heads(header), load_part,
                                _load_scripts(header), _load_struck_shapes(header), _load_char_sizes(header),
                                _load_emphasis(header) if options.keep_emphasis else None,
                                lambda: _load_boxed_fills(header))
        blocks = []
        tracked = _tracks_deletions(header)
        sections = _section_order(package, names, extraction.warnings)
        broken: list[Exception] = []
        for number, name in enumerate(sections, 1):
            # If the XML or compression (CRC) of one section is broken, only that section is skipped with a warning. The whole document
            # used to be rejected even when the other sections were fine (same as the section isolation of HWP).
            # If every section is broken it is a corrupted document as before (Pinned by: test_hwp_robustness.py::TestBrokenHwpxSection).
            try:
                section = parse_xml(archive.read(name))
            except (CorruptedDocumentError, *_MEMBER_ERRORS) as exc:
                broken.append(exc)
                extraction.warnings.append(f"Section {number}: corrupted and could not be read.")
                continue
            if tracked:
                _drop_tracked_deletions(section)

            # Read per top-level paragraph and count characters. Once the limit is exceeded, the remaining paragraphs and sections are not read.
            # The notes the paragraph added and kept count too (a branch of a switch that gives nothing takes its notes back while
            # the paragraph is rendered): they were not counted while reading, so a document of footnotes was read to its end
            # (Pinned by: test_hwp_robustness.py::TestNotesCountTowardTheLimit).
            for paragraph in _children(section, "p"):
                noted = len(renderer.notes)
                rendered = renderer.render_paragraph(paragraph)
                blocks.extend(rendered)
                # Without a limit nothing is counted: the count is only ever compared with the limit, and measuring every note made
                # a document of 60,000 notes 30% slower (Pinned by: test_hwp_robustness.py::TestNotesCountTowardTheLimit::
                # test_notes_are_not_measured_without_a_limit)
                if options.max_chars is None:
                    continue
                added = sum(solid_length(block) for block in rendered) + sum(solid_length(note) for note in renderer.notes[noted:])
                if extraction.count(added, options):
                    break
            if extraction.exceeded:
                break
        if sections and len(broken) == len(sections):
            raise broken[0]
        extraction.metadata.update(_load_metadata(package))
        preview = _read_member(archive, names, "Preview/PrvText.txt") or b""

    # The preview replaces the body only when parsing the body finished and there is no text. Hangul writes table cells in the preview
    # as `<cell text>`, so if no text remains after removing the cell marks
    # it is a document without text (empty table, shapes only). Used as is, `<>` noise went out as the body
    # (Pinned by: test_hwp.py::TestPreviewFallback).
    extraction.count(sum(solid_length(text) for text in renderer.footers), options)
    blocks = renderer.size_headings.resolve(blocks)
    body = join_blocks([*renderer.headers, *blocks, *renderer.footers, "\n".join(renderer.notes), renderer.memo_lines()])
    preview_text = preview.decode("utf-8", errors="replace")
    if not body.strip() and preview_has_words(preview_text):
        body = preview_text
        extraction.warnings.append("No text was found in the body, so the preview text saved by Hancom Office was used instead "
                                   "(it may contain only the beginning).")
    extraction.warnings.extend(renderer.unread_warnings())
    if renderer.omitted_rows:
        extraction.warnings.append(omitted_rows_warning(renderer.omitted_rows))
    extraction.sections.append(Section(SectionKind.DOCUMENT, 1, body))
    return extraction


def _auto_number(node: etree._Element) -> str:
    """hp:autoNum → the number in a caption's "표 1" ("Table 1"). The number value (num) is in the file, so it is not counted. The shape
    and the prefix/suffix characters are in hp:autoNumFormat
    (Pinned by: test_hwp.py::TestAutoNumber)."""
    number = decimal_int(node.get("num"), None)
    if node.get("numType") not in _AUTO_NUMBER_KINDS or number is None:
        return ""
    shape = _child(node, "autoNumFormat")
    look = shape.attrib if shape is not None else {}
    text = format_number(number, look.get("type", "DIGIT"))
    return f"{look.get('prefixChar', '')}{text}{look.get('suffixChar', '')}"


def _form_value(node: etree._Element) -> str:
    """The value a form object showed on screen: check boxes and radio buttons are ☒/☐ + label (caption), list boxes the selected value
    (selectedValue),
    edit boxes hp:text. Command buttons and empty values give an empty string."""
    name = _local(node)
    if name in ("checkBtn", "radioBtn"):
        mark = _CHECKED if node.get("value") == "CHECKED" else _UNCHECKED
        return f"{mark} {form_caption(node.get('caption', ''))}".strip()
    if name in ("comboBox", "listBox"):
        return node.get("selectedValue", "").strip()
    if name == "edit":
        text = _child(node, "text")
        return (text.text or "").strip() if text is not None else ""
    return ""


# What an empty cell is made of: its paragraph list, paragraphs, runs and texts, their line layout and its own address and size
_EMPTY_CELL_PARTS = frozenset({"subList", "p", "run", "t", "linesegarray", "lineseg", "cellAddr", "cellSpan", "cellSz", "cellMargin"})


def _holds_content(cell: etree._Element) -> bool:
    """Whether a table cell holds anything besides empty paragraphs: text that is not whitespace, a mark inside a text, or any
    other element (an object, a control, a table). Told from the XML before the cell is read, and on the side of content: what
    is not known to be empty is not (`_is_frame`)."""
    for text in cell.iter("{*}t"):   # the common answer first: the cell's first text says it
        if len(text) or (text.text or "").strip():
            return True
    for node in cell.iterdescendants():
        name = _local(node)
        if name and name not in _EMPTY_CELL_PARTS:   # a name: not a comment or a processing instruction
            return True
    return False


def _tracks_deletions(header: etree._Element | None) -> bool:
    """Whether the document lists a deletion among its tracked changes (hh:refList/hh:trackChanges, an hh:trackChange of
    type Delete for each). Asked once of the header, so that a document without change tracking pays nothing for the
    deletions of one with it: of 2,180 documents, the five that list one are the five whose sections hold the marks."""
    references = _child(header, "refList") if header is not None else None
    changes = _child(references, "trackChanges") if references is not None else None
    return changes is not None and any(change.get("type") == "Delete" for change in changes)


# What stands in a run beside its text and is kept inside a deleted range: a control — the two ends of a field have to
# stay paired, a comment is its reviewer's and not the deleted text's, a header or the columns belong to the section —
# and the properties of a section. A control that holds a note is not kept: the note is of the deleted text.
_KEPT_IN_A_DELETION = frozenset({"ctrl", "secPr"})
_NOTES = frozenset({"footNote", "endNote"})


def _drop_tracked_deletions(section: etree._Element) -> None:
    """Take out of the paragraphs of a section what was deleted under change tracking across the texts of a paragraph.

    Hangul writes a deletion as hp:deleteBegin and hp:deleteEnd with one Id, inside hp:t. When what was deleted is set in
    more than one character shape, or holds a table or a picture, the two marks are in different hp:t of one paragraph, with
    the runs and the objects between them — and the end mark is written once more at the start of the run after it. So a
    deletion runs from its begin to the first end of its Id among the texts of the same paragraph, and everything between
    is left out: the text, the marks of a tab or a line break, and the tables, pictures and other objects of the runs. A
    begin whose end is not in its paragraph deletes nothing, so a document that is cut or damaged loses no text. Run for
    the documents whose header lists a deletion (`_tracks_deletions`); in another only the pairs inside one hp:t are
    left out, as before.

    Only the pairs inside one hp:t were left out before (`_t_text`): four documents of 2,192 held 32 pairs that are not,
    all of them inside one paragraph, and wrote what they deleted as text - two names and titles in front of the one that
    replaced them, a programme and the tables of the careers of people taken out of a proposal
    (Pinned by: test_hwp.py::TestHwpxTrackChanges)."""
    for paragraph in list(section.iter("{*}p")):
        _drop_deletions_of(paragraph)


def _drop_deletions_of(paragraph: etree._Element) -> None:
    # The place of the last end of each Id among the marks of the paragraph's texts: a begin after it, or without one,
    # opens nothing. Places are counted over those marks alone.
    last_end: dict[str | None, int] = {}
    place = 0
    begins = False
    texts = [node for run in paragraph if _local(run) == "run" for node in run if _local(node) == "t" and len(node)]
    for text in texts:
        for mark in text:
            name = _local(mark)
            if name == "deleteEnd":
                last_end[mark.get("Id")] = place
            elif name == "deleteBegin":
                begins = True
            place += 1
    if not begins or not last_end:
        return

    open_ids: set[str | None] = set()
    place = 0
    for run in paragraph:
        if _local(run) != "run":
            continue
        for node in list(run):
            name = _local(node)
            if name != "t":
                if open_ids and (name not in _KEPT_IN_A_DELETION or any(_local(child) in _NOTES for child in node)):
                    run.remove(node)
                continue
            if open_ids:
                node.text = None
            for mark in list(node):
                name = _local(mark)
                if name == "deleteBegin":
                    if last_end.get(mark.get("Id"), -1) > place:
                        open_ids.add(mark.get("Id"))
                elif name == "deleteEnd":
                    open_ids.discard(mark.get("Id"))
                elif open_ids:
                    node.remove(mark)  # a tab or a line break of the deleted text; what follows it goes with it
                    place += 1
                    continue
                place += 1
                if open_ids:
                    mark.tail = None


def _t_text(node: etree._Element) -> str:
    """Text of hp:t. A range deleted under change tracking (hp:deleteBegin to the hp:deleteEnd with the same Id) is left out — left as
    plain text, both the deleted and the inserted value read as valid values
    (same direction as DOCX w:del). A pair that is not inside the same hp:t is taken out of its paragraph before it is read
    (`_drop_tracked_deletions`). Pinned by: test_hwp.py::TestHwpxTrackChanges."""
    children = list(node)
    parts = [node.text or ""]
    # Id → position of the last deleteEnd. Scanning all later siblings for every deleteBegin to find its pair took 12 seconds for
    # 16,000 of them (quadratic.
    # Pinned by: test_hwp.py::TestLinearTimeOnCraftedInput).
    last_end = {child.get("Id"): index for index, child in enumerate(children) if _local(child) == "deleteEnd"}
    deleting: str | None = None
    for index, child in enumerate(children):
        name = _local(child)
        if name == "deleteBegin" and deleting is None and last_end.get(child.get("Id"), -1) > index:
            deleting = child.get("Id", "")
        elif name == "deleteEnd" and deleting is not None and child.get("Id", "") == deleting:
            deleting = None
        if deleting is not None:
            continue

        # hp:hypen (so spelled by Hangul; hp:hyphen too) is the soft hyphen of HWP code 24, drawn only at a line break inside
        # the word: written as `-` it made a different word (Pinned by: test_hwp.py::TestSoftHyphen)
        parts.append(_TEXT_MARKS.get(name, ""))
        parts.append(child.tail or "")
    return "".join(parts)


def _top_sublists(element: etree._Element | None) -> Iterator[etree._Element]:
    """Only the outermost subLists. Inner subLists (table cells, nested text boxes) are handled when that subList is rendered."""
    if element is None:
        return
    for child in element:
        if _local(child) == "subList":
            yield child
        elif _local(child) not in ("tbl", "p"):
            yield from _top_sublists(child)


def _int(value: str | None, default: int) -> int:
    """Integer attribute value. '²' and numbers longer than 4,300 digits give `default` (`_package.decimal_int`. Pinned by:
    test_hwp.py::TestOneOddValueInHangulDocuments,
    test_hwp_robustness.py::TestHugeNumbersInHangulDocuments)."""
    return decimal_int(value, default)


def _field_command(field: etree._Element) -> str:
    """The `Command` parameter of an `hp:fieldBegin` (empty when there is none)."""
    parameters = _child(field, "parameters")
    return next((param.text or "" for param in _children(parameters, "stringParam") if param.get("name") == "Command"), "") \
        if parameters is not None else ""


def _content_package(archive: zipfile.ZipFile, names: Collection[str]) -> etree._Element | None:
    """content.hpf (body order and picture list). None if missing or broken — the body then goes in section number order and pictures
    are found by BinData file name.
    A single broken content.hpf used to reject the whole document (Pinned by: test_hwp.py::TestOneOddValueInHangulDocuments::
    test_hwpx_with_a_broken_content_hpf_falls_back_to_section_and_picture_names)."""
    data = _read_member(archive, names, "Contents/content.hpf")
    return parse_part(data) if data is not None else None


def _read_member(archive: zipfile.ZipFile, names: Collection[str], name: str) -> bytes | None:
    """Bytes of a part inside the archive. None if missing or it cannot be decompressed (CRC or compression error)."""
    try:
        return archive.read(name) if name in names else None
    except _MEMBER_ERRORS:
        return None


def _parse_member(archive: zipfile.ZipFile, names: Collection[str], name: str) -> etree._Element | None:
    """A part XML outside the body (header.xml). None if missing, not decompressible, or the XML is broken."""
    data = _read_member(archive, names, name)
    if data is None:
        return None
    try:
        return etree.fromstring(data, parser=_xml_parser())
    except etree.XMLSyntaxError:
        return None


def _section_order(package: etree._Element | None, names: Collection[str], warnings: list[str]) -> list[str]:
    """The body order is set by the spine of content.hpf. Section number order if it is missing or broken. A spine cut at the
    cap is reported in `warnings`."""
    # A number of more than 4,300 digits cannot be read by `int()` and sorts after the others: one extra member with such a
    # name rejected a document whose spine never mentions it (Pinned by: test_audit_hangul.py::TestHugeHwpxSectionNumber).
    fallback = sorted((name for name in names if _SECTION_NAME.match(name)),
                      key=lambda name: decimal_int(_SECTION_NAME.match(name).group(1), float("inf")))
    if package is None:
        return fallback

    manifest = {item.get("id"): item.get("href", "") for item in package.iter() if _local(item) == "item"}
    # A section listed a second time is read once, and at most `_MAX_SPINE_ITEMS` entries are looked at (the same two guards
    # as the EPUB spine): one section of 300,000 empty paragraphs listed 40 times (a 33 KB file) was parsed and rendered 40
    # times, 29 s (deliberate. Pinned by: test_audit_hangul.py::TestHwpxSpine).
    ordered: dict[str, None] = {}
    for references, itemref in enumerate((node for node in package.iter() if _local(node) == "itemref"), 1):
        if references > _MAX_SPINE_ITEMS:
            warnings.append(f"The HWPX document lists more than {_MAX_SPINE_ITEMS:,} sections; the rest were not read.")
            break
        href = manifest.get(itemref.get("idref"), "")
        name = href if href.startswith("Contents/") else f"Contents/{href}"
        if _SECTION_NAME.match(name) and name in names:
            ordered[name] = None
    return list(ordered) or fallback


def _binary_items(package: etree._Element | None, names: Collection[str]) -> dict[str, str]:
    """Manifest item id → path inside the archive (BinData/image1.png). If the manifest is missing or broken, the BinData file name
    (without the extension) is taken as the id
    — in files saved by Hancom Office the two are the same."""
    if package is None:
        return {name.removeprefix("BinData/").rsplit(".", 1)[0]: name for name in names if name.startswith("BinData/")}
    return {item.get("id", ""): item.get("href", "") for item in package.iter() if _local(item) == "item"}


def _load_styles(header: etree._Element | None) -> dict[str, str]:
    if header is None:
        return {}
    return {style.get("id", ""): style.get("name", "") for style in header.iter() if _local(style) == "style"}


def _load_scripts(header: etree._Element | None) -> dict[str, str]:
    """Character shape (hh:charPr) id → super/subscript mark. Only shapes with an hh:supscript or hh:subscript child."""
    scripts: dict[str, str] = {}
    if header is None:
        return scripts
    for node in header.iter():
        if _local(node) == "charPr":
            marks = [mark for name, mark in (("supscript", SUPERSCRIPT), ("subscript", SUBSCRIPT)) if _child(node, name) is not None]
            if marks:
                scripts[node.get("id", "")] = marks[0]
    return scripts


# Runs of a paragraph looked at for its character size (`HwpxRenderer._smallest_text_size`)
_SIZE_SCAN_RUNS = 64


def _load_char_sizes(header: etree._Element | None) -> dict[str, int]:
    """Character shape (hh:charPr) id → its size (the height attribute, 1/100 pt). A height that is not a positive number is left out."""
    if header is None:
        return {}
    sizes = {}
    for node in header.iter():
        if _local(node) == "charPr":
            height = decimal_int(node.get("height"), 0)
            if height > 0:
                sizes[node.get("id", "")] = height
    return sizes


def _load_boxed_fills(header: etree._Element | None) -> dict[str, bool]:
    """Border fill (hh:borderFill) id → whether a cell with it has a visible border on all four sides: a line of a type
    other than NONE that is not white, the paper's colour. Diagonals and the fill are not looked at."""
    if header is None:
        return {}
    fills = {}
    for node in header.iter("{*}borderFill"):
        sides = {_local(side): side for side in node}
        fills[node.get("id", "")] = all(
            (border := sides.get(name)) is not None and border.get("type", "NONE") != "NONE"
            and border.get("color", "#000000").upper() != "#FFFFFF"
            for name in ("leftBorder", "rightBorder", "topBorder", "bottomBorder"))
    return fills


def _load_emphasis(header: etree._Element | None) -> dict[str, tuple[bool, bool]]:
    """Character shape (hh:charPr) id → (bold, italic), for the shapes with an hh:bold or hh:italic child."""
    emphasis: dict[str, tuple[bool, bool]] = {}
    if header is None:
        return emphasis
    for node in header.iter():
        if _local(node) == "charPr":
            bold, italic = _child(node, "bold") is not None, _child(node, "italic") is not None
            if bold or italic:
                emphasis[node.get("id", "")] = (bold, italic)
    return emphasis


def _load_struck_shapes(header: etree._Element | None) -> frozenset[str]:
    """Ids of character shapes (hh:charPr) that are struck through: the shape of hh:strikeout is a line type (`_STRIKEOUT_SHAPES`)."""
    if header is None:
        return frozenset()
    return frozenset(node.get("id", "") for node in header.iter() if _local(node) == "charPr"
                     and (strikeout := _child(node, "strikeout")) is not None and strikeout.get("shape") in _STRIKEOUT_SHAPES)


def _load_paragraph_heads(header: etree._Element | None) -> ParagraphHeads:
    """hh:heading (type, idRef, level from 0) and left margin of hh:paraPr, hh:paraHead of hh:numbering (level from 1, numFormat, text
    "^1."),
    the id of hh:bullet (char and checkedChar for a check bullet), and hh:paraPr checked. If a level value is not a number that
    definition is dropped (left without a number)."""
    heads = ParagraphHeads()
    if header is None:
        return heads
    for node in header.iter():
        name = _local(node)
        if name == "paraPr":
            heading = next((child for child in node.iter() if _local(child) == "heading"), None)
            if node.get("checked") == "1":
                heads.checked.add(node.get("id", ""))
            if heading is not None:
                level = min(_int(heading.get("level"), 0), 9)
                heads.shapes[node.get("id", "")] = (heading.get("type", "NONE"), heading.get("idRef", ""), level)
            # Left margin (hh:margin/hc:left). If both hp:case and hp:default of an hp:switch have one, the first is used — it is only
            # compared between paragraph shapes
            left = next((child for margin in node.iter() if _local(margin) == "margin"
                         for child in margin if _local(child) == "left"), None)
            if left is not None:
                heads.margins[node.get("id", "")] = _int(left.get("value"), 0)
        elif name == "numbering":
            levels = {_int(head.get("level"), 0): head for head in _children(node, "paraHead")}
            # Levels are 1-10 (since Hangul 2010). The list size is not taken from the level values in the file.
            heads.numberings[node.get("id", "")] = [
                NumberLevel(levels[index].text or "", levels[index].get("numFormat", "DIGIT"),
                            _int(levels[index].get("start"), 1) or 1)
                if index in levels else NumberLevel(f"^{index}.")
                for index in range(1, min(max([*levels, 1]), 10) + 1)
            ]
        elif name == "bullet":
            heads.bullets.add(node.get("id", ""))
            if node.get("checkedChar"):
                heads.check_bullets[node.get("id", "")] = (node.get("char", ""), node.get("checkedChar", ""))
    return heads


def _load_metadata(package: etree._Element | None) -> dict[str, str]:
    if package is None:
        return {}
    metadata = {}
    for node in package.iter():
        name = _local(node)
        text = (node.text or "").strip()
        if name == "title" and text:
            metadata["title"] = text
        elif name == "meta" and node.get("name") == "creator" and text:
            metadata["author"] = text
    return metadata
