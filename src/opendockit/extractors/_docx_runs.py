"""The text of a DOCX paragraph: its runs walked in order with tracked changes, fields, form fields, equation fields,
symbol fonts, scripts, strikethrough, emphasis and links, and what a run looks like (painted boxes, placeholders).
"""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
import re

from docx.opc.part import Part, PartFactory
from docx.parts.document import DocumentPart
from lxml import etree

from opendockit.extractors._charts import drawing_paragraph_lines
from opendockit.extractors._inline import (
    LINK_OPEN,
    SUBSCRIPT,
    SUPERSCRIPT,
    field_link,
    link_close,
    mark_emphasis,
    mark_script,
    mark_strike,
    merge_emphasis_marks,
    resolve_scripts,
    resolve_strikes,
)
from opendockit.extractors._notation import (
    format_dutmal,
)
from opendockit.extractors._omml import render_omml
from opendockit.extractors._package import decimal_int, parse_part
from opendockit.extractors._symbols import SYMBOL_FONTS, SYMBOL_GLYPHS, symbol_font_text, symbol_glyph


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_MC = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_M = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"


_V = "urn:schemas-microsoft-com:vml"
_O = "urn:schemas-microsoft-com:office:office"
_WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
_CHART = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_CHARTEX = "http://schemas.microsoft.com/office/drawing/2014/chartex"
_DIAGRAM = "http://schemas.openxmlformats.org/drawingml/2006/diagram"


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


# Markdown tables of imported content (`_embed_markdown_tables`): a row line, the rule line under the header row, a cell border (a `\|`
# neutralized with a backslash is cell text).
_TABLE_ROW = re.compile(r"\|.*\|")
_TABLE_RULE = re.compile(r"\|(?: ?:?-{3,}:? ?\|)+")
_TABLE_CELL_BORDER = re.compile(r"(?<!\\)\|")


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




@dataclass(frozen=True, slots=True)
class _Inline:
    """What `_paragraph_text` marks inside a paragraph besides its text (`ConvertOptions.keep_links`, `keep_emphasis`)."""

    link: Callable[[etree._Element], str | None] | None  # w:hyperlink → vetted address, None when links are not kept
    emphasis: bool
    styles: dict[str, tuple[bool, bool]]  # character style id → (bold, italic) (`_EMPHASIS_STYLE_NAMES`)


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
                        fields[-1].link = field_link("".join(fields[-1].instruction))
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
                    link = field_link(child.get(_w("instr"), ""))
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
                label = notes.label(kind, child.get(_w("id"), ""))
                if label is not None:
                    parts.append(f"[^{label}]")
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


def _int_value(value: str | None, default: int) -> int:
    """Integer attribute value. '²' and numbers of more than 4,300 digits give `default` (`_package.decimal_int`. Pinned by:
    test_office.py::TestOneOddValueDoesNotRejectTheDocument::test_docx_odd_grid_span_is_read_as_one_column)."""
    return decimal_int(value, default, signed=True)


class _NoteLabels:
    """Footnotes/endnotes referenced in the body (kind, id) → label. Each kind is numbered from 1 in order of first reference (same
    as HWP/HWPX). Footnotes and endnotes are numbered separately and would collide, so endnotes get the prefix 'endnote' to tell
    them apart. With the XML id used as is, the first footnote of a document saved by LibreOffice (the separators take ids 0 and 1)
    was `[^2]` (Pinned by: test_office_scaling_and_note_order.py::TestDocxFootnoteNumbersFollowTheOrderOfReference).

    `written` holds the notes that will have content below the body; a reference to any other note (no such id, no notes part, a
    note with nothing in it) gets no label and no mark — `[^1]` pointed at a definition that was never written (Pinned by:
    test_office_symbols_fields_notes.py::TestDocxNoteReferenceWithoutContent). None takes every reference.
    """

    def __init__(self, written: set[tuple[str, str]] | None = None) -> None:
        self.labels: dict[tuple[str, str], str] = {}  # in order of first reference
        self._counts = {"footnote": 0, "endnote": 0}
        self._written = written

    def label(self, kind: str, note_id: str) -> str | None:
        if self._written is not None and (kind, note_id) not in self._written:
            return None
        if (kind, note_id) not in self.labels:
            self._counts[kind] += 1
            number = self._counts[kind]
            self.labels[kind, note_id] = str(number) if kind == "footnote" else f"endnote{number}"
        return self.labels[kind, note_id]
