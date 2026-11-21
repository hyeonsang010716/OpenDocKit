"""HWP 5.x (binary) → Markdown.

HWP 5.x is an OLE compound document. The body is a run of records in the `BodyText/Section{n}` streams (usually raw-deflate compressed),
and the records form a tree through a 4-byte header (tag 10 bits, level 10 bits, size 12 bits). Public spec: Hancom "Hangul Document
File Format 5.0".

pyhwp (hwp5txt) is AGPL and flattens tables into plain text, so it is not used. Only the records needed for text, tables, text boxes,
footnotes and outline headings are read directly.
"""

from array import array
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from io import BytesIO
import re
import struct
import sys
import zlib

from lxml import etree
import olefile

from opendockit.exceptions import CorruptedDocumentError, DocumentTooLargeError, EncryptedDocumentError
from opendockit.extractors._charts import embedded_table, render_chart
from opendockit.extractors._equation import render_equation
from opendockit.extractors._hancom_symbols import FRAME_GLYPHS, framed_number, hancom_symbols, unfold
from opendockit.extractors._hangul import (
    CommentRanges,
    ParagraphPieces,
    SizeHeadings,
    click_here_guide,
    command_link,
    decorated_text,
    drop_unfilled_guide,
    emphasis_pieces,
    form_caption,
    header_by_default,
    link_end,
    link_start,
    preview_has_words,
)
from opendockit.extractors._inline import SUBSCRIPT, SUPERSCRIPT, mark_script, mark_strike
from opendockit.extractors._lists import NUMBER_SHAPES, BulletNesting, Numbering, NumberLevel, format_number
from opendockit.extractors._notation import (
    TableBudget,
    chars_left,
    comment_line,
    format_dutmal,
    note_table,
    omitted_rows_warning,
)
from opendockit.extractors._ole import OleFile, OleStreams
from opendockit.extractors._package import decimal_int, parse_part
from opendockit.markdown import TableGrid, join_blocks, render_table, solid_length
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
# The base size (INT32, in 1/100 pt) just before the attribute (`parse_char_sizes`)
_CHAR_SHAPE_SIZE_OFFSET = 42
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
# A chart is an OLE object whose BinData stream (after a UINT32 size) is an OLE container holding the chart as OOXML XML in the
# `OOXMLChartContents` stream, the part HWPX keeps as Chart/chartN.xml (`HwpRenderer._ole_chart`)
_OLE_CHART_STREAM = "OOXMLChartContents"
# The UINT32 of a PARA_SHAPE that holds the paragraph's level 0-9 (`parse_paragraph_heads`)
_PARA_SHAPE_LEVEL_OFFSET = 54
# BIN_DATA kinds (low 4 bits of the attribute): a picture, an OLE object (`parse_bin_items`)
_BIN_EMBEDDING, _BIN_STORAGE = 1, 2
# The position of the BinItem ID in SHAPE_COMPONENT_OLE: attribute (4), extent (4 + 4)
_OLE_BIN_ID_OFFSET = 12
TAG_SHAPE_PICTURE = _HWPTAG_BEGIN + 69
# Position of the BinItem ID in SHAPE_COMPONENT_PICTURE: border (12) + image rectangle (32) + crop (16) + inner margin (8) +
# brightness, contrast and effect (3).
# Confirmed with three real Hangul files (docs/design.md).
_PICTURE_BIN_ID_OFFSET = 71
# Text art inside a drawing object (gso). The text is an HWP string (WORD length + UTF-16) after the vertex coordinates (INT32×8)
# (hwplib ForControlTextArt).
# Line breaks are written as CR/LF or, as in HWPX, as control picture symbols (␍␊).
TAG_SHAPE_TEXTART = _HWPTAG_BEGIN + 74
# Form object (`form` control: check box, radio button, combo box, edit box, push button). Its kind is 4 characters (`tbc+`)
# written twice, then a UINT32 and the properties as a string (WORD length + WCHARs) of `name:type:value` items: `set:N:` opens
# a group of the next N characters, `wstring:N:` is followed by N characters of text, `int:`/`bool:` by a value up to the next
# space (`_form_properties`)
TAG_FORM_OBJECT = _HWPTAG_BEGIN + 75
# A comment (memo): the field of its range has the command `MEMO/<shape>/<number>/<time>/<time>/<author>/…`; its text is a
# MEMO_LIST record (UINT32 number) among the children of a paragraph, followed by a LIST_HEADER and its paragraphs
# (`HwpRenderer._read_memos`)
TAG_MEMO_LIST = _HWPTAG_BEGIN + 77
_FORM_PROPERTIES_OFFSET = 12
_TEXTART_TEXT_OFFSET = 32
_TEXTART_BREAKS = re.compile("\r\n|\r|\n|␍␊|␍|␊")

# Control characters in PARA_TEXT (spec table 6). A char control takes 1 WCHAR; inline and extended controls take 8 WCHARs (16 bytes).
# Only extended controls have a CTRL_HEADER record each, and their order within the paragraph equals the order of the CTRL_HEADER children.
_EXTENDED_CONTROLS = {1, 2, 3, 11, 12, 14, 15, 16, 17, 18, 21, 22, 23}
_INLINE_CONTROLS = {4, 5, 6, 7, 8, 9, 19, 20}
# Code 24 is a soft hyphen, drawn only where Hangul breaks a line inside the word: Hancom's own PDF of a document reads
# `Wordprocessor` where the file holds `Word` 24 `processor`, and the HWPX Hangul saves of it holds `<hp:hypen/>` there. Written as
# `-`, it made a different word, so it is dropped like the other char controls without a character (Pinned by:
# test_hwp.py::TestSoftHyphen).
_CHAR_REPLACEMENTS = {9: "\t", 10: "\n", 30: " ", 31: " "}
# The inline control that ends a field (spec table 6). The field starts at an extended control (3) whose CTRL_HEADER names the
# field (`%hlk` a hyperlink, `%%me` a comment, `%clk` a click-here field …). The end does not say which field it closes; it is
# taken to close the last field opened.
FIELD_END = 4
# The field control (CTRL_HEADER of a field; hwplib ForControlField): control ID UINT32, attribute UINT32, other attribute BYTE,
# command (WORD length + WCHARs), instance id UINT32. The command of a hyperlink is `address;…` (`_hangul.command_link`).
_FIELD_COMMAND_OFFSET = 9

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


# The characters (UTF-16 units, two bytes each) from an even byte up to the first control character of 8 WCHARs (inline or
# extended: 1-9, 11, 12, 14-23). It goes two bytes at a time, so bytes spanning two characters (the '01 00' in 'Ā' 00 01 +
# '가' 00 AC) are never taken for a control, and possessively, so it keeps no backtracking state however long the text is. The
# char controls of one WCHAR stay in the text and are replaced after decoding (`_CHAR_CONTROLS`).
_UP_TO_WIDE_CONTROL = re.compile(rb"(?:[^\x01-\x09\x0b\x0c\x0e-\x17].|.[^\x00])*+", re.DOTALL)
# What the char controls (one WCHAR: 0, 10, 13 and 24-31) become in the text: a space, or nothing (a line break stays
# itself). Replaced with `str.replace`, one control at a time and only when the text holds one: `str.translate` goes character by
# character on Hangul text, 40 times slower.
_CHAR_CONTROLS = tuple((chr(unit), _CHAR_REPLACEMENTS.get(unit, "")) for unit in range(32)
                       if unit not in _EXTENDED_CONTROLS and unit not in _INLINE_CONTROLS and unit != 10)
_CHAR_CONTROL_TEXT = re.compile("[" + "".join(control for control, _ in _CHAR_CONTROLS) + "]")
# Characters that are nothing but char controls that are dropped (0, 13, 24-29).
_DROPPED_CONTROLS_ONLY = re.compile(rb"(?:[\x00\x0d\x18-\x1d]\x00)*+")
# Character shape runs of a paragraph: (start character position, (super/subscript mark, struck through or not, bold, italic)).
# Ascending by position, and neighboring runs have different values.
RunShape = tuple[str | None, bool, bool, bool]
ShapeRuns = list[tuple[int, RunShape]]
_PLAIN_RUN: RunShape = (None, False, False, False)


def decode_para_text(payload: bytes, shape_runs: ShapeRuns | None = None) -> list[str | int]:
    """Decode PARA_TEXT into a list of [string piece | extended control code | `FIELD_END`]. Tables, text boxes etc. go where the
    extended controls are, and a field ends where `FIELD_END` is.

    With `shape_runs`, text in super/subscript and strikethrough runs gets temporary marks (`mark_script`, the strike mark). Before the
    first run there is no mark.
    Text between control characters is gathered as bytes and decoded at once. Making a Python integer per character (tuple or list), one
    paragraph of 24 million characters used 1.9GB
    (Pinned by: test_hwp_robustness.py::TestHugeParagraphText). Text with the same mark is decoded as one piece even across control characters.
    The char controls of one WCHAR (line break, hyphen, fixed-width space, paragraph end …) are decoded with the text around them and
    replaced in the decoded text (`_CHAR_CONTROLS`): taken one at a time, a paragraph of line breaks made a bytes object per character
    again, and one of dropped controls a loop round per character (60 MB of line breaks in a 57 KB file: 13 s and 4.6 GB)
    (Pinned by: test_hwp_robustness.py::TestHugeParagraphText::test_paragraph_of_char_controls_is_decoded_in_one_go).
    """
    count = len(payload) // 2
    view = memoryview(payload)
    parts: list[str | int] = []
    pieces: list[bytes | memoryview] = []  # text being gathered now (UTF-16 bytes)
    shape: RunShape = _PLAIN_RUN  # (script mark, struck through, bold, italic) of the text being gathered now
    runs = shape_runs or []
    run = -1  # index of the run covering the current position (-1 is before the first run)

    def flush() -> None:
        if pieces:
            script, struck, bold, italic = shape
            decoded = b"".join(pieces).decode("utf-16-le", errors="replace")
            pieces.clear()
            if _CHAR_CONTROL_TEXT.search(decoded):
                for control, replacement in _CHAR_CONTROLS:
                    if control in decoded:
                        decoded = decoded.replace(control, replacement)
            # Hancom's own symbols (the seal mark, boxed numbers, bullets) as text (`_hancom_symbols`)
            decoded = hancom_symbols(unfold(decoded))
            if decoded:  # nothing but controls that are dropped
                decoded = mark_script(decoded, script) if script else decoded
                parts.extend(emphasis_pieces(mark_strike(decoded) if struck else decoded, bold, italic))

    def add(start: int, stop: int, piece: bytes | None = None) -> None:
        """Gather the text at character positions [start, stop) (that one character if `piece` is given), split per run mark."""
        nonlocal shape, run
        while start < stop:
            while run + 1 < len(runs) and runs[run + 1][0] <= start:
                run += 1
            wanted = runs[run][1] if run >= 0 else _PLAIN_RUN
            end = min(stop, runs[run + 1][0]) if run + 1 < len(runs) else stop
            span = piece if piece is not None else view[2 * start:2 * end]
            if wanted != shape:
                if _DROPPED_CONTROLS_ONLY.fullmatch(span):  # leaves no text, so it does not split the run around it
                    start = end
                    continue
                flush()
                shape = wanted
            pieces.append(span)
            start = end

    index = 0
    while index < count:
        control = _UP_TO_WIDE_CONTROL.match(payload, 2 * index, 2 * count).end() // 2
        add(index, control)
        if control == count:
            break

        unit = payload[2 * control]
        if unit in _EXTENDED_CONTROLS:
            flush()
            parts.append(unit)
        elif unit == 9:
            add(control, control + 1, "\t".encode("utf-16-le"))
        elif unit == FIELD_END:
            flush()
            parts.append(unit)
        index = control + 8
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


@dataclass(slots=True)
class _ClickHere:
    """A click-here field opened in a paragraph: the pieces it was opened in, where its text starts there, its guide text, and how
    many click-here fields had been opened when it was (`HwpRenderer._clicks_opened`)."""

    pieces: list[str]
    start: int
    guide: str | None
    serial: int


@dataclass(slots=True)
class _Memo:
    """A comment field opened in a paragraph: its key in `HwpRenderer._memo_ranges`."""

    key: int


class HwpRenderer:
    def __init__(self, styles: list[str], options: ConvertOptions, extraction: Extraction | None = None,
                 load_bin: Callable[[int], bytes | None] | None = None, heads: ParagraphHeads | None = None,
                 scripts: dict[int, str] | None = None, struck: frozenset[int] = frozenset(),
                 sizes: list[int] | None = None, emphasis: dict[int, tuple[bool, bool]] | None = None) -> None:
        self._styles = styles
        # Character shape ID → (bold, italic) for the shapes that have either (parse_char_emphasis; empty without `keep_emphasis`)
        self._emphasis = emphasis or {}
        # Character shape ID → base size (parse_char_sizes), and the headings told by size (`_hangul.SizeHeadings`; the body
        # blocks go through `size_headings.resolve` once everything is read)
        self._sizes = sizes or []
        self.size_headings = SizeHeadings()
        # Character shape ID → super/subscript mark (parse_char_scripts), and the character shape IDs that are struck through
        # (parse_struck_shapes)
        self._scripts = scripts or {}
        self._struck = struck
        # Number of OLE objects whose content was not read and only a placeholder was left, number of inner contents not read because
        # they were nested too deeply (`_MAX_NESTING`), and number of rows dropped by the table grid cell limit
        # (`TableGrid.omitted_rows`). Reported as warnings after everything is read.
        self.unread_ole = 0
        # BinItem ID of a chart → the `[Chart] title` line later references to it give (`_ole_chart`)
        self._charts: dict[int, str | None] = {}
        # BinItem ID of an OLE object → its stream (`extract_hwp.load_ole`); None reads no OLE object
        self.load_ole: Callable[[int], bytes | None] | None = None
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
        # The fields opened and not yet ended, innermost last: the address of a hyperlink that is kept, a click-here field
        # (`_ClickHere`), else None (`_start_field`)
        self._fields: list[str | _ClickHere | _Memo | None] = []
        # Comments: (author, memo number, the pieces of its range) in the order of their fields; the text of each memo number
        # (`_read_memos`); the ranges not yet closed; 1 or more while a comment's text is rendered
        self.memos: list[tuple[str, int, list[str]]] = []
        self._memo_texts: dict[int, str] = {}
        self._memo_ranges = CommentRanges()
        self._memo_depth = 0
        self._clicks_opened = 0  # click-here fields opened so far: one opened inside another is not its guide (`_start_field`)

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
        size_pairs = self._count_sizes(paragraph, text_record)

        # The head goes on the first text piece only, and a paragraph without text keeps its number (`_hangul.ParagraphPieces`).
        pieces = ParagraphPieces(lambda text, plain, heading: self._decorate(paragraph, text, plain, heading, text_record, size_pairs))
        blocks, buffer, flush = pieces.blocks, pieces.buffer, pieces.flush
        if not self._memo_depth:
            self._memo_ranges.add(" ")  # between paragraphs of a comment range that spans several paragraphs

        for part in parts:
            if isinstance(part, str):
                buffer.append(part)
                if not self._memo_depth:
                    self._memo_ranges.add(part)
                continue
            if part == FIELD_END:
                field = self._fields.pop() if self._fields else None
                if isinstance(field, _Memo):
                    self._memo_ranges.close(field.key)
                elif isinstance(field, _ClickHere):
                    if field.pieces is buffer and field.serial == self._clicks_opened:
                        drop_unfilled_guide(buffer, field.start, field.guide)
                elif field is not None:
                    buffer.append(link_end(field))
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
                self._collect_header_footer(control, self.headers if kind == "head" else self.footers)
            elif kind == "gso ":
                # Text art text is a separate block, like hp:textart in HWPX. It was not read and was dropped entirely (Pinned by:
                # test_hwp_heads_and_shapes.py::
                # TestHwpTextArt).
                markers, charts = self._object_markers(control)
                buffer.extend(markers)
                inner = [*charts, *_text_arts(control), *self.render(control.children)]
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
                buffer.append(_composed_text(control))
            elif kind == "atno":
                buffer.append(_auto_number(control))
            elif kind == "form":
                # Form objects: the value is in the properties of the FORM_OBJECT record and was dropped, and a form of nothing
                # but form objects gave only its preview text (Pinned by: test_hwp.py::TestHwpFormObjects)
                value = _form_value(control)
                buffer.append(f" {value} " if value else "")
            elif kind.startswith("%"):
                self._start_field(kind, control, buffer)
        self._read_memos(paragraph)
        return pieces.finish(lambda: self._paragraph_head(paragraph), in_cell=bool(self._cell_depth))

    def _read_memos(self, paragraph: Record) -> None:
        """The text of the comments kept among the children of a paragraph (Hangul puts them under the last paragraph of the section):
        a MEMO_LIST record with the memo number, then a LIST_HEADER and the comment's paragraphs, up to the next MEMO_LIST. The text is
        the paragraphs on one line, rendered as the text of an HWPX comment is (`hwpx.HwpxRenderer._open_memo`); the first text of a
        number is kept."""
        if not any(child.tag == TAG_MEMO_LIST for child in paragraph.children):
            return
        groups: list[tuple[int, list[Record]]] = []
        for child in paragraph.children:
            if child.tag == TAG_MEMO_LIST:
                groups.append((struct.unpack_from("<I", child.payload)[0] if len(child.payload) >= 4 else -1, []))
            elif groups and child.tag == TAG_PARA_HEADER:
                groups[-1][1].append(child)
        for number, paragraphs in groups:
            self._grids.append(None)
            self._memo_depth += 1
            try:
                with self._own_numbering():
                    text = " ".join(" ".join(block.split()) for block in self.render(paragraphs))
            finally:
                self._memo_depth -= 1
                self._grids.pop()
            self._memo_texts.setdefault(number, text)

    def memo_lines(self) -> str:
        """The comment lines written after the notes: `> Comment(author, on "range"): text`, as in HWPX (Pinned by:
        test_hwp.py::TestMemo)."""
        return "\n".join(comment_line(author, text, "".join(target).strip()) for author, number, target in self.memos
                         if (text := self._memo_texts.get(number, "")))

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
        if record is None or not (self._scripts or self._struck or self._emphasis):
            return None
        values = array("I", record.payload[:len(record.payload) // 8 * 8])
        if sys.byteorder == "big":
            values.byteswap()
        starts, shape_ids = values[0::2], values[1::2]
        if not any(shape_id in self._scripts or shape_id in self._struck or shape_id in self._emphasis for shape_id in shape_ids):
            return None

        # If the positions always increase, use them as is; otherwise by position — at the same position the larger ID wins, as it
        # did sorted by (position, ID). Only positions inside the text are kept before ordering them: sorted as tuples, 10 million
        # pairs in a 114 KB section past a one-character text took 3 s and 1 GB
        # (Pinned by: test_hwp_robustness.py::TestHugeParagraphText::test_unordered_shape_positions_past_the_text_are_not_sorted).
        increasing = all(before < after for before, after in zip(starts, starts[1:], strict=False))
        pairs: Iterable[tuple[int, int]]
        if increasing:
            pairs = zip(starts, shape_ids, strict=True)
        else:
            last: dict[int, int] = {}
            for start, shape_id in zip(starts, shape_ids, strict=True):
                if start < length and shape_id > last.get(start, -1):
                    last[start] = shape_id
            pairs = sorted(last.items())
        runs: ShapeRuns = []
        for start, shape_id in pairs:
            if start >= length:
                break
            if runs and runs[-1][0] == start:
                runs.pop()
            shape = (self._scripts.get(shape_id), shape_id in self._struck, *self._emphasis.get(shape_id, (False, False)))
            if (runs[-1][1] if runs else _PLAIN_RUN) != shape:
                runs.append((start, shape))
        return runs

    def _object_markers(self, control: Record) -> tuple[list[str], list[str]]:
        """Picture record inside a drawing object (gso) → DocInfo BIN_DATA index → BinData stream; the placeholders of the pictures,
        and the charts among the OLE objects (`_ole_chart`) as blocks. Any other OLE object record leaves only a placeholder — it used
        to vanish without a trace, so there was no telling what was missing (kept even when picture extraction is off. Pinned by:
        test_hwp.py::TestEmbeddedObjects).

        Paragraphs inside a text box (PARA_HEADER) are not descended into. A picture inside one is put in place when that paragraph is
        rendered. Descending registered the picture twice and placed it at the outer paragraph
        (Pinned by: test_hwp.py::TestHwpObjects::test_picture_inside_text_box_is_registered_once_where_it_is).
        """
        pictures = self._options.extract_images and self._load_bin is not None
        markers: list[str] = []
        charts: list[str] = []
        for record in _flatten(control.children, stop=TAG_PARA_HEADER):
            if record.tag == TAG_SHAPE_OLE:
                chart = self._ole_chart(record)
                if chart is None:
                    self.unread_ole += 1
                    markers.append(f" {_OLE_MARK} ")
                elif chart:
                    charts.append(chart)
            if not pictures or record.tag != TAG_SHAPE_PICTURE or len(record.payload) < _PICTURE_BIN_ID_OFFSET + 2:
                continue
            (item_id,) = struct.unpack_from("<H", record.payload, _PICTURE_BIN_ID_OFFSET)
            data = self._load_bin(item_id)
            if data:
                markers.append(f" {self._extraction.add_image(data)} ")
        return markers, charts

    def _ole_chart(self, record: Record) -> str | None:
        """The chart of an OLE object (SHAPE_COMPONENT_OLE), by the renderer HWPX, DOCX and PPTX use: its BinData stream is a UINT32
        size and an OLE container whose `OOXMLChartContents` stream is the OOXML chart, the part the HWPX save of the same document
        keeps as Chart/chartN.xml. Hangul charts were left as `[OLE object]` in HWP while HWPX gave their values (Pinned by:
        test_hwp.py::TestHwpCharts). None when the object is not a chart or cannot be read (it stays a placeholder), '' for a chart
        without values.

        As in HWPX (`hwpx.HwpxRenderer._render_chart`): a chart is read and rendered once, later references give its
        `[Chart] title` line; inside a table cell its value table goes to the outer table; its value rows stop at the characters
        the output can still take, counted on their own in a note, header or footer."""
        if self.load_ole is None or len(record.payload) < _OLE_BIN_ID_OFFSET + 2:
            return None
        (item_id,) = struct.unpack_from("<H", record.payload, _OLE_BIN_ID_OFFSET)
        # What an object turned out to be is kept, a chart or not: opened and parsed again at each reference, 2,000 references
        # to one object holding 300,000 empty elements took 28 s (Pinned by: test_hwp.py::TestHwpCharts::
        # test_object_referenced_many_times_is_read_once)
        if item_id in self._charts:
            return self._charts[item_id]
        self._charts[item_id] = None
        data = self.load_ole(item_id)
        root = _chart_part(data) if data else None
        if root is None:
            return None
        self._charts[item_id] = ""
        outer = self._grids[-1] if self._grids else None
        table = embedded_table(outer) if outer is not None else render_table
        own = self._note_depth or any(grid is None for grid in self._grids)
        room = chars_left(Extraction() if own else self._extraction, self._options)
        rendered = render_chart(root, table, None, room, self._extraction.warnings)
        self._charts[item_id] = rendered.split("\n", 1)[0]
        return rendered

    def _start_field(self, kind: str, control: Record, buffer: list[str]) -> None:
        """A field starts. A hyperlink (`%hlk`) becomes `[text](address)` up to its `FIELD_END` under `keep_links`, with the address
        from the command of the field control (`_hangul.command_link`: http, https and mailto only), as in HWPX; every other field
        is only remembered, so that its end closes it and not a link (Pinned by: test_hwp.py::TestHyperlinks)."""
        url = None
        payload = control.payload
        command = ""
        if len(payload) >= _FIELD_COMMAND_OFFSET + 2:
            (length,) = struct.unpack_from("<H", payload, _FIELD_COMMAND_OFFSET)
            start = _FIELD_COMMAND_OFFSET + 2
            command = payload[start:start + 2 * length].decode("utf-16-le", errors="replace")
        if command.startswith("MEMO/"):
            # A comment: its range up to the field end goes on the comment line; its text comes later, at the end of the section
            # (`_read_memos`). The fields of Hangul's comments are `%unk` with this command
            parts = command.split("/")
            number = decimal_int(parts[2], -1) if len(parts) > 2 else -1
            author = parts[5] if len(parts) > 5 else ""
            key = len(self.memos)
            self.memos.append((" ".join(author.split()), number, self._memo_ranges.open(key) if not self._memo_depth else []))
            self._fields.append(_Memo(key))
            return
        if kind == "%clk":
            # A click-here field (누름틀): its text is dropped at its end when it is still the guide (`drop_unfilled_guide`), unless
            # another click-here field was opened inside it. Checked at every end, 40,000 fields opened in one paragraph and then
            # ended joined the text from each start again: 19 s (Pinned by: test_hwp.py::TestClickHereFields::
            # test_fields_opened_inside_each_other_take_linear_time)
            self._clicks_opened += 1
            self._fields.append(_ClickHere(buffer, len(buffer), click_here_guide(unfold(command)), self._clicks_opened))
            return
        # The text of a comment is plain, as in HWPX and DOCX
        if kind == "%hlk" and self._options.keep_links and command and not self._memo_depth:
            url = command_link(command)
        self._fields.append(url)
        if url is not None:
            buffer.append(link_start())

    def _decorate(self, paragraph: Record, text: str, plain: str, heading: Callable[[], str], text_record: Record | None,
                  size_pairs: list[tuple[int, int]]) -> str:
        """Attach the paragraph head (`_lists.paragraph_head`) and, for an outline style or head, the heading mark to the paragraph's
        first text piece. A top-level body paragraph (not in a cell, note, header, text box or caption) passes its smallest
        character size, to become a heading by size (`_hangul.SizeHeadings`)."""
        mark, level = self._paragraph_head(paragraph)
        style_id = paragraph.payload[10] if len(paragraph.payload) > 10 else -1
        style_name = self._styles[style_id] if 0 <= style_id < len(self._styles) else ""
        shape_id = struct.unpack_from("<H", paragraph.payload, 8)[0] if len(paragraph.payload) >= 10 else -1
        margin = self._heads.margins[shape_id] if 0 <= shape_id < len(self._heads.margins) else 0
        outline = 0 <= shape_id < len(self._heads.shapes) and self._heads.shapes[shape_id][0] == _HEAD_OUTLINE
        top = self._depth == 1 and not self._cell_depth and not self._note_depth
        size = _smallest_text_size(text_record.payload, size_pairs, self._sizes) if top and text_record is not None else 0
        return decorated_text(text, mark, level, style_name, margin, self._bullets[-1], in_cell=bool(self._cell_depth), plain=plain,
                              outline=outline, size=size, size_headings=self.size_headings, heading_text=heading, top=top,
                              in_note=bool(self._note_depth), no_heading=self._plain_header)

    def _count_sizes(self, paragraph: Record, text_record: Record | None) -> list[tuple[int, int]]:
        """Count the text characters of a paragraph per character size (`SizeHeadings.count`, which finds the body size; those
        of top-level body paragraphs apart, notes not at all) and return its character shape runs (`_size_pairs`). The 8 WCHARs
        of a wide control are not text and are skipped, as the characters of HWPX `hp:t` are all that HWPX counts."""
        if text_record is None or not self._sizes:
            return []
        payload = text_record.payload
        length = len(payload) // 2
        pairs = _size_pairs(paragraph, length)
        if not pairs or self._note_depth:
            return pairs
        body = self._depth == 1 and not self._cell_depth
        run = 0
        index = 0
        while index < length:
            control = _UP_TO_WIDE_CONTROL.match(payload, 2 * index, 2 * length).end() // 2
            while index < control:   # the text up to the control, run by run
                while run + 1 < len(pairs) and pairs[run + 1][0] <= index:
                    run += 1
                end = min(control, pairs[run + 1][0]) if run + 1 < len(pairs) else control
                shape_id = pairs[run][1]
                if 0 <= shape_id < len(self._sizes):
                    self.size_headings.count(self._sizes[shape_id], payload[2 * index:2 * end].decode("utf-16-le", errors="replace"),
                                             body=body)
                index = end
            index = control + 8
        return pairs

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
        """A footnote or endnote: `[^n]` in place and `[^n]: text` at the end. The number is taken once the text is known, so a
        note without text leaves neither reference nor definition and the notes after it stay consecutive; a note inside a note is
        left out, as in RTF and ODF — Markdown cannot reference it from a definition, and numbering it first gave the outer note's
        number to both (Pinned by: test_hwp.py::TestNotesInsideNotes)."""
        if not self._options.include_footnotes or self._note_depth:
            return
        self._grids.append(None)  # footnote text is not cell text — handing its table to the outer cell leaks the placeholder into the footnote
        self._note_depth += 1
        try:
            with self._own_numbering():
                note = " ".join(" ".join(block.split()) for block in self.render(control.children))
        finally:
            self._note_depth -= 1
            self._grids.pop()
        if not note.strip():
            return
        self._note_count += 1
        buffer.append(f"[^{self._note_count}]")
        self.notes.append(f"[^{self._note_count}]: {note}")

    @contextmanager
    def _own_numbering(self) -> Iterator[None]:
        """Paragraph numbers of a header, footer or note counted on their own: a numbered paragraph there advanced the body's
        counters, so the body's next item skipped a number (Pinned by: test_hwp.py::TestNumberingOutsideTheBody)."""
        saved, self._numberings = self._numberings, {}
        try:
            yield
        finally:
            self._numberings = saved

    def _collect_header_footer(self, control: Record, found: dict[str, None]) -> None:
        """Render a header or footer control into `found` (`headers` or `footers`). A header is counted against the character
        limit when it is first seen, because every header is written before the body; footers are counted after the body.
        Without `include_headers_footers` only headers that hold a word are kept (`_hangul.header_by_default`)."""
        chosen = self._options.include_headers_footers
        if not chosen and found is not self.headers:
            return
        pictures, notes, note_count, unread_ole = len(self._extraction.images), len(self.notes), self._note_count, self.unread_ole
        memos = len(self.memos)
        self._grids.append(None)
        self._plain_header = not chosen
        try:
            with self._own_numbering():
                text = join_blocks(self.render(control.children))
        finally:
            self._grids.pop()
            self._plain_header = False
        if not chosen:
            # Only the words of the header: the pictures it registered are taken back with their placeholders, and a header that
            # is not written takes back its notes, which would be left without a reference. Its OLE objects are taken back too: they
            # would warn of contents not converted that are not written (Pinned by:
            # test_hwp_headers.py::TestHeadersByDefault::test_object_of_a_header_not_written_does_not_warn)
            del self._extraction.images[pictures:]
            self.unread_ole = unread_ole
            text = header_by_default(text)
            if not text or text in found:
                del self.notes[notes:], self.memos[memos:]
                self._note_count = note_count
        elif text in found:
            # A header written once has its comments once, as in HWPX (Pinned by:
            # test_hwp.py::TestHwpMemo::test_comment_of_a_repeated_hwp_header_is_written_once)
            del self.memos[memos:]
        if text and text not in found:
            found[text] = None
            if found is self.headers:
                self._extraction.count(solid_length(text), self._options)

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
                row_span, col_span = grid.place_or_next(row, col, text, row_span=row_span, col_span=col_span)
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
            return join_blocks([caption_text, note_table(grid.rows(), self._options.max_chars)])
        if outer is not None:
            return join_blocks([caption_text, outer.embed(grid)])
        if self._cell_depth:
            return join_blocks([caption_text, grid.flatten(self._options.max_chars)])
        # A table without a budget (in a header or footer) is drawn with the whole limit as room: what it writes past that is
        # over the limit whatever comes before it (Pinned by:
        # test_office_table_char_limit.py::TestMergedCopiesPastTheLimit::test_hangul_header_table_stops_at_the_limit)
        rendered = grid.render(room=budget.room if budget is not None else self._options.max_chars)
        self.omitted_rows += grid.omitted_rows()
        return join_blocks([caption_text, rendered])


# A paragraph longer than this (in WCHARs, controls included) is not scanned for its character sizes: it is not a heading anyway
# (`_hangul.SizeHeadings` takes paragraphs of up to 80 characters)
_SIZE_SCAN_MAX = 512


def _size_pairs(paragraph: Record, length: int) -> list[tuple[int, int]]:
    """The character shape runs of a paragraph from its PARA_CHAR_SHAPE: (start position, character shape ID), the starts inside
    the text (`length` WCHARs). Only increasing starts, as Hangul writes them, are used — the runs of a paragraph that has them out
    of order give no size. At most `length` runs can start inside the text, so only that many are looked at, and a paragraph of
    millions of runs costs no more than its text (as in `_shape_runs`)."""
    record = next((child for child in paragraph.children if child.tag == TAG_PARA_CHAR_SHAPE), None)
    if record is None or length <= 0:
        return []
    values = array("I", record.payload[:min(len(record.payload) // 8, length + 1) * 8])
    if sys.byteorder == "big":
        values.byteswap()
    starts, shape_ids = values[0::2], values[1::2]
    if not starts or starts[0] != 0 or not all(before < after for before, after in zip(starts, starts[1:], strict=False)):
        return []
    return [(start, shape_id) for start, shape_id in zip(starts, shape_ids, strict=True) if start < length]


def _smallest_text_size(payload: bytes, pairs: list[tuple[int, int]], sizes: list[int]) -> int:
    """The smallest base size among the visible characters of a short paragraph (not whitespace, not controls); 0 when the
    paragraph is too long to be a heading, has no visible character, or one of them has no known size."""
    count = len(payload) // 2
    if not pairs or count > _SIZE_SCAN_MAX:
        return 0
    units = struct.unpack_from(f"<{count}H", payload)
    smallest = 0
    run = 0
    index = 0
    while index < count:
        unit = units[index]
        if unit < 32:   # a control: extended and inline controls take 8 WCHARs, char controls one
            index += 8 if unit in _EXTENDED_CONTROLS or unit in _INLINE_CONTROLS else 1
            continue
        if not chr(unit).isspace():
            while run + 1 < len(pairs) and pairs[run + 1][0] <= index:
                run += 1
            shape_id = pairs[run][1]
            size = sizes[shape_id] if 0 <= shape_id < len(sizes) else 0
            if size <= 0:
                return 0
            smallest = min(smallest, size) if smallest else size
        index += 1
    return smallest


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
    # Without the control characters that are not white space: U+0002-U+0006 are the temporary script and strike marks, and a
    # crafted script `\x05a+b\x06 c` wrote `$~~a+b~~ c$` (Pinned by: test_hwp.py::TestEquations::
    # test_hwp_equation_script_cannot_forge_strike_or_script_marks). White space separates terms, as before.
    return " ".join(script.translate(_SCRIPT_CONTROLS).split())


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
    # A control character (below U+0020) is no prefix or suffix: U+0002-U+0006 are the temporary script and strike marks, and
    # a crafted one wrote `~~3~~` (Pinned by: test_hwp.py::TestHyperlinks::test_hwp_control_text_cannot_forge_strike_or_script_marks).
    prefix, suffix = (chr(code) if code >= 0x20 and not 0xD800 <= code <= 0xDFFF else "" for code in (prefix, suffix))
    return f"{prefix}{text}{suffix}"


# Form object kinds → what they show: check boxes and radio buttons the check state and caption, combo boxes and edit boxes
# their text, push buttons nothing (as HWPX checkBtn, radioBtn, comboBox, edit and btn, `hwpx._form_value`)
_FORM_CHECKS = {b"tbc+", b"tbr+"}
_FORM_TEXTS = {b"boc+", b"tde+"}


def _form_value(control: Record) -> str:
    """The value a form object (`form`) showed: `☒ caption` / `☐ caption` for a check box or radio button (Value 1 is checked,
    as HWPX value="CHECKED" for the same object), the Text of a combo box or edit box. A push button, an unknown kind and a broken
    record give an empty string."""
    record = next((child for child in control.children if child.tag == TAG_FORM_OBJECT), None)
    if record is None or len(record.payload) < _FORM_PROPERTIES_OFFSET + 2:
        return ""
    kind = record.payload[:4]
    if kind not in _FORM_CHECKS and kind not in _FORM_TEXTS:
        return ""
    (length,) = struct.unpack_from("<H", record.payload, _FORM_PROPERTIES_OFFSET)
    start = _FORM_PROPERTIES_OFFSET + 2
    properties = _form_properties(record.payload[start:start + 2 * length].decode("utf-16-le", errors="replace"))
    if kind in _FORM_CHECKS:
        mark = "☒" if properties.get("Value") == "1" else "☐"
        return f"{mark} {form_caption(properties.get('Caption', ''))}".translate(_CONTROL_TEXT).strip()
    return properties.get("Text", "").translate(_CONTROL_TEXT).strip()


def _form_properties(text: str) -> dict[str, str]:
    """The `name:type:value` items of a form object's properties, the first value of each name. The items of a `set` are read as
    if they stood outside it; a `wstring` value is taken by its length, so it may hold spaces and colons. Each item moves on past
    its value, so a broken string ends the reading instead of repeating it."""
    properties: dict[str, str] = {}
    position, end = 0, len(text)
    while position < end:
        while position < end and text[position] == " ":
            position += 1
        name_end = text.find(":", position)
        kind_end = text.find(":", name_end + 1) if name_end >= 0 else -1
        if kind_end < 0:
            break
        name, kind = text[position:name_end], text[name_end + 1:kind_end]
        if kind in ("set", "wstring"):
            length_end = text.find(":", kind_end + 1)
            length = decimal_int(text[kind_end + 1:length_end], None) if length_end >= 0 else None
            if length is None:
                break
            if kind == "set":
                position = length_end + 1
                continue
            value, position = text[length_end + 1:length_end + 1 + length], length_end + 1 + length
        else:
            value_end = text.find(" ", kind_end + 1)
            value_end = end if value_end < 0 else value_end
            value, position = text[kind_end + 1:value_end], value_end
        properties.setdefault(name, value)
    return properties


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
        # Without control characters: what a ruby or composed character shows has none, and U+0002-U+0006 are the temporary
        # script and strike marks, so a crafted control wrote `~~본말~~` (the link marks were stripped already). A tab or line
        # break is a space, as it was before: deleted, it joined the words around it (Pinned by: test_hwp.py::TestHyperlinks::
        # test_hwp_control_text_cannot_forge_strike_or_script_marks, TestDutmal::test_tab_or_line_break_in_ruby_text_is_a_space).
        text = control.payload[offset + 2:offset + 2 + length * 2].decode("utf-16-le", errors="replace")
        texts.append(text.translate(_CONTROL_TEXT).strip())
        offset += 2 + length * 2
    return texts


_CONTROL_TEXT = dict.fromkeys(range(32)) | dict.fromkeys(range(0x09, 0x0E), " ")


def _composed_text(control: Record) -> str:
    """The characters of composed characters (`tcps`: WORD count + WCHARs, then the border kind BYTE) as the HWPX save of the same
    document writes them in composeText: a number in a frame, which HWP writes as private-use pieces of its digits, is its digits
    (`12`), and the frame glyph HWP writes first (`△12`, `◯가`) is left out when it is the glyph of the border kind — HWPX keeps
    the frame as circleType. Matched one by one on a Hancom file holding every border kind in both formats (Pinned by:
    test_hwp.py::TestComposedCharacters)."""
    payload = control.payload
    if len(payload) < 6:
        return ""
    (count,) = struct.unpack_from("<H", payload, 4)
    text = unfold(payload[6:6 + 2 * count].decode("utf-16-le", errors="replace"))
    border = payload[6 + 2 * count] if len(payload) > 6 + 2 * count else 0
    number = framed_number(text.strip())
    if number is not None:
        return number
    frame = FRAME_GLYPHS.get(border)
    if frame is not None and len(text) > 1 and text[0] == frame:
        text = text[1:]
    return hancom_symbols(text).translate(_CONTROL_TEXT).strip()
# The control characters of an equation script that `str.split` does not take for white space
_SCRIPT_CONTROLS = {code: None for code in range(0x20) if not chr(code).isspace()}


def parse_styles(doc_info: list[Record]) -> list[str]:
    """Extract the list of style names (index = style ID) from the STYLE records of DocInfo. The DocInfo functions take the flattened
    record list — parsing the stream again in each function would spend the record limit five times.

    A broken record keeps its place with an empty name, as in `parse_paragraph_heads`: skipping it gave every later style the
    name of the next one (Pinned by: test_hwp_robustness.py::TestBrokenDocInfoRecordsKeepTheirPlace)."""
    names = []
    for record in doc_info:
        if record.tag != TAG_STYLE:
            continue
        if len(record.payload) < 2:
            names.append("")
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


def parse_char_sizes(doc_info: list[Record]) -> list[int]:
    """The base size (1/100 pt) of each CHAR_SHAPE record of DocInfo (index is the character shape ID); 0 for a broken record."""
    sizes = []
    for record in doc_info:
        if record.tag == TAG_CHAR_SHAPE:
            payload = record.payload
            sizes.append(struct.unpack_from("<i", payload, _CHAR_SHAPE_SIZE_OFFSET)[0]
                         if len(payload) >= _CHAR_SHAPE_SIZE_OFFSET + 4 else 0)
    return sizes


# Bits 0 (italic) and 1 (bold) of the character shape attribute (spec table 35; bold confirmed on real files, see above)
_CHAR_SHAPE_ITALIC, _CHAR_SHAPE_BOLD = 1 << 0, 1 << 1


def parse_char_emphasis(doc_info: list[Record]) -> dict[int, tuple[bool, bool]]:
    """CHAR_SHAPE records of DocInfo (index is the character shape ID) that are bold or italic → (bold, italic)."""
    emphasis = {}
    shapes = (record for record in doc_info if record.tag == TAG_CHAR_SHAPE)
    for shape_id, record in enumerate(shapes):
        if len(record.payload) < _CHAR_SHAPE_ATTRIBUTE_OFFSET + 4:
            continue
        (attribute,) = struct.unpack_from("<I", record.payload, _CHAR_SHAPE_ATTRIBUTE_OFFSET)
        if attribute & (_CHAR_SHAPE_BOLD | _CHAR_SHAPE_ITALIC):
            emphasis[shape_id] = (bool(attribute & _CHAR_SHAPE_BOLD), bool(attribute & _CHAR_SHAPE_ITALIC))
    return emphasis


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
    numbering/bullet ID (2, at byte 30). The 3 bits of the level stop at 6 (level 7); Hangul 5.1 keeps the level 0-9 of levels 8-10 in
    the UINT32 at byte 54 too, so the larger of the two is the level (in public Hangul files, 1,629 shapes of levels 8-10 had 6 in
    the attribute and 7-9 there, and no shape disagreed otherwise).
    NUMBERING: for each of 7 levels, 12 bytes of paragraph head information (number shape in attribute bits 5-9) + a format string
    (^n in "^1." is the number of level n),
    then the start number (2) and, from 5.0.2.5 on, per-level start numbers (4×7); from Hangul 5.1 on, levels 8-10 follow in the
    same shape (head information and format string 3 times, then 3 start numbers). The number shape values (0 digits, 1 circled
    digits, 8 가나다 (Hangul syllables)) were confirmed with real files holding Hangul's default
    numbering definition (1. 가. 1) 가) (1) (가) ①) (public hwplib samples and Hangul form documents); the levels 8-10 with a Hancom
    file numbered to level 10 in HWP and HWPX (Pinned by: test_hwp.py::TestNumberingLevelsEightToTen).
    """
    heads = ParagraphHeads()
    for record in doc_info:
        payload = record.payload
        if record.tag == TAG_PARA_SHAPE:
            if len(payload) >= 32:
                (properties,) = struct.unpack_from("<I", payload, 0)
                (numbering_id,) = struct.unpack_from("<H", payload, 30)
                level = (properties >> 25) & 0x7
                if len(payload) >= _PARA_SHAPE_LEVEL_OFFSET + 4:
                    level = max(level, min(struct.unpack_from("<I", payload, _PARA_SHAPE_LEVEL_OFFSET)[0], 9))
                heads.shapes.append(((properties >> 23) & 0x3, level, numbering_id))
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
    formats, offset = _number_formats(payload, 0, 7)
    if formats is None:
        return []

    # If per-level start numbers (4×7) follow the start number (2), use them (otherwise 1). 0 is taken as 1.
    starts = [1] * 7
    if offset + 2 + 28 <= len(payload):
        starts = [value or 1 for value in struct.unpack_from("<7I", payload, offset + 2)]
        # Levels 8-10 (Hangul 5.1): three more levels and their three start numbers. A broken or missing part leaves seven levels
        more, end = _number_formats(payload, offset + 2 + 28, 3)
        if more is not None and end + 12 <= len(payload):
            formats += more
            starts += [value or 1 for value in struct.unpack_from("<3I", payload, end)]
    return [NumberLevel(template, shape, start) for (template, shape), start in zip(formats, starts, strict=True)]


def _number_formats(payload: bytes, offset: int, count: int) -> tuple[list[tuple[str, str]] | None, int]:
    """`count` levels of a NUMBERING record from `offset`: 12 bytes of paragraph head information (number shape in attribute bits
    5-9) and the format string (WORD length + WCHARs) each → ([(format string, shape)], the offset after them), or (None, offset)
    when the record ends before them."""
    formats: list[tuple[str, str]] = []
    for _ in range(count):
        if offset + 14 > len(payload):
            return None, offset
        (properties,) = struct.unpack_from("<I", payload, offset)
        (length,) = struct.unpack_from("<H", payload, offset + 12)
        offset += 14
        if offset + length * 2 > len(payload):
            return None, offset
        template = payload[offset:offset + length * 2].decode("utf-16-le", errors="replace")
        offset += length * 2
        shape = (properties >> 5) & 0x1F
        formats.append((template, NUMBER_SHAPES[shape] if shape < len(NUMBER_SHAPES) else "DIGIT"))
    return formats, offset


def _chart_part(data: bytes) -> etree._Element | None:
    """The OOXML chart (`c:chartSpace`) inside the BinData stream of an OLE object, or None when it holds none. The stream is a
    UINT32 size and an OLE container; the container's streams are read no further than its size (`_ole.OleStreams`), and one that
    cannot be read leaves the object a placeholder, whatever olefile raises: the object was never read before, and a container
    with a huge mini sector shift (OverflowError) made the whole document fail that converted with `[OLE object]` (Pinned by:
    test_hwp.py::TestHwpCharts::test_ole_object_that_is_no_chart_stays_a_placeholder)."""
    if len(data) < 4 + len(olefile.MAGIC):
        return None
    (size,) = struct.unpack_from("<I", data, 0)
    container = data[4:4 + size]
    if not container.startswith(olefile.MAGIC):
        return None
    try:
        with OleFile(BytesIO(container)) as ole:
            streams = OleStreams(ole, len(container))
            xml = streams.read(_OLE_CHART_STREAM) if streams.exists(_OLE_CHART_STREAM) else b""
    except MemoryError:
        raise
    except Exception:  # noqa: BLE001
        return None
    root = parse_part(xml) if xml else None
    return root if root is not None and etree.QName(root).localname == "chartSpace" else None


def parse_bin_items(doc_info: list[Record], kind: int = _BIN_EMBEDDING) -> list[tuple[str | None, bool | None]]:
    """DocInfo BIN_DATA records → [(stream name, compressed or not)]. The index is the BinItem ID.

    There is a stream of the `kind` asked for when the low 4 bits of the attribute are that kind: 1 (EMBEDDING, a picture) or 2
    (STORAGE, an OLE object, saved by Hangul as the stream `BinData/BINxxxx.OLE` too); 0 LINK is an external file. Pictures are
    read from EMBEDDING items only, OLE objects from STORAGE items only (`HwpRenderer._ole_chart`).
    Compression is in attribute bits 4-5: 0 means the document default (None), 1 compressed, 2 uncompressed.
    A broken record keeps its place without a stream: skipping it gave every later picture the stream of the next one
    (Pinned by: test_hwp_robustness.py::TestBrokenDocInfoRecordsKeepTheirPlace).
    """
    items: list[tuple[str | None, bool | None]] = []
    for record in doc_info:
        if record.tag != TAG_BIN_DATA:
            continue
        if len(record.payload) < 2:
            items.append((None, None))
            continue
        (properties,) = struct.unpack_from("<H", record.payload, 0)
        compress = {1: True, 2: False}.get((properties >> 4) & 0x3)
        if properties & 0xF != kind or len(record.payload) < 6:
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


def _read_stream(streams: OleStreams, name: str, compressed: bool, budget: _InflateBudget) -> bytes:
    return _read_stream_to_end(streams, name, compressed, budget)[0]


def _read_stream_to_end(streams: OleStreams, name: str, compressed: bool,
                        budget: _InflateBudget) -> tuple[bytes, bool]:
    """Stream bytes and whether it was read to the end. zlib.error if the compression is broken.

    The stream as stored counts toward the limit, whether or not it is compressed, as a stored ZIP member does, and is not read
    when it is larger than what is left: the directory entries of an OLE file can all point at one sector chain, so a 4 MB file
    with 300 picture entries on one 4 MB stream read 1.2 GB, and 100 section entries on one 1 MB stream took 12 s, past any
    `max_uncompressed_bytes`. Counting only what a compressed stream inflates to, 1,000 section entries on one 4 MB stream that
    holds an empty deflate block and then zeros read 4 GB under a limit of 10 MB, without an error. A compressed stream counts
    the larger of its stored and its decompressed size. The streams of a real file add up to no more than the file, and Hangul's
    compressed streams to less than they decompress to (Pinned by: test_hwp.py::
    test_uncompressed_streams_are_bounded_by_uncompressed_limit, test_hwp_robustness.py::TestStreamsLargerThanTheFile)."""
    if streams.size(name) > budget.remaining:
        raise DocumentTooLargeError("The document becomes too large when decompressed.")
    raw = streams.read(name)
    if not compressed:
        budget.remaining -= len(raw)
        if budget.remaining < 0:
            raise DocumentTooLargeError("The document becomes too large when decompressed.")
        return raw, True

    # decompressobj raises no exception on a stream with a truncated end, which salvages what was read (eof is false if the end marker
    # was not reached). One byte more than the limit is decompressed to notice an overrun.
    inflater = zlib.decompressobj(-15)
    # zlib takes the length as a C ssize_t: a limit of sys.maxsize or more ("no limit") raised OverflowError, and every
    # compressed HWP was reported corrupted (Pinned by: test_converter.py::TestHugeLimits)
    data = inflater.decompress(raw, min(budget.remaining + 1, sys.maxsize))
    budget.remaining -= max(len(raw), len(data))
    if budget.remaining < 0:
        raise DocumentTooLargeError("The document becomes too large when decompressed.")
    return data, inflater.eof


def _section_streams(ole: olefile.OleFileIO) -> list[str]:
    sections = [entry for entry in ole.listdir() if len(entry) == 2 and entry[0] == "BodyText"
                and entry[1].startswith("Section") and entry[1][7:].isdecimal()]
    return ["/".join(entry) for entry in sorted(sections, key=lambda entry: int(entry[1][7:]))]


def extract_hwp(data: bytes, options: ConvertOptions) -> Extraction:
    try:
        ole = OleFile(BytesIO(data))
    except OSError as exc:
        raise CorruptedDocumentError("The HWP document structure could not be read.") from exc

    with ole:
        streams = OleStreams(ole, len(data))
        header = streams.read("FileHeader")
        if len(header) < 40 or not header.startswith(b"HWP Document File"):
            raise CorruptedDocumentError("The HWP file header is invalid.")
        (flags,) = struct.unpack_from("<I", header, 36)
        if flags & _FLAG_PASSWORD:
            raise EncryptedDocumentError("The HWP document is password-protected. Please remove the password and upload it again.")

        compressed = bool(flags & _FLAG_COMPRESSED)
        preview = _preview_text(streams)
        metadata = _summary_metadata(streams)
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
            doc_info_stream = _read_stream(streams, "DocInfo", compressed, budget) if streams.exists("DocInfo") else b""
        except zlib.error:
            doc_info_stream = b""
            extraction.warnings.append("The document style information (DocInfo) could not be read, so the document was converted "
                                       "without headings, numbering, character formatting and pictures.")
        doc_info = _flatten(parse_records(doc_info_stream, records))
        bin_items = parse_bin_items(doc_info)

        loaded: dict[tuple[str, bool], bytes | None] = {}

        def load_bin(item_id: int) -> bytes | None:
            """A picture stream is read only once, however many BinItems name it. Reading and decompressing the same picture again
            per reference took 150 seconds for a document with 16,384 references to one picture, and the decompression limit was
            also cut per reference; kept per BinItem, 8,000 BinItems naming one 2 MB stream took 16.6 s. A picture whose compression
            is broken skips just that picture (exceeding the limit is a problem of the whole document, so it is raised as is)
            (Pinned by: test_image.py::TestHwpRepeatedAndBrokenPictures)."""
            # A BinItem ID is the 1-based index of the DocInfo BIN_DATA records.
            name, compress = bin_items[item_id - 1] if 1 <= item_id <= len(bin_items) else (None, None)
            if not name or not streams.exists(name):
                return None
            key = (name.lower(), compressed if compress is None else compress)
            if key not in loaded:
                try:
                    loaded[key] = _read_stream(streams, name, key[1], budget)
                except zlib.error:
                    loaded[key] = None
            return loaded[key]

        ole_items = parse_bin_items(doc_info, _BIN_STORAGE)
        ole_budget = _InflateBudget(options.max_uncompressed_bytes)

        def load_ole(item_id: int) -> bytes | None:
            """The stream of an OLE object, read to look for a chart (`HwpRenderer._ole_chart`). Its decompression has a budget of
            its own, as large as the document's: charged to the document's, a pasted sheet that the conversion never reads made a
            document fail as too large that converted before. An object past it, or whose compression is broken, stays a
            placeholder; the stream is not kept (Pinned by: test_hwp.py::TestHwpCharts::
            test_large_ole_object_does_not_make_the_document_too_large)."""
            name, compress = ole_items[item_id - 1] if 1 <= item_id <= len(ole_items) else (None, None)
            if not name or not streams.exists(name):
                return None
            try:
                return _read_stream(streams, name, compressed if compress is None else compress, ole_budget)
            except (zlib.error, DocumentTooLargeError):
                return None

        renderer = HwpRenderer(parse_styles(doc_info), options, extraction, load_bin, parse_paragraph_heads(doc_info),
                               parse_char_scripts(doc_info), parse_struck_shapes(doc_info), parse_char_sizes(doc_info),
                               parse_char_emphasis(doc_info) if options.keep_emphasis else None)
        renderer.load_ole = load_ole
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
                stream, complete = _read_stream_to_end(streams, name, compressed, budget)
            except zlib.error as exc:
                broken.append(exc)
                extraction.warnings.append(f"Section {number}: the compressed data is broken and could not be read.")
                continue
            if not complete:
                extraction.warnings.append(f"Section {number}: the stream is truncated, so only the part that could be read was converted.")

            # Read per top-level record (paragraph) and count characters. Once the limit is exceeded, the remaining paragraphs and
            # sections are not read. The notes the paragraph added count too: written after the body, they were not counted while
            # reading, so a document of footnotes was read to its end (9.6 million characters: 3.7 s against 0.2 s in the body)
            # (Pinned by: test_hwp_robustness.py::TestNotesCountTowardTheLimit).
            for record in iter_records(stream, records):
                noted = len(renderer.notes)
                rendered = renderer.render([record])
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
        if names and len(broken) == len(names):
            raise broken[0]

    # The preview replaces the body only when parsing the body finished and there is no text. A document without text (empty table,
    # shapes only) has an empty preview too
    # (`_preview_text`), so the result stays empty (Pinned by: test_hwp.py::TestPreviewFallback).
    extraction.count(sum(solid_length(text) for text in renderer.footers), options)
    blocks = renderer.size_headings.resolve(blocks)
    body = join_blocks([*renderer.headers, *blocks, *renderer.footers, "\n".join(renderer.notes), renderer.memo_lines()])
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


def _preview_text(streams: OleStreams) -> str:
    """The preview text saved by Hancom Office. It writes table cells as `<cell text>`, so if no letter or digit remains outside the
    cell marks (`<`, `>`) the preview is considered absent (`_hangul.preview_has_words`). Used as is, a document without text
    emitted `<>` noise as its body (Pinned by: test_hwp.py::TestPreviewFallback)."""
    if not streams.exists("PrvText"):
        return ""
    text = streams.read("PrvText").decode("utf-16-le", errors="replace").strip()
    return text if preview_has_words(text) else ""


def _summary_metadata(streams: OleStreams) -> dict[str, str]:
    """HWP keeps summary information not in the standard SummaryInformation but in a property set with its own name (ID 2 title, 4 author).

    olefile's `getproperties` is not used. It trusts the value count as is, and a 21KB file with 8 bytes changed to FF hung looping
    4.3 billion times
    (Pinned by: test_hwp.py::TestHwpSummaryInformation). Only the two needed strings are read directly within the stream size, and
    anything out of range is dropped.
    """
    name = "\x05HwpSummaryInformation"
    if not streams.exists(name):
        return {}
    try:
        stream = streams.read(name)[:_SUMMARY_MAX_BYTES]
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
