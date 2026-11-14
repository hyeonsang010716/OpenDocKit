"""HWP 5.x (binary) → Markdown.

HWP 5.x is an OLE compound document. The body is a run of records in the `BodyText/Section{n}` streams (usually raw-deflate compressed),
and the records form a tree through a 4-byte header (tag 10 bits, level 10 bits, size 12 bits). Public spec: Hancom "Hangul Document
File Format 5.0".

pyhwp (hwp5txt) is AGPL and flattens tables into plain text, so it is not used. Only the records needed for text, tables, text boxes,
footnotes and outline headings are read directly.
"""

from array import array
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from io import BytesIO
import re
import struct
import sys
import zlib

import olefile

from opendockit.exceptions import CorruptedDocumentError, DocumentTooLargeError, EncryptedDocumentError
from opendockit.extractors._equation import render_equation
from opendockit.extractors._hangul import PREVIEW_CELL_MARKS, ParagraphPieces, decorated_text
from opendockit.extractors._inline import SUBSCRIPT, SUPERSCRIPT, mark_script, mark_strike
from opendockit.extractors._lists import NUMBER_SHAPES, BulletNesting, Numbering, NumberLevel, format_number
from opendockit.extractors._notation import TableBudget, format_dutmal, note_table, omitted_rows_warning
from opendockit.markdown import TableGrid, join_blocks, solid_length
from opendockit.models import ConvertOptions, Extraction, Section, SectionKind


# Maximum size read from the summary information stream. Only the title and author strings are needed.
_SUMMARY_MAX_BYTES = 64 * 1024


_HWPTAG_BEGIN = 0x10
TAG_BIN_DATA = _HWPTAG_BEGIN + 2
TAG_CHAR_SHAPE = _HWPTAG_BEGIN + 5
TAG_NUMBERING = _HWPTAG_BEGIN + 7
TAG_BULLET = _HWPTAG_BEGIN + 8
TAG_PARA_SHAPE = _HWPTAG_BEGIN + 9
TAG_STYLE = _HWPTAG_BEGIN + 10
TAG_PARA_HEADER = _HWPTAG_BEGIN + 50
TAG_PARA_TEXT = _HWPTAG_BEGIN + 51
TAG_PARA_CHAR_SHAPE = _HWPTAG_BEGIN + 52
# Attribute position in a character shape (CHAR_SHAPE): after the font IDs (2×7) + width ratio, letter spacing, relative size and
# character offset (1×7 each) + base size (4). In spec table 35, bit 15 is superscript and bit 16 is subscript (hwplib CharShapeProperty
# agrees). The position was confirmed on real files through bold (bit 1) and the base size, and the script bits by comparing with the
# HWPX twin (hh:supscript) of the same document (Pinned by: test_hwp.py::TestSuperscript).
_CHAR_SHAPE_ATTRIBUTE_OFFSET = 46
_CHAR_SHAPE_SUPERSCRIPT, _CHAR_SHAPE_SUBSCRIPT = 1 << 15, 1 << 16
# In the same table, bits 18-20 are the strikeout flag and bits 26-29 the strikeout shape (line type, in the order of spec table 27 =
# hwplib BorderType2: 0 solid … 7 double … 10 thin+thick+thin, 11 and 12 wave, 13-16 3D). It counts as strikethrough only when the flag
# value is 1 and the shape is 0-10 — when hwp2hwpx converts to HWPX it maps only 0-10 to LineType2 line types and wave and 3D to NONE,
# so this matches the rule on the HWPX side (`hwpx._STRIKEOUT_SHAPES`).
# In real government press-release HWP files, the body character shapes without strikethrough had flag 1 + shape 15, and the HWPX of
# the same documents (character shape indexes 1:1) had `3D` (342 shapes in 16 pairs). Flag 1 + shape 7 paired with `DOUBLE_SLIM` in
# HWPX. There is no HWP sample yet in which text is actually struck through
# (Pinned by: test_hwp.py::TestStrikethrough).
_CHAR_SHAPE_STRIKEOUT_SHIFT, _CHAR_SHAPE_STRIKEOUT_MASK = 18, 0x7
_CHAR_SHAPE_STRIKEOUT_SHAPE_SHIFT, _CHAR_SHAPE_STRIKEOUT_SHAPE_MASK = 26, 0xF
_STRIKEOUT_LINE_SHAPES = range(11)
TAG_CTRL_HEADER = _HWPTAG_BEGIN + 55
TAG_LIST_HEADER = _HWPTAG_BEGIN + 56
TAG_TABLE = _HWPTAG_BEGIN + 61
TAG_EQEDIT = _HWPTAG_BEGIN + 72
# OLE object (pasted Excel sheet or chart) inside a drawing object (gso). Its content is not read; only a placeholder is left.
TAG_SHAPE_OLE = _HWPTAG_BEGIN + 68
_OLE_MARK = "[OLE object]"
TAG_SHAPE_PICTURE = _HWPTAG_BEGIN + 69
# Position of the BinItem ID in SHAPE_COMPONENT_PICTURE: border (12) + image rectangle (32) + crop (16) + inner margin (8) +
# brightness, contrast and effect (3).
# Confirmed with three real Hangul files (docs/design.md).
_PICTURE_BIN_ID_OFFSET = 71
# Text art inside a drawing object (gso). The text is an HWP string (WORD length + UTF-16) after the vertex coordinates (INT32×8)
# (hwplib ForControlTextArt).
# Line breaks are written as CR/LF or, as in HWPX, as control picture symbols (␍␊).
TAG_SHAPE_TEXTART = _HWPTAG_BEGIN + 74
_TEXTART_TEXT_OFFSET = 32
_TEXTART_BREAKS = re.compile("\r\n|\r|\n|␍␊|␍|␊")

# Control characters in PARA_TEXT (spec table 6). A char control takes 1 WCHAR; inline and extended controls take 8 WCHARs (16 bytes).
# Only extended controls have a CTRL_HEADER record each, and their order within the paragraph equals the order of the CTRL_HEADER children.
_EXTENDED_CONTROLS = {1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23}
_INLINE_CONTROLS = {4, 5, 6, 7, 8, 9, 19, 20}
_CHAR_REPLACEMENTS = {9: "\t", 10: "\n", 24: "-", 30: " ", 31: " "}

_FLAG_COMPRESSED = 0x01
_FLAG_PASSWORD = 0x02
_FLAG_DISTRIBUTION = 0x04
# Bodies encrypted by DRM or with a public certificate (FileHeader attribute bits 4 'DRM 보안 문서', 8 '공인 인증서 암호화' and
# 10 '공인 인증서 DRM 보안 문서' of the 5.0 spec; pyhwp `drm`, `cert_encrypted`, `cert_drm`; hwp.js the same). Their streams are
# not readable without the key: they came out as an empty result or as a 'corrupted' document
# (Pinned by: test_hwp.py::TestProtectedDocuments).
_FLAG_PROTECTED = 0x10 | 0x100 | 0x400

# Paragraph head kind in bits 23-24 of paragraph shape attribute 1 (spec table 44)
_HEAD_OUTLINE, _HEAD_NUMBER, _HEAD_BULLET = 1, 2, 3

# Auto-number (atno) kinds in attribute bits 0-3 (spec table 144) that write the number in place in the text: 3 picture, 4 table,
# 5 equation — the number in a caption's "표 1" ("Table 1").
# 0 page number is not document content, and 1 footnote and 2 endnote numbers are replaced by `[^n]`, so they are not written
# (deliberate.
# Pinned by: test_hwp.py::TestAutoNumber).
_AUTO_NUMBER_KINDS = {3, 4, 5}
# Directions in bits 0-1 of the caption attribute of a caption LIST_HEADER (the UINT32 after paragraph count 2 + attribute 4 + 2 bytes)
# that put the caption before the object: 0 left, 2 top.
# The position was confirmed with public hwplib samples (equation and table captions below = 3).
_CAPTION_BEFORE = {0, 2}
# Limit on the number of records built for one document = `max_uncompressed_bytes` // this value (about 4.1 million by default). A body
# holding 16 million zero-size records (4-byte header only) used 12 seconds and 2.4GB from 62KB compressed — the decompression limit
# bounds only bytes and cannot stop the number of record objects. Records in real Hangul files average 35 bytes (26 bytes even for
# nothing but empty paragraphs), so reaching this limit takes a decompressed body of around 140MB and 16 million characters, and the
# default character limit (500,000 characters) stops reading long before that (the body is read as a stream, so records after the
# stop are not counted).
# Known accepted gap: with the character limit turned off (max_chars=None), a real document that large is also a
# `DocumentTooLargeError`
# (Pinned by: test_hwp.py::TestHwpRecordBudget).
_BYTES_PER_RECORD_BUDGET = 128
# Upper bound on the depth of descending into paragraphs inside controls (table cells, text boxes, footnotes, headers)
# (`HwpRenderer.render`). Anything deeper is not read and a warning is given.
# A table nested 400 levels deep exceeded the recursion limit (RecursionError) and the whole document was rejected — the outer levels
# are kept. `markdown.TableGrid`, which draws tables, also recurses per inner table.
# A 128-level table uses about 400 of Python's default recursion limit of 1,000, leaving room for the caller's stack.
# Known accepted gap: the inner part of 129-200-level tables that used to be read is now dropped with a warning (Pinned by:
# test_hwp_robustness.py::
# TestDeepRecordChains).
_MAX_NESTING = 128


@dataclass(slots=True)
class Record:
    tag: int
    level: int
    payload: bytes
    children: list["Record"] = field(default_factory=list)


@dataclass(slots=True)
class _RecordBudget:
    """Number of records that can still be built for one document (`_BYTES_PER_RECORD_BUDGET`)."""

    remaining: int

    def spend(self) -> None:
        self.remaining -= 1
        if self.remaining < 0:
            raise DocumentTooLargeError("The document has too many internal records.")


def parse_records(stream: bytes, budget: _RecordBudget | None = None) -> list[Record]:
    """Build a tree from the record stream according to the level. A truncated stream is used as far as it could be read."""
    return list(iter_records(stream, budget))


def iter_records(stream: bytes, budget: _RecordBudget | None = None) -> Iterator[Record]:
    """Yield each top-level record as soon as its subtree ends. The body is not built into a tree as a whole, so once reading stops
    (character limit) the later records are neither built nor counted. `budget` is spent for every record."""
    root: Record | None = None
    stack: list[Record] = []
    offset = 0
    end = len(stream)
    while offset + 4 <= end:
        (header,) = struct.unpack_from("<I", stream, offset)
        offset += 4
        tag, level, size = header & 0x3FF, (header >> 10) & 0x3FF, (header >> 20) & 0xFFF
        if size == 0xFFF:
            if offset + 4 > end:
                break
            (size,) = struct.unpack_from("<I", stream, offset)
            offset += 4
        if budget is not None:
            budget.spend()
        record = Record(tag, level, stream[offset:offset + size])
        offset += size

        while stack and stack[-1].level >= level:
            stack.pop()
        if stack:
            stack[-1].children.append(record)
        else:
            if root is not None:
                yield root
            root = record
        stack.append(record)
    if root is not None:
        yield root


# The two bytes of one character (UTF-16 unit) that is a control character (0-31). A match at an odd byte is made of bytes spanning two
# characters (the '00 00' in 'A' 41 00 + '가' 00 AC), so it is text (`decode_para_text` skips it).
_CONTROL_UNIT = re.compile(rb"[\x00-\x1f]\x00")
# Character shape runs of a paragraph: (start character position, (super/subscript mark, struck through or not)). Ascending by position,
# and neighboring runs have different values.
ShapeRuns = list[tuple[int, tuple[str | None, bool]]]


def decode_para_text(payload: bytes, shape_runs: ShapeRuns | None = None) -> list[str | int]:
    """Decode PARA_TEXT into a list of [string piece | extended control code]. Tables, text boxes etc. go where the extended controls are.

    With `shape_runs`, text in super/subscript and strikethrough runs gets temporary marks (`mark_script`, the strike mark). Before the
    first run there is no mark.
    Text between control characters is gathered as bytes and decoded at once. Making a Python integer per character (tuple or list), one
    paragraph of 24 million characters used 1.9GB
    (Pinned by: test_hwp_robustness.py::TestHugeParagraphText). Text with the same mark is decoded as one piece even across control characters.
    """
    count = len(payload) // 2
    view = memoryview(payload)
    parts: list[str | int] = []
    pieces: list[bytes | memoryview] = []  # text being gathered now (UTF-16 bytes)
    shape: tuple[str | None, bool] = (None, False)  # (script mark, struck through or not) of the text being gathered now
    runs = shape_runs or []
    run = -1  # index of the run covering the current position (-1 is before the first run)

    def flush() -> None:
        if pieces:
            script, struck = shape
            decoded = b"".join(pieces).decode("utf-16-le", errors="replace")
            decoded = mark_script(decoded, script) if script else decoded
            parts.append(mark_strike(decoded) if struck else decoded)
            pieces.clear()

    def add(start: int, stop: int, piece: bytes | None = None) -> None:
        """Gather the text at character positions [start, stop) (that one character if `piece` is given), split per run mark."""
        nonlocal shape, run
        while start < stop:
            while run + 1 < len(runs) and runs[run + 1][0] <= start:
                run += 1
            wanted = runs[run][1] if run >= 0 else (None, False)
            end = min(stop, runs[run + 1][0]) if run + 1 < len(runs) else stop
            if wanted != shape:
                flush()
                shape = wanted
            pieces.append(piece if piece is not None else view[2 * start:2 * end])
            start = end

    index = 0
    while index < count:
        match = _CONTROL_UNIT.search(payload, 2 * index, 2 * count)
        while match is not None and match.start() % 2:
            match = _CONTROL_UNIT.search(payload, match.start() + 1, 2 * count)
        control = match.start() // 2 if match is not None else count
        add(index, control)
        if control == count:
            break

        unit = payload[2 * control]
        if unit in _EXTENDED_CONTROLS:
            flush()
            parts.append(unit)
            index = control + 8
        elif unit in _INLINE_CONTROLS:
            if unit == 9:
                add(control, control + 1, "\t".encode("utf-16-le"))
            index = control + 8
        else:
            replacement = _CHAR_REPLACEMENTS.get(unit)
            if replacement:
                add(control, control + 1, replacement.encode("utf-16-le"))
            index = control + 1
    flush()
    return parts


def ctrl_id(record: Record) -> str:
    """The 4-character control ID of a CTRL_HEADER. It is stored as a little-endian UINT32, so it is read reversed ("tbl ", "gso ", "fn  " …)."""
    return record.payload[:4][::-1].decode("latin-1") if len(record.payload) >= 4 else ""


@dataclass(slots=True)
class ParagraphHeads:
    """Paragraph head definitions from DocInfo: per paragraph shape (head kind, level, numbering/bullet ID) and left margin, the paragraph
    numbering definitions, and the number of bullets."""

    shapes: list[tuple[int, int, int]] = field(default_factory=list)
    margins: list[int] = field(default_factory=list)
    numberings: list[list[NumberLevel]] = field(default_factory=list)
    bullet_count: int = 0


class HwpRenderer:
    def __init__(self, styles: list[str], options: ConvertOptions, extraction: Extraction | None = None,
                 load_bin: Callable[[int], bytes | None] | None = None, heads: ParagraphHeads | None = None,
                 scripts: dict[int, str] | None = None, struck: frozenset[int] = frozenset()) -> None:
        self._styles = styles
        # Character shape ID → super/subscript mark (parse_char_scripts), and the character shape IDs that are struck through
        # (parse_struck_shapes)
        self._scripts = scripts or {}
        self._struck = struck
        # Number of OLE objects whose content was not read and only a placeholder was left, number of inner contents not read because
        # they were nested too deeply (`_MAX_NESTING`), and number of rows dropped by the table grid cell limit
        # (`TableGrid.omitted_rows`). Reported as warnings after everything is read.
        self.unread_ole = 0
        self.too_deep = 0
        self.omitted_rows = 0
        self._depth = 0
        self._heads = heads or ParagraphHeads()
        # Margin levels of bullet paragraphs (`BulletNesting`). The top-level body is [0]; a new one is pushed while inner content (cells,
        # footnotes, text boxes, captions) is rendered.
        self._bullets = [BulletNesting()]
        # Number counters: (kind, numbering ID). Outline numbers and paragraph numbers are counted separately. The outline numbering ID is
        # set by the section definition (secd).
        self._numberings: dict[tuple[int, int], Numbering] = {}
        self._outline_numbering = 1
        self._options = options
        self._extraction = extraction or Extraction()
        self._load_bin = load_bin
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

    def render(self, records: list[Record]) -> list[str]:
        """Find paragraphs (PARA_HEADER) and render them. A record that is not a paragraph is descended to find inner paragraphs — with a
        stack (no recursion). Descending by recursion, a chain of 1,000 unknown records raised RecursionError → the whole document was
        rejected. Entering paragraphs inside a control (calling this method again) goes at most `_MAX_NESTING` levels deep
        (Pinned by: test_hwp_robustness.py::TestDeepRecordChains)."""
        if self._depth >= _MAX_NESTING:
            self.too_deep += bool(records)
            return []

        blocks: list[str] = []
        stack = [iter(records)]

        nested = self._depth > 0
        if nested:
            self._bullets.append(BulletNesting())
        self._depth += 1
        try:
            while stack:
                record = next(stack[-1], None)
                if record is None:
                    stack.pop()
                elif record.tag == TAG_PARA_HEADER:
                    blocks.extend(self._render_paragraph(record))
                elif record.tag != TAG_CTRL_HEADER:
                    stack.append(iter(record.children))
        finally:
            self._depth -= 1
            if nested:
                self._bullets.pop()
        return blocks

    def _render_paragraph(self, paragraph: Record) -> list[str]:
        """Paragraph text and the controls at their places (table, text box, footnote, header, equation, ruby text, composed characters,
        auto number). Other controls are not read even if they have inner paragraphs.
        Not putting comments (field `%%me`) into the body is deliberate (same direction as DOCX comments. Pinned by: test_hwp.py::TestMemo)."""
        text_record = next((child for child in paragraph.children if child.tag == TAG_PARA_TEXT), None)
        controls = iter([child for child in paragraph.children if child.tag == TAG_CTRL_HEADER])
        parts = decode_para_text(text_record.payload, self._shape_runs(paragraph, len(text_record.payload) // 2)) if text_record else []

        # The head goes on the first text piece only, and a paragraph without text keeps its number (`_hangul.ParagraphPieces`).
        pieces = ParagraphPieces(lambda text: self._decorate(paragraph, text))
        blocks, buffer, flush = pieces.blocks, pieces.buffer, pieces.flush

        for part in parts:
            if isinstance(part, str):
                buffer.append(part)
                continue
            control = next(controls, None)
            if control is None:
                continue
            kind = ctrl_id(control)
            if kind == "secd" and len(control.payload) >= 20:
                (self._outline_numbering,) = struct.unpack_from("<H", control.payload, 18)
            elif kind == "tbl ":
                flush()
                blocks.append(self._render_table(control))
            elif kind in ("fn  ", "en  "):
                self._render_note(control, buffer)
            elif kind in ("head", "foot"):
                self._collect_header_footer(control)
            elif kind == "gso ":
                # Text art text is a separate block, like hp:textart in HWPX. It was not read and was dropped entirely (Pinned by:
                # test_hwp_heads_and_shapes.py::
                # TestHwpTextArt).
                buffer.extend(self._object_markers(control))
                inner = [*_text_arts(control), *self.render(control.children)]
                if inner:
                    flush()
                    blocks.extend(inner)
            elif kind == "eqed":
                buffer.append(self._equation_with_caption(control))
            elif kind == "tdut":
                buffer.append(_dutmal_text(control))
            elif kind == "tcps":
                # Composed characters: the overlapped characters are also inside the control, so they were dropped
                # (Pinned by: test_hwp.py::TestComposedCharacters).
                buffer.extend(_control_strings(control, 1))
            elif kind == "atno":
                buffer.append(_auto_number(control))
        return pieces.finish(lambda: self._paragraph_head(paragraph), in_cell=bool(self._cell_depth))

    def _equation_with_caption(self, control: Record) -> str:
        """Equation (eqed) + its caption. The caption paragraph ('수식 1 넓이', including atno) comes as a sibling of the LIST_HEADER under
        the control and was dropped. Like HWPX `_with_caption`, top and left captions go before, others after (Pinned by:
        test_hwp.py::TestEquations::
        test_hwp_equation_caption_is_kept_on_its_side)."""
        equation = render_equation(_equation_script(control))
        caption = " ".join(" ".join(block.split()) for block in self.render(control.children))
        if not caption:
            return equation

        header = next((child for child in control.children if child.tag == TAG_LIST_HEADER), None)
        before = header is not None and len(header.payload) >= 12 and header.payload[8] & 0b11 in _CAPTION_BEFORE
        return f" {caption} {equation} " if before else f" {equation} {caption} "

    def _shape_runs(self, paragraph: Record, length: int) -> ShapeRuns | None:
        """The runs where the super/subscript or strikethrough mark changes (`ShapeRuns`), from the paragraph's PARA_CHAR_SHAPE (pairs of
        character position and character shape ID). Runs past the text length (`length`) are dropped. None for a paragraph with neither
        (Pinned by: test_hwp.py::TestSuperscript, TestStrikethrough). The pairs are read as an array rather than unpacked into tuples
        (same reason as `decode_para_text`)."""
        record = next((child for child in paragraph.children if child.tag == TAG_PARA_CHAR_SHAPE), None)
        if record is None or not (self._scripts or self._struck):
            return None
        values = array("I", record.payload[:len(record.payload) // 8 * 8])
        if sys.byteorder == "big":
            values.byteswap()
        starts, shape_ids = values[0::2], values[1::2]
        if not any(shape_id in self._scripts or shape_id in self._struck for shape_id in shape_ids):
            return None

        # If the positions always increase, use them as is; otherwise sort by (position, ID) — at the same position the later one (larger ID) wins.
        increasing = all(before < after for before, after in zip(starts, starts[1:], strict=False))
        runs: ShapeRuns = []
        for start, shape_id in zip(starts, shape_ids, strict=True) if increasing else sorted(zip(starts, shape_ids, strict=True)):
            if start >= length:
                break
            if runs and runs[-1][0] == start:
                runs.pop()
            shape = (self._scripts.get(shape_id), shape_id in self._struck)
            if (runs[-1][1] if runs else (None, False)) != shape:
                runs.append((start, shape))
        return runs

    def _object_markers(self, control: Record) -> list[str]:
        """Picture record inside a drawing object (gso) → DocInfo BIN_DATA index → BinData stream. An OLE object record leaves only a
        placeholder — it used to vanish without a trace, so there was no telling what was missing (kept even when picture extraction is
        off. Pinned by: test_hwp.py::TestEmbeddedObjects).

        Paragraphs inside a text box (PARA_HEADER) are not descended into. A picture inside one is put in place when that paragraph is
        rendered. Descending registered the picture twice and placed it at the outer paragraph
        (Pinned by: test_hwp.py::TestHwpObjects::test_picture_inside_text_box_is_registered_once_where_it_is).
        """
        pictures = self._options.extract_images and self._load_bin is not None
        markers = []
        for record in _flatten(control.children, stop=TAG_PARA_HEADER):
            if record.tag == TAG_SHAPE_OLE:
                self.unread_ole += 1
                markers.append(f" {_OLE_MARK} ")
            if not pictures or record.tag != TAG_SHAPE_PICTURE or len(record.payload) < _PICTURE_BIN_ID_OFFSET + 2:
                continue
            (item_id,) = struct.unpack_from("<H", record.payload, _PICTURE_BIN_ID_OFFSET)
            data = self._load_bin(item_id)
            if data:
                markers.append(f" {self._extraction.add_image(data)} ")
        return markers

    def _decorate(self, paragraph: Record, text: str) -> str:
        """Attach the paragraph head (`_lists.paragraph_head`) and, for an outline style, the heading mark to the paragraph's first text piece."""
        mark, level = self._paragraph_head(paragraph)
        style_id = paragraph.payload[10] if len(paragraph.payload) > 10 else -1
        style_name = self._styles[style_id] if 0 <= style_id < len(self._styles) else ""
        shape_id = struct.unpack_from("<H", paragraph.payload, 8)[0] if len(paragraph.payload) >= 10 else -1
        margin = self._heads.margins[shape_id] if 0 <= shape_id < len(self._heads.margins) else 0
        return decorated_text(text, mark, level, style_name, margin, self._bullets[-1], in_cell=bool(self._cell_depth))

    def _paragraph_head(self, paragraph: Record) -> tuple[str, int]:
        """Paragraph head of the paragraph shape → (number "1." / "가." or bullet "-", level). ("", 0) if the definition is missing or
        broken. The number counters are advanced here
        (Pinned by: test_hwp.py::TestParagraphNumbering)."""
        shape_id = struct.unpack_from("<H", paragraph.payload, 8)[0] if len(paragraph.payload) >= 10 else -1
        if not 0 <= shape_id < len(self._heads.shapes):
            return "", 0
        head_type, level, numbering_id = self._heads.shapes[shape_id]
        if head_type == _HEAD_BULLET:
            return ("-", level) if 1 <= numbering_id <= self._heads.bullet_count else ("", 0)
        if head_type == _HEAD_OUTLINE:
            numbering_id = self._outline_numbering
        elif head_type != _HEAD_NUMBER:
            return "", 0
        if not 1 <= numbering_id <= len(self._heads.numberings):
            return "", 0
        key = (head_type, numbering_id)
        if key not in self._numberings:
            self._numberings[key] = Numbering(self._heads.numberings[numbering_id - 1])
        return self._numberings[key].advance(level), level

    def _render_note(self, control: Record, buffer: list[str]) -> None:
        if not self._options.include_footnotes:
            return
        self._note_count += 1
        self._grids.append(None)  # footnote text is not cell text — handing its table to the outer cell leaks the placeholder into the footnote
        self._note_depth += 1
        try:
            note = " ".join(" ".join(block.split()) for block in self.render(control.children))
        finally:
            self._note_depth -= 1
            self._grids.pop()
        buffer.append(f"[^{self._note_count}]")
        self.notes.append(f"[^{self._note_count}]: {note}")

    def _collect_header_footer(self, control: Record) -> None:
        if not self._options.include_headers_footers:
            return
        self._grids.append(None)
        try:
            text = join_blocks(self.render(control.children))
        finally:
            self._grids.pop()
        if text:
            self.headers_footers.setdefault(text)

    def _render_table(self, control: Record) -> str:
        """Table: under CTRL_HEADER(tbl) comes TABLE, followed, for each cell, by a LIST_HEADER and that cell's paragraphs as siblings.

        A LIST_HEADER before TABLE is the caption.
        """
        table = next((child for child in control.children if child.tag == TAG_TABLE), None)
        row_count, col_count = struct.unpack_from("<HH", table.payload, 4) if table and len(table.payload) >= 8 else (0, 0)

        # Caption paragraphs, like cell paragraphs, come as siblings of the LIST_HEADER, not its children
        # (Pinned by: test_hwp.py::TestHwpObjects::test_table_caption_is_kept_before_the_table).
        caption: list[Record] = []
        cells: list[tuple[Record, list[Record]]] = []
        seen_table = False
        for child in control.children:
            if child.tag == TAG_TABLE:
                seen_table = True
            elif child.tag == TAG_LIST_HEADER:
                if seen_table:
                    cells.append((child, list(child.children)))
                else:
                    caption.extend(child.children)
            elif child.tag == TAG_PARA_HEADER:
                if cells:
                    cells[-1][1].append(child)
                elif not seen_table:
                    caption.append(child)

        grid = TableGrid()
        # The top-level table checks the character limit while reading cells (`_notation.TableBudget`). Once exceeded, the remaining cells
        # are not read.
        budget = None if self._grids or self._cell_depth or self._note_depth else TableBudget(self._extraction, self._options)
        outer = self._grids[-1] if self._grids else None
        self._grids.append(grid)
        self._cell_depth += 1
        try:
            for position, (header, paragraphs) in enumerate(cells):
                text = "\n".join(self.render(paragraphs))
                columns = max(col_count, 1)
                row, col, row_span, col_span = (_cell_address(header.payload, row_count, col_count)
                                                or (position // columns, position % columns, 1, 1))
                grid.place_or_next(row, col, text, row_span=row_span, col_span=col_span)
                if budget is not None and budget.add(row, col, text, row_span=row_span, col_span=col_span):
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
        caption_text = join_blocks(self.render(caption))
        if self._note_depth:
            return join_blocks([caption_text, note_table(grid.rows())])
        if outer is not None:
            return join_blocks([caption_text, outer.embed(grid)])
        if self._cell_depth:
            return join_blocks([caption_text, grid.flatten()])
        rendered = grid.render()
        self.omitted_rows += grid.omitted_rows()
        return join_blocks([caption_text, rendered])


def _cell_address(payload: bytes, row_count: int, col_count: int) -> tuple[int, int, int, int] | None:
    """(row, column, row span, column span) of a cell LIST_HEADER.

    The spec document says it comes 6 bytes in, after the paragraph count (INT16) + attribute (UINT32), but real files have 2 more bytes
    so it is 8 bytes in
    (pyhwp and hwp.js also use 8). Both are validated against the grid range and the one that fits is used; if both are wrong, None (fill
    in order) is returned.
    """
    for offset in (8, 6):
        if len(payload) < offset + 8:
            continue
        col, row, col_span, row_span = struct.unpack_from("<HHHH", payload, offset)
        if (row_count == 0 or row < row_count) and (col_count == 0 or col < col_count) \
                and 1 <= col_span <= max(col_count, 1) and 1 <= row_span <= max(row_count, 1):
            return row, col, row_span, col_span
    return None


def _equation_script(control: Record) -> str:
    record = next((child for child in control.children if child.tag == TAG_EQEDIT), None)
    if record is None or len(record.payload) < 6:
        return ""
    (length,) = struct.unpack_from("<H", record.payload, 4)
    script = record.payload[6:6 + length * 2].decode("utf-16-le", errors="replace")
    return " ".join(script.split())


def _auto_number(control: Record) -> str:
    """Auto-number control (atno) → the number in a caption's "표 1" ("Table 1"). After the control ID come attribute (4; kind in bits 0-3,
    number shape in bits 4-11) + number (2) + user symbol, prefix and suffix characters (2×3). The number value is in the file, so it is
    not counted. The structure was confirmed with real Hangul files (table and picture captions, footnote "<1>" and endnote "1)" in
    public hwplib samples) (Pinned by: test_hwp.py::TestAutoNumber)."""
    if len(control.payload) < 16:
        return ""
    attribute, number, _, prefix, suffix = struct.unpack_from("<IHHHH", control.payload, 4)
    if attribute & 0xF not in _AUTO_NUMBER_KINDS:
        return ""
    shape = (attribute >> 4) & 0xFF
    text = format_number(number, NUMBER_SHAPES[shape] if shape < len(NUMBER_SHAPES) else "DIGIT")
    # Half of a surrogate pair (0xD800-0xDFFF) is dropped: it is not a character, and the result could not be encoded as UTF-8
    # (same as RTF `\uN`. Pinned by: test_audit_hangul.py::TestHwpAutoNumberDecoration).
    prefix, suffix = (chr(code) if code and not 0xD800 <= code <= 0xDFFF else "" for code in (prefix, suffix))
    return f"{prefix}{text}{suffix}"


def _dutmal_text(control: Record) -> str:
    """Ruby text (dutmal) control → "main text(ruby text)". The main text is not in the paragraph text either but inside the control
    (after the control ID: length + characters, main text then ruby text), so even the main text was dropped entirely. The structure was
    confirmed with real Hangul files (public hwplib samples) (Pinned by: test_hwp.py::TestDutmal)."""
    texts = _control_strings(control, 2)
    return format_dutmal(*texts) if texts else ""


def _control_strings(control: Record, count: int) -> list[str]:
    """Up to the first `count` strings that follow the control ID as (length WORD + characters). Ruby text (`tdut`) and composed
    characters (`tcps`, where border, size and character shape IDs follow the overlapped characters) have this shape. Composed
    characters were confirmed with a public hwplib sample ('글자겹침.hwp')."""
    texts = []
    offset = 4
    for _ in range(count):
        if offset + 2 > len(control.payload):
            break
        (length,) = struct.unpack_from("<H", control.payload, offset)
        texts.append(control.payload[offset + 2:offset + 2 + length * 2].decode("utf-16-le", errors="replace").strip())
        offset += 2 + length * 2
    return texts


def parse_styles(doc_info: list[Record]) -> list[str]:
    """Extract the list of style names (index = style ID) from the STYLE records of DocInfo. The DocInfo functions take the flattened
    record list — parsing the stream again in each function would spend the record limit five times."""
    names = []
    for record in doc_info:
        if record.tag != TAG_STYLE or len(record.payload) < 2:
            continue
        (length,) = struct.unpack_from("<H", record.payload, 0)
        names.append(record.payload[2:2 + length * 2].decode("utf-16-le", errors="replace"))
    return names


def parse_char_scripts(doc_info: list[Record]) -> dict[int, str]:
    """CHAR_SHAPE records of DocInfo (index is the character shape ID) that are superscript or subscript → mark."""
    scripts: dict[int, str] = {}
    shapes = (record for record in doc_info if record.tag == TAG_CHAR_SHAPE)
    for shape_id, record in enumerate(shapes):
        if len(record.payload) < _CHAR_SHAPE_ATTRIBUTE_OFFSET + 4:
            continue
        (attribute,) = struct.unpack_from("<I", record.payload, _CHAR_SHAPE_ATTRIBUTE_OFFSET)
        if attribute & _CHAR_SHAPE_SUPERSCRIPT:
            scripts[shape_id] = SUPERSCRIPT
        elif attribute & _CHAR_SHAPE_SUBSCRIPT:
            scripts[shape_id] = SUBSCRIPT
    return scripts


def parse_struck_shapes(doc_info: list[Record]) -> frozenset[int]:
    """CHAR_SHAPE records of DocInfo (index is the character shape ID) that are struck through: flag value 1 and a line-type shape (0-10)."""
    struck = set()
    shapes = (record for record in doc_info if record.tag == TAG_CHAR_SHAPE)
    for shape_id, record in enumerate(shapes):
        if len(record.payload) < _CHAR_SHAPE_ATTRIBUTE_OFFSET + 4:
            continue
        (attribute,) = struct.unpack_from("<I", record.payload, _CHAR_SHAPE_ATTRIBUTE_OFFSET)
        if (attribute >> _CHAR_SHAPE_STRIKEOUT_SHIFT) & _CHAR_SHAPE_STRIKEOUT_MASK == 1 and \
                (attribute >> _CHAR_SHAPE_STRIKEOUT_SHAPE_SHIFT) & _CHAR_SHAPE_STRIKEOUT_SHAPE_MASK in _STRIKEOUT_LINE_SHAPES:
            struck.add(shape_id)
    return frozenset(struck)


def parse_paragraph_heads(doc_info: list[Record]) -> ParagraphHeads:
    """PARA_SHAPE, NUMBERING and BULLET records of DocInfo (index is the ID; paragraph numberings and bullets start at 1). A broken
    record is treated as having no head.

    PARA_SHAPE: attribute 1 (head kind in bits 23-24, level in bits 25-27) + 6 INT32s (the first is the left margin) + tab ID (2) +
    numbering/bullet ID (2, at byte 30).
    NUMBERING: for each of 7 levels, 12 bytes of paragraph head information (number shape in attribute bits 5-8) + a format string
    (^n in "^1." is the number of level n),
    then the start number (2) and, from 5.0.2.5 on, per-level start numbers (4×7). The number shape values (0 digits, 1 circled digits,
    8 가나다 (Hangul syllables)) were confirmed with real files holding Hangul's default
    numbering definition (1. 가. 1) 가) (1) (가) ①) (public hwplib samples and Hangul form documents).
    """
    heads = ParagraphHeads()
    for record in doc_info:
        payload = record.payload
        if record.tag == TAG_PARA_SHAPE:
            if len(payload) >= 32:
                (properties,) = struct.unpack_from("<I", payload, 0)
                (numbering_id,) = struct.unpack_from("<H", payload, 30)
                heads.shapes.append(((properties >> 23) & 0x3, (properties >> 25) & 0x7, numbering_id))
                heads.margins.append(struct.unpack_from("<i", payload, 4)[0])
            else:
                heads.shapes.append((0, 0, 0))
                heads.margins.append(0)
        elif record.tag == TAG_NUMBERING:
            heads.numberings.append(_numbering_levels(payload))
        elif record.tag == TAG_BULLET:
            heads.bullet_count += 1
    return heads


def _numbering_levels(payload: bytes) -> list[NumberLevel]:
    offset = 0
    formats: list[tuple[str, str]] = []
    for _ in range(7):
        if offset + 14 > len(payload):
            return []
        (properties,) = struct.unpack_from("<I", payload, offset)
        (length,) = struct.unpack_from("<H", payload, offset + 12)
        offset += 14
        if offset + length * 2 > len(payload):
            return []
        template = payload[offset:offset + length * 2].decode("utf-16-le", errors="replace")
        offset += length * 2
        shape = (properties >> 5) & 0xF
        formats.append((template, NUMBER_SHAPES[shape] if shape < len(NUMBER_SHAPES) else "DIGIT"))

    # If per-level start numbers (4×7) follow the start number (2), use them (otherwise 1). 0 is taken as 1.
    starts = [1] * 7
    if offset + 2 + 28 <= len(payload):
        starts = [value or 1 for value in struct.unpack_from("<7I", payload, offset + 2)]
    return [NumberLevel(template, shape, start) for (template, shape), start in zip(formats, starts, strict=True)]


def parse_bin_items(doc_info: list[Record]) -> list[tuple[str | None, bool | None]]:
    """DocInfo BIN_DATA records → [(stream name, compressed or not)]. The index is the BinItem ID.

    There is a stream only when the low 4 bits of the attribute are 1 (EMBEDDING) (0 LINK is an external file, 2 STORAGE an OLE object).
    Compression is in attribute bits 4-5: 0 means the document default (None), 1 compressed, 2 uncompressed.
    """
    items: list[tuple[str | None, bool | None]] = []
    for record in doc_info:
        if record.tag != TAG_BIN_DATA or len(record.payload) < 2:
            continue
        (properties,) = struct.unpack_from("<H", record.payload, 0)
        compress = {1: True, 2: False}.get((properties >> 4) & 0x3)
        if properties & 0xF != 1 or len(record.payload) < 6:
            items.append((None, compress))
            continue
        bin_id, ext_length = struct.unpack_from("<HH", record.payload, 2)
        extension = record.payload[6:6 + ext_length * 2].decode("utf-16-le", errors="replace")
        items.append((f"BinData/BIN{bin_id:04X}.{extension}", compress))
    return items


def _text_arts(control: Record) -> list[str]:
    """Text of the text art records under a drawing object. Paragraphs inside a text box are not descended into (same as
    `_object_markers`). A broken record whose length runs past the record skips just that text art."""
    texts = []
    for record in _flatten(control.children, stop=TAG_PARA_HEADER):
        if record.tag != TAG_SHAPE_TEXTART or len(record.payload) < _TEXTART_TEXT_OFFSET + 2:
            continue
        (length,) = struct.unpack_from("<H", record.payload, _TEXTART_TEXT_OFFSET)
        raw = record.payload[_TEXTART_TEXT_OFFSET + 2:_TEXTART_TEXT_OFFSET + 2 + length * 2]
        if len(raw) != length * 2:
            continue
        text = _TEXTART_BREAKS.sub("\n", raw.decode("utf-16-le", errors="replace")).strip()
        if text:
            texts.append(text)
    return texts


def _flatten(records: list[Record], stop: int | None = None) -> list[Record]:
    """Flatten a record tree in pre-order (parent → children). A record with the `stop` tag is skipped together with everything under
    it. Runs on a stack — run by recursion, a 1,000-level record chain in DocInfo or a drawing object rejected the whole document with
    RecursionError
    (Pinned by: test_hwp_robustness.py::TestDeepRecordChains)."""
    flat = []
    stack = [iter(records)]
    while stack:
        record = next(stack[-1], None)
        if record is None:
            stack.pop()
        elif record.tag != stop:
            flat.append(record)
            stack.append(iter(record.children))
    return flat


@dataclass(slots=True)
class _InflateBudget:
    """Total bytes that may be decompressed for one document. Applies the same limit as the `max_uncompressed_bytes` check of the ZIP
    family to HWP streams.

    HWP streams are raw deflate, so the size is unknown before decompressing. Without a cap, 284KB becomes 300MB
    (Pinned by: test_hwp.py::test_compressed_stream_is_bounded_by_uncompressed_limit).
    """

    remaining: int


def _read_stream(ole: olefile.OleFileIO, name: str, compressed: bool, budget: _InflateBudget) -> bytes:
    return _read_stream_to_end(ole, name, compressed, budget)[0]


def _read_stream_to_end(ole: olefile.OleFileIO, name: str, compressed: bool,
                        budget: _InflateBudget) -> tuple[bytes, bool]:
    """Stream bytes and whether it was read to the end. zlib.error if the compression is broken."""
    raw = ole.openstream(name).read()
    if not compressed:
        return raw, True

    # decompressobj raises no exception on a stream with a truncated end, which salvages what was read (eof is false if the end marker
    # was not reached). One byte more than the limit is decompressed to notice an overrun.
    inflater = zlib.decompressobj(-15)
    data = inflater.decompress(raw, budget.remaining + 1)
    budget.remaining -= len(data)
    if budget.remaining < 0:
        raise DocumentTooLargeError("The document becomes too large when decompressed.")
    return data, inflater.eof


def _section_streams(ole: olefile.OleFileIO) -> list[str]:
    sections = [entry for entry in ole.listdir() if len(entry) == 2 and entry[0] == "BodyText"
                and entry[1].startswith("Section") and entry[1][7:].isdecimal()]
    return ["/".join(entry) for entry in sorted(sections, key=lambda entry: int(entry[1][7:]))]


def extract_hwp(data: bytes, options: ConvertOptions) -> Extraction:
    try:
        ole = olefile.OleFileIO(BytesIO(data))
    except OSError as exc:
        raise CorruptedDocumentError("The HWP document structure could not be read.") from exc

    with ole:
        header = ole.openstream("FileHeader").read()
        if len(header) < 40 or not header.startswith(b"HWP Document File"):
            raise CorruptedDocumentError("The HWP file header is invalid.")
        (flags,) = struct.unpack_from("<I", header, 36)
        if flags & _FLAG_PASSWORD:
            raise EncryptedDocumentError("The HWP document is password-protected. Please remove the password and upload it again.")

        compressed = bool(flags & _FLAG_COMPRESSED)
        preview = _preview_text(ole)
        metadata = _summary_metadata(ole)
        if flags & _FLAG_DISTRIBUTION:
            return _distribution_fallback(preview, metadata)
        if flags & _FLAG_PROTECTED:
            raise EncryptedDocumentError("The HWP document is protected by DRM or certificate encryption, so its body cannot be read. "
                                         "Please save it as a regular document and upload it again.")

        budget = _InflateBudget(options.max_uncompressed_bytes)
        records = _RecordBudget(options.max_uncompressed_bytes // _BYTES_PER_RECORD_BUDGET)
        extraction = Extraction(metadata=metadata)
        # DocInfo holds styles, numbering, character shapes and the picture list, not text. When its compression is broken the
        # body is read without them, with a warning — one broken DocInfo rejected documents whose sections were fine (the
        # same rule as HWPX header.xml. Pinned by: test_hwp_robustness.py::TestBrokenDocInfo).
        try:
            doc_info_stream = _read_stream(ole, "DocInfo", compressed, budget) if ole.exists("DocInfo") else b""
        except zlib.error:
            doc_info_stream = b""
            extraction.warnings.append("The document style information (DocInfo) could not be read, so the document was converted "
                                       "without headings, numbering, character formatting and pictures.")
        doc_info = _flatten(parse_records(doc_info_stream, records))
        bin_items = parse_bin_items(doc_info)

        loaded: dict[int, bytes | None] = {}

        def load_bin(item_id: int) -> bytes | None:
            """A picture stream is read only once. Reading and decompressing the same picture again per reference took 150 seconds for a
            document with 16,384 references to one picture, and the decompression limit was also cut per reference. A picture whose
            compression is broken skips just that picture (exceeding the limit is a problem of the whole document, so it is raised as is)
            (Pinned by: test_image.py::TestHwpRepeatedAndBrokenPictures)."""
            if item_id not in loaded:
                # A BinItem ID is the 1-based index of the DocInfo BIN_DATA records.
                name, compress = bin_items[item_id - 1] if 1 <= item_id <= len(bin_items) else (None, None)
                try:
                    loaded[item_id] = (_read_stream(ole, name, compressed if compress is None else compress, budget)
                                       if name and ole.exists(name) else None)
                except zlib.error:
                    loaded[item_id] = None
            return loaded[item_id]

        renderer = HwpRenderer(parse_styles(doc_info), options, extraction, load_bin, parse_paragraph_heads(doc_info),
                               parse_char_scripts(doc_info), parse_struck_shapes(doc_info))
        blocks = []
        names = _section_streams(ole)
        broken: list[zlib.error] = []
        for number, name in enumerate(names, 1):
            # If the compression of one section stream is broken, only that section is skipped, and if its end is truncated it is used
            # as far as it was read (both with a warning). The whole document used to be rejected even when Section0 was fine. If every
            # section is broken it is a corrupted document as before (Pinned by: test_hwp_robustness.py::
            # TestBrokenSectionStream). The number in the warning is the position in the body, as for HWPX, not the number in
            # the stream name: up to 24 digits chosen by the document reached the warning
            # (Pinned by: test_audit_hangul.py::TestHwpSectionWarningNumber).
            try:
                stream, complete = _read_stream_to_end(ole, name, compressed, budget)
            except zlib.error as exc:
                broken.append(exc)
                extraction.warnings.append(f"Section {number}: the compressed data is broken and could not be read.")
                continue
            if not complete:
                extraction.warnings.append(f"Section {number}: the stream is truncated, so only the part that could be read was converted.")

            # Read per top-level record (paragraph) and count characters. Once the limit is exceeded, the remaining paragraphs and
            # sections are not read.
            for record in iter_records(stream, records):
                rendered = renderer.render([record])
                blocks.extend(rendered)
                if extraction.count(sum(solid_length(block) for block in rendered), options):
                    break
            if extraction.exceeded:
                break
        if names and len(broken) == len(names):
            raise broken[0]

    # The preview replaces the body only when parsing the body finished and there is no text. A document without text (empty table,
    # shapes only) has an empty preview too
    # (`_preview_text`), so the result stays empty (Pinned by: test_hwp.py::TestPreviewFallback).
    body = join_blocks([*renderer.headers_footers, *blocks, "\n".join(renderer.notes)])
    if not body.strip() and preview:
        body = preview
        extraction.warnings.append("The body could not be parsed, so the preview text saved by Hancom Office was used instead "
                                   "(it may contain only the beginning).")
    if renderer.unread_ole:
        extraction.warnings.append(f"The contents of {renderer.unread_ole} OLE object(s) (pasted tables, charts, etc.) could not be converted.")
    if renderer.too_deep:
        extraction.warnings.append(f"Tables or text boxes are nested too deeply, so {renderer.too_deep} inner part(s) were not read.")
    if renderer.omitted_rows:
        extraction.warnings.append(omitted_rows_warning(renderer.omitted_rows))
    extraction.sections.append(Section(SectionKind.DOCUMENT, 1, body))
    return extraction


def _distribution_fallback(preview: str, metadata: dict[str, str]) -> Extraction:
    """A distribution document has its body (ViewText) encrypted. Copy protection is not bypassed; only the preview text is used (deliberate)."""
    if not preview:
        raise EncryptedDocumentError("The body of a distribution (copy-protected) HWP document cannot be read. "
                                     "Please save it as a regular document and upload it again.")
    return Extraction(
        sections=[Section(SectionKind.DOCUMENT, 1, preview)],
        metadata=metadata,
        warnings=["This is a distribution (copy-protected) document, so only the preview text (the beginning) was converted instead of the body."],
    )


def _preview_text(ole: olefile.OleFileIO) -> str:
    """The preview text saved by Hancom Office. It writes table cells as `<cell text>`, so if no text remains after removing the cell marks
    (`<`, `>`) the preview is considered absent. Used as is, a document without text emitted `<>` noise as its body
    (Pinned by: test_hwp.py::TestPreviewFallback)."""
    if not ole.exists("PrvText"):
        return ""
    text = ole.openstream("PrvText").read().decode("utf-16-le", errors="replace").strip()
    return text if text.translate(PREVIEW_CELL_MARKS).strip() else ""


def _summary_metadata(ole: olefile.OleFileIO) -> dict[str, str]:
    """HWP keeps summary information not in the standard SummaryInformation but in a property set with its own name (ID 2 title, 4 author).

    olefile's `getproperties` is not used. It trusts the value count as is, and a 21KB file with 8 bytes changed to FF hung looping
    4.3 billion times
    (Pinned by: test_hwp.py::TestHwpSummaryInformation). Only the two needed strings are read directly within the stream size, and
    anything out of range is dropped.
    """
    name = "\x05HwpSummaryInformation"
    if not ole.exists(name):
        return {}
    try:
        stream = ole.openstream(name).read(_SUMMARY_MAX_BYTES)
    except OSError:
        return {}  # summary information is supplementary. Even if broken it must not block converting the body

    properties = _summary_strings(stream)
    metadata = {}
    for key, property_id in (("title", 2), ("author", 4)):
        value = properties.get(property_id, "").strip("\x00 ").strip()
        if value:
            metadata[key] = value
    return metadata


def _summary_strings(stream: bytes) -> dict[int, str]:
    """Extract only the string properties (VT_LPSTR, VT_LPWSTR) from the first section of a property set stream (MS-OLEPS). Other types
    and out-of-range values are skipped."""
    if len(stream) < 48:
        return {}
    section = int.from_bytes(stream[44:48], "little")
    if section + 8 > len(stream):
        return {}
    count = min(int.from_bytes(stream[section + 4:section + 8], "little"), (len(stream) - section - 8) // 8)

    strings = {}
    for index in range(count):
        entry = section + 8 + index * 8
        property_id = int.from_bytes(stream[entry:entry + 4], "little")
        value_at = section + int.from_bytes(stream[entry + 4:entry + 8], "little")
        if value_at + 8 > len(stream):
            continue
        kind = int.from_bytes(stream[value_at:value_at + 4], "little")
        length = int.from_bytes(stream[value_at + 4:value_at + 8], "little")
        if kind == 0x1F and value_at + 8 + length * 2 <= len(stream):
            strings[property_id] = stream[value_at + 8:value_at + 8 + length * 2].decode("utf-16-le", errors="replace")
        elif kind == 0x1E and value_at + 8 + length <= len(stream):
            strings[property_id] = stream[value_at + 8:value_at + 8 + length].decode("cp949", errors="replace")
    return strings
