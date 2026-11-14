"""HWPX (OWPML, KS X 6101) → Markdown.

HWPX is XML inside a ZIP. The body in `Contents/section*.xml` is structured as `hp:p` (paragraph) → `hp:run` → `hp:t` (text), `hp:tbl`
(table) and shapes (text boxes).
Elements are compared **by local name only**: the namespace URI (2011/2016) differs between Hangul versions.
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from io import BytesIO
import re
import unicodedata
import zipfile

from lxml import etree

from opendockit.exceptions import CorruptedDocumentError, EncryptedDocumentError
from opendockit.extractors._charts import embedded_table, package_loader, render_chart
from opendockit.extractors._equation import render_equation
from opendockit.extractors._hangul import PREVIEW_CELL_MARKS, ParagraphPieces, decorated_text
from opendockit.extractors._inline import SUBSCRIPT, SUPERSCRIPT, mark_script, mark_strike
from opendockit.extractors._lists import BulletNesting, Numbering, NumberLevel, format_number
from opendockit.extractors._notation import COMMENT_TARGET_MAX, TableBudget, comment_line, format_dutmal, note_table, omitted_rows_warning
from opendockit.extractors._package import ZIP_MEMBER_ERRORS, decimal_int, parse_part
from opendockit.markdown import TableGrid, join_blocks, render_table, solid_length
from opendockit.models import ConvertOptions, Extraction, Section, SectionKind


_SECTION_NAME = re.compile(r"^Contents/section(\d+)\.xml$")
# Spine entries (itemref of content.hpf) looked at, the same number as for an EPUB (`_section_order`).
_MAX_SPINE_ITEMS = 5_000
_XML_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, remove_comments=True)
# Pictures, OLE objects and videos have no text, and a picture's description (shapeComment) is the "그림입니다. 원본 그림의 이름…" text
# that Hangul inserts automatically.
# The caption of an object (hp:caption) is read separately by `HwpxRenderer._caption`.
_SKIP_OBJECTS = {"pic", "ole", "video", "chart", "linesegarray", "secPr", "shapeComment"}
# Sides where the caption comes before the object (hp:caption side)
_CAPTION_BEFORE = {"TOP", "LEFT"}
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


def _local(element: etree._Element) -> str:
    tag = element.tag
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _children(element: etree._Element, name: str) -> Iterator[etree._Element]:
    return (child for child in element if _local(child) == name)


def _child(element: etree._Element, name: str) -> etree._Element | None:
    return next(_children(element, name), None)


def parse_xml(data: bytes) -> etree._Element:
    try:
        return etree.fromstring(data, parser=_XML_PARSER)
    except etree.XMLSyntaxError as exc:
        raise CorruptedDocumentError("The XML of the HWPX document is corrupted.") from exc


class HwpxRenderer:
    def __init__(self, styles: dict[str, str], options: ConvertOptions, extraction: Extraction,
                 load_binary: Callable[[str], bytes | None], heads: ParagraphHeads | None = None,
                 load_part: Callable[[str], bytes | None] | None = None, scripts: dict[str, str] | None = None,
                 struck: frozenset[str] = frozenset()) -> None:
        self._styles = styles
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
        self._outline_numbering = "1"
        self._options = options
        self._extraction = extraction
        self._load_binary = load_binary
        self._note_count = 0
        self._cell_depth = 0
        self._note_depth = 0  # 1 or more while footnote/endnote text is rendered. A table inside it becomes one line (`note_table`)
        # Tables being rendered (an inner table is handed to the top-level table). None is a place where a table cannot be handed over
        # (a footnote or header inside a cell).
        self._grids: list[TableGrid | None] = []
        self.notes: list[str] = []
        # Texts in order of first appearance, each once. Looked up in a list, 60,000 different headers took 11 s
        # (Pinned by: test_audit_hangul.py::TestManyDistinctHeaders).
        self.headers_footers: dict[str, None] = {}
        # Comments (review remarks): (author, text, pieces of the body range the comment is attached to). The range is the text between
        # fieldBegin and the fieldEnd with the same id
        self.memos: list[tuple[str, str, list[str]]] = []
        self._open_memos: dict[str, list[str]] = {}  # comment id not yet closed by fieldEnd and still being gathered → range pieces
        self._memo_lengths: dict[str, int] = {}  # comment id above → number of characters gathered
        self._unspaced_memos: dict[str, None] = {}  # comments above with a non-empty range not ending in whitespace (get a space between paragraphs)
        self._memo_depth = 0  # 1 or more while comment text is rendered. That text is not the range of another comment
        self.omitted_rows = 0  # rows dropped by the table grid cell limit (`TableGrid.omitted_rows`). Reported as a warning

    def render_paragraphs(self, container: etree._Element) -> list[str]:
        """An inner paragraph list (hp:subList etc.). The top-level body calls `render_paragraph` per paragraph."""
        blocks = []
        self._bullets.append(BulletNesting())
        try:
            for paragraph in _children(container, "p"):
                blocks.extend(self.render_paragraph(paragraph))
        finally:
            self._bullets.pop()
        return blocks

    def render_paragraph(self, paragraph: etree._Element) -> list[str]:
        self._note_memo_target(" ")  # between paragraphs of a comment range that spans several paragraphs
        # The head goes on the first text piece only, and a paragraph without text keeps its number (`_hangul.ParagraphPieces`).
        pieces = ParagraphPieces(lambda text: self._decorate(paragraph, text))
        blocks, buffer, flush = pieces.blocks, pieces.buffer, pieces.flush

        for run in _children(paragraph, "run"):
            script = self._scripts.get(run.get("charPrIDRef", ""))
            struck = run.get("charPrIDRef", "") in self._struck_shapes
            for node in run:
                # Reading inner paragraphs (table, text box) changes these by the time it returns, so set them again per node
                self._script, self._struck = script, struck
                self._render_object(node, buffer, blocks, flush)
        return pieces.finish(lambda: self._paragraph_head(paragraph), in_cell=bool(self._cell_depth))

    def _render_object(self, node: etree._Element, buffer: list[str], blocks: list[str],
                       flush: Callable[[], None]) -> None:
        name = _local(node)
        if name == "t":
            # A run with a super/subscript character shape. Appended as plain text, 10⁶ became 106 (Pinned by: test_hwp.py::TestSuperscript).
            plain = _t_text(node)
            text = mark_script(plain, self._script) if self._script else plain
            self._note_memo_target(plain)
            # A run with a strikethrough character shape. Appended as plain text, a deleted value read as a valid one
            # (Pinned by: test_hwp.py::TestStrikethrough).
            buffer.append(mark_strike(text) if self._struck else text)
        elif name == "tbl":
            flush()
            blocks.append(self._render_table(node))
        elif name == "ctrl":
            self._render_control(node, buffer)
        elif name == "equation":
            script = _child(node, "script")
            text = render_equation(script.text or "") if script is not None else ""
            buffer.append(self._with_caption(node, text))
        elif name == "pic":
            buffer.append(self._with_caption(node, self._picture_marker(node)))
        elif name in _UNREAD_OBJECTS:
            # OLE object (pasted Excel sheet or chart) and video: the content is not read and only a placeholder is left. They used to
            # vanish without a trace, so there was no telling what was missing
            # (Pinned by: test_hwp.py::TestEmbeddedObjects).
            self._unread[name] = self._unread.get(name, 0) + 1
            buffer.append(self._with_caption(node, f" {_UNREAD_OBJECTS[name][0]} "))
        elif name == "chart":
            flush()
            caption, before = self._caption(node)
            chart = self._render_chart(node)
            blocks.extend(block for block in ((caption, chart) if before else (chart, caption)) if block)
        elif name == "textart":
            # Text art: the characters are in the text attribute, not the element text
            # (Pinned by: test_hwp.py::TestHwpxObjects::test_text_art_is_kept).
            flush()
            blocks.append(_TEXTART_BREAKS.sub("\n", node.get("text", "")).strip())
        elif name == "dutmal":
            # Ruby text (dutmal): the main text (hp:mainText) is also inside this element, not in hp:t, so even the main text was dropped
            # (Pinned by: test_hwp.py::TestDutmal).
            main, annotation = (_child(node, part) for part in ("mainText", "subText"))
            buffer.append(format_dutmal(*("".join(part.itertext()).strip() if part is not None else ""
                                          for part in (main, annotation))))
        elif name == "compose":
            # Composed characters: the overlapped characters are in the composeText attribute and were dropped
            # (Pinned by: test_hwp.py::TestComposedCharacters).
            buffer.append(node.get("composeText", ""))
        elif name in _FORM_OBJECTS:
            # Form objects: the value is in attributes and children and was dropped (Pinned by: test_hwp.py::TestHwpxFormObjects).
            value = _form_value(node)
            buffer.append(f" {value} " if value else "")
        elif name == "secPr":
            self._outline_numbering = node.get("outlineShapeIDRef", self._outline_numbering)
        elif name == "switch":
            # Compatibility switch: read the newer object (hp:case, e.g. an OOXML chart) first, and if nothing comes out read the
            # fallback object (hp:default).
            for branch in (_child(node, "case"), _child(node, "default")):
                if branch is None:
                    continue
                buffered, emitted = len(buffer), len(blocks)
                for child in branch:
                    self._render_object(child, buffer, blocks, flush)
                if len(blocks) > emitted or "".join(buffer[buffered:]).strip():
                    break
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

    def _with_caption(self, node: etree._Element, content: str) -> str:
        """Attach the caption before or after an object inside a paragraph (picture placeholder, equation)."""
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
        rendered = render_chart(root, table, package_loader(self._load_part, name))
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
            elif name == "fieldEnd":
                self._close_memo(node.get("beginIDRef", ""))
            elif name in ("footNote", "endNote") and self._options.include_footnotes:
                self._note_count += 1
                self._grids.append(None)  # footnote text is not cell text — handing its table to the outer cell leaks the placeholder to the note
                self._note_depth += 1
                try:
                    note = " ".join(" ".join(block.split()) for sub in _top_sublists(node)
                                    for block in self.render_paragraphs(sub))
                finally:
                    self._note_depth -= 1
                    self._grids.pop()
                buffer.append(f"[^{self._note_count}]")
                self.notes.append(f"[^{self._note_count}]: {note}")
            elif name in ("header", "footer") and self._options.include_headers_footers:
                self._grids.append(None)
                try:
                    text = join_blocks(block for sub in _top_sublists(node) for block in self.render_paragraphs(sub))
                finally:
                    self._grids.pop()
                if text:
                    self.headers_footers.setdefault(text)
            elif name == "autoNum":
                buffer.append(_auto_number(node))

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
            text = " ".join(" ".join(block.split()) for sub in _top_sublists(field) for block in self.render_paragraphs(sub))
        finally:
            self._memo_depth -= 1
            self._grids.pop()
        target: list[str] = []
        self.memos.append((author, text, target))
        self._open_memos[field.get("id", "")] = target
        self._memo_lengths[field.get("id", "")] = 0
        self._unspaced_memos.pop(field.get("id", ""), None)

    def _note_memo_target(self, text: str) -> None:
        """Add a piece of body text to the ranges of the open comments. Only as much as will be shown is gathered (limit + 1 characters
        even without one trailing space, so that exceeding `COMMENT_TARGET_MAX` can be noticed), and a comment that has gathered enough
        is removed from the open list. A whitespace-only piece (between paragraphs) is not appended at the start of a range or after
        whitespace.
        Recounting the gathered characters for every piece and appending between-paragraph spaces without limit, a single comment
        without fieldEnd took time quadratic in the number of paragraphs
        (Pinned by: test_hwp.py::TestMemo::test_hwpx_memos_without_an_end_take_linear_time). A whitespace piece scans only the comments
        that will take it (`_unspaced_memos`)
        — scanning all open comments for every paragraph was quadratic when many comments were open with an empty range (Pinned by: same
        class
        test_many_hwpx_memos_over_empty_paragraphs_take_linear_time)."""
        if self._memo_depth or not text or not self._open_memos:  # empty text (`<hp:t/>`) does not extend a range
            return
        for memo_id in list(self._unspaced_memos if text.isspace() else self._open_memos):
            self._open_memos[memo_id].append(text)
            self._memo_lengths[memo_id] += len(text)
            if self._memo_lengths[memo_id] > COMMENT_TARGET_MAX + 1:
                self._close_memo(memo_id)
            elif text[-1:].isspace():
                self._unspaced_memos.pop(memo_id, None)
            else:
                self._unspaced_memos[memo_id] = None

    def _close_memo(self, memo_id: str) -> None:
        self._open_memos.pop(memo_id, None)
        self._memo_lengths.pop(memo_id, None)
        self._unspaced_memos.pop(memo_id, None)

    def memo_lines(self) -> str:
        return "\n".join(comment_line(author, text, "".join(target).strip()) for author, text, target in self.memos if text)

    def _decorate(self, paragraph: etree._Element, text: str) -> str:
        """Attach the paragraph head (`_lists.paragraph_head`) and, for an outline style, the heading mark to the paragraph's first text piece."""
        mark, level = self._paragraph_head(paragraph)
        style_name = self._styles.get(paragraph.get("styleIDRef", ""), "")
        margin = self._heads.margins.get(paragraph.get("paraPrIDRef", ""), 0)
        return decorated_text(text, mark, level, style_name, margin, self._bullets[-1], in_cell=bool(self._cell_depth),
                              check=self._check_mark(paragraph))

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
        caption_text = join_blocks(block for sub in _top_sublists(caption) for block in self.render_paragraphs(sub)) \
            if caption is not None else ""

        grid = TableGrid()
        # The top-level table checks the character limit while reading cells (`_notation.TableBudget`). Once exceeded, the remaining rows
        # are not read.
        budget = None if self._grids or self._cell_depth or self._note_depth else TableBudget(self._extraction, self._options)
        outer = self._grids[-1] if self._grids else None
        self._grids.append(grid)
        self._cell_depth += 1
        try:
            for row_index, row in enumerate(_children(table, "tr")):
                grid.reach(row_index, 0)  # a row (hp:tr) whose cells are all covered by vertical spans from above is in the table (TableGrid.reach)
                if self._place_row(grid, row, row_index, budget):
                    break
        finally:
            self._cell_depth -= 1
            self._grids.pop()

        # A table inside a footnote (and tables inside it) is flattened to one line (`note_table`: cells ' / ', rows '; '). Rendering a
        # Markdown table gave '| 연도 | 값 | | --- |' when the footnote was joined into one line
        # (Pinned by: test_hwp.py::TestTableInsideFootnote). A table inside a table is drawn in place by the outer table
        # (markdown.TableGrid.embed). Where it cannot be handed over, as in a header inside a cell, it is flattened to 'cell / cell'
        # (Pinned by: test_hwp.py::TestNestedTables).
        # The caption stays with the table wherever it is. Dropping it loses '표 2' ("Table 2"), and a picture in the caption was only
        # registered with no placeholder, costing a description for nothing
        # (Pinned by: test_hwp.py::TestNestedTables::test_caption_of_an_inner_table_is_kept_with_it).
        if self._note_depth:
            return join_blocks([caption_text, note_table(grid.rows())])
        if outer is not None:
            return join_blocks([caption_text, outer.embed(grid)])
        if self._cell_depth:
            return join_blocks([caption_text, grid.flatten()])
        rendered = grid.render()
        self.omitted_rows += grid.omitted_rows()
        return join_blocks([caption_text, rendered])

    def _place_row(self, grid: TableGrid, row: etree._Element, row_index: int, budget: TableBudget | None) -> bool:
        """Place the cells of a table row (hp:tr) on the grid. True if `budget` says the limit is exceeded — the remaining cells and rows are not
        read."""
        for cell in _children(row, "tc"):
            text = "\n".join(block for sub in _children(cell, "subList") for block in self.render_paragraphs(sub))
            address = _child(cell, "cellAddr")
            span = _child(cell, "cellSpan")
            row_span = _int(span.get("rowSpan") if span is not None else None, 1)
            col_span = _int(span.get("colSpan") if span is not None else None, 1)
            if address is None:
                grid.place_next(row_index, text, row_span=row_span, col_span=col_span)
                if budget is not None:
                    budget.forget()
                continue

            row_at, col_at = _int(address.get("rowAddr"), row_index), _int(address.get("colAddr"), 0)
            grid.place_or_next(row_at, col_at, text, row_span=row_span, col_span=col_span)
            if budget is not None and budget.add(row_at, col_at, text, row_span=row_span, col_span=col_span):
                return True
        return False


def extract_hwpx(data: bytes, options: ConvertOptions) -> Extraction:
    try:
        archive = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise CorruptedDocumentError("The HWPX archive structure is corrupted.") from exc

    with archive:
        names = archive.namelist()
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
                                _load_scripts(header), _load_struck_shapes(header))
        blocks = []
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

            # Read per top-level paragraph and count characters. Once the limit is exceeded, the remaining paragraphs and sections are not read.
            for paragraph in _children(section, "p"):
                rendered = renderer.render_paragraph(paragraph)
                blocks.extend(rendered)
                if extraction.count(sum(solid_length(block) for block in rendered), options):
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
    body = join_blocks([*renderer.headers_footers, *blocks, "\n".join(renderer.notes), renderer.memo_lines()])
    preview_text = preview.decode("utf-8", errors="replace")
    if not body.strip() and preview_text.translate(PREVIEW_CELL_MARKS).strip():
        body = preview_text
        extraction.warnings.append("The body could not be parsed, so the preview text saved by Hancom Office was used instead "
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
        return f"{mark} {node.get('caption', '')}".strip()
    if name in ("comboBox", "listBox"):
        return node.get("selectedValue", "").strip()
    if name == "edit":
        text = _child(node, "text")
        return (text.text or "").strip() if text is not None else ""
    return ""


def _t_text(node: etree._Element) -> str:
    """Text of hp:t. A range deleted under change tracking (hp:deleteBegin to the hp:deleteEnd with the same Id) is left out — left as
    plain text, both the deleted and the inserted value read as valid values
    (same direction as DOCX w:del). If the pair is not inside the same hp:t, where it ends cannot be known for sure, so it is left as is
    (Known accepted gap: a deletion spanning runs with different character shapes remains. Pinned by: test_hwp.py::TestHwpxTrackChanges)."""
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

        if name == "tab":
            parts.append("\t")
        elif name == "lineBreak":
            parts.append("\n")
        elif name == "hyphen":
            parts.append("-")
        elif name in ("nbSpace", "fwSpace"):
            parts.append(" ")
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


def _content_package(archive: zipfile.ZipFile, names: list[str]) -> etree._Element | None:
    """content.hpf (body order and picture list). None if missing or broken — the body then goes in section number order and pictures
    are found by BinData file name.
    A single broken content.hpf used to reject the whole document (Pinned by: test_hwp.py::TestOneOddValueInHangulDocuments::
    test_hwpx_with_a_broken_content_hpf_falls_back_to_section_and_picture_names)."""
    data = _read_member(archive, names, "Contents/content.hpf")
    return parse_part(data) if data is not None else None


def _read_member(archive: zipfile.ZipFile, names: list[str], name: str) -> bytes | None:
    """Bytes of a part inside the archive. None if missing or it cannot be decompressed (CRC or compression error)."""
    try:
        return archive.read(name) if name in names else None
    except _MEMBER_ERRORS:
        return None


def _parse_member(archive: zipfile.ZipFile, names: list[str], name: str) -> etree._Element | None:
    """A part XML outside the body (header.xml). None if missing, not decompressible, or the XML is broken."""
    data = _read_member(archive, names, name)
    if data is None:
        return None
    try:
        return etree.fromstring(data, parser=_XML_PARSER)
    except etree.XMLSyntaxError:
        return None


def _section_order(package: etree._Element | None, names: list[str], warnings: list[str]) -> list[str]:
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


def _binary_items(package: etree._Element | None, names: list[str]) -> dict[str, str]:
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
