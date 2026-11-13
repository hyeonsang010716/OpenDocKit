"""PDF → Markdown, page by page.

The engine is pdfplumber (pdfminer.six, MIT). Six things that decide the quality are done here directly.

1. **Reading order**: uses the text-box flow order of pdfminer's layout analysis (LAParams). Sorting by y coordinate alone mixes the
   lines of the left and right columns in a two-column document. Two columns that still end up mixed line by line because of wide
   line spacing are put back by the position of their lines (`_read_columns_in_turn`). Conversely, where right-aligned lines between
   left lines (a chat transcript, speaker names) were read as a column, the order is put back to top → bottom
   (`_read_alternating_sides_in_turn`). A centred caption pushed behind a lower line in a single-column flow is moved up to its
   height (`_read_lone_lines_in_place`).
2. **Tables**: finds tables drawn with ruling lines, turns them into Markdown tables, and removes the characters of that area from
   the body (to avoid duplicates). A border around the whole page, an outer box that contains other tables, and a one-column box are
   layout, not tables, and are dropped.
3. **Paragraph recovery**: a PDF has no "paragraphs", only lines. A paragraph is split where the line gap is clearly wider than the
   document's usual line gap or where the font size changes. Line breaks inside a paragraph are kept as they are and not joined with
   a space (deliberate): Korean PDFs often break lines at character level ("파\n악합니다"), so joining with a space would split
   words. In Markdown a single line break stays in the same paragraph.
4. **Headings**: a short paragraph in a font clearly larger than the body is marked with `#`, `##`, `###`.
5. **Noise removal**: removes headers, footers and page numbers repeated over several pages, diagonal watermarks, and the copies of
   'fake bold' text drawn twice on top of itself.
6. **Rotated text**: text drawn rotated by 90 degrees within a page is taken out of the body lines, turned to its reading direction
   and placed at the end of the page.

The values entered into a fillable form (AcroForm) are not in the page content, so they are read separately and placed at the end of
the page as 'field name: value' (`_form_values`).

Images (`extract_images`): for an image page without characters (a scan) and a page whose characters were converted to outlines the
whole page is rendered with pypdfium2 and registered; for other pages the picture areas are. Turning them into text (OCR,
description) is done by describe.py.

Known limitations: a borderless table is recovered as a table only when its columns line up clearly; otherwise it becomes line-by-line
text. A vector chart (lines and shapes, not an image) on a page that has characters cannot be extracted as a picture.
"""

from bisect import bisect_left, bisect_right, insort
from collections import Counter
from collections.abc import Callable
from contextvars import ContextVar
import copy
from dataclasses import dataclass, field
from functools import lru_cache
from io import BytesIO
from itertools import accumulate, count, islice, pairwise
import logging
import math
import re
import statistics
import threading
import time
import zlib

from pdfminer import layout as pdfminer_layout
from pdfminer import pdffont
from pdfminer.ascii85 import ascii85decode, asciihexdecode
from pdfminer.cmapdb import CMap, CMapParser, FileUnicodeMap
from pdfminer.layout import LTChar, LTContainer, LTFigure, LTLayoutContainer, LTTextLineHorizontal
from pdfminer.pdfdevice import PDFDevice
from pdfminer.pdfinterp import PDFGraphicState, PDFPageInterpreter
from pdfminer.pdftypes import (
    LITERALS_ASCII85_DECODE,
    LITERALS_ASCIIHEX_DECODE,
    LITERALS_FLATE_DECODE,
    PDFObjRef,
    PDFStream,
    resolve1,
)
from pdfminer.utils import MATRIX_IDENTITY, apply_matrix_pt, choplist, decode_text, mult_matrix
import pdfplumber
from pdfplumber.page import PDFPageAggregatorWithMarkedContent
from pdfplumber.utils.exceptions import PdfminerException
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from opendockit.exceptions import CorruptedDocumentError, DocumentTooLargeError, EncryptedDocumentError
from opendockit.extractors._shared import (
    SUBSCRIPT,
    SUPERSCRIPT,
    mark_script,
    resolve_scripts,
    symbol_glyph,
)
from opendockit.markdown import render_table, solid_length
from opendockit.models import ConvertOptions, Extraction, ImageKind, Section, SectionKind


# all_texts: also groups the characters inside a Form XObject (LTFigure) into text boxes. Some generators put the whole body in an XObject.
_LAPARAMS = {"line_overlap": 0.5, "char_margin": 2.0, "line_margin": 0.5, "word_margin": 0.1,
             "boxes_flow": 0.5, "all_texts": True}
_FIRST_SEEN = count()


def _first_seen_order(obj: object) -> int:
    """Number used instead of id() inside pdfminer.layout: the order in which an object was first seen (stored on the object, never reused).

    Deliberate: pdfminer's text-box grouping (LTLayoutContainer.group_textboxes) decides which of two equally distant pairs to group
    first by the ids in the heap element (distance, id(box), id(box), …), so for 13 of 30 table-heavy public paper PDFs the reading
    order and character count differed from run to run even within one process (the character-limit verdict wavered too). In that
    module id is used only for this tie-break and only has to be unique per object. First-seen order is the order of the text-box
    list, so it is deterministic, and the result is one of the orders that could already come out before. The function is not copied
    and changed so that later pdfminer fixes are not overwritten (Pinned by: test_pdf.py::TestDeterministicLayout).
    """
    attributes = getattr(obj, "__dict__", None)
    if attributes is None:
        return id(obj)  # text boxes and groups all have a __dict__
    order = attributes.get("_opendockit_order")
    if order is None:
        order = attributes["_opendockit_order"] = next(_FIRST_SEEN)
    return order


pdfminer_layout.id = _first_seen_order  # a module-global name, so only that module's id() changes (the builtin id is untouched)
_resolve_one_step = PDFObjRef.resolve


def _resolve_without_cycles(reference: PDFObjRef, default: object = None) -> object:
    """Resolver used instead of `PDFObjRef.resolve`: follows a chain of indirect objects that hold only a reference to its end, and
    returns `default` when the chain loops back (same as a missing object).

    Deliberate: pdfminer's `resolve1` and `resolve_all` repeat `resolve` until the value is no longer a reference, so with an object
    that points to itself (or two that point to each other) such as `7 0 obj 7 0 R endobj`, wherever it sat — document info, form
    annotations, page resources — the conversion never finished (both our own resolving and the page reading of pdfminer and
    pdfplumber). All resolving goes through this method, so it is stopped in this one place. An ordinary reference without a chain is
    the same single `getobj` as before, and a chain ends at the same object too — only looping chains change
    (Pinned by: test_pdf.py::TestSelfReferencingObjects).
    """
    target = _resolve_one_step(reference, default)
    if not isinstance(target, PDFObjRef):
        return target  # almost every reference ends here
    seen = {reference.objid}

    while isinstance(target, PDFObjRef):
        if target.objid in seen:
            return default
        seen.add(target.objid)
        target = _resolve_one_step(target, default)
    return target


PDFObjRef.resolve = _resolve_without_cycles  # a class attribute, so it applies to every PDFObjRef resolution in this process
_TOO_LARGE = "The document becomes too large when decompressed."  # same wording as the ZIP/HWP decompression bomb
_INFLATE_CHUNK = 1 << 20  # amount inflated at a time when measuring size (only this much is held in memory)


@dataclass(slots=True)
class _InflateBudget:
    """Total bytes one document may inflate to (`max_uncompressed_bytes`, the same limit as ZIP/HWP). Measured streams (`charged`)
    and walked objects (`walked`) are held as id → object (prevents id reuse) so nothing is charged or walked twice. When exceeded,
    `exceeded` is set — noticed even if pdfplumber wraps the exception in `PdfminerException`."""

    remaining: int
    exceeded: bool = False
    charged: dict[int, object] = field(default_factory=dict)
    walked: dict[int, object] = field(default_factory=dict)
    font_entries: int = 0  # number of entries font ranges (/W, mapping tables) may still expand to (`_bounded_font_ranges`)
    font_ranges_cut: bool = False
    painted: dict[int, tuple[object, int]] = field(default_factory=dict)  # content stream → paint operator count (`_drawn_objects`)
    forms: dict[tuple[int, int], tuple[object, ...]] = field(default_factory=dict)  # form → drawn object count (`_content_objects`)
    undecoded_fonts: bool = False  # met a CID font whose encoding CMap was not found, so no character decodes (`_cmap_or_embedded`)


_INFLATE_BUDGET: ContextVar[_InflateBudget | None] = ContextVar("pdf_inflate_budget", default=None)
_decode_unbounded = PDFStream.decode


def _decode_within_budget(stream: PDFStream) -> None:
    """Decoder used instead of `PDFStream.decode`: during a conversion (`_INFLATE_BUDGET`) it first measures the inflated size by
    inflating a little at a time (`_charge`) and calls pdfminer's decoder unchanged only when within the limit. This guards, in one
    place, even the streams pdfminer decodes on its own such as object streams and cross-reference streams
    (Pinned by: test_pdf_resource_limits.py::TestDecompressedStreamBudget). The function is not copied (same reason as
    `_first_seen_order`)."""
    budget = _INFLATE_BUDGET.get()
    if budget is not None:
        _charge(stream, budget)
    _decode_unbounded(stream)


PDFStream.decode = _decode_within_budget  # outside a conversion (no budget) it behaves as before


def _charge(stream: PDFStream, budget: _InflateBudget) -> None:
    """Charges the size the stream's Flate stage will inflate to against the limit. Raises DocumentTooLargeError when exceeded.
    Other filters and broken streams are not measured (pdfminer decodes them as before — ASCII filters shrink the data, image
    filters are not decoded)."""
    if id(stream) in budget.charged or stream.rawdata is None:
        return
    budget.charged[id(stream)] = stream
    try:
        data = stream.rawdata
        if stream.decipher:
            data = stream.decipher(stream.objid, stream.genno, data, stream.attrs)
        for name, _ in stream.get_filters():
            if name in LITERALS_ASCII85_DECODE:
                data = ascii85decode(data)
            elif name in LITERALS_ASCIIHEX_DECODE:
                data = asciihexdecode(data)
            elif name in LITERALS_FLATE_DECODE:
                budget.remaining -= _inflated_size(data, budget.remaining + 1)
                break
            else:
                break
    except Exception:  # noqa: BLE001 — a stream that could not be measured is handled by pdfminer's decoder as before
        return

    if budget.remaining < 0:
        budget.exceeded = True
        raise DocumentTooLargeError(_TOO_LARGE)


def _inflated_size(data: bytes, limit: int) -> int:
    """Inflates zlib data `_INFLATE_CHUNK` at a time and only measures the size (stops on reaching `limit`). Returns the size
    inflated up to the broken spot — pdfminer's corrupted-stream decoding (`decompress_corrupted`) inflates that far as well. Stops
    at the zlib end (`eof`) — bytes left after the end (a line break counted in `/Length`) stay in `unconsumed_tail`, and without
    this check they were fed back in forever (Pinned by:
    test_pdf_resource_limits.py::TestStreamWithBytesAfterTheZlibEnd)."""
    inflater, size = zlib.decompressobj(), 0
    try:
        while data and size < limit and not inflater.eof:
            size += len(inflater.decompress(data, _INFLATE_CHUNK))
            data = inflater.unconsumed_tail
    except zlib.error:
        pass
    return size


def _charge_page(page: object, budget: _InflateBudget) -> None:
    """Charges the page's content streams and resources (Form XObjects, fonts, patterns etc., including nested resources) against
    the limit before pdfium or pdfminer decode them.

    pdfium inflates the content streams whole when surveying a page (`_survey_page`), so 1.6GB had already been used before
    anything was handed to pdfminer. Image XObjects and attachments are not measured (deliberate) — they are not decoded to read
    text, and large scanned-image documents would hit the limit. Resource dictionaries are shared by several pages, so they are
    walked only once per document (`walked`).
    """
    pending = [page.page_obj.attrs.get("Contents"), page.page_obj.resources]
    while pending:
        obj = pending.pop()
        try:
            obj = obj.resolve() if isinstance(obj, PDFObjRef) else obj
        except DocumentTooLargeError:
            raise
        except Exception:  # noqa: BLE001 — page reading meets a broken reference itself and handles it
            continue
        if not isinstance(obj, PDFStream | dict | list) or id(obj) in budget.walked:
            continue
        budget.walked[id(obj)] = obj
        if isinstance(obj, PDFStream):
            if _name(obj.attrs.get("Subtype")) == "Image" or _name(obj.attrs.get("Type")) == "EmbeddedFile":
                continue
            _charge(obj, budget)
            obj = obj.attrs
        if isinstance(obj, dict):
            pending.extend(value for key, value in obj.items() if key != "Parent")
        else:
            pending.extend(obj)


# Estimated upper bound on the number of objects a page will draw (`_drawn_objects`). When opening a page pdfium creates one object
# per paint operator and per drawn form (about 2.8KB per form object, about 0.4KB per rectangle), so a 7-level chain in which each
# form draws the next one 10 times (a 3.4KB file, 2.1 million objects) used 5.9GB, and a 253KB file repeating `0 0 1 1 re f` ten
# million times without any form used 3.6GB, merely by opening the page. Content streams are measured only once per document
# (`walked` in `_charge_page`), so the decompression limit cannot stop this (when several pages share one stream it was opened
# again for each page), and the drawn-object limit (`_MAX_PAGE_OBJECTS`) is counted only after pdfium has opened the page. At
# 500,000 objects pdfium needs about 1.4GB to open a page of forms and about 0.2GB for a page of shapes — a paper's figure page with
# 240,000 Form XObjects (480,000 objects) is still read with pdfium's simple extraction as before.
# Known accepted gap: a page with more than 500,000 paint operators (a very dense drawing or map) is not read for text either;
# only a warning is given (Pinned by: test_pdf_resource_limits.py::TestRepeatedFormXObjects, TestManyPaintOperators).
_MAX_DRAWN_OBJECTS = 500_000
# A form chain deeper than this is treated as over the limit without counting how much it multiplies further down. When the deep
# part was counted as one object, pdfminer (no depth limit) spent more than 12 minutes drawing a 10KB file that multiplies below
# level 32. The page survey (`_survey_page`) counts down to this depth as well.
_FORM_NESTING_MAX = 32
# Operators that create a drawn object (text showing, path painting, shading, inline images) and XObject drawing (`/Name Do`).
# The same characters inside strings are counted too, so the estimate is slightly high (fine for an upper-bound check).
_PAINT_OPERATOR = re.compile(rb"(?<![^\s\])>])(?:Tj|TJ|'|\"|[fFSsBb]\*?|sh|BI)(?=[\s\[(/<%]|$)")
_XOBJECT_CALL = re.compile(rb"/([^\s/\[\]()<>{}%]+)\s*Do(?=[\s\[(/<%]|$)")
_NAME_ESCAPE = re.compile(rb"#([0-9A-Fa-f]{2})")
_REPEATED_FORMS_SKIPPED = " were not read because they have too many drawing elements or forms (Form XObject) nested too deeply."


def _drawn_objects(page: object, limit: int, painted: dict[int, tuple[object, int]] | None = None,
                   forms: dict[tuple[int, int], tuple[object, ...]] | None = None) -> int:
    """Estimate of the number of drawn objects that drawing the page will create (forms are multiplied by the number of times they
    are drawn; once over `limit` the value where counting stopped).

    Called before pdfium or pdfminer open the page (`_MAX_DRAWN_OBJECTS`). pdfminer decodes the content and form streams anyway
    (the decoded data is cached), so the extra cost is one regex pass. With `painted` handed over from the document (content stream
    id → (stream, paint operator count)) a content stream shared by several pages is counted only once. The count inside a form
    (`forms`) is also taken only once per document — when 300 pages draw a form with 400,000 operators, each page spent 0.2 seconds
    more (Pinned by: test_pdf_resource_limits.py::TestFormSharedByPages). Broken references and streams count as 0 and are left for
    page reading to meet as before.
    """
    painted = {} if painted is None else painted
    forms = {} if forms is None else forms
    try:
        resources = resolve1(page.page_obj.resources)
        contents = resolve1(page.page_obj.attrs.get("Contents"))
        streams = [resolve1(part) for part in contents] if isinstance(contents, list) else [contents]
        streams = [stream for stream in streams if isinstance(stream, PDFStream)]
        for stream in streams:
            if id(stream) not in painted:
                painted[id(stream)] = (stream, _paint_operators(stream.get_data(), limit))
        paints = sum(painted[id(stream)][1] for stream in streams)
        data = b"\n".join(stream.get_data() for stream in streams)
        return _content_objects(data, resources, limit, forms, set(), 0, paints)
    except DocumentTooLargeError:
        raise
    except Exception:  # noqa: BLE001 — the estimate is a bonus. Page reading meets a broken structure itself and handles it
        return 0


def _paint_operators(data: bytes, limit: int) -> int:
    """Number of paint operators in a content stream (`limit + 1` when over `limit`). Only counts, so that a stream with ten million
    of them does not build a list."""
    return sum(1 for _ in islice(_PAINT_OPERATOR.finditer(data), limit + 1))


def _is_form(obj: object) -> bool:
    return isinstance(obj, PDFStream) and _name(obj.attrs.get("Subtype")) == "Form"


def _content_objects(data: bytes, resources: object, limit: int, counted: dict[tuple[int, int], tuple[object, ...]],
                     active: set[int], depth: int, paints: int | None = None) -> int:
    """Estimate of the number of drawn objects one content stream `data` will create (`_drawn_objects`). A form counts as times
    drawn × (1 form object + the count inside the form) and is counted only once per form (`counted`, (form id, id of the resources
    it uses) → (form, resources, count) — holding the objects prevents id reuse. A form without resources inherits those of whatever
    draws it, so if it draws other XObjects it is counted per such resources, otherwise once under resource id 0). A form that comes
    back to itself (`active`) counts as one object, and a form deeper than `_FORM_NESTING_MAX` counts as over the limit. The number
    of paint operators already counted is passed in as `paints`."""
    total = _paint_operators(data, limit) if paints is None else paints
    calls = Counter(_XOBJECT_CALL.findall(data))
    xobjects = resolve1(resources.get("XObject")) if isinstance(resources, dict) and calls else None

    for raw, times in calls.items():
        name = _NAME_ESCAPE.sub(lambda match: bytes([int(match[1], 16)]), raw)
        form = resolve1(xobjects.get(name.decode("utf-8", "replace"))) if isinstance(xobjects, dict) else None
        if not _is_form(form) or id(form) in active:
            total += times  # an image, a missing name or a form that loops back is one object
            continue
        if depth >= _FORM_NESTING_MAX:
            return limit + 1
        inherited = resolve1(form.attrs.get("Resources")) or resources
        key = (id(form), 0) if (id(form), 0) in counted else (id(form), id(inherited))
        if key not in counted:
            active.add(id(form))
            form_data = form.get_data()
            inner = _content_objects(form_data, inherited, limit, counted, active, depth + 1)
            active.discard(id(form))
            if not _XOBJECT_CALL.search(form_data):
                key = (id(form), 0)  # the count of a form that draws no other XObject does not depend on the resources
            counted[key] = (form, inherited, inner)
        total += times * (1 + counted[key][-1])
        if total > limit:
            break
    return total


# Upper bound on expanding font ranges. pdfminer expands the ranges of a CID font's /W and /W2 (`c1 c2 w`) and the bfrange/cidrange
# of mapping tables (ToUnicode, Encoding CMap) into a dictionary one entry at a time. A 3KB PDF with the single line
# `/W [40000 400000000 500]` used tens of GB (the decompression limit does not measure expansion). One font's table is expanded
# only up to `_FONT_RANGE_ENTRIES` (CIDs are 0–65535, so twice that is plenty) and the total for the whole document up to
# `_DOCUMENT_FONT_RANGE_ENTRIES` (so that thousands of small font dictionaries cannot get around it; an entry is around 100 bytes,
# so a few hundred MB); the remaining ranges are cut and a warning is given. By the specification a mapping-table range stays
# within the last byte (256 entries), so ordinary documents never reach this
# (Pinned by: test_pdf_resource_limits.py::TestFontRangeBounds).
_FONT_RANGE_ENTRIES = 1 << 17
_DOCUMENT_FONT_RANGE_ENTRIES = 1 << 21
_FONT_RANGES_CUT = ("The character width or Unicode mapping table ranges of a PDF font are abnormally large, so part of them was not read "
                    "(some characters may be missing or misplaced).")
_unbounded_widths, _unbounded_widths2 = pdffont.get_widths, pdffont.get_widths2
_unbounded_cmap_keyword = CMapParser.do_keyword


def _range_allowance(count: int, left: int, budget: _InflateBudget) -> int:
    """How many entries of a `count`-entry range may be expanded (up to the smaller of what is left for this font, `left`, and what
    is left for the whole document)."""
    allowed = max(0, min(count, left, budget.font_entries))
    budget.font_entries -= allowed
    if allowed < count:
        budget.font_ranges_cut = True
    return allowed


def _bounded_font_ranges(seq: object, group: int) -> list:
    """Copy of a /W (`group` 3: c1 c2 w) or /W2 (5: c1 c2 w vx vy) array with the end (c2) of range entries reduced to fit the bound.
    Array-form entries (`c [w1 w2 …]`) are left alone because they expand only to the array length. Grouping works as in pdfminer's
    `get_widths` (numbers are collected; `group` of them make a range, and when an array comes the preceding number is its start)."""
    budget = _INFLATE_BUDGET.get()
    if budget is None or not isinstance(seq, list):
        return seq
    bounded: list = []
    numbers: list = []
    left = _FONT_RANGE_ENTRIES

    for value in seq:
        value = resolve1(value)
        if isinstance(value, list):
            bounded.extend(numbers)
            bounded.append(value)
            numbers = []
            continue
        if not isinstance(value, int | float):
            continue  # pdfminer also skips values that are neither numbers nor arrays (collected numbers are kept)
        numbers.append(value)
        if len(numbers) < group:
            continue
        first, last = numbers[0], numbers[1]
        if isinstance(first, int) and isinstance(last, int) and last >= first:
            allowed = _range_allowance(last - first + 1, left, budget)
            left -= allowed
            numbers[1] = first + allowed - 1  # with 0 allowed the end is before the start, so pdfminer expands nothing
        bounded.extend(numbers)
        numbers = []
    bounded.extend(numbers)
    return bounded


def _bounded_cmap_keyword(self: CMapParser, pos: int, token: object) -> None:
    """Handler used instead of `CMapParser.do_keyword`: at `endbfrange`/`endcidrange`, which close a range block, it reduces the
    ends of the stacked ranges to fit the bound and then calls pdfminer's handler unchanged (only during a conversion). A bfrange
    whose value is an array is left alone because it expands only to the array length. cidrange is counted by the last 4 bytes, as
    pdfminer does. The function is not copied (same reason as `_first_seen_order`)."""
    budget = _INFLATE_BUDGET.get()
    if budget is not None and token in (CMapParser.KEYWORD_ENDBFRANGE, CMapParser.KEYWORD_ENDCIDRANGE):
        tail = 4 if token is CMapParser.KEYWORD_ENDCIDRANGE else None
        left = getattr(self, "_opendockit_left", _FONT_RANGE_ENTRIES)
        stack = self.curstack

        for index in range(0, len(stack) - 2, 3):
            (_, start), (end_pos, end), (_, code) = stack[index:index + 3]
            if not isinstance(start, bytes) or not isinstance(end, bytes) or len(start) != len(end) or isinstance(code, list):
                continue
            low = int.from_bytes(start[-tail:] if tail else start, "big")
            high = int.from_bytes(end[-tail:] if tail else end, "big")
            if high < low:
                continue
            allowed = _range_allowance(high - low + 1, left, budget)
            left -= allowed
            if allowed <= high - low:
                width = min(len(end), tail or len(end))
                # with 0 allowed, move the end to just before the start — if the start is 0, give it another length so pdfminer skips it
                cut = (low + allowed - 1).to_bytes(width, "big") if low + allowed > 0 else b""
                stack[index + 1] = (end_pos, end[:len(end) - width] + cut)
        self._opendockit_left = left
    _unbounded_cmap_keyword(self, pos, token)


pdffont.get_widths = lambda seq: _unbounded_widths(_bounded_font_ranges(seq, 3))
pdffont.get_widths2 = lambda seq: _unbounded_widths2(_bounded_font_ranges(seq, 5))
CMapParser.do_keyword = _bounded_cmap_keyword  # outside a conversion (no budget) it behaves as before


# When the same character is drawn on the same spot (character, font, size, position to 0.1pt) more often than this, the remaining
# copies are dropped as soon as their layout object is created (`_render_char_few_copies`). A 106KB page that repeated its content
# stream thousands of times grew with the square of the number of copies in pdfminer's line grouping (group_textlines) and fragment
# merging and used 110 seconds and 2.2GB. Fake bold (drawn two or three times on top of itself) is left alone so that copy removal
# (`_without_drawing_artifacts`) handles it as before. Known limitation: copies drawn slightly shifted are not on the same spot and
# are not reduced (Pinned by: test_pdf_resource_limits.py::TestOverprintedText).
_MAX_OVERPRINTS = 3
_render_char_all_copies = PDFPageAggregatorWithMarkedContent.render_char


def _render_char_few_copies(self: PDFPageAggregatorWithMarkedContent, *args: object, **kwargs: object) -> float:
    """Renderer used instead of pdfplumber's `render_char`: during a conversion, when the character just created is a copy beyond
    the `_MAX_OVERPRINTS`-th on the same spot, it is removed from the layout at once (the advance width is returned unchanged, so
    the next character's position is the same). For a character under fill opacity (`_FILL_ALPHA`) the fill colour is replaced by
    the visible colour blended onto a white background (`_blended_fill`). The function is not copied."""
    advance = _render_char_all_copies(self, *args, **kwargs)
    if _INFLATE_BUDGET.get() is None:
        return advance
    objs = self.cur_item._objs
    char = objs[-1] if objs else None
    if isinstance(char, LTChar):
        _blended_fill(char.graphicstate)
        _symbol_font_glyph(char, kwargs.get("font", args[1] if len(args) > 1 else None))
        copies = self.cur_item.__dict__.setdefault("_opendockit_copies", Counter())
        key = (char.get_text(), char.fontname, round(char.size, 1), round(char.x0, 1), round(char.y0, 1))
        copies[key] += 1
        if copies[key] > _MAX_OVERPRINTS:
            objs.pop()
    return advance


# Symbol font PostScript name (subset prefix, ',Bold' and '-Regular' removed; lower case without spaces) → font name in
# `_shared.SYMBOL_GLYPHS`.
_SYMBOL_FONT_NAMES = {"symbol": "symbol", "symbolmt": "symbol", "wingdings": "wingdings", "wingdings2": "wingdings 2"}


@lru_cache(maxsize=256)
def _symbol_font(fontname: str) -> str | None:
    """pdfminer font name ('ABCDEF+Wingdings-Regular', 'Wingdings2,Bold') → symbol font table name. None if not a symbol font."""
    name = re.sub(r"^[A-Z]{6}\+", "", fontname)
    return _SYMBOL_FONT_NAMES.get(re.split(r"[,-]", name, maxsplit=1)[0].replace(" ", "").lower())


def _symbol_font_glyph(char: LTChar, font: object) -> None:
    """When a symbol font character comes out in the private use area (U+F020–F0FF), replaces it using that font's table
    (`_shared.symbol_glyph`).

    The check boxes in a PDF exported by Word (☑ = F052 and ☐ = F0A3 in Wingdings 2, □ = F06F and ☒ = F078 in Wingdings) are code
    points missing from the shared mapping (`markdown._SYMBOL_FONT_GLYPHS`), so they were deleted and which side had been ticked
    was lost (Pinned by: test_pdf_fonts.py::TestSymbolFontCheckboxes). Bullet code points (`_SYMBOL_BULLETS`) are left alone —
    bullet detection works on those characters and the shared mapping converts them later. Code points not in the table are left
    alone too (left to the shared mapping as before). The font name is 'unknown' when there is no font descriptor (FontName), so
    the font dictionary's BaseFont is checked as well.
    """
    text = char._text
    if len(text) != 1 or not 0xF020 <= ord(text) <= 0xF0FF or text in _SYMBOL_BULLETS:
        return
    basefont = getattr(font, "basefont", None)
    name = _symbol_font(char.fontname) or (_symbol_font(basefont) if isinstance(basefont, str) else None)
    glyph = symbol_glyph(name, ord(text)) if name else None
    if glyph:
        char._text = glyph


PDFPageAggregatorWithMarkedContent.render_char = _render_char_few_copies  # outside a conversion (no budget) it behaves as before
# Fill opacity (ExtGState `/ca`). pdfminer does not read the `gs` operator (`do_gs` is an empty function), so a browser-printed
# `color: rgba(0,0,0,0.08)` watermark looked like black text, slipped past the faint-watermark check (`_faint_watermark`) and got
# into table cells as 'S AM P LE'. Only `/ca` is read from `gs` and attached to the graphics state (`copy` carries it over too so
# that `q`/`Q` save and restore it), and the fill colour of characters is replaced by the visible colour blended onto a white
# background — for characters only, during a conversion only. Transparency groups, soft masks and blend modes are not considered
# (known limitation).
# Pinned by: test_pdf.py::TestHorizontalWatermark::test_black_watermark_made_faint_by_transparency_is_dropped
_FILL_ALPHA = "_opendockit_fill_alpha"
_copy_graphic_state = PDFGraphicState.copy


def _set_graphic_state(self: PDFPageInterpreter, name: object) -> None:
    """`gs` operator: reads only the fill opacity `/ca` from the resources' ExtGState dictionary (pdfminer reads nothing)."""
    try:
        states = resolve1(self.resources.get("ExtGState"))
        state = resolve1(states.get(getattr(name, "name", name))) if isinstance(states, dict) else None
        alpha = resolve1(state.get("ca")) if isinstance(state, dict) else None
    except Exception:  # noqa: BLE001 — broken resources are drawn without opacity (same as before)
        return
    if isinstance(alpha, int | float) and not isinstance(alpha, bool):
        setattr(self.graphicstate, _FILL_ALPHA, min(max(float(alpha), 0.0), 1.0))


def _copy_with_fill_alpha(self: PDFGraphicState) -> PDFGraphicState:
    copied = _copy_graphic_state(self)
    alpha = getattr(self, _FILL_ALPHA, None)
    if alpha is not None:
        setattr(copied, _FILL_ALPHA, alpha)
    return copied


def _blended_fill(state: PDFGraphicState) -> None:
    """Replaces the fill colour of a graphics state whose fill opacity is below 1 (the copy made for one character drawing) with
    the colour blended onto a white background (once only)."""
    alpha = getattr(state, _FILL_ALPHA, 1.0)
    if alpha >= 1.0 or getattr(state, "_opendockit_blended", False):
        return
    state._opendockit_blended = True
    colour = state.ncolor
    if isinstance(colour, int | float):
        state.ncolor = 1 - alpha * (1 - colour)
    elif isinstance(colour, tuple) and len(colour) == 3 and all(isinstance(value, int | float) for value in colour):
        state.ncolor = tuple(1 - alpha * (1 - value) for value in colour)
    elif isinstance(colour, tuple) and len(colour) == 4 and all(isinstance(value, int | float) for value in colour):
        state.ncolor = tuple(alpha * value for value in colour)  # for CMYK the amount of ink shrinks by the opacity


PDFPageInterpreter.do_gs = _set_graphic_state
PDFGraphicState.copy = _copy_with_fill_alpha


_type3_font_init = pdffont.PDFType3Font.__init__


def _type3_font_with_descriptor_descent(self: pdffont.PDFType3Font, rsrcmgr: object, spec: dict) -> None:
    """Takes a Type3 font's descent from the Descent of its font descriptor (FontDescriptor). If missing or 0, the bottom of FontBBox
    as pdfminer does.

    pdfminer takes the bottom of a Type3 character box from the bottom of FontBBox. When LibreOffice exports a variable font
    ('Noto Sans KR'), only the digits are Type3 (FontBBox -1.05em, Descent -0.288em), so they sat 0.78em below characters of other
    fonts on the same baseline, and a table cell split into 'Total<br>450' and a paragraph into '검 / . / 토'
    (Pinned by: test_pdf_fonts.py::TestType3DigitsOnALine). A Type3 font descriptor's values are in glyph space units.

    Descent is used only when it lies between the bottom of FontBBox and 0; a value outside that which falls inside once divided by
    1000 is taken as written in 1000 units. Some generator writes Descent in 1000 units (-288) for a font whose glyph space is text
    space (FontMatrix [1 0 0 1 0 0]), which made the descent -288em, moved the character boxes off the page and made the digits
    vanish without a warning (Pinned by: test_pdf_round44.py::TestType3DescentInTheWrongUnit).
    If neither applies, the pdfminer value (bottom of FontBBox) stays."""
    _type3_font_init(self, rsrcmgr, spec)
    try:
        descriptor = resolve1(spec.get("FontDescriptor"))
        descent = resolve1(descriptor.get("Descent")) if isinstance(descriptor, dict) else None
    except Exception:  # noqa: BLE001 — with a broken font descriptor the pdfminer value is kept
        return
    if not isinstance(descent, int | float) or isinstance(descent, bool) or not math.isfinite(descent) or not descent:
        return
    lowest = self.descent  # bottom of FontBBox as taken by pdfminer
    if not isinstance(lowest, int | float) or lowest >= 0:
        return
    # Dividing by 1000 is tried only for fonts whose glyph space is roughly em units (FontMatrix scale 0.1 or more) — not for a
    # large Descent in a 1000-unit font
    candidates = (-abs(float(descent)), -abs(float(descent)) / 1000) if self.vscale >= 0.1 else (-abs(float(descent)),)
    for candidate in candidates:
        if lowest <= candidate:
            self.descent = candidate
            return


pdffont.PDFType3Font.__init__ = _type3_font_with_descriptor_descent


class _EncodingCMap(CMap):
    """Table holding an embedded encoding CMap stream (character code bytes → CID). pdfminer's `CMapParser` is meant for ToUnicode
    and passes a cidrange on as `add_cid2unichr(CID, code bytes)` — here it is taken as code → CID. When a 1-byte code and a 2-byte
    code starting with it collide, the one that came first is kept. `codes` is the same table flattened to (code bytes → CID)
    (`_cid_font_with_code_keyed_unicode`)."""

    def __init__(self) -> None:
        super().__init__()
        self.codes: dict[bytes, int] = {}

    def add_cid2unichr(self, cid: int, code: object) -> None:
        if not isinstance(code, bytes) or not code or not isinstance(cid, int):
            return
        table = self.code2cid
        for byte in code[:-1]:
            table = table.setdefault(byte, {})
            if not isinstance(table, dict):
                return
        if not isinstance(table.get(code[-1]), dict):
            table[code[-1]] = cid
            self.codes[code] = cid


class _EncodingCMapParser(CMapParser):
    """`CMapParser` that reads cidchar as (code bytes, CID). pdfminer unpacks cidchar pairs in (CID, code) order and skips them
    all. The other keywords (including the range expansion bound `_bounded_cmap_keyword`) are unchanged."""

    def do_keyword(self, pos: int, token: object) -> None:
        if token is not self.KEYWORD_ENDCIDCHAR:
            super().do_keyword(pos, token)
            return
        for code, cid in choplist(2, [obj for _, obj in self.popall()]):
            self.cmap.add_cid2unichr(cid, code)


def _embedded_cmap(stream: PDFStream) -> CMap | None:
    """Table read from a CMap stream embedded as the encoding. None if it cannot be read or is empty."""
    cmap = _EncodingCMap()
    try:
        _EncodingCMapParser(cmap, BytesIO(stream.get_data())).run()
    except Exception:  # noqa: BLE001 — a broken stream stays an empty CMap as in pdfminer (`_check_budget` notices an exceeded limit)
        return None
    return cmap if cmap.code2cid else None


_cid_font_cmap = pdffont.PDFCIDFont.get_cmap_from_spec


def _cmap_or_embedded(self: pdffont.PDFCIDFont, spec: dict, strict: bool) -> object:
    """Encoding CMap of a CID font. Even when /Encoding is a stream, pdfminer only looks up a predefined CMap by the CMapName inside
    it and, if there is none, uses an empty CMap, so not one character of that font came out. WeasyPrint embeds the code → CID table
    as a stream for OTF (CFF) Korean fonts (`/WP-Encod-0`) — 4,117 Hangul characters out of the 4,476 characters of an exam paper
    PDF vanished without a warning (Pinned by: test_pdf_round48.py::TestEmbeddedEncodingCMap). When the lookup by name fails and it
    is a stream, that table is read. If it is still empty, the document being converted is told so that each page is compared with
    pdfium (`_UNDECODED_TEXT_RATIO`)."""
    cmap = _cid_font_cmap(self, spec, strict)
    if type(cmap) is not CMap or cmap.code2cid:
        return cmap
    try:
        stream = resolve1(spec.get("Encoding"))
    except Exception:  # noqa: BLE001 — with a broken reference pdfminer's empty CMap stays
        stream = None
    embedded = _embedded_cmap(stream) if isinstance(stream, PDFStream) else None
    if embedded is not None:
        return embedded
    budget = _INFLATE_BUDGET.get()
    if budget is not None:
        budget.undecoded_fonts = True
    return cmap


pdffont.PDFCIDFont.get_cmap_from_spec = _cmap_or_embedded
_cid_font_init = pdffont.PDFCIDFont.__init__


def _cid_font_with_code_keyed_unicode(self: pdffont.PDFCIDFont, rsrcmgr: object, spec: dict, *args: object) -> None:
    """A font that uses an embedded encoding CMap (`_EncodingCMap`) together with ToUnicode uses the character code itself as the
    character number, and its character widths (/W, /W2) are re-keyed to that code.

    By the specification ToUnicode maps character code → character, but pdfminer looks it up by the CID the CMap gave. When the
    embedded table gives a CID different from the code, the character became `(cid:n)` and was deleted, and a fallback font without
    Hangul glyphs (DejaVu) sends hundreds of Hangul codes all to CID 0 (.notdef), so the characters cannot even be told apart by
    CID (a real textbook PDF: 17,810 characters → 5,189, Pinned by: test_pdf_round48.py::TestEmbeddedEncodingCMap::
    test_text_is_read_when_character_codes_differ_from_cids, test_codes_that_all_share_the_notdef_glyph_keep_their_own_letters).
    Without ToUnicode a mapping table looked up by CID (Adobe-Korea1 etc.) has to be used, so the font is left as it is."""
    _cid_font_init(self, rsrcmgr, spec, *args)
    cmap = self.cmap
    if not isinstance(cmap, _EncodingCMap) or type(self.unicode_map) is not FileUnicodeMap:
        return
    numbers = {int.from_bytes(code, "big"): cid for code, cid in cmap.codes.items()}  # code as an integer → original CID
    self.cmap = _EncodingCMap()
    for code in cmap.codes:
        self.cmap.add_cid2unichr(int.from_bytes(code, "big"), code)
    self.widths = {number: self.widths[cid] for number, cid in numbers.items() if cid in self.widths}
    self.disps = {number: self.disps[cid] for number, cid in numbers.items() if cid in self.disps}


pdffont.PDFCIDFont.__init__ = _cid_font_with_code_keyed_unicode


# A page with more text pieces than this is read without text-box grouping (boxes_flow). Grouping takes time and memory with the
# square of the number of text boxes, so a single page of 4,700 scattered characters used 54 seconds and 3.2GB (0.4 seconds without
# grouping). Over 993 pages of public PDFs the maximum is 1,900 and the median 92
# (Pinned by: test_pdf.py::test_page_of_thousands_of_scattered_text_pieces_is_read_without_box_grouping). The number is measured by
# joining pdfium's text pieces into line pieces (`_text_line_pieces`) — Chrome places Hangul character by character, so when counted
# as pieces an ordinary two-column paper page (about 2,900 characters) went over the limit and the column order got mixed
# (Pinned by: test_pdf_resource_limits.py::TestGlyphByGlyphText). The limit value is the owner's decision and stays as it is.
_MAX_GROUPED_TEXT_PIECES = 2_000
# A page with more drawn objects than this (characters, lines, shapes, images, Form XObjects, including inside forms) is not handed
# to pdfminer but read with pdfium's simple extraction. pdfminer creates a layout object per object, so one figure page of a paper
# with 240,000 Form XObjects (480,000 objects, 280 text pieces) used 65 seconds and 4.6GB. Counting takes a little over a second
# even on that page and stops at the limit. Over 993 pages of public PDFs the maximum is 12,500
# (Pinned by: test_pdf.py::TestDrawingArtifacts::test_page_with_huge_number_of_drawing_objects_is_read_with_pdfium).
_MAX_PAGE_OBJECTS = 50_000
# A page with more text pieces than this whose lines and shapes (paths) number at least half as many (a ruled table of 1,210 cells)
# is also read without text-box grouping (`_is_ruled_grid`). Grouping took 3.6 seconds per page, so a 30-page table document went
# over the time budget from page 18 on and fell back to simple extraction without tables. The cell text moves into the table, so
# there is no order for grouping to decide. Known accepted gap: text outside the table on such a page is read top → bottom
# (no warning) (Pinned by: test_pdf_resource_limits.py::TestLargeRuledGrid).
_GRID_MIN_TEXT_PIECES = 1_000
_ATTACHMENT_NAMES_SHOWN = 10  # number of names written in the attachment warning (beyond that, 'and N more')
_ATTACHMENT_TREE_NODES = 10_000  # number of nodes to look at in the attachment name tree
_UNGROUPED_LAPARAMS = {**_LAPARAMS, "boxes_flow": None}
# Tolerance for grouping the characters of a table cell into words. The pdfplumber default (a fixed 3pt) is larger than the word
# gap of a 10–11pt font (0.2 × the character size), so spaces disappeared in table cells only. It is made proportional to the
# character size, on the same basis as body lines (pdfminer word_margin 0.1)
# (Pinned by: test_quality.py::test_pdf_table_cell_keeps_spaces_between_words_in_small_fonts).
_CELL_TEXT = {"x_tolerance_ratio": _LAPARAMS["word_margin"]}
# Table finder settings. By default pdfplumber discards lines shorter than 1pt before joining them. A dotted cell border is drawn
# as hundreds of short 0.4pt lines, so left as is the border disappeared and cells merged (23 names crowded into one cell, and
# values and rows went out of step). If they are not discarded, joining (default 3pt) turns the dotted line into one line.
_TABLE_SETTINGS = {"edge_min_length_prefilter": 0}
# Highlighter rectangle (`_without_highlights`): its height is at most this multiple of the size of the covered characters, its
# left edge is within this distance of the first covered character (ratio of the character size, capped in pt), and its right edge
# reaches at most `_HIGHLIGHT_TAIL_RATIO` × the character size past the last character (the width of a painted trailing space).
# Cell shading is wider than the text by the cell's left padding (LibreOffice 2.8pt, Word 5.4pt), so it is not caught
_HIGHLIGHT_MAX_HEIGHT = 2.0
_HIGHLIGHT_EDGE_RATIO, _HIGHLIGHT_EDGE_MAX = 0.15, 1.5
_HIGHLIGHT_TAIL_RATIO = 0.4
# Filled table row (`_without_cell_rows`): it is a cell if it lines up, within this distance (pt), with a column of words within
# this many lines (multiple of the character size) above or below
_CELL_ROW_REACH, _CELL_ALIGN = 6.0, 1.0
_CID = re.compile(r"\(cid:\d+\)")
# Predefined Unicode CMap name → CIDSystemInfo Ordering (_repair_cid_system_info)
_UNICODE_CMAP = re.compile(r"Uni(KS|JIS|GB|CNS)-")
_CID_ORDERINGS = {"KS": b"Korea1", "JIS": b"Japan1", "GB": b"GB1", "CNS": b"CNS1"}
# Orderings a correctly read CIDSystemInfo of such a font can hold (Japan2 and KR are the newer collections of the same scripts).
_KNOWN_CID_ORDERINGS = frozenset((b"Korea1", b"KR", b"Japan1", b"Japan2", b"GB1", b"CNS1", b"Identity"))
# Bullets in the Symbol and Wingdings fonts come out as private use area characters (a PDF exported from a Word list). These are
# the ones markdown._SYMBOL_FONT_GLYPHS later converts to Unicode symbols.
_SYMBOL_BULLETS = "\uf0b7\uf06c\uf0a1\uf0a7\uf06e\uf071\uf075\uf076\uf0a8\uf0d8\uf0e0\uf0e8\uf0f0\uf0fc\uf0fe\uf02d"
_LONE_BULLET = re.compile(rf"^[•●○◦▪▫■□◆◇►▶➢✓✔※\-–·*{_SYMBOL_BULLETS}]$")
# List number placed apart from the item text ('1.', '2)', '(3)', '가.', 'IV.', '①'). A bare number ('1') is only ever taken as a
# footnote number (`_marker_rows`).
_LIST_NUMBER = re.compile(r"^(?:\(?(?:\d{1,3}|[가나다라마바사아자차카타파하]|[A-Za-z]|[IVX]{2,5}|[ivx]{2,5})[.)]"
                          r"|\((?:\d{1,3}|[가나다라마바사아자차카타파하])\)|[①-⑳Ⅰ-Ⅻ])$")
_BARE_NUMBER = re.compile(r"^\d{1,3}$")
# List number at the start of a cell text line ('1)', '(2)', '가.', '①'). The break before such a line is not a wrap caused by the
# cell width (`_soft_wrapped`).
_BARE_LIST_NUMBER = re.compile(r"^(?:\(?(?:\d{1,3}|[가나다라마바사아자차카타파하])[.)]|\((?:\d{1,3}|[가나다라마바사아자차카타파하])\)|[①-⑳])")
_FOOTNOTE_MARK_PIECE = re.compile(r"^(?:\d{1,3}\)|\*{1,3})$")  # footnote number piece drawn separately ('1)', '*', `_footnote_mark_piece`)
_EQUATION_NUMBER = re.compile(r"^\(\d{1,3}(?:[.\-]\d{1,3})?[a-z]?\)$")  # equation number '(1)', '(2.3)', '(4a)'
_WORD_CHAR = re.compile(r"[0-9A-Za-z가-힣ㄱ-ㅎ一-鿿]")
# Line starting with a bullet symbol. First-level bullets in a slide PDF have large text and were mistaken for headings.
_BULLET_START = re.compile(rf"^[•●○◦▪▫■□◆◇►▶➢✓✔※\-–―·*{_SYMBOL_BULLETS}]")
# Minimum font size ratio (relative to the body) for a heading. The level (#, ##, ###) is assigned not by the ratio but by the rank
# of the heading size within the document.
_HEADING_MIN_RATIO = 1.2
# Borderless table: taken as a table only when at least 3 consecutive lines each have 3 or more short pieces (at most this ratio of
# the page width) at the same height (tolerance in pt) and the pieces sit on the column boundaries of the first line. A two-column
# layout (2 pieces) and long sentence lines are not tables.
_TABLE_ROW_TOLERANCE = 2.5
_ROW_OVERLAP_RATIO = 0.5  # vertical overlap for pieces with offset tops to count as one row (vs. the lower line height, staggered in `_visual_rows`)
_TABLE_CELL_MAX_RATIO = 0.25
_TABLE_MIN_ROWS = 3
_TABLE_MIN_COLUMNS = 3
# Character size ratio (larger / smaller, from after the first data row) up to which a borderless table's row continues the same
# table as the row before. A 12pt heading under a 10pt table (1.2) breaks it; a table whose subtotal row is slightly larger (11/10)
# continues.
_TABLE_SIZE_RATIO = 1.15
# Gap for a line to count as wrapped cell text in a borderless table (_continues_cell): at most this multiple of this table's gap
# between rows, and narrower than the row gap by at least this multiple of the line height. Line spacing inside a cell is clearly
# narrower than row spacing. The staggered lines of a list with tightly packed rows (2pt gap) are not wrapped lines.
_WRAP_GAP_RATIO = 0.6
# The 'narrower by at least this multiple of the line height' side of the condition above. In a Chrome table (4px cell padding) the
# gap between rows is 12.7pt at a 12pt line height and the gap of a wrapped line 6pt, so the difference (6.7pt) fell short of 0.6×
# (7.2pt) and the wrapped line became a row of its own (Pinned by: test_pdf_table_columns.py::TestWideWrappedFirstColumn).
# For a list with a 2pt gap the difference is far below even 0.5×, so it is still filtered out.
_WRAP_GAP_MIN_RATIO = 0.5
# Divider row without values in a borderless table (_is_section_row): the gaps above and below are at most this multiple of this
# table's gap between rows. A caption between tables is set further apart.
_SECTION_ROW_GAP_RATIO = 1.5
# Number of data rows directly above whose column alignment is compared when checking whether a borderless table's row belongs to
# another table (`_out_of_line`).
_OUT_OF_LINE_ROWS = 20
# Cell holding only a numeric value ('1,500', '-3.2%', '(120)', '△45'). When a row without any such cell follows rows with values,
# it is the header row of a new table.
_NUMBER_CELL = re.compile(r"^[(△▲▼+\-−–]?\d[\d,.]*%?\)?$")
# Maximum cell text length of a header row to be repeated on a table continued onto the next page (`_carry_table_headers`). The
# body cell on the previous page of a frame box filling every slide's page (layout caught as a ruled table) was put on as a header.
_HEADER_CELL_MAX_CHARS = 30
# Year and date cells common in header rows ('2024', '2024년', 'FY2024', '2024.12.31', '2024-12'). When deciding on the header row to
# repeat they are not treated as numeric value cells (`_has_value_text`).
_YEAR_OR_DATE = re.compile(r"^(?:FY\s?)?(?:19|20)\d{2}(?:년도?|[.\-/]\s?\d{1,2}(?:[.\-/]\s?\d{1,2})?\.?)?$")
# Gap within a line (relative to the character size, `_split_merged_cells`) taken as a spot where pdfminer (char_margin 2.0) merged
# neighbouring cells into one piece inside a borderless table. Ordinary word gaps are 0.25–0.5×. Between the cells of a narrow
# column (32pt) the cell padding is 6pt + 6pt, which was 1.2× for 10pt characters.
_CELL_GAP_RATIO = 0.8
# A spot this far apart (relative to the character size) without a space character is also a cell boundary candidate
# (`_Line.fine_parts`). In Chrome the table cell padding (6px + 6px) is 0.75× a 10pt character and no space character is placed
# between cells. Between words there is either a space character (mostly) or a gap of 0.25–0.5×.
_UNSPACED_GAP_RATIO = 0.6
# A spot where digit and digit are this far apart without a space character is also a cell boundary candidate (`fine_parts`). The
# 4px + 4px cell padding of a Chrome table is 0.5× a 12pt character, so a right-aligned amount and the next column's percentage
# became the single cell '1,250 65%' (Pinned by: test_pdf_table_columns.py::TestWideWrappedFirstColumn).
# Digits within one number touch each other, and between words there is a space character.
_NUMBER_GAP_RATIO = 0.4
_NUMBER_EDGE = re.compile(r"[\d%‰)]")  # numeric character ending a cell (before the gap)
_NUMBER_START = re.compile(r"[\d(△▲▼+\-−–]")  # numeric character starting a cell (after the gap)
# Difference between the two vertical centres (relative to the line height, `_with_centred_wrapped_cells`) for a cell to count as
# vertically centred beside a two-line cell. In a Chrome table it was 0.4pt, and a neighbouring text box with different line spacing
# (an invoice's '항목 값' (item, value)) was off by 4pt (Pinned by: test_pdf_label_value_rows.py::TestLeftBlockIsNotAWrappedCell).
_CENTRED_CELL_RATIO = 0.2
# Upper bound on the number of visual rows of a centred row whose cells wrapped to different numbers of lines
# (`_centred_band_row`). Keeps each row of a long table with narrow line gaps from being scanned to the end of the table (beyond
# this it is not such a row).
_CENTRED_BAND_ROWS = 16
# Letter-spaced heading: single-character pieces of the same height and size placed at even intervals (widest gap ≤ narrowest gap ×
# this multiple + 1pt).
_SPACED_GAP_SPREAD = 1.5
_SPACED_CHAR = re.compile(r"[가-힣一-鿿]")
# Upper bound on the gap between characters (relative to the character size) for a line to count as written with widened character
# spacing (Tc), and the difference (pt) within which two gaps count as the same spacing. Ordinary word gaps are 0.25–0.5×, so
# character spacing widened beyond that cannot be told from word gaps (`_letter_spacings`).
_LETTER_SPACING_MAX = 0.5
_SPACING_TOLERANCE = 0.3
# Word of an item name spread by justification across one cell of a borderless table (`_spread_over_one_cell`).
_SPREAD_WORD = re.compile(r"^[가-힣一-鿿]{1,4}$")
# Upper bound on the gap (ratio of the page width) between the label column (the end of the longest label) and the value column
# that are joined as a two-column '항목  값' (item, value) layout. Two columns further apart are two side-by-side text columns
# (Pinned by: test_quality.py::test_pdf_side_by_side_form_xobjects_are_not_interleaved).
_LABEL_GAP_RATIO = 0.2
# Width of a value piece (relative to the maximum table cell width; half the page width). A wider value is accepted only when such
# rows are fewer in the group than the rows with narrower values (`_label_value_rows`).
_VALUE_MAX_RATIO = 2.0
# Upper bound on the gap (relative to the line height) between a wrapped line in the value column (`_wraps_value`) and the value
# line above it. This is the line spacing inside a cell.
_VALUE_WRAP_GAP_RATIO = 1.0
# Lower bound on how much smaller a wrapped value line may be than the value line above (relative to the size of the line above).
# The second line of a schedule (speaker, affiliation) is written smaller at 9/11 (`_wraps_value`). A larger line is not accepted —
# it is the next section's heading.
_VALUE_WRAP_MIN_SIZE_RATIO = 0.75
_WRAP_LABEL_RATIO = 0.6  # upper bound on the label width of a group that accepts wrapped value lines (vs. max table cell width; 15% of page width)
_LABEL_MAX_WORDS = 2  # word count of a label that accepts wrapped value lines even beyond the width bound ('총 연구개발기간')
# Upper bound on the gap (relative to the line height, `_wrapped_header_line`) between the first line and a wrapped second line in a
# header cell of a borderless table.
_HEADER_WRAP_GAP_RATIO = 0.6
# Upper bound on the item indentation (relative to the character size, `_pairs_with`) accepted into the same group in a table of
# contents without dot leaders. Up to three levels (20pt each).
_TOC_INDENT_RATIO = 4.0
_LINE_NUMBER_LABEL = re.compile(r"^\s*\d+\s*$")  # label taken as a line number (`_sectioned_values`)
# Right-aligned numeric value joined as '항목 값' (item, value) even beyond the gap bound above (`_right_aligned_numbers`):
# '3,000,000', '₩1,200', '(120)', '15.3%', '2025.03.31', '1,200원', '12건', and the unit values of a product specification table
# '220V', '1.5kW', '60L/일', '52dB', '45kg', '0.5㎡', '6개월' (Pinned by:
# test_pdf_forms_and_amounts.py::TestFarRightAmounts::test_right_aligned_values_with_units_stay_beside_their_items).
_FAR_UNIT = (r"%|원|천원|만원|백만원|억원|건|명|개|회|일|시간|점|년|개월|월|주|분|초|대|매|부|권|층|세|"
             r"[kKmM]?(?:V|W|Wh|A|Hz)|VA|kVA|dB|[mk]?g|t|[mck]?m[23²³]?|[mM]?L|ℓ|℃|°C|"
             r"[㎜㎝㎞㎏㎎㎖㎗㎘㎡㎥㎾㎽㎸㏈]")
_FAR_VALUE = re.compile(rf"^[(△▲▼+\-−–₩$€£¥]{{0,2}}\d[\d,.]*(?:(?:{_FAR_UNIT})(?:/(?:{_FAR_UNIT}|h|s))?)?\)?$")
# Re-reading side-by-side columns (_read_columns_in_turn): when two lines adjacent in reading order are horizontally apart, both
# are wider than a table cell (_TABLE_CELL_MAX_RATIO) and their height difference is at most this multiple of the line height, it
# is a 'jump to the neighbouring column'. Only pages with at least _COLUMN_MIX_MIN such jumps are fixed. A correctly read two-column
# page jumps once, from the bottom of the left column to the top of the right column, and those two are far apart in height.
_COLUMN_MIX_NEAR = 1.5
_COLUMN_MIX_MIN = 3
# Re-reading two columns mixed paragraph by paragraph (_read_paragraph_columns_in_turn): the minimum number of wide lines for a
# column (on the side with more), and the gap (multiple of the line height) within which a line that came back counts as continuing
# directly below the column. A blank line between paragraphs or the space above a subheading does not exceed it; a picture or table
# separating the two columns does.
_COLUMN_LINES_MIN = 5
# Gap (multiple of the line height, `_fills_column_below`) for a line that fills the column width to count as wrapping onto the
# next line. Even the gap between lines at 200% line spacing exceeds it.
_WRAP_LINE_GAP_RATIO = 1.5
# Minimum gap between two table-like rows (`_aligned_pair`) (multiple of the first row's line height). Table rows are apart by the
# cell padding, while a label wrapped onto two lines inside a box sits as close as the line spacing (0.2–0.8×).
_PAIR_ROW_GAP_RATIO = 1.0
# Maximum gap between a separately placed list number and the first line of its item (relative to the number's line height,
# `_marker_rows`). The hanging indent of a Word list (0.25 inch = 18pt) fits within it at a 10pt font.
_MARKER_GAP_RATIO = 3.0
# A line ending in the dot leader of a table of contents or price list (5 or more periods/middle dots, or 2 or more ellipses, in a
# row) and the short value placed separately at its right end (page number, amount). The value starts within `_LEADER_GAP_RATIO` ×
# the character size from the end of the leader and is at most `_LEADER_VALUE_MAX_RATIO` × the character size wide (`_leader_rows`).
# A run of dots is tried only from its first dot (the look-behinds): tried from every dot, each time to the end of the run and back,
# a piece with a long run that does not end the text took time with the square of the run — 18 seconds for 20,000 dots followed by
# a word (Pinned by: test_audit_pdf_tail.py::TestLongDotRunInsideALine). A match from inside a run is also one from its start.
_LEADER_END = re.compile(r"(?:(?<![.·∙・])[.·∙・]{5,}|(?<![…‥])[…‥]{2,})$")  # scattered dots of an OCR text layer ('... .. . .') are not a dot leader
_LEADER_GAP_RATIO = 3.0
_LEADER_VALUE_MAX_RATIO = 10.0
# End of a table-of-contents line whose dot leader and page number are one piece with the item text ('서론 ....... 1') (`_leader_rows`).
_LEADER_TAIL = re.compile(r"(?:(?<![.·∙・])[.·∙・]{5,}|(?<![…‥])[…‥]{2,})\s*\S{1,12}$")
# Dot leader left in the page text (5 or more dots/middle dots or 3 or more ellipses; they may be spaced one apart). Shortened to a
# single ellipsis (`_shortened_leaders`).
_LEADER_RUN = re.compile(r"[ \t]*(?:[.·∙・](?:[ \t]?[.·∙・]){4,}|[…‥](?:[ \t]?[…‥]){2,})[ \t]*")
# Dot leader characters left out of the running character count (`_countable_chars`).
_LEADER_DOTS = re.compile(r"[.·∙・…‥]")
# Side length (pt) of a rectangle taken as a check box, and the maximum distance between the box and its label (a line starting to
# the right of the box or ending to its left) (relative to the larger of the box width and the label's character size,
# `_mark_checkboxes`). 14pt between an 8pt box and a 10pt label is common.
_CHECKBOX_SIDE = (5.0, 16.0)
_CHECKBOX_GAP_RATIO = 2.0
# Distance a check mark may spill outside the box (relative to the box width). A hand-drawn X or an angled stroke goes 1–3pt past
# the box corners.
_CHECKBOX_MARK_OVERFLOW_RATIO = 0.3
_COLUMN_CONTINUE_RATIO = 3.0
# On a page with more lines than this, lines pushed back in a single-column flow are not moved up (`_read_lone_lines_in_place`).
# Each candidate to move looks at the page's line pairs, so it gets slow on a page of scattered text pieces. A body page usually has
# around 100 lines.
_LONE_LINE_MAX_LINES = 500
# Difference between centres (relative to the page width, `_centred_on`) for a line to count as placed in the middle of the page.
_CENTRED_TOLERANCE_RATIO = 0.03
_DIGITS = re.compile(r"\d+")
# Piece that looks like a page number ("3", "- 3 -", "3 / 10", "Page 3", "3쪽"). For a margin line with several pieces at the same
# height, only the digits of this piece are ignored; the rest must match character for character to count as a repeat.
_PAGE_NUMBER = re.compile(r"^[-–(\[]?\s*(?:page|p\.)?\s*\d{1,4}\s*(?:/\s*\d{1,4})?\s*(?:쪽|페이지)?\s*[-–)\]]?$", re.IGNORECASE)
# Token that can be a page number (_page_number_blind): without its digits only punctuation, a page word or a single Latin letter
# remains ('3', '3/10', '3쪽', 'p.3', '2024-01-05', 'A-1'). A value with a unit ('12건') is not one.
_PAGE_WORDS = frozenset({"page", "p", "pp", "of", "쪽", "페이지", "면", "頁"})
# Token that is a value, not a page number: a thousands comma ('3,450,000'), a percent or currency sign, a decimal joining two
# digit groups with one dot ('6.8'). A date written with dots ('2024.01.05') has three digit groups and is not a decimal.
_VALUE_TOKEN = re.compile(r"\d,\d{3}(?!\d)|[%‰₩$€£¥]|^\D*\d+\.\d+\D*$")
_NON_WORD = re.compile(r"[\W_]+")
_TOKEN = re.compile(r"\S+")
# Candidate area for headers and footers (ratio of the page height) and the share of pages needed to count as repeated.
_MARGIN_RATIO = 0.1
_REPEAT_RATIO = 0.5
# Ratio when odd and even pages are counted separately. At one half, the title (always on odd pages) and signature block (always on
# even pages) of a batch of four-page forms were removed. The left/right page headers of books and journals are on 78–100% of those
# pages (Pinned by: test_pdf.py::TestRepeatedMarginsAndBorderlessTables::
# test_batched_four_page_forms_keep_their_titles_and_signature_lines).
_SIDE_REPEAT_RATIO = 2 / 3
# Maximum gap (relative to the line height) for the line above or below a ruled table to count as belonging to that table (title,
# unit, note). Narrower than one skipped line.
_CAPTION_GAP_RATIO = 1.5
# When a row shaped like the margin row also appears this many times at the same position in the body of the same page, it is a
# column's values, not a header or footer.
_DATA_REPEATS = 2
# The same character drawn again within this distance (pt, at most _DUPLICATE_RATIO × the character size) is one character. 'Fake
# bold' draws the same text twice, shifted by 0.3–0.4pt. Kept smaller than the distance at which the same character normally follows
# itself (even the narrowest 'l' is 0.22 × the character size).
_DUPLICATE_DISTANCE = 1.0
_DUPLICATE_RATIO = 0.15
# A copy in a different colour (shadow effect) is looked for beyond 1pt, up to _DUPLICATE_RATIO × the character size. It is searched
# with this grid (pt) (shadows are within 3–4pt).
_SHADOW_GRID = 4.0
# When the slope (sin) of the writing direction is larger than this, the text is taken as slanted rather than horizontal or
# vertical and removed from the body lines (about 6 degrees). In table cells only large characters (watermarks) above
# _WATERMARK_SIZE_RATIO × the page's median character size are removed — the slanted tick labels of a chart axis ('36m 24m',
# 'Pre Post') vanished from chart cells caught as a table (public paper PDF).
_TILT_RATIO = 0.1
# A character smaller than this size (pt) is taken as invisible and removed (text hidden with `0 Tf`). Even the smallest characters
# in real documents are above 2pt.
_MIN_VISIBLE_SIZE = 0.1
# Superscript/subscript estimation (_scripted_text): maximum size of a script relative to the line's usual character size, and the
# minimum distance raised or lowered. Typesetting convention is a size of 0.6–0.7×, a rise of 0.33–0.4 and a drop of 0.15–0.25.
_SCRIPT_SIZE_RATIO = 0.8
_SUPERSCRIPT_RISE = 0.2
_SUBSCRIPT_DROP = 0.1
# The sup/sub of browser (Chrome) printing are 5/6 the size (0.833×) with the baseline shifted by 0.37 / 0.3 em. A character larger
# than 0.8× is a script only when it is at most this size ratio and its baseline is shifted up or down by at least
# _SCRIPT_FAR_SHIFT (relative to the usual character size) — slightly smaller emphasised text raised a little stays as it is
# (Pinned by: test_pdf.py::TestSuperscriptByPosition::test_browser_scripts_five_sixths_the_size_and_shifted_far_are_marked,
# test_slightly_smaller_text_shifted_only_a_little_is_not_marked).
_SCRIPT_FAR_SIZE_RATIO = 0.85
_SCRIPT_FAR_SHIFT = 0.25
_WATERMARK_SIZE_RATIO = 2.0
# Horizontally written watermark: a line at least this multiple of the page's median character size whose colour components are all
# at least _WATERMARK_LIGHT_MIN (light grey) and which overlaps other characters (`_faint_watermark`). Horizontal text may be a
# heading, so it is judged more narrowly than slanted text (_WATERMARK_SIZE_RATIO).
_WATERMARK_FLAT_RATIO = 3.0
_WATERMARK_LIGHT_MIN = 0.7
# Share of the page area for a table to count as a page frame. In slide PDFs the whole page is often a frame box. A table this
# large is dropped as a frame (layout box) only when it has at most _FRAME_MAX_ROWS rows or a single column. When only the area was
# considered, a requirements table filling the page (29 rows × 6 columns) was dropped and its cell text scattered column by column
# (Pinned by: test_pdf.py::TestRuledTables::
# test_grid_filling_the_whole_page_is_a_table_not_a_page_frame, test_page_frame_split_into_two_boxes_is_still_layout).
_PAGE_FRAME_RATIO = 0.8
# In an outer table taken as a form that contains tables within a table, the maximum text length of the label cell in the same row
# as the cell holding the inner table (`_with_inner_tables`).
_NESTED_LABEL_MAX = 20
# Maximum number of lines of a label paragraph that sits to the left of a table or picture and is read before it (`_is_side_label`).
_SIDE_LABEL_MAX_LINES = 3
_FRAME_MAX_ROWS = 2
_FILL_MARK = "■"  # ruled table cell marked only by its fill colour (`_mark_filled_cells`). Same character as DOCX's filled box
_FILL_MIN_COVER = 0.6  # share of the cell area the fill rectangle has to cover
_FILL_WHITE_MIN = 0.95  # white if all fill colour components (0–1) are at least this (`_is_mark_fill`)
_FILL_HUE_MIN = 0.1  # chromatic if the components differ by at least this
_FILL_GREY_MAX = 0.6  # an achromatic fill marks the cell only if all components are this dark or darker
# Shaded table (`_with_shaded_rows`): only a filled rectangle with both sides longer than this (pt) is cell shading (thinner ones
# are ruling lines drawn as fills).
_SHADE_MIN_SIDE = 2.0
_SHADED_ROW_REACH = 1.5  # maximum height of a band accepted as continuing a shaded table fragment (vs. the fragment's median row height)
_BAND_SLACK_RATIO = 0.3  # share of the character height that may stick out of a band and still count as the band's text (font box slack)
# Text reaching deeper than this share of the character height onto both sides of a row boundary is the text of a vertically merged
# cell (`_regridded_fragments`). Chrome placed the text of a cell merged over an even number of rows 25% across the row boundary.
# This tells it from a character box grazing the neighbouring row (0.06pt).
_MERGED_TEXT_CROSSING = 0.15
_CELL_GAP_MIN_RATIO = 0.5  # characters either side of a cell boundary closer than this (vs. character size) are text crossing it (a word gap)
# Gap and overlap (pt) allowed between the end of one segment and the start of the next when joining horizontal line segments into
# one line (_joined_rules). Lines drawn per cell have the same coordinate (gap 0) or overlap by the line width.
_RULE_JOIN_GAP = 0.1
_RULE_JOIN_OVERLAP = 1.0
# Vertical gap (pt) allowed when taking vertical line segments drawn broken at each row as one line (_chained_verticals). Real
# generators break them by 0.25pt
_VERTICAL_JOIN_GAP = 1.5
# Glyph outline curve (_without_glyph_outlines): a curve smaller than this size (pt) both horizontally and vertically, and thicker
# than this on both sides (not a straight line)
_GLYPH_OUTLINE_MAX, _STRAIGHT_MAX = 20.0, 0.1
# Joining a line drawn in pieces (_rejoined_lines): the gap/overlap allowed between the end of one piece and the start of the next,
# and the baseline difference of the two touching characters (all relative to the character size). Measured: gap 0–0.01, baseline
# difference caused by differing fonts 0.07. Kept far smaller than a word gap (0.2 or more).
_PIECE_JOIN_GAP_RATIO = 0.05
# Gap allowed between two pieces with a space character in between (relative to the character size, `_continues_piece`). Hanja in a
# fallback font were 0.07–0.23 apart. Far smaller than the gap between text columns (1em or more).
_PIECE_SPACE_GAP_RATIO = 0.35
_PIECE_BAND = 8.0  # height band (pt) for finding pieces to join. One band above/below is also searched, so a bottom within 8pt is not missed
# A single symbol taken as a piece to join even without word characters (a currency sign drawn separately in a fallback font, a
# raised digit character, `_edge_chars`).
_PIECE_SYMBOL = re.compile(r"^[₩$€£¥¢%‰¹²³⁰⁴-⁹₀-₉°※†‡]$")
_PIECE_BASELINE_RATIO = 0.1
# Pieces on the same baseline whose x ranges overlap (_merged_overlapping_pieces): the vertical overlap is at least this share of
# the lower piece's height, and the height ratio (lower/higher) is at least `_PIECE_HEIGHT_RATIO`. Small scripts and large watermark
# text fall short of the ratios.
_PIECE_OVERLAP_RATIO = 0.8
_PIECE_HEIGHT_RATIO = 0.6
# When the same text starts again within this much (relative to the character height), it is not an overlapping piece but a copy
# drawn on top (fake bold beyond `_DUPLICATE_DISTANCE`).
_PIECE_COPY_SHIFT_RATIO = 0.3
# When the merged characters are put in x order and neighbouring characters overlap by more than this share of the wider one's
# width, it is not an inserted piece but text drawn on top of other text (_glyphs_collide).
_GLYPH_COLLISION_RATIO = 0.25
# Minimum width (relative to the table/picture width) for a paragraph that does not overlap a table or picture horizontally to
# count as 'body text of another column'. Narrow text such as a category name placed separately to the left of a table stays between
# the tables in height order as before — treated as a column, the category names were separated from their tables (public paper PDF).
_COLUMN_MIN_WIDTH_RATIO = 0.5
# Minimum size to crop out as a picture (pt, about 1.4cm). A smaller image is an icon or bullet.
_MIN_FIGURE_POINTS = 40
# Number of rows that must continue with the same boundaries in each group when splitting touching ruled tables at column
# boundaries (`_split_column_sections`).
_SECTION_MIN_ROWS = 2
# Pictures placed side by side at the same height are kept together as one row when there are at least this many (`_picture_rows`).
_PICTURE_ROW_MIN = 3
# Width (pt, `_place_images_in_cells`) a picture may spill outside the cell box and still count as inside a ruled table cell. About
# the cell border thickness plus rounding.
_CELL_IMAGE_SLACK = 2.0
# On a page with characters, an image covering at least this share of the area is the background.
_BACKGROUND_RATIO = 0.9
# When images drawn directly on the page adjoin edge to edge (gap/overlap at most this many pt) and their union covers the page,
# they are one background (_tiled_background).
_TILE_GAP = 2.0
# On a page with 0 characters, the pdfium character count has to exceed this multiple of the hidden character count (outside the
# visible area, size 0, copies) to count as text pdfminer missed.
_HIDDEN_TEXT_RATIO = 1.5
# In a document with an undecoded CID font (`_cmap_or_embedded`), a page where pdfium's character count is larger than this multiple
# of the characters pdfminer read (`_read_chars`) plus `_UNDECODED_MIN_CHARS` is read with pdfium's simple extraction. Only that
# font's text was missing while the text of other fonts remained, so the fallback for pages with 0 characters did not kick in.
_UNDECODED_TEXT_RATIO = 1.25
_UNDECODED_MIN_CHARS = 10
# A page where at least this share of the visible characters has zero width is read with pdfium's simple extraction (`_unmeasured`).
_UNMEASURED_RATIO = 0.5
# When the raw length of a content stream is longer than this but the decoded result is empty, decoding failed (an empty Flate
# stream is 8 bytes, 12 bytes even when wrapped in ASCII85).
_MIN_CONTENT_LENGTH = 16
# A page with an image covering it and a text layer of at most this many characters is a scanned page. Because of a text layer of
# only a few characters — a scanner app stamp, a fax header, a page number — the full-page image was filtered out as "background"
# and the content vanished altogether
# (Pinned by: test_image.py::test_scanned_page_with_a_few_stamp_characters_is_still_sent_whole).
_SCAN_MAX_TEXT_CHARS = 100
# Criterion for not trusting a document's text layer (_untrusted_text_layer_pages): the median area taken up by characters
# (relative to the page) on the pages that have a page-covering image is smaller than this. In real documents a scan holding only
# Latin and digit fragments was 0.033, a scan with proper OCR 0.111, and a designed one-page certificate 0.062 (a value set from 3
# samples).
_UNTRUSTED_LAYER_MAX_TEXT_AREA = 0.05
_HANGUL = re.compile(r"[가-힣]")
_RIGHT_TO_LEFT = re.compile(r"[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufefc]")  # Hebrew and Arabic script (`_late_symbols_in_place`)
# When there are neither characters nor images but this many curve points, the page is taken as one whose characters were converted
# to outlines (a design PDF) and sent whole. One character's outline is dozens of points, so a few words exceed it, and borders and
# dividers (rectangles, straight lines) are not curves and are not counted
# (Pinned by: test_image.py::test_page_of_vector_outlines_without_text_is_sent_whole_for_ocr, test_page_with_only_a_frame_is_not_sent).
_OUTLINE_MIN_POINTS = 200
# Repairing separately placed runs of characters (_repair_placed_runs). Some text in PDFs exported by PowerPoint places the text
# layer separately for each word (each time the font changes) and inserts no space characters. The Hangul width the fallback font
# states (1em) is wider than the real advance width, so the next run starts before the end of the previous run. As a result
# (1) pdfminer, pdfplumber and pdfium, which all find spaces by gaps, read the words run together ("자동차공장냉매주입"), and
# (2) sorting by x coordinate scrambled the character order ("연구원(KIER)" → "연구(K원IER)"). 2 real documents, a dozen pages.
#
# Run = characters placed in one go, where the end of one character and the start of the next are exactly equal. A run's start
# position is exact.
# A page is repaired only when it has at least one font confirmed as "separately placed text". The confirmation conditions are in
# _placement_fit: per font and size there must be enough Hangul, almost no space characters and several overlapping boundaries, and
# the real character width (clearly narrower than the width the font states) and the space width must come out consistently from
# the distances by run length. A document whose character spacing differs per line because of justification (a PDF exported from
# Hangul (HWP)) and a document with tightened character spacing break at every character and do not meet these conditions. With
# looser conditions a space got inserted after every character of a government press release PDF ("새 만 금 기 본 계 획") — do
# not revert.
# - Overlap repair: when the next run starts before the end of the previous run, the character box of the previous run is shrunk
#   to the next start point.
# - Space recovery: before a new run that follows a run ending in Hangul, a space is inserted when the distance left over, computed
#   with the real character width, exceeds half the space width.
# Pinned by: test_quality.py::test_pdf_words_placed_separately_without_space_characters_get_their_spaces_back,
#       test_pdf_with_tight_letter_spacing_does_not_get_extra_spaces, test_pdf_overlapping_runs_keep_reading_order,
#       test_pdf_justified_text_with_varying_letter_spacing_is_left_alone.
# In a two-character word the overlap and the word gap almost cancel out, so the break is just over 2% of the character size
# (measured: -0.025em). For characters placed in one go it is 0.
_RESTART_GAP_RATIO = 0.005
_SPACE_POOR_RATIO = 0.2
_MIN_HANGUL_FOR_REPAIR = 15
_MIN_OVERLAPPING_RESTARTS = 3
# Upper and lower bound on the real character width accepted as separately placed text (relative to the 1em width the font states),
# and the tolerance of the distance fit (em).
# Measured: width 0.865em, space 0.24em. A width close to 1em is a normal document without overlap. The lower bound prevents a
# bogus fit: in a PowerPoint PDF that splits words into two-character + one-character runs (character width as the font states,
# only the word gaps widened by 0.3em), run length and boundary kind (after one character a word gap, after two characters inside a
# word) interlock so that 'width 0.67em, space 0.6em' fits, and a space was inserted at every boundary ('상태 에 저장 된',
# Pinned by: test_pdf_round48.py::TestWordsSplitIntoTwoPiecesAtFullWidth). Hangul advancing by less than 0.8 × the font width
# visibly overlaps its neighbours.
_MAX_PLACED_ADVANCE = 0.95
_MIN_PLACED_ADVANCE = 0.8
_FIT_TOLERANCE = 0.08
# Form field flags (/Ff): password field (bit 14), radio (bit 16), push button (bit 17). Maximum number of levels to follow
# `/Parent` upwards.
_FIELD_PASSWORD = 1 << 13
_FIELD_RADIO = 1 << 15
_FIELD_PUSH_BUTTON = 1 << 16
_FIELD_PARENT_DEPTH = 8
# pdfium (the PDFium C library) is not thread-safe. Every pdfium call in the process (opening a document, rendering, text, closing)
# is serialized with this lock. Without it the process died with SIGSEGV/SIGTRAP when several PDFs were converted at once in a
# thread pool (Pinned by: test_pdf.py::test_concurrent_pdf_conversion_in_threads_does_not_crash). Objects are closed inside the
# lock rather than left to garbage collection.
_PDFIUM_LOCK = threading.RLock()
# A picture is rendered to match its original pixel size, but not beyond this scale (4 = 288dpi). Drawing it smaller than the
# original made the reading of small characters unreliable.
_MAX_FIGURE_SCALE = 4.0
# Maximum number of pixels of the bitmap created by one render (about 5000×5000, 75MB as RGB). Beyond it the scale is lowered. The
# image is sent to the vision model reduced to a long side of `image_max_side` (default 2048), so reading is not affected. Without
# it the bitmap of a large page such as a poster or drawing became several GB
# (Pinned by: test_image.py::TestResourceBounds::test_pdf_figure_on_a_huge_page_renders_only_its_own_area,
# test_pdf_scanned_huge_page_is_rendered_within_the_pixel_cap).
_MAX_RENDER_PIXELS = 25_000_000
# When the number of Form XObject line clusters × the number of text lines on the page is larger than this, nothing is moved
# (finding the position takes time proportional to the product of the two; 4 million ≈ 0.3 seconds).
_XOBJECT_PLACEMENT_MAX_WORK = 4_000_000
# Number of pictures to render per document = max_images × this factor (no limit if max_images is None). Repeated logos are
# filtered out by deduplication after rendering, so some headroom is left.
_RENDER_BUDGET_FACTOR = 2


@dataclass(slots=True)
class _Line:
    top: float
    bottom: float
    x0: float
    x1: float
    text: str
    size: float
    # Pieces of the line split where it is at least `_CELL_GAP_RATIO` × the character size apart ((x0, x1, text), left → right).
    # Filled only when the text read from the pieces equals the line text. Used only to split a piece in which pdfminer merged
    # neighbouring cells inside a borderless table (`_split_merged_cells`).
    parts: tuple[tuple[float, float, str], ...] = ()
    # In addition to `parts`, pieces also split where it is at least `_UNSPACED_GAP_RATIO` × the character size apart without a
    # space character. Used only when splitting lines in which no table was found and searching again (`_split_line_table_runs`).
    fine_parts: tuple[tuple[float, float, str], ...] = ()
    # The original lines merged into this line (cell text wrapped onto two lines, `_with_centred_wrapped_cells`). They are removed
    # from the body flow together when it moves into a table.
    joined: tuple["_Line", ...] = ()
    # If this line is a piece (`parts`) of another line, that original line. Used to remove the original line of a piece moved into
    # a table from the body flow.
    source: "_Line | None" = None

    @property
    def height(self) -> float:
        return max(self.bottom - self.top, 1.0)


@dataclass(slots=True)
class _Table:
    top: float
    text: str
    bottom: float
    x0: float
    x1: float
    # The cell text the table was built from (first row is the header row) and each column's horizontal range (x0, x1). Consulted
    # when repeating the header row on a table continued onto the next page (`_carry_table_headers`). An empty tuple for a table
    # whose column ranges could not be determined.
    rows: list[list[str]] = field(default_factory=list)
    spans: tuple[tuple[float, float], ...] = ()
    # Line pieces of each row of a borderless table. Used to re-fit a continued table to the columns of the previous table (an
    # empty list for a ruled table).
    lines: list[list[_Line]] = field(default_factory=list)
    # Cell boxes of a ruled table (x0, top, x1, bottom) and each row's vertical range (top, bottom). Consulted when placing a
    # picture inside a cell in that cell (`_place_images_in_cells`). An empty tuple if the number of row ranges does not match the
    # number of rows.
    cells: tuple[tuple[float, float, float, float], ...] = ()
    bands: tuple[tuple[float, float], ...] = ()
    # Top coordinate of each text line of a cell holding a picture (index into `cells`). If it matches the number of lines of the
    # cell text, the picture is placed between the lines at that height.
    cell_lines: dict[int, tuple[float, ...]] = field(default_factory=dict)
    # Ruled table continued from a ruled table on the previous page. The top row is a title only when it is a horizontally merged
    # cell (`_carry_table_headers`, `_table_text`).
    continued: bool = False
    # Columns of a ruled table whose bottom cell has no bottom border (a vertically merged cell split at the page end,
    # `_open_below_columns`). Its text is continued in the empty top cell of the next page's table (`_carry_open_cells`).
    open_below: tuple[int, ...] = ()


@dataclass(slots=True)
class _Page:
    number: int
    width: float
    height: float
    lines: list[_Line]
    tables: list[_Table]
    has_images: bool
    # Visible area (x0, top, x1, bottom) = MediaBox ∩ CropBox. Character and picture coordinates are relative to the MediaBox, but
    # pdfium draws only this area.
    view: tuple[float, float, float, float]
    # (x0, top, x1, bottom, original width in pixels / displayed width in pt)
    figures: list[tuple[float, float, float, float, float]] = field(default_factory=list)
    # Has many curves that look like characters converted to outlines (_OUTLINE_MIN_POINTS).
    has_outlines: bool = False
    # Page read with pdfium's simple extraction. It has no line positions or font sizes, so it is left out of header removal and
    # line gap statistics.
    simple: bool = False
    # Separately placed text (_repair_placed_runs): the (real character width, space width) determined on this page. If they could
    # not be determined but a font looks like such text, pending — the page is read again once another page of the same document
    # yields the values.
    placement_fit: tuple[float, float] | None = None
    placement_pending: bool = False
    # Paragraphs read by turning text drawn rotated by 90 degrees within the page (an identifier in the margin, a table set
    # sideways). Left out of the body flow and the table and header checks and placed at the end of the page (_sideways_paragraphs).
    sideways: list[str] = field(default_factory=list)
    # Page of a document whose text layer does not hold the body (_untrusted_text_layer_pages). The whole page is handed over as
    # an image even though it has characters.
    text_untrusted: bool = False
    # Values entered into form fields ('field name: value' lines, _form_values). They are in the widget annotations, not the page
    # content, so they are placed separately at the end of the page.
    form_values: list[str] = field(default_factory=list)
    # Visible area against which the margins of repeated lines are measured (_text_view). None means the whole page (rotated
    # pages, simple-extraction pages).
    text_view: tuple[float, float, float, float] | None = None
    # Number of characters removed as hidden text (outside the visible area, size 0, copies, watermarks) on a page without a single
    # line. Decides whether to swap in pdfium's simple extraction.
    hidden_chars: int = 0
    # Page where most visible characters have zero width, so pdfminer cannot tell character order or spacing (_unmeasured).
    unmeasured: bool = False
    # Page with no text, pictures or vectors at all that has a content stream which failed to decode (_has_undecodable_contents).
    undecodable: bool = False
    # Thin horizontal ruling lines of the page (height, left end, right end; top → bottom, `_thin_rules`). Used to join the
    # multi-line header of a three-line table (`_join_ruled_header_lines`) and to cut a borderless table at its bottom border
    # (`_cut_at_closing_rule`).
    rules: list[tuple[float, float, float]] = field(default_factory=list)
    # Filled rectangles (top, bottom, left end, right end, `_shade_bands`). Used to join the wrapped line of a striped table's last
    # row to that row (`_table_runs`).
    bands: list[tuple[float, float, float, float]] = field(default_factory=list)


@dataclass(slots=True)
class _Paragraph:
    lines: list[_Line] = field(default_factory=list)

    @property
    def top(self) -> float:
        return self.lines[0].top


def extract_pdf(data: bytes, options: ConvertOptions) -> Extraction:
    """Reads with the decompression limit (`_InflateBudget`) in force. The limit is shared by all of pdfminer's stream decoding
    (`_decode_within_budget`) and by the measuring of page streams before pdfium opens a page (`_charge_page`)."""
    budget = _InflateBudget(options.max_uncompressed_bytes, font_entries=_DOCUMENT_FONT_RANGE_ENTRIES)
    token = _INFLATE_BUDGET.set(budget)
    _quiet_pdfminer(True)
    try:
        return _extract_pdf(data, options, budget)
    finally:
        _quiet_pdfminer(False)
        _INFLATE_BUDGET.reset(token)


_PDFMINER_LOGGER = logging.getLogger("pdfminer")
_QUIET_LOCK = threading.Lock()
_quiet_state = {"active": 0, "level": logging.NOTSET}


def _quiet_pdfminer(entering: bool) -> None:
    """Raises the pdfminer logger to ERROR during a conversion and restores the original level when the last conversion ends
    (concurrent conversions are counted).

    Deliberate: pdfminer logs a warning such as 'Could not get FontBBox' for every font without a font descriptor, which flooded
    the server's stderr on every conversion. The result is the same, and telling the user is the job of the warnings list. The
    logger level is process-wide, so pdfminer warnings from elsewhere are not visible during a conversion either (known accepted
    gap). Errors and above still come out. Pinned by: test_pdf_resource_limits.py::TestPdfminerLogNoise.
    """
    with _QUIET_LOCK:
        if entering:
            if not _quiet_state["active"]:
                _quiet_state["level"] = _PDFMINER_LOGGER.level
                _PDFMINER_LOGGER.setLevel(logging.ERROR)
            _quiet_state["active"] += 1
        else:
            _quiet_state["active"] -= 1
            if not _quiet_state["active"]:
                _PDFMINER_LOGGER.setLevel(_quiet_state["level"])


def _check_budget(budget: _InflateBudget) -> None:
    """DocumentTooLargeError if the limit was exceeded. Called before an exception wrapped by pdfplumber (PdfminerException) is
    passed on to pdfium's simple extraction — once passed on, pdfium would inflate the same stream whole again."""
    if budget.exceeded:
        raise DocumentTooLargeError(_TOO_LARGE)


def _extract_pdf(data: bytes, options: ConvertOptions, budget: _InflateBudget) -> Extraction:
    extraction = Extraction()
    try:
        pdf = _open_pdfplumber(data)
    except PdfminerException as exc:
        _check_budget(budget)
        cause = exc.args[0] if exc.args else exc
        if "password" in type(cause).__name__.lower():
            raise EncryptedDocumentError("The PDF is password-protected. Remove the password and upload it again.") from exc
        pages, total, limit = _read_with_pdfium(data, options, exc, extraction)
        failed = [page.number for page in pages]
        dense: set[int] = set()
        crowded = []
        refused = []
    else:
        with pdf:
            total = len(pdf.pages)
            limit = total if options.max_pages is None else min(total, options.max_pages)
            pages = []
            placement_fit: tuple[float, float] | None = None
            fallback: pdfium.PdfDocument | None = None
            ungrouped: object | None = None  # pdfplumber document with text-box grouping off (for pages with very many text pieces)
            dense = set()
            grids: set[int] = set()  # ruled grid pages (read without text-box grouping, but not reported since cell text goes into the table)
            attachments: dict[int, str] = {}  # attachment file specification (id) → name
            started = time.perf_counter()
            over_budget = 0
            failed = []
            crowded: list[int] = []  # pages not handed to pdfminer because they have too many drawn objects
            refused: list[int] = []  # pages nobody opened: too many drawn objects or forms nested too deeply (`_drawn_objects`)
            tally = _EdgeTally(limit, options.remove_repeated_page_lines)
            try:
                for page in pdf.pages[:limit]:
                    try:
                        _charge_page(page, budget)
                        _annotated_attachments(page.page_obj, attachments)
                        if _drawn_objects(page, _MAX_DRAWN_OBJECTS, budget.painted, budget.forms) > _MAX_DRAWN_OBJECTS:
                            # Both pdfium and pdfminer use memory per draw merely by opening the page — hand it to neither
                            refused.append(page.page_number)
                            pages.append(_Page(number=page.page_number, width=float(page.width), height=float(page.height),
                                               lines=[], tables=[], has_images=False,
                                               view=(0.0, 0.0, float(page.width), float(page.height)), simple=True))
                            continue
                        fallback = fallback or _open_pdfium(data)
                        late = options.pdf_time_budget is not None and time.perf_counter() - started > options.pdf_time_budget
                        pieces, line_pieces, paths, crowded_page = ((0, 0, 0, None) if late
                                                                    else _survey_page(fallback, page.page_number - 1))
                        if late:
                            over_budget += 1
                            pages.append(_pdfium_page(fallback, page.page_number - 1))
                        elif crowded_page is not None:
                            crowded.append(page.page_number)
                            pages.append(crowded_page)
                        else:
                            if line_pieces > _MAX_GROUPED_TEXT_PIECES:
                                dense.add(page.page_number)
                            elif _is_ruled_grid(pieces, paths):
                                grids.add(page.page_number)
                            if page.page_number in dense | grids:
                                ungrouped = ungrouped or pdfplumber.open(BytesIO(data), laparams=_UNGROUPED_LAPARAMS)
                            read = _read_page_from(ungrouped if page.page_number in dense | grids else pdf, page, placement_fit)
                            placement_fit = placement_fit or read.placement_fit
                            undecoded = budget.undecoded_fonts and not read.unmeasured and bool(
                                read.lines or read.tables or read.sideways)
                            if (not read.lines and not read.tables and not read.sideways) or read.unmeasured or undecoded:
                                # On some pages pdfminer silently fails to interpret a font and sees 0 characters. If pdfium
                                # finds characters, its result is used. On a scanned page pdfium has no characters either, so
                                # it stays an image page. A page without lines because all its characters are hidden text
                                # (outside the trim line etc.) is swapped only when pdfium reads clearly more than that
                                # (`_HIDDEN_TEXT_RATIO`) — otherwise only the hidden text would come back (Pinned by:
                                # test_pdf.py::TestDrawingArtifacts::
                                # test_page_whose_text_lies_entirely_outside_the_crop_box_stays_empty). A page whose characters
                                # pile up on one spot because their widths are unknown (`_UNMEASURED_RATIO`) is also read by
                                # pdfium with the font's own widths. In a document where an undecoded font was met, a page
                                # with remaining text is swapped too if pdfium reads clearly more (`_UNDECODED_TEXT_RATIO`,
                                # Pinned by: test_pdf_round48.py::TestEmbeddedEncodingCMap).
                                fallback = fallback or _open_pdfium(data)
                                simple = _pdfium_page(fallback, page.page_number - 1)
                                floor = (_read_chars(read) * _UNDECODED_TEXT_RATIO + _UNDECODED_MIN_CHARS if undecoded
                                         else read.hidden_chars * _HIDDEN_TEXT_RATIO)
                                if simple.lines and _solid_chars(simple) > floor:
                                    simple.has_images, simple.figures, simple.view = read.has_images, read.figures, read.view
                                    simple.form_values = read.form_values
                                    read = simple
                                    failed.append(page.page_number)
                            pages.append(read)
                    except PdfminerException:
                        # A page pdfminer cannot read (some AES encryption, broken font streams) is read with pdfium's simple
                        # extraction instead.
                        _check_budget(budget)
                        fallback = fallback or _open_pdfium(data)
                        pages.append(_pdfium_page(fallback, page.page_number - 1))
                        failed.append(page.page_number)
                    finally:
                        page.close()
                    _check_budget(budget)  # an exceeded limit swallowed by broad exception handling (font repair, form reading)
                    # Page analysis is the slowest part of this extractor. Once the character limit is exceeded, the remaining
                    # pages are not read.
                    if extraction.count(_countable_chars(pages[-1], tally), options):
                        break

                # A page with few characters, such as a cover or table of contents, cannot determine the character width on its
                # own. If a later page determined it, only those pages are read again.
                for index, read in enumerate(pages):
                    if placement_fit is None or not read.placement_pending or extraction.exceeded:
                        continue
                    page = (ungrouped if read.number in dense | grids else pdf).pages[read.number - 1]
                    try:
                        pages[index] = _read_page(page, placement_fit)
                    except PdfminerException:
                        pass
                    finally:
                        page.close()
            finally:
                if fallback is not None:
                    with _PDFIUM_LOCK:
                        fallback.close()
                if ungrouped is not None:
                    ungrouped.close()
            extraction.metadata.update(_metadata(pdf.metadata, pdf.doc.info))
            # In an XFA form the content is in XML and the page holds only a 'Please wait...' notice, so the notice alone came out
            # as a 'success'. Reported only when not a single entered value was read from the pages — an XFA form that has both
            # page content and AcroForm fields is read normally
            # (Pinned by: test_pdf.py::TestFormFields::test_xfa_form_without_readable_values_is_reported).
            if _has_xfa(pdf) and not any(page.form_values for page in pages):
                extraction.warnings.append("This is an XFA form document, so the content of the form may not have been read. "
                                           "Upload a file printed to PDF (saved under another name) instead.")
            _named_attachments(pdf.doc.catalog, attachments)
            if attachments:
                names = list(dict.fromkeys(attachments.values()))
                shown = ", ".join(names[:_ATTACHMENT_NAMES_SHOWN]) + (f" and {len(names) - _ATTACHMENT_NAMES_SHOWN} more"
                                                                     if len(names) > _ATTACHMENT_NAMES_SHOWN else "")
                extraction.warnings.append(f"{len(names)} PDF attachment(s) ({shown}) were not read. Convert attachments separately.")
            if over_budget:
                extraction.warnings.append(f"PDF structure analysis exceeded the time budget ({options.pdf_time_budget:g} s), so the "
                                           f"remaining {over_budget} page(s) were read with simple extraction (no tables or headings).")
    extraction.metadata["page_count"] = str(total)
    if budget.font_ranges_cut:
        extraction.warnings.append(_FONT_RANGES_CUT)

    if dense:
        extraction.warnings.append(f"{len(dense)} PDF page(s) ({_page_list(sorted(dense))}) have too many text pieces and were read "
                                   "without text-box reading-order analysis (the order may be off).")
    if refused:
        extraction.warnings.append(f"{len(refused)} PDF page(s) ({_page_list(refused)}){_REPEATED_FORMS_SKIPPED}")
    if crowded:
        extraction.warnings.append(f"{len(crowded)} PDF page(s) ({_page_list(crowded)}) have too many drawing elements and were "
                                   "read with simple extraction (no tables or headings).")
    if failed:
        extraction.warnings.append(f"Structure analysis failed for {len(failed)} PDF page(s) ({_page_list(failed)}), so they were "
                                   "read with simple extraction (no tables or headings).")
    undecodable = [page.number for page in pages if page.undecodable]
    if undecodable:
        extraction.warnings.append(f"The content of {len(undecodable)} PDF page(s) ({_page_list(undecodable)}) could not be decoded "
                                   "(the file may be corrupted).")

    if limit < total:
        extraction.warnings.append(f"Of the PDF's {total} page(s), only the first {limit} were converted.")
    if options.remove_repeated_page_lines:
        _remove_repeated_margins(pages)
    # Checked before borderless tables are taken out of the body lines. Afterwards the lines of a fragmented text layer move into
    # tables and the character area changes.
    untrusted = _untrusted_text_layer_pages(pages) if options.extract_images and not extraction.exceeded else []
    for page in untrusted:
        page.text_untrusted = True
    # The body size is measured before borderless tables are taken out. Table cell characters are body text too, and in a document
    # where only headings remain afterwards the baseline went wrong.
    body_size = _body_font_size(pages)
    for page in pages:
        _join_ruled_header_lines(page)
        _extract_aligned_tables(page)
        _join_row_fragments(page)
        _read_columns_in_turn(page)
        _read_alternating_sides_in_turn(page)
        _read_lone_lines_in_place(page)
        _read_sidebar_first(page)
    _carry_table_headers(pages)
    line_gap = _typical_line_gap(pages)
    heading_levels = _heading_levels(pages, body_size, line_gap)

    scanned = []
    has_cid = False
    # Once the limit is exceeded no pictures are rendered (that leads on to image decoding and vision model calls, all of it wasted).
    renderer = _PageRenderer(data, options) if options.extract_images and not extraction.exceeded else None
    try:
        for page in pages:
            images = _page_images(page, renderer, extraction) if renderer else []
            text = _render_page(page, heading_levels, line_gap, images)
            if _CID.search(text):
                has_cid = True
                text = _CID.sub("", text)
            if (page.has_images or page.has_outlines) and not text.strip():
                scanned.append(page.number)
            extraction.sections.append(Section(SectionKind.PAGE, page.number, text))
    finally:
        if renderer:
            renderer.close()

    if renderer and renderer.skipped:
        extraction.warnings.append(f"There are too many pictures, so {renderer.skipped} of them were not extracted as images.")
    if untrusted:
        extraction.warnings.append(
            f"{len(untrusted)} PDF page(s) ({_page_list([page.number for page in untrusted])}) appear to have no body text in their "
            "text layer (no Hangul and sparse characters), so each whole page was handed over as an image.")
    if scanned:
        extraction.warnings.append(
            f"{len(scanned)} page(s) ({_page_list(scanned)}) whose text exists only as images are empty because they need OCR.")
    if has_cid:
        extraction.warnings.append("Some characters have no Unicode mapping table in their font, so some characters may be missing.")
    return extraction


def _open_pdfplumber(data: bytes) -> object:
    """Opens the document with pdfplumber and also builds the page list. If the page list cannot be built, PdfminerException (same
    as failing to open the document).

    Deliberate: when one page has a malformed page attribute (`/CropBox /Foo`, `/Rotate /Foo`, a box of 3 numbers), pdfplumber
    raises TypeError/IndexError while building the page list, so a whole document that pdfium can read was rejected as a 'corrupted
    document'. pdfplumber builds the page list in one go and that page alone cannot be skipped, so the whole document is read with
    pdfium's simple extraction (Pinned by: test_pdf.py::TestPdfiumFallback::
    test_document_with_a_malformed_page_attribute_is_read_with_pdfium). Not closing it is deliberate — pdfplumber treats the
    BytesIO passed in as an external stream, so all close does is clean up the pages, and in a document with a broken page list
    that can fail again.
    """
    pdf = pdfplumber.open(BytesIO(data), laparams=_LAPARAMS)
    try:
        pages = pdf.pages
    except PdfminerException:
        raise
    except Exception as exc:  # arbitrary exceptions pdfplumber/pdfminer raise on a malformed page attribute
        raise PdfminerException(exc) from exc
    # Deliberate: for a document whose cross-reference offsets are all wrong, pdfplumber returned 0 pages without an exception
    # and the result was empty. If pdfium finds pages, it is handled like a document that could not be opened
    # (Pinned by: test_pdf.py::TestPdfiumFallback::
    # test_document_whose_cross_reference_offsets_are_all_wrong_is_read_with_pdfium).
    if not pages and _pdfium_page_count(data):
        raise PdfminerException(ValueError("pdfminer found no pages"))
    return pdf


def _pdfium_page_count(data: bytes) -> int:
    """Number of pages pdfium found. 0 if it cannot open the document."""
    try:
        document = _open_pdfium(data)
    except pdfium.PdfiumError:
        return 0
    with _PDFIUM_LOCK:
        try:
            return len(document)
        finally:
            document.close()


def _solid_chars(page: _Page) -> int:
    """Number of non-space characters in the page's lines."""
    return sum(len(line.text.replace(" ", "")) for line in page.lines)


def _read_chars(page: _Page) -> int:
    """Number of non-space characters of a page read with pdfminer (lines, table cells, text read rotated)."""
    return _solid_chars(page) + sum(solid_length(text) for text in [*(table.text for table in page.tables), *page.sideways])


@dataclass(slots=True)
class _EdgeTally:
    """Counts, while reading, on how many pages the text of the page margin lines (the rim of `_countable_chars`) appeared.
    Repeated-line removal (`_remove_repeated_margins`) removes only lines that appeared on at least `max(3, k × _REPEAT_RATIO)` of
    the k pages read (or `_SIDE_REPEAT_RATIO` of the pages of the same parity). Text that appeared on c pages up to page j is
    counted if, even appearing on every remaining page to the end (page `limit`), it would fall short of that threshold — then it
    is never removed wherever reading stops; the headroom to the threshold shrinks more slowly than the number of remaining pages,
    so looking at the last page alone is enough.

    The text is counted with digits masked as '#' and whitespace removed. The repeat-detection key (`_row_key`) masks only some of
    the digits or leaves them, so lines with the same key also have the same text here — it never counts too much (the `exceeded`
    invariant in docs/decisions/core.md). In a slide-like document with one or two lines per page every line is a margin line and
    was counted as 0 characters, so page analysis ran to the end even far beyond the limit (1,500 pages in 7–11 seconds, Pinned by:
    test_pdf_round48.py::TestEdgeLinesThatCannotRepeatCount). With the setting that does not remove repeated lines (`removes`
    False), margin lines are all counted too."""

    limit: int
    removes: bool = True
    seen: Counter = field(default_factory=Counter)  # text → number of pages it appeared on
    seen_by_side: tuple[Counter, Counter] = field(default_factory=lambda: (Counter(), Counter()))  # even pages, odd pages

    def unrepeatable(self, number: int, rim: list[_Line]) -> list[_Line]:
        """The lines among the margin lines `rim` of page `number` that cannot be removed."""
        if not self.removes:
            return rim
        keys = [_DIGITS.sub("#", "".join(line.text.split())) for line in rim]
        side = number % 2
        for key in set(keys):
            self.seen[key] += 1
            self.seen_by_side[side][key] += 1
        left = self.limit - number
        side_total, side_read = _side_pages(self.limit, side), _side_pages(number, side)
        return [line for line, key in zip(rim, keys, strict=True)
                if self.seen[key] + left < max(3, self.limit * _REPEAT_RATIO)
                and self.seen_by_side[side][key] + side_total - side_read < max(3, side_total * _SIDE_REPEAT_RATIO)]


def _side_pages(count: int, side: int) -> int:
    """Number of pages among pages 1–`count` whose page number has the given parity (`side` 0 even, 1 odd)."""
    return (count + side) // 2


def _countable_chars(page: _Page, tally: _EdgeTally | None = None) -> int:
    """Number of characters of the page used for the limit check. Never more than the real count: lines that may later be removed
    as repeated headers/footers or page numbers (the margin area and the top and bottom two lines) and `(cid:n)`, which will be
    removed for lack of a font mapping table, are not counted. A margin line that `tally` has determined cannot be removed is
    counted.

    Dots, middle dots and ellipses (`_LEADER_DOTS`) are not counted either. The dot leaders of a table of contents shrink to a
    single ellipsis only after the whole page text has been collected (`_shortened_leaders`), and some become a dot leader only
    after pieces are joined, so the amount that will shrink cannot be known per piece. When they were counted, a table-of-contents
    document whose result is 2,741 characters was rejected as over a limit of 8,223 characters
    (Pinned by: test_pdf_toc_and_checkboxes.py::
    TestDotLeaderPageNumbers::test_long_dot_leaders_do_not_count_against_the_limit_before_they_are_shortened).
    """
    lines = page.lines
    if not page.simple:
        edge = {id(line) for row in _edge_rows(_visual_rows(lines)) for line in row}
        rim = [line for line in lines if id(line) in edge or _in_margin(line, page)]
        kept = {id(line) for line in rim}
        lines = [line for line in lines if id(line) not in kept]
        if tally is not None:
            lines.extend(tally.unrepeatable(page.number, rim))
    return sum(solid_length(_LEADER_DOTS.sub("", _CID.sub("", text)))
               for text in [*(line.text for line in lines), *(table.text for table in page.tables)])


def _read_with_pdfium(data: bytes, options: ConvertOptions, error: Exception,
                      extraction: Extraction) -> tuple[list[_Page], int, int]:
    """When pdfminer cannot open the document at all, reads only the page text with pdfium. If pdfium cannot open it either, the
    document is corrupted.

    As on the normal path, the character count is reported page by page (`Extraction.count`) and the remaining pages are not read
    once the limit is exceeded — without counting, a 200-page document with 300,000 characters per page was read for 122 seconds
    (Pinned by: test_pdf.py::TestPdfiumFallback::
    test_document_pdfminer_cannot_open_stops_reading_pages_once_the_character_limit_is_exceeded). The time budget is not checked —
    the point of the budget is to switch to simple extraction when it is exceeded, and this path is simple extraction from the
    start. Known accepted gap: pdfminer cannot interpret the pages, so neither the decompression limit before opening a page
    (`_charge_page`) nor the drawn-object estimate (`_drawn_objects`) can be applied."""
    try:
        document = _open_pdfium(data)
    except pdfium.PdfiumError as exc:
        if "password" in str(exc).lower():
            raise EncryptedDocumentError("The PDF is password-protected. Remove the password and upload it again.") from exc
        raise CorruptedDocumentError("The PDF structure could not be read.") from error

    try:
        total = len(document)
        limit = total if options.max_pages is None else min(total, options.max_pages)
        pages = []
        for index in range(limit):
            pages.append(_pdfium_page(document, index))
            if extraction.count(_countable_chars(pages[-1]), options):
                break
        return pages, total, limit
    finally:
        with _PDFIUM_LOCK:
            document.close()


def _open_pdfium(data: bytes) -> pdfium.PdfDocument:
    with _PDFIUM_LOCK:
        return pdfium.PdfDocument(data)


def _pdfium_page(document: pdfium.PdfDocument, index: int) -> _Page:
    with _PDFIUM_LOCK:
        page = document[index]
        try:
            return _simple_page(page, index)
        finally:
            page.close()


def _simple_page(page: pdfium.PdfPage, index: int) -> _Page:
    """Reads only the text of a page opened with pdfium, line by line (no line positions or font sizes). Called inside
    `_PDFIUM_LOCK`.

    Whether there are images is looked for only on a page without text — only the scan check (`_is_scanned`) and the OCR warning
    use it, and on a simple-extraction page with text it is unused because there are no picture positions (`figures`). Finding out
    means walking the page's objects one by one in Python, so a 300-page document sharing a form of 400,000 objects still took 1
    second per page after the time budget was exceeded
    (Pinned by: test_pdf_resource_limits.py::TestSimpleReadingOfPagesWithText).
    """
    width, height = page.get_size()
    textpage = page.get_textpage()
    text = textpage.get_text_range()
    textpage.close()

    rows = [" ".join(row.split()) for row in text.splitlines() if row.strip()]
    has_images = not rows and any(True for _ in page.get_objects(filter=(pdfium_c.FPDF_PAGEOBJ_IMAGE,), max_depth=3))
    lines = [_Line(top=float(number), bottom=number + 0.9, x0=0.0, x1=1.0, text=row, size=0.0)
             for number, row in enumerate(rows)]
    return _Page(number=index + 1, width=float(width), height=float(height), lines=lines, tables=[],
                 has_images=has_images, view=(0.0, 0.0, float(width), float(height)), simple=True)


def _read_page_from(source: object, page: object, placement_fit: tuple[float, float] | None) -> _Page:
    """Reads `page` from the `source` document. If that is another document (the one with text-box grouping off), the same page of
    that document is opened, read and closed."""
    if source is page.pdf:
        return _read_page(page, placement_fit)
    other = source.pages[page.page_number - 1]
    try:
        return _read_page(other, placement_fit)
    finally:
        other.close()


def _survey_page(document: pdfium.PdfDocument, index: int) -> tuple[int, int, int, _Page | None]:
    """Surveys a page with pdfium before handing it to pdfminer. (number of text pieces, number of line pieces those pieces join
    into (`_text_line_pieces`), number of line/shape objects, the page read with pdfium if there are too many drawn objects).

    Text pieces (runs of characters continuing on the same line) take a few ms per page. With a generator such as Chrome that
    places every character separately the pieces are almost as many as the characters (13% of the characters for LibreOffice, 71%
    for Chrome Hangul), so the text-box grouping limit is measured in line pieces. Drawn objects are counted down into Form
    XObjects (`_FORM_NESTING_MAX` levels — with pypdfium2's default of 15 levels, 160,000 objects below that went uncounted and on
    to pdfminer) and counting stops the moment `_MAX_PAGE_OBJECTS` is exceeded. Such a page is read right away from the page
    already open — pdfium too spends time and memory per object to open a page (1.7 seconds for a page of 240,000 forms), so it is
    not opened again. The number of lines/shapes (paths) is used to tell ruled grid pages apart (`_is_ruled_grid`).
    """
    with _PDFIUM_LOCK:
        try:
            page = document[index]
        except pdfium.PdfiumError:
            return 0, 0, 0, None
        try:
            try:
                text_page = page.get_textpage()
                try:
                    pieces = text_page.count_rects()
                    line_pieces = _text_line_pieces([text_page.get_rect(number) for number in range(pieces)])
                finally:
                    text_page.close()
            except pdfium.PdfiumError:
                pieces = line_pieces = 0
            paths = objects = 0
            for obj in islice(page.get_objects(max_depth=_FORM_NESTING_MAX), _MAX_PAGE_OBJECTS + 1):
                objects += 1
                paths += obj.type == pdfium_c.FPDF_PAGEOBJ_PATH
            return pieces, line_pieces, paths, _simple_page(page, index) if objects > _MAX_PAGE_OBJECTS else None
        except pdfium.PdfiumError:
            return 0, 0, 0, None
        finally:
            page.close()


def _text_line_pieces(rects: list[tuple[float, float, float, float]]) -> int:
    """Number of line pieces that pdfium's text piece boxes ((left, bottom, right, top)) join into: scanning left → right, a box is
    joined to a preceding piece that overlaps it vertically by at least half the height of the lower one and whose horizontal gap
    is at most the height (the larger one). It is an estimate matched to pdfminer's criterion for joining characters into a line
    (horizontal gap at most `char_margin` × the character width), so a line placed character by character is one piece and
    scattered characters stay separate (`_survey_page`). Only the rightmost piece is kept per 2pt-high bucket and neighbouring
    buckets are checked too, so the time is proportional to the number of pieces.
    """
    rightmost: dict[int, list[float]] = {}  # vertical position bucket → rightmost piece in that bucket [left, bottom, right, top]
    count = 0

    for left, bottom, right, top in sorted(rects):
        height = max(top - bottom, 0.1)
        key = round((top + bottom) / 4)
        for near in (key, key - 1, key + 1):
            piece = rightmost.get(near)
            if (piece is not None and min(piece[3], top) - max(piece[1], bottom) >= min(height, piece[3] - piece[1]) * 0.5
                    and left - piece[2] <= max(height, piece[3] - piece[1])):
                piece[2] = max(piece[2], right)
                rightmost[key] = piece
                break
        else:
            count += 1
            rightmost[key] = [left, bottom, right, top]

    return count


def _normalize_page_boxes(page: object) -> None:
    """Puts the MediaBox and CropBox of the pdfminer page into (lower left, upper right) order. Called before layout analysis.

    By the specification a box may be written with any two opposite corners (`[0 842 595 0]`). pdfplumber corrects the page size,
    but pdfminer shifts the characters using the y of the first corner written (842) as the origin, so all characters went below
    the page and were removed as outside the visible area (Pinned by: test_pdf_page_boxes.py::TestReversedPageBoxes). The
    corrected box is the same as the box pdfplumber uses.
    """
    source = page.page_obj
    for name in ("mediabox", "cropbox"):
        box = getattr(source, name, None)
        if (isinstance(box, list | tuple) and len(box) == 4 and all(isinstance(value, int | float) for value in box)
                and (box[0] > box[2] or box[1] > box[3])):
            x0, y0, x1, y1 = box
            setattr(source, name, (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)))


def _is_ruled_grid(pieces: int, paths: int) -> bool:
    """Whether the page has more than `_GRID_MIN_TEXT_PIECES` text pieces and at least half as many lines/shapes (a large ruled
    table with lines drawn for every cell)."""
    return pieces > _GRID_MIN_TEXT_PIECES and paths * 2 >= pieces


def _repair_cid_system_info(page: object) -> None:
    """In an encrypted document, restores the CIDSystemInfo (Registry, Ordering) of a CID font broken by decryption from the name of
    its encoding CMap.

    reportlab writes these strings of the font dictionary unencrypted even in a PDF with only an owner password (copy protection).
    pdfminer 'decrypts' them and with the broken Ordering cannot find the Unicode mapping table, so every character of a
    non-embedded Korean font (UniKS-UCS2-H) became `(cid:n)` and the page was left with only empty tables (pdfium reads by the
    CMap name). Only fonts in an encrypted document whose Registry/Ordering are not `Adobe` plus a known ordering and whose encoding is a
    predefined `Uni(KS|JIS|GB|CNS)-…` CMap are repaired — correctly decrypted values are not touched. The font dictionary of that
    document cached by pdfminer is what gets repaired, so no global state changes. Known limitation: fonts used only inside a Form
    XObject are not looked at
    (Pinned by: test_pdf.py::test_copy_protected_pdf_with_a_non_embedded_korean_cid_font_keeps_its_text).
    """
    if page.pdf.doc.decipher is None:
        return
    try:
        fonts = resolve1(resolve1(page.page_obj.resources or {}).get("Font"))
        for font in (resolve1(value) for value in fonts.values()) if isinstance(fonts, dict) else []:
            encoding = _UNICODE_CMAP.match(_name(resolve1(font.get("Encoding")))) if isinstance(font, dict) else None
            for descendant in (resolve1(font.get("DescendantFonts")) or []) if encoding else []:
                descendant = resolve1(descendant)
                info = resolve1(descendant.get("CIDSystemInfo")) if isinstance(descendant, dict) else None
                if isinstance(info, dict) and not _sound_cid_system_info(info):
                    info["Registry"], info["Ordering"] = b"Adobe", _CID_ORDERINGS[encoding.group(1)]
    except Exception:  # noqa: BLE001 — the repair is a bonus. Page reading meets a broken reference itself and handles it
        return


def _name(value: object) -> str:
    return str(getattr(value, "name", "") or "")


def _sound_cid_system_info(info: dict) -> bool:
    """Registry `Adobe` with a known Ordering. Checking only for printable ASCII let through wrongly decrypted bytes that happened
    to be printable (`b'"w#"Q'`, `b'(|>%Ue'`): with the random IV of AES about 1 file in 400 kept its broken Ordering and lost all
    of its text (Pinned by: test_pdf.py::test_wrongly_decrypted_font_info_that_happens_to_be_printable_is_still_repaired)."""
    return resolve1(info.get("Registry")) == b"Adobe" and resolve1(info.get("Ordering")) in _KNOWN_CID_ORDERINGS


def _read_page(page: object, placement_fit: tuple[float, float] | None = None) -> _Page:
    _normalize_page_boxes(page)
    _repair_cid_system_info(page)
    layout_page = page  # the layout of a derived page (FilteredPage) redoes layout analysis from scratch, so read from the original page
    unmeasured = _unmeasured(page.chars)
    page, dropped = _without_drawing_artifacts(page)
    hidden_chars = sum(1 for char in dropped if char["text"].strip())
    placement_fit, placement_pending = _repair_placed_runs(page, placement_fit)
    tables = _real_tables(_without_unadvanced_spaces(_without_highlights(_without_glyph_outlines(page))))
    # What is taken out of the body flow is not the whole table area but the characters actually inside cells. Characters where the
    # table finder could not build a cell (e.g. a table continued onto the next page without a bottom border) are not held by any
    # cell, so removing by area would make them vanish silently
    # (Pinned by: test_quality.py::test_pdf_text_inside_a_table_area_is_never_dropped).
    table_boxes = [cell for _, _, cells in tables for cell in cells]
    in_cells = _BoxIndex(table_boxes).holds  # on a page with thousands of cells, scanning every cell per character is quadratic time
    page_chars = page.chars  # the chars of a filtered page (FilteredPage) are filtered again on every access

    # Slanted characters are not put into body lines (_without_drawing_artifacts). Small ones stay on the page for the table cells
    # and are separated out here. Characters drawn rotated by 90 degrees are also taken out of the body lines and read separately
    # after turning (_sideways_paragraphs). Those inside table cells stay as cell text.
    chars, tilted, sideways = [], [], []
    for char in page_chars:
        if not in_cells(char):
            (tilted if _is_tilted(char) else sideways if _is_sideways(char) else chars).append(char)
    chars.sort(key=lambda char: char["top"])
    char_tops = [char["top"] for char in chars]
    dropped = sorted([*(char for char in dropped if not in_cells(char)), *tilted, *sideways],
                     key=lambda char: char["top"])
    dropped_tops = [char["top"] for char in dropped]
    layout_lines = page.objects.get("textlinehorizontal", [])
    owners = _xobject_owners(layout_page)
    if len(owners) != len(layout_lines):
        owners = [None] * len(layout_lines)  # if the order correspondence is not certain, nothing is moved
    else:
        layout_lines, owners = _rejoined_lines(layout_page, layout_lines, owners)
    prepared = []
    # For lines on the same spot (copies of a line drawn on top of the same text) the characters at that height are picked only
    # once — same reason as `overprinted` below
    in_place: dict[tuple[float, float, float, float], list[dict]] = {}
    for line, owner in zip(layout_lines, owners, strict=True):
        text = " ".join(line.get("text", "").split())
        if not text:
            continue
        spot = (line["x0"], line["x1"], line["top"], line["bottom"])
        in_table = in_cells(line)
        if spot in in_place and not in_table:
            prepared.append((line, owner, text, in_place[spot]))
            continue
        level = _chars_at_line_level(line, chars, char_tops)
        if in_table:
            # The line is rebuilt from only the characters outside the cells that were grouped into one line with cell text ('주1)',
            # '원' right up against the table border). Removing the whole line made them vanish, held neither by the cell text nor
            # by the body (Pinned by: test_pdf.py::TestRuledTables::
            # test_text_right_outside_a_cell_on_the_same_line_as_the_cell_text_is_kept).
            line = _part_outside_cells(line, level, page_chars)
            if line is None:
                continue
            text = line["text"]
        inside = [char for char in level if line["x0"] - 1 <= char["x0"] <= line["x1"] + 1]
        if not in_table:
            in_place[spot] = inside
        prepared.append((line, owner, text, inside))
    spacings = _letter_spacings(list({id(inside): inside for *_, inside in prepared}.values()))  # each identical group of characters once

    lines, line_owners = [], []
    kept_lines: dict[str, list[dict]] = {}  # text → lines already kept (to find copies of a line drawn on top of the same text)
    # (spot, text) of lines kept with their text unchanged although copies were nearby. A later line with the same spot and text has
    # the same copies, so `_text_without_artifacts` always drops it; it is dropped without scanning the copies again — a 3.2KB PDF
    # that drew the same text 16,000 times on top of itself through a form scanned all the copies for every line and took 52 seconds
    # (Pinned by: test_pdf_resource_limits.py::TestOverprintedCopiesOfOneLine).
    overprinted: set[tuple[float, float, float, float, str]] = set()
    for line, owner, text, inside in prepared:
        spot = (line["x0"], line["x1"], line["top"], line["bottom"], line.get("text", ""))
        if spot in overprinted:
            continue
        if any(char.get("restored") for char in inside):
            text = _text_from_chars(inside) or text  # the line text pdfminer built lacks the restored spaces
        text = _letter_spaced_text(inside, spacings) or text
        copies = [char for char in _chars_at_line_level(line, dropped, dropped_tops) if line["x0"] - 1 <= char["x0"] <= line["x1"] + 1]
        if copies:
            kept = _text_without_artifacts(line, text, inside, kept_lines, any(char.get("shadow") for char in copies))
            if kept is None:
                continue
            if kept == text:
                overprinted.add(spot)
            text = kept
        kept_lines.setdefault(text, []).append(line)
        text = _scripted_text(inside, text) or text
        sizes = [char["size"] for char in inside if char["text"].strip()]
        size = statistics.median(sizes) if sizes else 0.0
        lines.append(_Line(top=line["top"], bottom=line["bottom"], x0=line["x0"], x1=line["x1"], text=_drawn_text(text),
                           size=size, parts=_gap_parts(inside, _drawn_text(text), size),
                           fine_parts=_gap_parts(inside, _drawn_text(text), size, _UNSPACED_GAP_RATIO)))
        line_owners.append(owner)
    lines = _place_xobject_lines(lines, line_owners)
    _mark_checkboxes(page, lines, [bbox for bbox, _, _ in tables])
    _mark_circle_bullets(page, lines)

    if not lines and chars:
        # A page for which layout analysis could not build lines (rare) is read with simple extraction instead.
        fallback = page.filter(lambda obj: obj.get("object_type") != "char"
                               or not (in_cells(obj) or _is_tilted(obj)))
        for index, text in enumerate((fallback.extract_text() or "").splitlines()):
            if text.strip():
                lines.append(_Line(top=float(index), bottom=float(index) + 0.9, x0=0.0, x1=1.0, text=_drawn_text(text.strip()),
                                   size=0.0))

    view = _view_box(page)
    text_view = _text_view(page)
    sideways_text = [_drawn_text(text) for text in _sideways_paragraphs(sideways)]
    outline_points = (sum(len(curve.get("pts", ())) for curve in page.curves)
                      if not lines and not tables and not sideways_text else 0)
    blank = not (page.chars or page.images or page.curves or page.rects or page.lines or dropped)
    figures = _figure_boxes(page, view)
    return _Page(number=page.page_number, width=float(page.width), height=float(page.height), lines=lines,
                 tables=[_with_open_below(page, _ruled_table(bbox, rows, boxes, page.chars, figures))
                         for bbox, rows, boxes in tables],
                 has_images=bool(page.images), view=view, figures=figures,
                 has_outlines=outline_points >= _OUTLINE_MIN_POINTS,
                 placement_fit=placement_fit, placement_pending=placement_pending, sideways=sideways_text,
                 text_view=text_view[0] if text_view else None,
                 form_values=_form_values(layout_page, lines),
                 hidden_chars=hidden_chars if not lines and not tables and not sideways_text else 0,
                 undecodable=blank and _has_undecodable_contents(layout_page), unmeasured=unmeasured,
                 rules=_thin_rules(page) if lines else [], bands=_shade_bands(page) if lines else [])


def _mark_checkboxes(page: object, lines: list[_Line], tables: list[tuple[float, float, float, float]]) -> None:
    """When a check box drawn as a rectangle sits right beside text, puts ☑ (a line or filled rectangle inside) or ☐ (empty box)
    on that line.

    When the '□ 동의함 □ 동의하지 않음' (agree / do not agree) of a consent form is drawn with rectangles and an angled stroke,
    there are no characters and which side was chosen was lost (Pinned by:
    test_pdf_toc_and_checkboxes.py::TestCheckboxesDrawnAsShapes). Only boxes meeting all of the following are considered
    (deliberate, conservative): an unfilled outlined rectangle, a square with a side in the `_CHECKBOX_SIDE` range (sides differ by
    at most 1.5pt), not touching another rectangle (a row of character cell boxes), outside ruled tables, the box centre within the
    label line's height, no characters inside the box. The label is, of the line starting to the right of the box (mark before the
    text) and the line ending to its left (mark after the text, '동의함 ☑'), the one closer to the box, and that distance is at
    most `_CHECKBOX_GAP_RATIO` × the larger of the box width and the label's character size. On a tie, the right one (the common
    form with the box in front). Looking only to the right, a box after its label attached to the next label and the chosen side
    was swapped (Pinned by: TestCheckboxesBesideTheirLabels). A line already starting (ending) with a box character (☐☑■□) is left
    alone.
    Known accepted gap: boxes inside ruled table cells and boxes wedged between words and grouped into one line are not
    considered. A square bullet drawn only as an outline becomes ☐.
    """
    rects = page.rects
    boxes = [rect for rect in rects
             if rect.get("stroke") and not rect.get("fill") and _CHECKBOX_SIDE[0] <= rect["width"] <= _CHECKBOX_SIDE[1]
             and abs(rect["width"] - rect["height"]) <= 1.5 and not _inside_any(rect, tables)]
    curve_boxes = _curve_boxes(page.curves, tables)
    if not (boxes or curve_boxes) or not lines:
        return

    marks = [*page.curves, *page.lines, *(rect for rect in rects if rect.get("fill"))]
    before: set[int] = set()  # lines with the mark placed before the text
    after: set[int] = set()  # lines with the mark placed after the text

    for box in boxes:
        if any(other is not box and other["x0"] <= box["x1"] + 0.5 and box["x0"] <= other["x1"] + 0.5
               and other["top"] <= box["bottom"] + 0.5 and box["top"] <= other["bottom"] + 0.5
               and not _encloses(_bbox(box), _bbox(other)) and not _encloses(_bbox(other), _bbox(box))
               for other in rects):
            continue  # box touching another rectangle (a row of character cell boxes). A frame around the box or a filled mark inside is fine
        _put_checkbox_mark(box, any(mark is not box and _ticks(box, mark) for mark in marks), page.chars, lines, before, after)

    # For curve boxes (rounded-corner boxes, radio buttons) the curves drawn on top of each other on the same spot (white background
    # + outline) are taken as one group. Without a mark, only if the group has a drawn outline and there is another curve box in
    # the same row — circle bullets placed one per line are not boxes.
    for group in curve_boxes:
        box = group[0]
        ticked = any(all(mark is not member for member in group) and _ticks(box, mark) for mark in marks)
        if not ticked and not (any(member.get("stroke") for member in group)
                               and any(other is not group and other[0]["top"] <= (box["top"] + box["bottom"]) / 2 <= other[0]["bottom"]
                                       for other in curve_boxes)):
            continue
        _put_checkbox_mark(box, ticked, page.chars, lines, before, after)


def _mark_circle_bullets(page: object, lines: list[_Line]) -> None:
    """Puts a small circle bullet drawn as a curve (filled circle → •, outlined circle → ◦) in front of the line that starts to its
    right at the same height.

    Chrome draws `<ul>` bullets not as characters but as 3pt curves (level 1 a filled circle, level 2 an outlined circle), so the
    bullets vanished and the list hierarchy went flat (Pinned by: test_pdf_toc_and_checkboxes.py::TestBulletsDrawnAsCircles). The
    position rule is the same as in `_marker_rows`: a line that starts close to the right of the circle (within
    `_MARKER_GAP_RATIO` × the line height) and whose height contains the circle's vertical centre. The circle has a nearly square
    box, a diameter of at least `_SHADE_MIN_SIDE`, smaller than a check box (`_CHECKBOX_SIDE`) and at most 0.6 × the line height —
    a circle as large as the line height (a radio button) is not a bullet. A line already starting with a bullet or box character
    is left alone.

    Chrome draws level 3 bullets as filled rectangles of the same size (not curves); by the same position rule ▪ is placed (▫ if
    outline only) (Pinned by:
    test_pdf_toc_and_checkboxes.py::TestBulletsDrawnAsCircles::test_third_level_square_bullets_are_kept).
    """
    for curve in [*page.curves, *page.rects]:
        width, height = curve["x1"] - curve["x0"], curve["bottom"] - curve["top"]
        if not (_SHADE_MIN_SIDE <= width < _CHECKBOX_SIDE[0] and abs(width - height) <= 0.5) or not (curve.get("fill") or curve.get("stroke")):
            continue
        middle = (curve["top"] + curve["bottom"]) / 2
        line = min((line for line in lines if line.top <= middle <= line.bottom and width <= line.height * 0.6
                    and curve["x1"] - 0.5 <= line.x0 <= curve["x1"] + line.height * _MARKER_GAP_RATIO),
                   key=lambda line: line.x0, default=None)
        if line is None or _BULLET_START.match(line.text) or line.text[:1] in "☐☑■□":
            continue
        filled = curve.get("fill") and not _is_white(curve.get("non_stroking_color"))
        square = curve.get("object_type") == "rect"
        line.text = f"{('▪' if filled else '▫') if square else ('•' if filled else '◦')} {line.text}"
        line.x0 = curve["x0"]


def _is_white(colour: object) -> bool:
    """Whether the fill colour is white (a numeric colour that is not `_is_shade`). A single grey value (0) is a numeric colour
    too. An unknown colour such as a pattern is not white."""
    colour = (colour,) if isinstance(colour, (int, float)) else colour
    numeric = isinstance(colour, (tuple, list)) and bool(colour) and all(isinstance(value, (int, float)) for value in colour)
    return numeric and not _is_shade(colour)


def _curve_boxes(curves: list[dict], tables: list[tuple[float, float, float, float]]) -> list[list[dict]]:
    """Candidate check boxes and radio buttons drawn as curves: closed (start and end point within 1pt) square-shaped curves (side
    `_CHECKBOX_SIDE`) grouped by same spot (each edge within 1.5pt), largest first. A group lying inside another group (the filled
    dot of a radio button) is a mark and is left out."""
    shapes = [curve for curve in curves
              if (curve.get("stroke") or curve.get("fill")) and _CHECKBOX_SIDE[0] <= curve["width"] <= _CHECKBOX_SIDE[1]
              and abs(curve["width"] - curve["height"]) <= 1.5 and len(curve.get("pts") or ()) >= 4
              and math.dist(curve["pts"][0], curve["pts"][-1]) <= 1.0 and not _inside_any(curve, tables)]
    groups: list[list[dict]] = []

    for shape in sorted(shapes, key=lambda curve: -curve["width"]):
        group = next((group for group in groups if all(abs(a - b) <= 1.5 for a, b in zip(_bbox(group[0]), _bbox(shape), strict=True))),
                     None)
        if group is not None:
            group.append(shape)
        elif not any(_encloses(_bbox(group[0]), _bbox(shape)) for group in groups):
            groups.append([shape])

    return groups


def _put_checkbox_mark(box: dict, ticked: bool, chars: list[dict], lines: list[_Line], before: set[int], after: set[int]) -> None:
    """Puts ☑ (`ticked`) or ☐ on the label line of the box `box`. A box with characters inside or without a label is left alone."""
    if any(box["x0"] < char["x0"] and char["x1"] < box["x1"] and box["top"] < char["top"] and char["bottom"] < box["bottom"]
           for char in chars):
        return
    label = _checkbox_label(box, lines, before, after)
    if label is None:
        return
    line, on_right = label
    mark = "☑" if ticked else "☐"
    line.text = f"{mark} {line.text}" if on_right else f"{line.text} {mark}"
    line.parts = line.fine_parts = ()  # the split pieces do not carry the mark
    (before if on_right else after).add(id(line))


def _checkbox_label(box: dict, lines: list[_Line], before: set[int], after: set[int]) -> tuple[_Line, bool] | None:
    """The label line of the check box `box` and whether that line is to the right of the box. The closer of the line starting to
    the right and the line ending to the left."""
    middle = (box["top"] + box["bottom"]) / 2
    found: list[tuple[float, bool, float, _Line]] = []

    for line in lines:
        if not line.top <= middle <= line.bottom:
            continue
        reach = max(box["width"], line.size) * _CHECKBOX_GAP_RATIO
        right_gap, left_gap = line.x0 - box["x1"], box["x0"] - line.x1
        if 0 <= right_gap <= reach and id(line) not in before and not line.text.startswith(("☐", "☑", "■", "□")):
            found.append((right_gap, False, line.x0, line))
        elif 0 <= left_gap <= reach and id(line) not in after and not line.text.endswith(("☐", "☑", "■", "□")):
            found.append((left_gap, True, -line.x1, line))

    if not found:
        return None
    _, on_left, _, line = min(found, key=lambda item: item[:3])
    return line, not on_left


def _ticks(box: dict, mark: dict) -> bool:
    """Whether `mark` (a line, curve or filled rectangle) is a check mark inside the box: its centre is inside the box, it is at
    least 0.4 × the box width, and it spills outside the box by at most `_CHECKBOX_MARK_OVERFLOW_RATIO` × the box width. A
    hand-drawn X going 1.5pt past the corners used to give an empty box (Pinned by:
    test_pdf_toc_and_checkboxes.py::TestCheckboxesBesideTheirLabels::test_mark_that_overflows_the_box_a_little_is_still_a_tick).
    """
    x0, top, x1, bottom = _bbox(mark)
    slack = box["width"] * _CHECKBOX_MARK_OVERFLOW_RATIO
    return (box["x0"] <= (x0 + x1) / 2 <= box["x1"] and box["top"] <= (top + bottom) / 2 <= box["bottom"]
            and _encloses((box["x0"] - slack, box["top"] - slack, box["x1"] + slack, box["bottom"] + slack), (x0, top, x1, bottom))
            and max(x1 - x0, bottom - top) >= box["width"] * 0.4)


def _bbox(obj: dict) -> tuple[float, float, float, float]:
    return obj["x0"], obj["top"], obj["x1"], obj["bottom"]


def _column_spans(cells: list[tuple], columns: int) -> tuple[tuple[float, float], ...]:
    """Horizontal range of each column, split at the vertical boundaries of a ruled table's cell boxes (`cells`). An empty tuple if
    the number of boundaries does not match the number of columns (`columns`)."""
    edges: list[float] = []
    for x in sorted(edge for cell in cells if cell for edge in (cell[0], cell[2])):
        if not edges or x - edges[-1] > 1.0:
            edges.append(x)
    spans = tuple(zip(edges, edges[1:], strict=False))
    return spans if len(spans) == columns else ()


def _ruled_table(bbox: tuple[float, float, float, float], rows: list[list[str | None]], boxes: list[tuple], chars: list[dict],
                 figures: list[tuple[float, float, float, float, float]]) -> _Table:
    """Turns one ruled table (a result of `_real_tables`) into a `_Table`. For a cell holding a picture the line heights of the cell
    text are stored as well (`_place_images_in_cells`)."""
    cells = [[cell and _drawn_text(cell) for cell in row] for row in rows]
    boxes = tuple(box for box in boxes if box)
    cell_lines = {}

    for index, box in enumerate(boxes):
        if any(_in_cell(figure[:4], box) for figure in figures):
            tops: list[float] = []

            for top in sorted(char["top"] for char in chars if char["text"].strip() and _inside_any(char, [box])):
                if not tops or top - tops[-1] > _TABLE_ROW_TOLERANCE:
                    tops.append(top)

            cell_lines[index] = tuple(tops)
    return _Table(top=bbox[1], text=render_table(cells), bottom=bbox[3], x0=bbox[0], x1=bbox[2], rows=cells,
                  spans=_column_spans(boxes, len(cells[0])), cells=boxes, bands=_row_bands(boxes, len(cells)),
                  cell_lines=cell_lines)


def _with_open_below(page: object, table: _Table) -> _Table:
    """The table with those of its columns put in `open_below` whose bottom cell has, at its bottom edge, a horizontal ruling line
    over less than 80% of the cell width (modified in place). Measuring at each column's own bottom cell keeps the other columns
    from being seen as open in a table that `_closed_at_the_bottom` closed by extending only the merged-cell column down to the end
    of its text (if every column is open it is a split row, `_carry_open_cells`)."""
    open_columns = []

    for column, (x0, x1) in enumerate(table.spans):
        bottom = max((box[3] for box in table.cells if box[0] < (x0 + x1) / 2 < box[2]), default=table.bottom)
        edges = [edge for edge in page.horizontal_edges if abs(edge["top"] - bottom) <= 1.5]
        covered = sum(max(0.0, min(edge["x1"], x1) - max(edge["x0"], x0)) for edge in edges)
        if x1 > x0 and covered < (x1 - x0) * 0.8:
            open_columns.append(column)
    table.open_below = tuple(open_columns)
    return table


def _in_cell(box: tuple[float, float, float, float], cell: tuple[float, float, float, float]) -> bool:
    """Whether the box (x0, top, x1, bottom) lies inside the cell box (it may spill out by `_CELL_IMAGE_SLACK`)."""
    return (cell[0] - _CELL_IMAGE_SLACK <= box[0] and box[2] <= cell[2] + _CELL_IMAGE_SLACK
            and cell[1] - _CELL_IMAGE_SLACK <= box[1] and box[3] <= cell[3] + _CELL_IMAGE_SLACK)


def _row_bands(cells: list[tuple], rows: int) -> tuple[tuple[float, float], ...]:
    """Vertical range of each row, split at the horizontal boundaries of a ruled table's cell boxes (`cells`). An empty tuple if the
    number of boundaries does not match the number of rows (`rows`)."""
    edges: list[float] = []
    for y in sorted(edge for cell in cells if cell for edge in (cell[1], cell[3])):
        if not edges or y - edges[-1] > 1.0:
            edges.append(y)
    bands = tuple(zip(edges, edges[1:], strict=False))
    return bands if len(bands) == rows else ()


def _has_undecodable_contents(page: object) -> bool:
    """Whether the page has a content stream that failed to decode. Called only for a page with no text, pictures or vectors at
    all.

    pdfminer only logs a stream with broken compression and returns it as empty data (not strict mode), so a broken page dropped
    out silently, looking the same as an empty page — a real file in which all 30 pages were like that gave nothing but the 'no
    text was extracted' message. It is a failure when the raw length (`/Length`) exceeds `_MIN_CONTENT_LENGTH` but the decoded
    result is empty, or when decoding raises an exception. A stream of unknown length is not judged
    (Pinned by: test_pdf.py::TestUndecodableContentStreams).
    """
    contents = resolve1(page.page_obj.attrs.get("Contents"))
    for stream in (contents if isinstance(contents, list) else [contents]):
        stream = resolve1(stream)
        if not isinstance(stream, PDFStream):
            continue
        length = resolve1(stream.attrs.get("Length"))
        try:
            data = stream.get_data()
        except Exception:  # noqa: BLE001 — pdfminer raises a different exception for each kind of filter
            return True
        if not data and isinstance(length, int | float) and length > _MIN_CONTENT_LENGTH:
            return True
    return False


def _drawn_text(text: str) -> str:
    """Text made from characters drawn on the page. A character pdfminer delivers as U+00AD (soft hyphen) is replaced by '-'.

    The '–' of a non-embedded Korean CID font (UniKS-UCS2-H) comes out as Adobe-Korea1's hyphen character (mapped to U+00AD), and
    the result clean-up (`markdown._INVISIBLE`) deleted it as an invisible character so that the numbers ran together
    ('2025–2026' → '20252026', Pinned by: test_pdf.py::test_dash_that_arrives_as_a_soft_hyphen_is_kept_as_a_hyphen). Every
    character in a PDF is drawn, so it is a visible hyphen (a hyphen at a line end is visible on screen too). Known limitation: ₩,
    €, £, ¥, the middle dot (·) and ► of the same font are missing from pdfminer's UniKS-UCS2-H mapping table and are dropped
    entirely (pdfium reads them) — not done because it would mean patching pdfminer's CMap globally.
    """
    return text.replace("\xad", "-")


def _chars_at_line_level(line: dict, chars: list[dict], tops: list[float]) -> list[dict]:
    """The characters among `chars` (sorted by top coordinate; `tops` are those top coordinates) whose vertical centre lies within
    the line's top and bottom.

    A line box contains all of its own characters, so none of the line's characters is missed. Picking characters whose top
    coordinate is within ±1pt of the line's top and bottom mixed in characters of the next line at 100% line spacing (less than
    1pt between the bottom of one line and the top of the next, the LibreOffice default), so the script check gave up and a
    footnote number stuck to the number ('135^1%' → '1351%'), and for lines placed word by word the characters of two lines were
    woven into one line (Pinned by: test_pdf.py::
    TestSuperscriptByPosition::test_footnote_mark_on_a_tightly_spaced_line_is_marked_not_merged_into_the_number,
    test_words_placed_separately_on_lines_as_tall_as_the_font_keep_their_lines).
    """
    height = line["bottom"] - line["top"]
    start, end = bisect_left(tops, line["top"] - height), bisect_right(tops, line["bottom"])
    return [char for char in chars[start:end] if line["top"] <= (char["top"] + char["bottom"]) / 2 <= line["bottom"]]


def _part_outside_cells(line: dict, chars: list[dict], page_chars: list[dict]) -> dict | None:
    """Line holding only the characters outside the cells, for a layout line whose centre lies inside a table cell. None if there are
    no characters outside the cells (most cases).

    `chars` are the characters outside the cells around the line's height (characters inside table cells are already removed). The
    characters outside are accepted only when they are at least a word gap (same criterion as `_text_from_chars`) away from the
    characters inside (deliberate): in a chart mistaken for a table the cell border runs through the middle of a word, and accepting
    them produced word fragments such as 'Figur' and 'lative to' as body text and headings (public paper PDF). Such lines are dropped
    as before. A character shorter than half the line height does not belong to that line — a horizontal watermark line lying across
    a table (`_faint_watermark`) was rebuilt from the body characters inside its box and the end of a body line ('계획이다.') came out
    once more (multipage.pdf sample from review round 32; it could not be reproduced with a small fixture, so there is no pinning
    test — same criterion as `_text_without_artifacts`).
    """
    box = [(line["x0"], line["top"], line["x1"], line["bottom"])]
    half = (line["bottom"] - line["top"]) / 2
    outside = {id(char) for char in chars if _inside_any(char, box) and char["bottom"] - char["top"] >= half}
    if not any(char["text"].strip() for char in chars if id(char) in outside):
        return None

    whole = sorted((char for char in page_chars if char["text"].strip() and _inside_any(char, box)), key=lambda char: char["x0"])
    for left, right in zip(whole, whole[1:], strict=False):
        is_boundary = (id(left) in outside) != (id(right) in outside)
        if is_boundary and right["x0"] - left["x1"] <= _LAPARAMS["word_margin"] * max(right["width"], right["height"]):
            return None
    own = [char for char in whole if id(char) in outside]
    return {**line, "x0": min(char["x0"] for char in own), "x1": max(char["x1"] for char in own), "text": _text_from_chars(own)}


def _form_values(page: object, lines: list[_Line]) -> list[str]:
    """Return the values entered in the page's form fields (AcroForm widget annotations) as 'name: value' lines (top to bottom, left
    to right).

    The values of a fillable form are not in the page content but in the annotation's `/V`, so they do not appear in the character
    extraction of pdfminer or pdfium. Only the labels remained and the values were dropped without a warning (Pinned by:
    test_pdf.py::TestFormFields::test_filled_form_values_are_appended_to_their_page).
    - The name is `/TU` (the name shown to the user), or `/T` when absent. Field attributes are inherited from `/Parent` (radio
      groups).
    - Text and choice fields (`/Tx`, `/Ch`) are included only when they have a value, checkboxes as on ☑ / off ☐, and for radio
      buttons only the value of the button that is on. A checkbox field with two or more widgets ('agree / do not agree' under the
      same name) contributes only the export value of the widget that is on, like a radio group — as a ☐·☑ pair the information
      about which one was chosen was lost (Pinned by: test_pdf_form_fields.py::TestGroupedFormFields).
      Push buttons, signatures and password fields are not included.
    - The values are placed at the end of the page instead of being inserted next to their labels (deliberate): fields sit inside
      tables as well as beside body lines, and inserting them at a guessed position unsettles the table and reading-order decisions.
      The field name serves as the label.
    - Text written with a viewer's 'add text' tool (Acrobat Fill & Sign, macOS Preview) is the `/Contents` of a FreeText annotation
      and was dropped for the same reason. It has no name, so the body line (`lines`) immediately to its left on the same row is
      attached as the label ('Name: Hong Gildong', `_free_text`). Hidden annotations (Hidden in `/F`) are not included (Pinned by:
      test_pdf_form_fields.py::TestFreeTextAnnotations).
    pdfplumber's `page.annots` is not used — while resolving annotations all the way down it raised on dictionaries that refer to each
    other (XFA form PDF).
    """
    try:
        placed = []
        for reference in resolve1(page.page_obj.annots) or []:
            widget = resolve1(reference)
            subtype = _name_of(widget.get("Subtype")) if isinstance(widget, dict) else None
            rect = resolve1(widget.get("Rect")) if subtype in ("Widget", "FreeText") else None
            if not isinstance(rect, list) or len(rect) != 4:
                continue
            x0, x1 = sorted(float(value) for value in rect[0::2])
            low, high = sorted(float(value) for value in rect[1::2])
            text = _field_text(widget) if subtype == "Widget" else _free_text(page, widget, (x0, low, x1, high), lines)
            if text:
                placed.append((-high, x0, text))
        return [text for _, _, text in sorted(placed)]
    except Exception:  # noqa: BLE001 — the page body is still read even if the annotations cannot be parsed
        return []


def _free_text(page: object, annotation: dict, rect: tuple[float, float, float, float], lines: list[_Line]) -> str | None:
    """Text of a FreeText annotation. Among the body lines on the same row (vertical overlap of at least half the height of the
    shorter one) that end before the left edge of the annotation box, the nearest one is attached as the label ('Name:' →
    'Name: value'). None for a hidden annotation (`/F` bit 2)."""
    flags = resolve1(annotation.get("F"))
    value = " ".join(_field_string(resolve1(annotation.get("Contents"))).split())
    if not value or (isinstance(flags, int) and flags & 2):
        return None
    left, page_top = float(page.mediabox[0]), float(page.mediabox[3])
    x0, top, bottom = rect[0] - left, page_top - rect[3], page_top - rect[1]

    def beside(line: _Line) -> bool:
        overlap = min(line.bottom, bottom) - max(line.top, top)
        return line.x1 <= x0 + 1 and overlap >= min(line.height, bottom - top) / 2

    label = max((line for line in lines if beside(line)), key=lambda line: line.x1, default=None)
    if label is None:
        return value
    return f"{label.text.rstrip(': ')}: {value}"


def _field_text(widget: dict) -> str | None:
    """'name: value' of one form widget. None if there is no value to include."""
    kind, flags = _name_of(_inherited(widget, "FT")), _inherited(widget, "Ff")
    flags = flags if isinstance(flags, int) else 0
    if kind == "Btn":
        state = _name_of(widget.get("AS")) or _name_of(_inherited(widget, "V")) or "Off"
        grouped = flags & _FIELD_RADIO or _widget_index(widget) is not None
        if flags & _FIELD_PUSH_BUTTON or (grouped and state == "Off"):
            return None
        value = _export_value(widget, state) if grouped else "☐" if state == "Off" else "☑"
    elif kind == "Tx" and not flags & _FIELD_PASSWORD:
        value = _field_string(_inherited(widget, "V"))
    elif kind == "Ch":
        value = _choice_text(widget)
    else:
        return None

    name = _field_string(_inherited(widget, "TU")) or _field_string(_inherited(widget, "T"))
    if not value:
        return None
    return f"{name}: {value}" if name else value


def _choice_text(widget: dict) -> str:
    """Displayed text of the value chosen in a choice field (`/Ch`). `/V` is the export value, so it is mapped through the
    [export value, displayed text] pairs of `/Opt` (single-text entries stay as they are). It used to come out as 'Region: 02'
    (Pinned by: test_pdf_form_fields.py::TestChoiceFieldOptions)."""
    options = _inherited(widget, "Opt")
    shown: dict[str, str] = {}

    for option in options if isinstance(options, list) else []:
        pair = resolve1(option)
        if isinstance(pair, list) and len(pair) == 2:
            shown.setdefault(_field_string(resolve1(pair[0])), _field_string(resolve1(pair[1])))

    selected = _inherited(widget, "V")
    texts = (_field_string(resolve1(item)) for item in (selected if isinstance(selected, list) else [selected]))
    return ", ".join(shown.get(text) or text for text in texts if text)


def _widget_index(widget: dict) -> int | None:
    """Index of the widget among the several `/Kids` of its field (`/Parent`). None if it is not a widget of a field with two or more
    widgets."""
    parent = resolve1(widget.get("Parent"))
    kids = resolve1(parent.get("Kids")) if isinstance(parent, dict) and widget.get("T") is None else None
    if not isinstance(kids, list) or len(kids) < 2:
        return None
    return next((index for index, kid in enumerate(kids) if resolve1(kid) is widget), None)


def _export_value(widget: dict, state: str) -> str:
    """Export value of a button or checkbox that is on: if the field has `/Opt`, the entry at the widget's position (`/Kids` order,
    or the ordinal state name '/1' when it is not found), otherwise the state name. Generators that use `/Opt` write the state name
    as an ordinal, which gave 'grade: 1' (Pinned by: test_pdf_form_fields.py::TestGroupedFormFields)."""
    options = _inherited(widget, "Opt")
    index = _widget_index(widget)
    if index is None and state.isdecimal():
        index = int(state)
    if isinstance(options, list) and index is not None and 0 <= index < len(options):
        return _field_string(resolve1(options[index])) or state
    try:
        return _info_text(state.encode("latin-1"))  # bytes of the name object (a state name written in EUC-KR or UTF-8)
    except UnicodeEncodeError:
        return state  # a name pdfminer has already decoded to text


def _inherited(widget: dict, key: str) -> object:
    """Field attribute. If the widget does not have it, follow `/Parent` upwards to find it (up to `_FIELD_PARENT_DEPTH` levels, to
    guard against documents that refer to each other)."""
    node: object = widget
    for _ in range(_FIELD_PARENT_DEPTH):
        if not isinstance(node, dict):
            break
        if node.get(key) is not None:
            return resolve1(node[key])
        node = resolve1(node.get("Parent"))
    return None


def _name_of(value: object) -> str | None:
    """Name of a PDF name object (`/Widget`). None if it is not a name."""
    name = getattr(resolve1(value), "name", None)
    return name.decode("latin-1") if isinstance(name, bytes) else name


def _field_string(value: object) -> str:
    """Field name or value as one line of text. A choice field with several selections is joined with commas."""
    if isinstance(value, list):
        return ", ".join(part for part in (_field_string(resolve1(item)) for item in value) if part)
    text = _info_text(value) if isinstance(value, bytes) else _name_of(value) or ""
    return " ".join(text.split())


def _annotated_attachments(page: object, found: dict[int, str]) -> None:
    """Add the attachments referenced by the page's FileAttachment annotations to `found` (file specification id → name)."""
    try:
        for reference in resolve1(page.annots) or []:
            annotation = resolve1(reference)
            if isinstance(annotation, dict) and _name_of(annotation.get("Subtype")) == "FileAttachment":
                _add_attachment(resolve1(annotation.get("FS")), None, found)
    except Exception:  # noqa: BLE001 — the page body is still read even if the annotations cannot be parsed
        return


def _named_attachments(catalog: dict, found: dict[int, str]) -> None:
    """Add the attachments in the document catalog's `/Names /EmbeddedFiles` name tree to `found` (following `/Kids`, up to
    `_ATTACHMENT_TREE_NODES` nodes, visiting a reference that comes back only once).

    Attachments are not page content and were dropped without a warning. Their names and count are reported like the attachments of
    an .eml (Pinned by: test_pdf_attachments.py::TestEmbeddedFiles). The attachments themselves are not read (deliberate) — their
    formats vary, and they are converted when uploaded separately.
    """
    try:
        names = resolve1(catalog.get("Names"))
        pending = [resolve1(names.get("EmbeddedFiles"))] if isinstance(names, dict) else []
        seen: set[int] = set()
        while pending and len(seen) < _ATTACHMENT_TREE_NODES:
            node = pending.pop()
            if not isinstance(node, dict) or id(node) in seen:
                continue
            seen.add(id(node))
            pairs = resolve1(node.get("Names"))
            if isinstance(pairs, list):
                for key, value in zip(pairs[::2], pairs[1::2], strict=False):
                    _add_attachment(resolve1(value), resolve1(key), found)
            kids = resolve1(node.get("Kids"))
            if isinstance(kids, list):
                pending.extend(resolve1(kid) for kid in kids)
    except Exception:  # noqa: BLE001 — the body is still read even if the name tree cannot be parsed
        return


def _add_attachment(spec: object, key: object, found: dict[int, str]) -> None:
    """Put one file specification into `found` with its name (`/UF` → `/F` → name tree key)."""
    if isinstance(spec, dict):
        name = next((text for text in (_field_string(resolve1(spec.get(field))) for field in ("UF", "F")) if text), "")
        found.setdefault(id(spec), name or _field_string(key) or "unnamed")
    elif isinstance(spec, bytes):
        found.setdefault(id(spec), _field_string(spec) or "unnamed")  # a specification that is just a file name


def _has_xfa(pdf: object) -> bool:
    """Whether this is an XFA form (`/XFA` in the document catalog's `/AcroForm`)."""
    try:
        form = resolve1(pdf.doc.catalog.get("AcroForm"))
        return isinstance(form, dict) and form.get("XFA") is not None
    except Exception:  # noqa: BLE001 — if the catalog cannot be parsed, treat it as not a form
        return False


def _is_sideways(char: dict) -> bool:
    """Whether the character's writing direction is vertical (up or down) — text drawn rotated 90 degrees within the page. Text on a
    rotated page (/Rotate) has already been set upright by pdfminer, so it is not caught here."""
    a, b = (char.get("matrix") or (1.0, 0.0))[:2]
    norm = (a * a + b * b) ** 0.5
    return norm > 0 and abs(a) <= norm * _TILT_RATIO


def _sideways_paragraphs(chars: list[dict]) -> list[str]:
    """Read characters drawn rotated 90 degrees in their own direction and build paragraph text (lines are joined with newlines).

    pdfminer builds horizontal lines only, so in such text every character became its own line and the order was reversed. An
    identifier in a paper's margin was inserted into the middle of the abstract as '6\n2\n0\n2', and a table laid sideways on a
    portrait page became a table of single characters. The characters are moved to coordinates of the reading direction (u) and the
    line-advance direction (v), and lines, words and paragraphs are split the same way as for horizontal text (Pinned by:
    test_pdf.py::TestSidewaysText).
    - Text read bottom to top (90 degrees counter-clockwise): the next line is to the right.
    - Text read top to bottom (90 degrees clockwise): the next line is to the left.
    """
    paragraphs: list[str] = []
    for upward in (True, False):
        # (u start, u end, v start, v end, char)
        placed = [(-char["bottom"], -char["top"], char["x0"], char["x1"], char["text"]) if upward
                  else (char["top"], char["bottom"], -char["x1"], -char["x0"], char["text"])
                  for char in chars if ((char.get("matrix") or (1.0, 0.0))[1] > 0) == upward]
        placed.sort(key=lambda item: (item[2] + item[3]) / 2)

        lines: list[list[tuple]] = []
        for item in placed:
            center, thickness = (item[2] + item[3]) / 2, max(item[3] - item[2], 1.0)
            if lines and center - (lines[-1][-1][2] + lines[-1][-1][3]) / 2 <= thickness * 0.5:
                lines[-1].append(item)
            else:
                lines.append([item])

        texts: list[str] = []
        previous_end = None
        for line in lines:
            text = _sideways_line_text(line)
            start, end = min(item[2] for item in line), max(item[3] for item in line)
            if text:
                if previous_end is not None and texts and start - previous_end <= (end - start) * 0.8:
                    texts[-1] = f"{texts[-1]}\n{text}"
                else:
                    texts.append(text)
                previous_end = end
        paragraphs.extend(texts)
    return paragraphs


def _sideways_line_text(line: list[tuple]) -> str:
    """Text of one line read in its rotated direction. Whitespace characters and wide gaps (same criterion as `_text_from_chars`)
    are the word spacing."""
    parts: list[str] = []
    previous = None
    for start, end, low, high, text in sorted(line, key=lambda item: item[0]):
        if not text.strip():
            parts.append(" ")
            previous = None
            continue
        if previous is not None and start - previous > _LAPARAMS["word_margin"] * max(end - start, high - low):
            parts.append(" ")
        parts.append(text)
        previous = end
    return " ".join("".join(parts).split())


def _unmeasured(chars: list[dict]) -> bool:
    """Whether at least `_UNMEASURED_RATIO` of the visible characters have zero width.

    A font without /Widths whose standard glyph widths pdfminer does not know either (a non-embedded TimesNewRomanPSMT, an embedded
    font that omits /Widths) has a zero advance width, so all characters of a line pile up at the same x. Removing copies at the same
    position (`_render_char_few_copies`, `_without_drawing_artifacts`) deleted real characters, turning '2024' into '204', and the
    spacing disappeared too. Excluding only zero-width characters from the copy check would no longer stop pages that use the same
    font for fake bold or repeated drawing, so the page as a whole is handed to pdfium (which reads with the font's own widths)
    (Pinned by: test_pdf_fonts.py::TestFontWithoutWidths). Known accepted gap: such a page comes out as plain lines without table or
    heading structure, and on a page where the zero-width font is less than half of the page's characters, characters of that font
    can still be lost as before.
    """
    visible = [char for char in chars if char["text"].strip() and char["size"] >= _MIN_VISIBLE_SIZE]
    narrow = sum(1 for char in visible if char["x1"] - char["x0"] < 0.01 and char.get("upright", True))
    return bool(visible) and narrow >= len(visible) * _UNMEASURED_RATIO


def _without_drawing_artifacts(page: object) -> tuple[object, list[dict]]:
    """Page without the copies of characters drawn twice, tilted characters, zero-size characters and characters outside the visible
    area (the page itself when there is nothing to remove), and the removed characters.

    - Fake bold: instead of a bold font the same text is drawn twice, shifted by 0.3–0.4pt. Left as is, headings came out twice and
      table cells became '항항목목' (every character doubled). When the same character, font and size appears again within
      `_DUPLICATE_DISTANCE`, only the first is kept. pdfplumber's `dedupe_chars` is not used because it looks up positions in the
      character list with `list.index`, which costs the square of the character count (Pinned by:
      test_pdf.py::TestDrawingArtifacts::test_fake_bold_drawn_twice_is_read_once).
    - Diagonal watermarks ('대외비', 'CONFIDENTIAL'): on every page they were inserted character by character at the end of the body
      and, being large, took first place in the heading levels. Large characters (`_WATERMARK_SIZE_RATIO`) whose writing direction is
      neither horizontal nor vertical are removed from the page. Small tilted characters (chart tick labels) are kept for table cells
      and removed from body lines only (`_read_page`). Vertical writing (90 degrees) and flipped text are left alone (Pinned by:
      test_pdf.py::TestDrawingArtifacts::test_diagonal_watermark_is_not_body_text_or_heading,
      test_small_tilted_labels_inside_a_ruled_table_are_kept).
    - Zero-size characters (`0 Tf`, a text matrix that makes the size 0): they are not on screen, yet they were inserted character by
      character into the visible line at the same position, giving 'Htzerosix ello world' (Pinned by:
      test_pdf.py::TestDrawingArtifacts::test_text_drawn_at_zero_size_is_not_read).
      Characters with only a zero width (`0 Tz`) are not removed (deliberate) — a font without /Widths also has zero width in
      pdfminer, but a viewer draws it with the font's own widths, so it is visible text. When such characters make up half the page
      or more, the page is read with pdfium (`_unmeasured`).
    - Characters outside the visible area (`_view_box`): print PDFs put printer's marks outside the trim lines ('….qxd 2/10/04 3:46 PM
      Page 7', proof line numbers). They are not visible on screen, yet they were inserted into the body on every page and became
      broken tables. A character whose centre is outside the area is removed (Pinned by:
      test_pdf.py::TestDrawingArtifacts::test_text_outside_the_crop_box_is_not_read).
    """
    sizes = [char["size"] for char in page.chars if char["text"].strip() and not _is_tilted(char)]
    watermark_size = statistics.median(sizes) * _WATERMARK_SIZE_RATIO if sizes else float("inf")
    faint = _faint_watermark(page.chars, statistics.median(sizes) * _WATERMARK_FLAT_RATIO) if sizes else set()
    view = _text_view(page)

    kept_ids: set[int] = set()
    dropped: list[dict] = []
    cells: dict[tuple, list[dict]] = {}  # (char, font, size, x cell, y cell) → chars kept. Cells are a 1pt grid
    coarse: dict[tuple, list[dict]] = {}  # the same key on the `_SHADOW_GRID` grid (for finding shadows)
    for char in page.chars:
        if (char["size"] < _MIN_VISIBLE_SIZE or (_is_tilted(char) and char["size"] > watermark_size) or id(char) in faint
                or (view and not _inside_any(char, view))):
            dropped.append(char)
            continue
        if char["text"].strip():
            key = (char["text"], char["fontname"], round(char["size"], 1))
            column, row = round(char["x0"] / _DUPLICATE_DISTANCE), round(char["top"] / _DUPLICATE_DISTANCE)
            tolerance = min(_DUPLICATE_DISTANCE, char["size"] * _DUPLICATE_RATIO)
            if any(abs(other["x0"] - char["x0"]) <= tolerance and abs(other["top"] - char["top"]) <= tolerance
                   for dx in (-1, 0, 1) for dy in (-1, 0, 1) for other in cells.get((*key, column + dx, row + dy), ())):
                dropped.append(char)
                continue
            if _is_shadow_copy(char, key, coarse):
                char["shadow"] = True  # the copy-line search (`_text_without_artifacts`) looks as far as the shadow distance
                dropped.append(char)
                continue
            cells.setdefault((*key, column, row), []).append(char)
            coarse.setdefault((*key, round(char["x0"] / _SHADOW_GRID), round(char["top"] / _SHADOW_GRID)), []).append(char)
        kept_ids.add(id(char))
    if not dropped:
        return page, []

    return page.filter(lambda obj: obj.get("object_type") != "char" or id(obj) in kept_ids), dropped


def _faint_watermark(chars: list[dict], min_size: float) -> set[int]:
    """ids of the characters of a faint, large watermark written horizontally: characters of size `min_size` or more whose colour
    components are all `_WATERMARK_LIGHT_MIN` or more are grouped by line (top, size, colour), and only the groups whose bounding box
    overlaps other characters are returned.

    A page with 'DRAFT' written horizontally (6 times the body size, grey 0.85) is not caught by the tilted-text condition
    (`_is_tilted`), so it was inserted as a paragraph between table cells and body text (Pinned by:
    test_pdf.py::TestHorizontalWatermark). Faint large text that does not overlap other text (decorative numbers on covers and
    slides) is kept.
    """
    groups: dict[tuple, list[dict]] = {}
    for char in chars:
        if char["text"].strip() and char["size"] >= min_size and not _is_tilted(char):
            colour = char.get("non_stroking_color")
            colour = (colour,) if isinstance(colour, (int, float)) else colour
            if len(colour or ()) == 4:  # CMYK
                colour = tuple((1 - value) * (1 - colour[3]) for value in colour[:3])
            if (isinstance(colour, (tuple, list)) and colour and all(isinstance(value, (int, float)) for value in colour)
                    and min(colour) >= _WATERMARK_LIGHT_MIN):
                groups.setdefault((round(char["top"]), round(char["size"]), tuple(colour)), []).append(char)

    faint: set[int] = set()
    for group in groups.values():
        ids = {id(char) for char in group}
        x0, x1 = min(char["x0"] for char in group), max(char["x1"] for char in group)
        top, bottom = min(char["top"] for char in group), max(char["bottom"] for char in group)
        if any(id(char) not in ids and char["text"].strip() and char["x0"] < x1 and x0 < char["x1"] and char["top"] < bottom
               and top < char["bottom"] for char in chars):
            faint |= ids
    return faint


def _is_shadow_copy(char: dict, key: tuple, coarse: dict[tuple, list[dict]]) -> bool:
    """Whether `char` is a copy (shadow effect) of the same character kept earlier (`key` = char, font, size), drawn again in a
    different fill colour within `_DUPLICATE_RATIO` times the character size.

    A page that first draws a grey shadow of the heading shifted by 1.2–3pt exceeds the fake-bold criterion (1pt), so every line came
    out twice (Pinned by: test_pdf.py::
    TestDrawingArtifacts::test_shadow_drawn_in_another_colour_slightly_offset_is_read_once). The overlap is closer than the distance
    at which the same character follows itself in running text (even the narrowest 'l' is 0.22 times the character size), so it
    cannot be read as text. Characters of the same colour are not considered (deliberate, conservative).
    """
    tolerance = char["size"] * _DUPLICATE_RATIO
    column, row = round(char["x0"] / _SHADOW_GRID), round(char["top"] / _SHADOW_GRID)
    colour = char.get("non_stroking_color")
    return any(abs(other["x0"] - char["x0"]) <= tolerance and abs(other["top"] - char["top"]) <= tolerance
               and other.get("non_stroking_color") != colour
               for dx in (-1, 0, 1) for dy in (-1, 0, 1) for other in coarse.get((*key, column + dx, row + dy), ()))


def _is_tilted(char: dict) -> bool:
    """Whether the character's writing direction (a, b of the text matrix) is tilted by `_TILT_RATIO` or more from both the horizontal
    and the vertical. Italics (the c component) do not matter."""
    a, b = (char.get("matrix") or (1.0, 0.0))[:2]
    norm = (a * a + b * b) ** 0.5
    return norm > 0 and abs(a) > norm * _TILT_RATIO and abs(b) > norm * _TILT_RATIO


def _text_without_artifacts(line: dict, text: str, inside: list[dict], kept_lines: dict[str, list[dict]],
                            shadowed: bool = False) -> str | None:
    """Text of a layout line that overlaps removed characters (copies, tilted characters). None if the whole line must be dropped.

    pdfminer's lines still contain the removed characters: a line made of watermark characters only, or a copy line with the same
    text at the same position as a line kept earlier, is dropped, and when the line text is longer than the remaining characters
    (a copy is mixed into the same line) it is rebuilt from the remaining characters. When the removed characters include a shadow
    copy (`shadowed`, `_is_shadow_copy`), "the same position" extends to `_DUPLICATE_RATIO` times the line height.

    A character shorter than half the line height is not counted as a character of that line — a large watermark written
    horizontally over the body (`_faint_watermark`) forms a line whose box contains characters of several body lines, so it looked
    like a line with remaining characters (Pinned by: test_pdf.py::TestHorizontalWatermark).
    """
    half = (line["bottom"] - line["top"]) / 2
    own = [char for char in inside if char["text"].strip() and line["top"] <= (char["top"] + char["bottom"]) / 2 <= line["bottom"]
           and char["bottom"] - char["top"] >= half]
    if not own:
        return None
    reach = max(_DUPLICATE_DISTANCE, (line["bottom"] - line["top"]) * _DUPLICATE_RATIO) if shadowed else _DUPLICATE_DISTANCE
    if any(abs(other["x0"] - line["x0"]) <= reach and abs(other["top"] - line["top"]) <= reach
           for other in kept_lines.get(text, ())):
        return None
    if len(text.replace(" ", "")) > len(own):
        return _text_from_chars(own) or text
    return text


def _xobject_owners(page: object) -> list[int | None]:
    """For each line of `page.objects["textlinehorizontal"]`, the number of the outermost Form XObject (LTFigure) that contains it.
    None for page content.

    The order in which pdfplumber walks the layout (the container itself → its children) must be followed exactly so that the order
    matches the line list.
    """
    owners: list[int | None] = []
    figures = 0

    def walk(objects: list, owner: int | None) -> None:
        nonlocal figures
        for obj in objects:
            if isinstance(obj, LTTextLineHorizontal):
                owners.append(owner)  # a line holds only characters, so do not descend further
            elif isinstance(obj, LTContainer):
                if owner is None and isinstance(obj, LTFigure):
                    figures += 1
                    walk(obj._objs, figures)
                else:
                    walk(obj._objs, owner)

    walk(page.layout._objs, None)
    return owners


class _RegroupedLines(LTLayoutContainer):
    """Container that reruns only pdfminer's text-box grouping and reading-order assignment on lines that are already built.

    Only the step where `analyze` groups characters into lines (`group_objects`) is replaced with the prepared lines. The rest
    (text-box grouping, flow order) uses pdfminer's own code — a copy would mask pdfminer's later fixes (same reason as
    `_first_seen_order`).
    """

    def __init__(self, bbox: tuple[float, float, float, float], lines: list[LTTextLineHorizontal]) -> None:
        super().__init__(bbox)
        self._lines = lines
        self.extend(obj for line in lines for obj in line if isinstance(obj, LTChar))  # without characters analyze returns immediately

    def group_objects(self, laparams: object, objs: object) -> object:
        return iter(self._lines)


def _rejoined_lines(page: object, lines: list[dict], owners: list[int | None]) -> tuple[list[dict], list[int | None]]:
    """On a page where one line was drawn as several separate pieces, the (line list, XObject number per line) regrouped after joining
    the pieces. Otherwise the arguments unchanged.

    pdfminer groups only characters that are adjacent in the content stream into a line. On a page where LibreOffice emitted Hangul,
    digits and spaces in different fonts, the characters of one line are drawn in several runs in an order different from the visible
    one, so every piece became a line, text boxes were grouped piece by piece and paragraphs got mixed up ('증가한 482 억 원이며,'
    followed by '본 보고서는 2026 년 3분기'. Pinned by: test_pdf.py::TestLinesDrawnInPieces).
    - The joining condition is in `_continues_piece`. A page with no piece to join is not touched (most pages).
    - Before that, pieces on the same baseline whose x ranges interleave are merged into one piece (`_merged_overlapping_pieces`).
      Cairo-type generators first draw all Hangul runs of a line in one font and then the digit runs in another font, so 8 pieces
      ('보호법」제 조 제 조', '15 ,  17 , 제 조') became heading fragments (Pinned by: test_pdf.py::TestLinesDrawnInPieces::
      test_pieces_of_one_baseline_drawn_font_by_font_that_overlap_are_read_as_one_line).
    - The characters of a joined line are put back in left-to-right order (even within a piece the space was drawn before the word,
      giving '2026  년').
    - When lines change, the text boxes and reading order must change too, so all lines drawn directly on the page are regrouped
      with `_RegroupedLines`. Lines inside Form XObjects are kept after them unchanged (`_place_xobject_lines` decides their
      position). Whitespace-only lines are dropped anyway, so they are left out.
    """
    pieces = _page_text_lines(page.layout)
    if len(pieces) != owners.count(None):
        return lines, owners
    laparams = page.pdf.laparams
    placed = [_late_symbols_in_place(piece, laparams.word_margin) for piece in pieces]
    pieces = [line or piece for line, piece in zip(placed, pieces, strict=True)]
    pieces, merged = _merged_overlapping_pieces(pieces, laparams.word_margin)
    following = _piece_joints(pieces)
    if not following and not merged and not any(placed):
        return lines, owners

    continued = set(following.values())
    rebuilt = []
    for index, piece in enumerate(pieces):
        if index in continued:
            continue
        chars = [obj for obj in piece if isinstance(obj, LTChar)]
        is_joined = index in following
        while index in following:
            index = following[index]
            chars.extend(obj for obj in pieces[index] if isinstance(obj, LTChar))
        if is_joined:
            chars.sort(key=lambda char: char.x0)
        line = LTTextLineHorizontal(laparams.word_margin)
        for char in chars:
            line.add(char)
        rebuilt.append(line)

    # A page whose count estimated from pdfium chunks (`_survey_page`) is within the limit but whose joined lines exceed the limit is
    # not regrouped. It spent the quadratic grouping time of the first read once more, and a single 20KB page took 48 seconds
    # (Pinned by: test_pdf.py::TestLinesDrawnInPieces::
    # test_page_with_more_lines_than_box_grouping_allows_is_not_grouped_twice)
    if laparams.boxes_flow is not None and len(rebuilt) > _MAX_GROUPED_TEXT_PIECES:
        laparams = copy.copy(laparams)
        laparams.boxes_flow = None
    regrouped = _RegroupedLines(page.layout.bbox, rebuilt)
    regrouped.analyze(laparams)
    joined = [page.process_object(line) for line in _page_text_lines(regrouped) if not line.get_text().isspace()]
    nested = [(line, owner) for line, owner in zip(lines, owners, strict=True) if owner is not None]
    return [*joined, *(line for line, _ in nested)], [*([None] * len(joined)), *(owner for _, owner in nested)]


def _late_symbols_in_place(piece: LTTextLineHorizontal, word_margin: float) -> LTTextLineHorizontal | None:
    """Line with symbols put in place that were drawn later than the rest of the line but sit before it (to the left). None if there
    is nothing to fix.

    LibreOffice draws bullet symbols and Roman numerals ('○', '※', 'Ⅰ') in a fallback font after the beginning of the line, so
    pdfminer joined them in drawing order as '5○ 개 기관 선정' and '. Ⅰ 사업 개요' (Pinned by:
    test_pdf_reading_order.py::TestSymbolsDrawnAfterTheirLine). The whole line is re-laid in x order only when every character placed
    to the left of characters drawn before it (the characters to be moved back) is a symbol that is neither a word character
    (`_WORD_CHAR`) nor whitespace, and the characters do not overlap when laid in x order (`_glyphs_collide` — copies drawn on top
    of each other). Deliberate: a line containing right-to-left characters (Hebrew, Arabic) is not touched — for text drawn in
    logical order a decreasing x is correct, and laying it in x order would reverse it (Pinned by:
    test_right_to_left_text_drawn_in_logical_order_is_not_reversed).
    """
    chars = [obj for obj in piece if isinstance(obj, LTChar)]
    if len(chars) < 2 or _RIGHT_TO_LEFT.search("".join(char.get_text() for char in chars)):
        return None

    moved, reach = [], -math.inf

    for char in chars:
        if char.x1 <= reach + char.size * _PIECE_JOIN_GAP_RATIO:
            moved.append(char)
        reach = max(reach, char.x0)

    if not moved or any(not char.get_text().strip() or _WORD_CHAR.search(char.get_text()) for char in moved):
        return None
    ordered = sorted(chars, key=lambda char: char.x0)
    if _glyphs_collide(ordered):
        return None

    line = LTTextLineHorizontal(word_margin)
    for char in ordered:
        line.add(char)
    return line


def _merged_overlapping_pieces(pieces: list[LTTextLineHorizontal], word_margin: float) -> tuple[list[LTTextLineHorizontal], bool]:
    """(piece list, whether anything was merged) after merging pieces on the same baseline whose x ranges overlap (`_shares_baseline`)
    into one piece with the characters re-laid by x. A merged piece takes the place of its first piece. The pieces are scanned in y0
    order so that only pairs that can overlap vertically are examined."""
    order = sorted(range(len(pieces)), key=lambda index: pieces[index].y0)
    parent = list(range(len(pieces)))
    texts = [piece.get_text().strip() for piece in pieces]  # rejoining the text for every pair would build text the square of the piece count times

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for position, index in enumerate(order):
        piece = pieces[index]
        for other in islice(order, position + 1, None):
            if pieces[other].y0 >= piece.y1:
                break
            if _shares_baseline(piece, pieces[other], texts[index], texts[other]):
                parent[root(other)] = root(index)

    groups: dict[int, list[int]] = {}
    for index in range(len(pieces)):
        groups.setdefault(root(index), []).append(index)
    if all(len(group) == 1 for group in groups.values()):
        return pieces, False

    result, done, merged = [], set(), False
    for index in range(len(pieces)):
        group = groups[root(index)]
        chars = sorted((obj for member in group for obj in pieces[member] if isinstance(obj, LTChar)), key=lambda char: char.x0)
        if len(group) == 1 or _glyphs_collide(chars):
            result.append(pieces[index])
        elif root(index) not in done:
            done.add(root(index))
            line = LTTextLineHorizontal(word_margin)
            for char in chars:
                line.add(char)
            result.append(line)
            merged = True
    return result, merged


def _glyphs_collide(chars: list[LTChar]) -> bool:
    """Whether, among characters laid in x order, two neighbours overlap by more than `_GLYPH_COLLISION_RATIO` of the wider one's
    width anywhere (whitespace and zero-width characters excluded).

    Interleaving pieces put their characters in each other's gaps, so they do not overlap. If they do, these are copies drawn at the
    same position (fake bold beyond 1pt, a cover title drawn three times) or overlaid text boxes ('Cookie' and 'Session' on a slide,
    a digit on top of a corrected digit), and merging them interleaves the characters ('데데이이터터랩랩', '44'). Measuring against
    the wider width is deliberate — in Cairo PDFs the advance width the font declares (1em) is wider than the real one, so even
    neighbouring characters of the same run overlap by 13% each, and a narrow character such as a comma overlaps by 60% of its own
    width.
    """
    solid = [char for char in chars if char.get_text().strip() and char.width >= 0.5]
    return any(previous.x1 - char.x0 > max(previous.width, char.width) * _GLYPH_COLLISION_RATIO
               for previous, char in zip(solid, solid[1:], strict=False))


def _shares_baseline(piece: LTTextLineHorizontal, other: LTTextLineHorizontal, text: str, other_text: str) -> bool:
    """Whether the two pieces are text on one baseline whose x ranges interleave (`text` and `other_text` are the precomputed text of
    the two pieces, stripped of surrounding whitespace).

    The vertical overlap must be at least `_PIECE_OVERLAP_RATIO` of the lower piece's height, the height ratio at least
    `_PIECE_HEIGHT_RATIO` (excluding small scripts and large watermark text), and the x overlap must exceed the touching tolerance
    (`_PIECE_JOIN_GAP_RATIO`) — pieces that merely touch are the job of `_piece_joints`, while a piece that overlaps its neighbour by
    1–2pt, like '」' in a fallback font, is merged here. Both must contain a non-whitespace character, and when the same text starts
    again within `_PIECE_COPY_SHIFT_RATIO` it is a copy drawn on top and is not merged (it would be interleaved character by
    character into '항항목목'). Whether the characters actually overlap is checked once more per group (`_glyphs_collide`).
    """
    low, high = min(piece.height, other.height), max(piece.height, other.height)
    if low <= 0 or low < high * _PIECE_HEIGHT_RATIO:
        return False
    if min(piece.y1, other.y1) - max(piece.y0, other.y0) < low * _PIECE_OVERLAP_RATIO:
        return False
    if min(piece.x1, other.x1) - max(piece.x0, other.x0) <= low * _PIECE_JOIN_GAP_RATIO:
        return False
    if not text or not other_text:
        return False
    return not (text == other_text and abs(piece.x0 - other.x0) <= low * _PIECE_COPY_SHIFT_RATIO)


def _page_text_lines(layout: LTContainer) -> list[LTTextLineHorizontal]:
    """Horizontal lines drawn directly on the page (excluding lines inside Form XObjects). The order is the same as
    `_xobject_owners`."""
    found: list[LTTextLineHorizontal] = []

    def walk(objects: list) -> None:
        for obj in objects:
            if isinstance(obj, LTTextLineHorizontal):
                found.append(obj)
            elif isinstance(obj, LTContainer) and not isinstance(obj, LTFigure):
                walk(obj._objs)

    walk(layout._objs)
    return found


def _piece_joints(pieces: list[LTTextLineHorizontal]) -> dict[int, int]:
    """Piece number → number of the piece that directly continues it. A piece is continued only once, and the joins never form a
    cycle.

    Pieces that start within one space width (`_PIECE_SPACE_GAP_RATIO`) of where the previous piece ended (`_piece_ends`) are the
    candidates, and `_continues_piece` decides whether to join.
    """
    edges = [_edge_chars(piece) for piece in pieces]
    starts: dict[tuple[int, int], list[int]] = {}  # (start x, height band) — same-column pieces of other rows are skipped (thousands of table cells)
    spaces: dict[int, list[LTTextLineHorizontal]] = {}
    for index, piece in enumerate(pieces):
        if edges[index] is not None:
            for x in {round(piece.x0), round(edges[index][0].x0)}:
                starts.setdefault((x, math.floor(piece.y0 / _PIECE_BAND)), []).append(index)
        elif _space_piece(piece):
            spaces.setdefault(round(piece.x0), []).append(piece)

    # height band → the start x values in that band (sorted). Scanning integer x over a space width never finished at character size 1e9
    columns: dict[int, list[int]] = {}
    for x, band in starts:
        columns.setdefault(band, []).append(x)
    for xs in columns.values():
        xs.sort()

    following: dict[int, int] = {}
    taken: set[int] = set()
    for index, piece in enumerate(pieces):
        if edges[index] is None:
            continue
        last = edges[index][1]
        reach = max(last.size, 1.0) * _PIECE_SPACE_GAP_RATIO
        band = math.floor(piece.y0 / _PIECE_BAND)
        other = next((other for end in _piece_ends(piece, last, spaces)
                      for x in _starts_between(columns, (band - 1, band, band + 1), round(end) - 1, round(end + reach) + 1)
                      for near in (band - 1, band, band + 1) for other in starts.get((x, near), ())
                      if other not in taken and not _leads_to(following, other, index)
                      and _continues_piece(piece, last, pieces[other], edges[other][0], end)), None)
        if other is not None:
            following[index] = other
            taken.add(other)
    return following


def _starts_between(columns: dict[int, list[int]], bands: tuple[int, ...], low: int, high: int) -> list[int]:
    """The x values at which pieces start in the height bands (`bands`) that lie between `low` and `high` inclusive, smallest first
    (once when duplicated)."""
    found = set()
    for band in bands:
        xs = columns.get(band, ())
        found.update(xs[bisect_left(xs, low):bisect_right(xs, high)])
    return sorted(found)


def _space_piece(piece: LTTextLineHorizontal) -> bool:
    """Whether the piece contains whitespace characters only."""
    chars = [obj for obj in piece if isinstance(obj, LTChar)]
    return bool(chars) and all(not char.get_text().strip() for char in chars)


def _piece_ends(piece: LTTextLineHorizontal, last: LTChar, spaces: dict[int, list[LTTextLineHorizontal]]) -> list[float]:
    """Positions where `piece` (rightmost character `last`) ends: the right edges of the piece box and of the last character, and the
    right edge of a whitespace-only piece of the same line height that starts there. LibreOffice draws the space before a highlight
    separately in another font, leaving 3.1pt between the pieces before and after it, so the pieces were not joined and the line
    order of the paragraph got mixed up (Pinned by:
    test_pdf.py::TestLinesDrawnInPieces::test_piece_after_a_separately_drawn_space_joins_its_line)."""
    tolerance = max(last.size, 1.0) * _PIECE_JOIN_GAP_RATIO
    ends = [piece.x1, last.x1]
    for x in {round(piece.x1) - 1, round(piece.x1), round(piece.x1) + 1, round(last.x1)}:
        ends += [space.x1 for space in spaces.get(x, ())
                 if abs(space.x0 - piece.x1) <= tolerance + 0.5
                 and min(space.y1, last.y1) - max(space.y0, last.y0) >= min(space.height, last.height) * _PIECE_OVERLAP_RATIO]
    return ends


def _leads_to(following: dict[int, int], start: int, target: int) -> bool:
    """Whether following the joined pieces from `start` reaches `target` (including the case where `start` is `target`)."""
    while start != target and start in following:
        start = following[start]
    return start == target


def _edge_chars(piece: LTTextLineHorizontal) -> tuple[LTChar, LTChar] | None:
    """(leftmost character, rightmost character) of the piece — whitespace excluded. None for a piece without any word character
    (whitespace and symbols only).

    They are chosen by position, not as the first and last in drawing order. Even within a piece the characters are drawn in an order
    different from the visible one.
    As an exception a piece of a single symbol (`_PIECE_SYMBOL`) is accepted — '₩' and '¹' drawn later in a fallback font could not
    become a piece to join, and the sentence was split into three paragraphs (Pinned by:
    test_pdf.py::TestLinesDrawnInPieces::test_one_symbol_drawn_later_in_a_fallback_font_joins_its_line).
    """
    chars = [obj for obj in piece if isinstance(obj, LTChar) and obj.get_text().strip()]
    if not any(_WORD_CHAR.match(char.get_text()) for char in chars) and not (len(chars) == 1 and _PIECE_SYMBOL.match(chars[0].get_text())):
        return None
    return min(chars, key=lambda char: char.x0), max(chars, key=lambda char: char.x1)


def _continues_piece(piece: LTTextLineHorizontal, last: LTChar, other: LTTextLineHorizontal, first: LTChar,
                     end: float | None = None) -> bool:
    """Whether `other` is the piece directly after `piece` on the same line (`last` and `first` are the two touching characters, `end`
    is where the previous piece ended as chosen by `_piece_ends`).

    The condition is kept narrow (deliberate): the next piece starts where the previous piece ended (`_PIECE_JOIN_GAP_RATIO`), the two
    touching characters have the same size and baseline (`_PIECE_BASELINE_RATIO`), and both are plain characters. The end position is
    measured both by the piece box and by the last non-whitespace character — sometimes the piece extends to its trailing space
    (') ' followed by '사업'), and sometimes the spaces between later words are drawn first in the earlier piece so that its box
    overflows.
    Lines of formulas are split into pieces to begin with (scripts, brackets several lines tall) and their order is correct — the
    touching pieces in public paper PDFs all differed in size or baseline or were characters of a bracket-piece font (private use
    area, `(cid:n)`). Widening the condition changes the reading order of those pages.
    One exception: when the previous piece ends with a footnote mark (`_trailing_footnote_mark`), size and baseline are measured on
    the plain character before it. A piece holding only a footnote mark ('1)', `_footnote_mark_piece`) is joined when it is smaller
    and raised relative to the touching characters before and after it.
    The baselines are the same when either the bottom of the character box (descender length differs per font) or the glyph origin
    (the text matrix y of an upright character) matches — a symbol drawn in a fallback font had its box bottom off by 1.4pt (10pt).
    Even without touching, when the previous piece ends with a space character or the next piece starts with one, they are joined up
    to one space width (`_PIECE_SPACE_GAP_RATIO`) — Hanja ('先', '中') drawn by LibreOffice in a fallback font was 0.7–2.3pt (10pt)
    away after/before the neighbouring piece's space, and the sentence was split into three paragraphs (Pinned by:
    test_pdf.py::TestLinesDrawnInPieces::test_fallback_font_piece_next_to_a_space_joins_its_line).
    """
    size = max(first.size, 1.0)
    tolerance = size * _PIECE_JOIN_GAP_RATIO
    touches = min(abs(other.x0 - piece.x1), abs(first.x0 - last.x1), abs(other.x0 - (piece.x1 if end is None else end))) <= tolerance
    if not touches and (piece.x1 - last.x1 > tolerance or first.x0 - other.x0 > tolerance):  # there is a space character in between
        touches = -tolerance <= other.x0 - piece.x1 <= size * _PIECE_SPACE_GAP_RATIO
    if touches and (_footnote_mark_piece(other, last) or _footnote_mark_piece(piece, first)):
        return True
    last = _trailing_footnote_mark(piece, last, first) or last
    same_baseline = (abs(last.y0 - first.y0) <= size * _PIECE_BASELINE_RATIO
                     or (_upright(last) and _upright(first) and abs(last.matrix[5] - first.matrix[5]) <= size * _PIECE_BASELINE_RATIO))
    return (touches and abs(last.size - first.size) <= 0.1 and same_baseline
            and _is_plain_glyph(last.get_text()) and _is_plain_glyph(first.get_text()))


def _upright(char: LTChar) -> bool:
    """Whether the text matrix is horizontal text that is neither rotated nor skewed (the origin y can be used as the baseline)."""
    matrix = getattr(char, "matrix", None)
    return bool(matrix) and matrix[1] == 0 and matrix[2] == 0 and matrix[0] > 0 and matrix[3] > 0


def _trailing_footnote_mark(piece: LTTextLineHorizontal, last: LTChar, first: LTChar) -> LTChar | None:
    """If `piece` ends with a footnote mark, the plain character right before the mark. Otherwise None.

    A footnote mark is a digit, `)` or `*` attached to the end of the piece that is smaller than `first` (the first character of the
    next piece; `_SCRIPT_SIZE_RATIO` or less) and raised (`_SUPERSCRIPT_RISE` or more). LibreOffice draws the footnote mark in one
    piece with the preceding text and the following text in another piece, so the two touching characters differed in size and
    baseline, were not joined, and the paragraph got mixed up piece by piece ('102,110¹' with '백만원으로 …' on the next line,
    Pinned by: test_pdf.py::TestLinesDrawnInPieces::test_piece_ending_with_a_footnote_mark_is_joined_to_the_next_piece).
    The join happens only when the character before the mark has the same size and baseline as the next piece, so formula pieces of
    a different size (a large bracket after a script) are left alone.
    """
    if not _is_footnote_mark(last, first):
        return None
    before = [obj for obj in piece if isinstance(obj, LTChar) and obj.get_text().strip() and obj.x1 <= last.x0 + 0.5]
    for char in sorted(before, key=lambda char: char.x1, reverse=True):
        if not _is_footnote_mark(char, first):
            return char
    return None


def _footnote_mark_piece(piece: LTTextLineHorizontal, body: LTChar) -> bool:
    """Whether `piece` holds only a footnote mark ('1)', '*') and all of its characters are smaller and raised relative to the touching
    body character (`body`) (`_is_footnote_mark`).

    A PDF exported by LibreOffice with a variable font (Noto Sans KR) draws a superscript footnote mark as a piece separate from both
    the preceding and the following text, so the paragraph was split in three around the mark and one line was pushed to the end of
    the page (Pinned by: test_pdf.py::TestLinesDrawnInPieces::
    test_footnote_mark_drawn_as_its_own_piece_joins_both_neighbours). A bare number is not accepted (deliberate) — superscript pieces
    of a formula ('x' and '2') are split into pieces to begin with and in the right order, and joining them changes the reading order
    of those pages (`_continues_piece`).
    """
    chars = [obj for obj in piece if isinstance(obj, LTChar) and obj.get_text().strip()]
    return bool(_FOOTNOTE_MARK_PIECE.match("".join(char.get_text() for char in chars))) and all(
        _is_footnote_mark(char, body) for char in chars)


def _is_footnote_mark(char: LTChar, body: LTChar) -> bool:
    # 0.01pt slack: a mark exported by LibreOffice from an HTML sup (0.8x) is exactly 0.8x and was rejected by a rounding error
    # (`_footnote_mark_piece`)
    return (len(char.get_text()) == 1 and char.get_text() in "0123456789)*" and char.size <= body.size * _SCRIPT_SIZE_RATIO + 0.01
            and char.y0 - body.y0 >= body.size * _SUPERSCRIPT_RISE)


def _is_plain_glyph(text: str) -> bool:
    """Whether this is one plain character with a font mapping. `(cid:n)` and the private use area (formula bracket pieces) are
    not."""
    return len(text) == 1 and not "\ue000" <= text <= "\uf8ff"


def _place_xobject_lines(lines: list[_Line], owners: list[int | None]) -> list[_Line]:
    """Move the lines inside Form XObjects, one block per XObject, to where they are drawn on the page.

    pdfminer puts the text inside an XObject after the other text of the page (text boxes → other objects), so the entered values of
    a flattened form were separated from their labels and gathered at the end of the page (Pinned by:
    test_quality.py::test_pdf_text_inside_form_xobjects_stays_where_it_is_drawn). The order inside a block is left as pdfminer
    decided. The position is decided against the lines drawn directly on the page only — using the lines of another block that was
    already inserted as reference made two side-by-side columns interleave (Pinned by:
    test_quality.py::test_pdf_side_by_side_form_xobjects_are_not_interleaved).
    A page without any page text (a generator that puts the whole body in an XObject) or with too many blocks × lines is left as is.
    """
    groups: dict[int, list[_Line]] = {}
    placed: list[_Line] = []

    for line, owner in zip(lines, owners, strict=True):
        if owner is None:
            placed.append(line)
        else:
            groups.setdefault(owner, []).append(line)
    if not groups or not placed or len(groups) * len(placed) > _XOBJECT_PLACEMENT_MAX_WORK:
        return lines

    # index of the page-text line to insert before (len(placed) means the end) → blocks, in their original order
    before: dict[int, list[_Line]] = {}
    for group in groups.values():
        before.setdefault(_xobject_position(placed, group[0]), []).extend(group)
    return [line for index in range(len(placed) + 1) for line in [*before.get(index, []), *placed[index:index + 1]]]


def _xobject_position(placed: list[_Line], first: _Line) -> int:
    """Position at which to insert the first line of an XObject (before which page-text line): right after the text to its left on
    the same line → before the first line below it in the same column → after the last line above it in the same column → before the
    first line below it regardless of column → the end."""
    def same_column(line: _Line) -> bool:
        return line.x0 < first.x1 and first.x0 < line.x1

    middle = (first.top + first.bottom) / 2
    left = [index for index, line in enumerate(placed)
            if line.top <= middle <= line.bottom and line.x1 <= first.x0 + 1]
    if left:
        return max(left, key=lambda index: placed[index].x1) + 1
    below = next((index for index, line in enumerate(placed) if line.top >= first.top and same_column(line)), None)
    if below is not None:
        return below
    above = [index for index, line in enumerate(placed) if line.top < first.top and same_column(line)]
    if above:
        return above[-1] + 1
    return next((index for index, line in enumerate(placed) if line.top >= first.top), len(placed))


def _text_view(page: object) -> list[tuple[float, float, float, float]]:
    """Visible area used to filter characters ([area], or [] when not filtering). For a rotated page (/Rotate) pdfplumber flips the
    CropBox by the pre-rotation height, so visible characters were judged to be outside the trim lines and deleted — rotated pages
    are not filtered (the previous behaviour. Pinned by:
    test_pdf.py::TestDrawingArtifacts::test_rotated_page_with_an_off_centre_crop_box_keeps_its_visible_text)."""
    return [] if getattr(page, "rotation", 0) % 360 else [_view_box(page)]


def _view_box(page: object) -> tuple[float, float, float, float]:
    """Return the area of the page that is actually visible (MediaBox ∩ CropBox) in the same coordinate system as characters and
    pictures.

    In print PDFs the CropBox is smaller than the MediaBox, or the MediaBox does not start at 0. pdfplumber's coordinates are relative
    to the MediaBox and pdfium draws only the visible area, so when cropping a picture the top-left of this area must be moved to the
    origin. Without that, the blank paper and body text beside the picture were cropped instead of the picture
    (Pinned by: test_image.py::test_figure_is_cropped_where_it_is_shown_when_page_boxes_are_offset).
    """
    media, crop = page.mediabox, page.cropbox
    view = (max(media[0], crop[0]), max(media[1], crop[1]), min(media[2], crop[2]), min(media[3], crop[3]))
    return tuple(float(value) for value in (view if view[0] < view[2] and view[1] < view[3] else media))


@dataclass(slots=True)
class _FontStats:
    hangul: int = 0
    spaces: int = 0
    continuous: int = 0
    overlapping: int = 0
    # Overlapping boundaries whose preceding run has two or more characters. In a line with tightened character spacing every run is
    # a single character, so this value is 0.
    overlapping_after_word: int = 0
    # (preceding run, first character of the next run): places where placement restarted after a run ending in Hangul
    restarts: list[tuple[list[dict], dict]] = field(default_factory=list)


def _repair_placed_runs(page: object, known_fit: tuple[float, float] | None = None,
                        ) -> tuple[tuple[float, float] | None, bool]:
    """Fix the overlapping character boxes of separately placed runs of characters, and insert space characters at word boundaries
    for fonts that have no space character.

    `page.chars` is modified in place (table extraction uses the same character list). Inserted spaces are marked `restored`.
    The criteria and reasons are in the `_RESTART_GAP_RATIO` comment above. `known_fit` is the value obtained from another page of
    the same document.
    Returns (the value obtained from this page, whether there is a font that looks like such text but could not be fixed for lack of
    a value).
    """
    chars = page.chars
    runs = _placed_runs(chars)
    pairs = [(run, following) for run, following in zip(runs, runs[1:], strict=False)
             if _same_row(run[-1], following[0]) and following[0]["x0"] > run[0]["x0"]]

    fonts: dict[tuple[str, float], _FontStats] = {}
    for run in runs:
        stats = fonts.setdefault(_font_key(run[-1]), _FontStats())
        stats.hangul += sum(1 for char in run if _is_hangul(char["text"]))
        stats.spaces += sum(1 for char in run if char["text"] == " ")
        stats.continuous += sum(1 for previous, char in zip(run, run[1:], strict=False)
                                if _is_hangul(previous["text"]) and _is_hangul(char["text"]))
    for run, following in pairs:
        last, first = run[-1]["text"], following[0]["text"]
        if _is_hangul(last) and (_is_hangul(first) or first.isascii() and first.isalnum()):
            stats = fonts[_font_key(run[-1])]
            stats.restarts.append((run, following[0]))
            overlaps = following[0]["x0"] < run[-1]["x1"]
            stats.overlapping += overlaps
            stats.overlapping_after_word += overlaps and len(run) >= 2

    # Whether this page is "separately placed text" is decided by whether at least one font yields a real character width
    # (_placement_fit). Normal documents whose character spacing differs per line because of justification are filtered out here and
    # nothing changes.
    fits = {key: _placement_fit(stats.restarts) for key, stats in fonts.items()
            if stats.hangul >= _MIN_HANGUL_FOR_REPAIR and stats.overlapping >= _MIN_OVERLAPPING_RESTARTS
            and _looks_placed_without_spaces(stats)}
    fits = {key: fit for key, fit in fits.items() if fit is not None}
    page_fit = ((statistics.median(fit[0] for fit in fits.values()), statistics.median(fit[1] for fit in fits.values()))
                if fits else None)
    # Fonts whose character width could not be obtained on their own: few characters, as in a heading, or two screen fonts (regular
    # and bold) sharing one font name in the text layer so that the widths are mixed (Measured: 0.86em and 0.80em on the same page).
    # They are fixed only when this page or another page of the same document has been confirmed as 'separately placed text'; a value
    # that fits is borrowed, and when there is none every new run after Hangul is treated as a word boundary (a space may end up
    # inside a word where only the font changed, but that is better than whole words running together).
    candidates = [*fits.values(), *([known_fit] if known_fit else [])]
    borrowers = [key for key, stats in fonts.items()
                 if key not in fits and stats.overlapping_after_word and _looks_placed_without_spaces(stats)]
    if not candidates:
        return None, bool(borrowers)

    gap_before: dict[int, dict] = {}
    for key, stats in fonts.items():
        if key not in fits and key not in borrowers:
            continue
        fit = fits.get(key) or next((candidate for candidate in candidates if _fits(stats.restarts, candidate)), None)
        gap_before.update((id(char), run[-1]) for run, char in stats.restarts
                          if fit is None or _is_word_gap(run, char, fit))

    # Word-boundary detection uses only the start points of the runs, so it was finished above. Now shrink the overlapping character
    # boxes to the start point of the next run.
    for run, following in pairs:
        start, end, limit = run[0]["x0"], run[-1]["x1"], following[0]["x0"]
        if limit >= end or limit - start < (end - start) * 0.5:
            continue
        scale = (limit - start) / (end - start)
        for char in run:
            char["x0"], char["x1"] = start + (char["x0"] - start) * scale, start + (char["x1"] - start) * scale
            char["width"] = char["x1"] - char["x0"]

    if not gap_before:
        return page_fit, False
    # The space goes right before the start point of the next word (both in content order and in coordinates).
    rebuilt = []
    for char in chars:
        previous = gap_before.get(id(char))
        if previous is not None:
            rebuilt.append({**previous, "text": " ", "x0": char["x0"] - 0.01, "x1": char["x0"], "width": 0.01,
                            "restored": True})
        rebuilt.append(char)
    chars[:] = rebuilt
    return page_fit, False


def _looks_placed_without_spaces(stats: _FontStats) -> bool:
    """There are almost no space characters, and at least half of the overlapping boundaries follow a run of two or more characters.

    The second condition separates justified documents: a line with tightened character spacing breaks at every character, so all
    overlapping boundaries follow single-character runs.
    """
    return (stats.spaces < stats.hangul * _SPACE_POOR_RATIO and stats.continuous * 2 >= stats.overlapping
            and stats.overlapping_after_word * 2 >= stats.overlapping)


def _placed_runs(chars: list[dict]) -> list[list[dict]]:
    """In content order, group characters on the same line and in the same font whose start continues exactly from the end of the
    previous character into one run."""
    runs: list[list[dict]] = []
    for char in chars:
        previous = runs[-1][-1] if runs else None
        if (previous is not None and _font_key(previous) == _font_key(char) and _same_row(previous, char)
                and abs(char["x0"] - previous["x1"]) <= previous["size"] * _RESTART_GAP_RATIO):
            runs[-1].append(char)
        else:
            runs.append([char])
    return runs


def _same_row(previous: dict, char: dict) -> bool:
    center = (char["top"] + char["bottom"]) / 2
    return abs(center - (previous["top"] + previous["bottom"]) / 2) <= (previous["bottom"] - previous["top"]) * 0.4


def _font_key(char: dict) -> tuple[str, float]:
    return char["fontname"], round(char["size"], 1)


def _measure(run: list[dict], char: dict) -> tuple[int, float] | None:
    """For a run made of Hangul only: (character count, distance from the run's start point to the next run's start point ÷ character
    size)."""
    if not all(_is_hangul(item["text"]) for item in run):
        return None
    return len(run), (char["x0"] - run[0]["x0"]) / run[0]["size"]


def _placement_fit(restarts: list[tuple[list[dict], dict]]) -> tuple[float, float] | None:
    """Return (real character width, space width) in units of the character size if this is separately placed text, otherwise None.

    The distance from a run's start point to the next run's start point is `character count × real character width (+ space width)`.
    The two values are derived from two medians of the distance per character count (most boundaries are between words). All of the
    following must hold for the text to be accepted as separately placed.
    - There are at least two kinds of runs with different character counts, at least 3 of each. In a justified document every
      character is broken off, so all runs are single characters.
    - The real character width is clearly narrower than the width the font declares (the cause of the overlap). Normal documents
      whose widths are right are caught here.
    - Most boundaries fit either "a space width is left over" or "nothing is left over", and the majority of them are between words.
    """
    measured = [item for item in (_measure(run, char) for run, char in restarts) if item]
    by_length: dict[int, list[float]] = {}
    for length, distance in measured:
        by_length.setdefault(length, []).append(distance)
    lengths = sorted((length for length, values in by_length.items() if len(values) >= 3),
                     key=lambda length: -len(by_length[length]))[:2]
    if len(lengths) < 2:
        return None

    short, long = sorted(lengths)
    advance = (statistics.median(by_length[long]) - statistics.median(by_length[short])) / (long - short)
    space = statistics.median(by_length[short]) - short * advance
    if not (_MIN_PLACED_ADVANCE <= advance <= _MAX_PLACED_ADVANCE and 0.1 <= space <= 0.7):
        return None

    return (advance, space) if _fits(restarts, (advance, space)) else None


def _fits(restarts: list[tuple[list[dict], dict]], fit: tuple[float, float]) -> bool:
    """Whether most boundaries fit either "a space width is left over" or "nothing is left over", and the majority of them are
    between words."""
    advance, space = fit
    leftovers = [item[1] - item[0] * advance for item in (_measure(run, char) for run, char in restarts) if item]
    if not leftovers:
        return False
    word_gaps = sum(1 for leftover in leftovers if abs(leftover - space) <= _FIT_TOLERANCE)
    inside_word = sum(1 for leftover in leftovers if abs(leftover) <= _FIT_TOLERANCE)
    return word_gaps + inside_word >= len(leftovers) * 0.8 and word_gaps >= len(leftovers) * 0.5


def _is_word_gap(run: list[dict], char: dict, fit: tuple[float, float]) -> bool:
    """Whether the restart is between words (a space) or inside a word where only the font changed ("개발" in bold + "하여"). A run
    that is not Hangul-only is treated as between words."""
    measured = _measure(run, char)
    advance, space = fit
    return measured is None or measured[1] - measured[0] * advance > space / 2


def _text_from_chars(chars: list[dict]) -> str:
    """Build text by joining the characters of a line left to right. Whitespace characters and wide gaps (same criterion as pdfminer's
    word_margin) are the word spacing."""
    parts: list[str] = []
    previous = None
    for char in sorted(chars, key=lambda char: char["x0"]):
        if not char["text"].strip():
            parts.append(" ")
            previous = None
            continue
        if previous is not None and char["x0"] - previous["x1"] > _LAPARAMS["word_margin"] * max(char["width"], char["height"]):
            parts.append(" ")
        parts.append(char["text"])
        previous = char
    return " ".join("".join(parts).split())


def _glyph_gaps(chars: list[dict]) -> tuple[list[dict], list[float]]:
    """The line's characters laid left to right, and the gaps between two neighbouring characters that have no space character
    between them."""
    ordered = sorted(chars, key=lambda char: char["x0"])
    gaps = [right["x0"] - left["x1"] for left, right in zip(ordered, ordered[1:], strict=False)
            if left["text"].strip() and right["text"].strip()]
    return ordered, gaps


def _letter_spacings(lines: list[list[dict]]) -> list[float]:
    """The gap values on the page that look like widened character spacing (Tc). The narrowest gap of each line in which every gap
    between characters exceeds pdfminer's word-gap criterion (`word_margin` × character size), the gaps close to the narrowest one
    (`_SPACING_TOLERANCE`) number three or more and 60% or more, and that gap is below `_LETTER_SPACING_MAX` times the character
    size. A short cell with a single gap ('3%') cannot be decided on its own, so it is judged with these values."""
    found: list[float] = []

    for chars in lines:
        _, gaps = _glyph_gaps(chars)
        size = max((char["size"] for char in chars), default=0.0)
        if len(gaps) < 3 or size <= 0:
            continue
        narrowest = min(gaps)
        near = sum(1 for gap in gaps if gap - narrowest <= _SPACING_TOLERANCE)
        if (_LAPARAMS["word_margin"] * size < narrowest < _LETTER_SPACING_MAX * size and near >= 3
                and near >= len(gaps) * 0.6 and not any(abs(narrowest - known) <= _SPACING_TOLERANCE for known in found)):
            found.append(narrowest)
    return found


def _letter_spaced_text(chars: list[dict], spacings: list[float]) -> str | None:
    """For a line written with widened character spacing (a line whose narrowest gap equals one of the page's spacing values
    `spacings`), the text rejoined with spaces only at gaps judged after subtracting the character spacing.

    Deliberate: when the character spacing exceeds 10% of the character size pdfminer puts a space after every character, giving
    '총 사 업 비 는 1 , 2 3 4'. Only space characters and gaps wider than the character spacing + the word-gap criterion remain as
    word spacing. Pieces drawn one character at a time, as in a letter-spaced heading (`_is_letter_spaced`), are separate lines and
    do not come here. Known limitation: text in ruled-table cells (pdfplumber extraction) is not fixed
    (Pinned by: test_pdf_character_spacing.py::TestWideCharacterSpacing)."""
    ordered, gaps = _glyph_gaps(chars)
    size = max((char["size"] for char in chars), default=0.0)
    if not gaps or size <= 0 or min(gaps) <= _LAPARAMS["word_margin"] * size:
        return None
    spacing = next((known for known in spacings if abs(min(gaps) - known) <= _SPACING_TOLERANCE), None)
    if spacing is None:
        return None

    parts: list[str] = []
    previous = None

    for char in ordered:
        if not char["text"].strip():
            parts.append(" ")
            previous = None
            continue
        if previous is not None and char["x0"] - previous["x1"] > spacing + _LAPARAMS["word_margin"] * size:
            parts.append(" ")
        parts.append(char["text"])
        previous = char
    return " ".join("".join(parts).split())


def _gap_parts(chars: list[dict], text: str, size: float, unspaced: float | None = None) -> tuple[tuple[float, float, str], ...]:
    """Pieces ((x0, x1, text)) of the line's characters (`chars`) split where they are `_CELL_GAP_RATIO` times the character size or
    more apart. With `unspaced`, also split where there is no space character yet the gap is that multiple or more (Chrome table cell
    padding 0.75x — between words there is a space character. Between a number and a number from `_NUMBER_GAP_RATIO` times). An
    empty tuple when there is no such gap or when the joined split text differs from the line text (`text`) (script marks,
    overlapping copies, lines in a different order)."""
    glyphs = sorted((char for char in chars if char["text"].strip()), key=lambda char: char["x0"])
    if len(glyphs) < 2 or size <= 0:
        return ()
    spaces = [char for char in chars if not char["text"].strip()]

    groups = [[glyphs[0]]]
    for previous, char in zip(glyphs, glyphs[1:], strict=False):
        gap = char["x0"] - previous["x1"]
        wide = unspaced is not None and (gap >= size * unspaced or (
            gap >= size * _NUMBER_GAP_RATIO and _NUMBER_EDGE.match(previous["text"]) and _NUMBER_START.match(char["text"])))
        if gap >= size * _CELL_GAP_RATIO or (wide and not any(previous["x0"] < space["x0"] < char["x1"] for space in spaces)):
            groups.append([])
        groups[-1].append(char)
    if len(groups) < 2:
        return ()
    parts = tuple((group[0]["x0"], max(char["x1"] for char in group),
                   _drawn_text(_text_from_chars([*group, *(space for space in spaces if group[0]["x0"] < space["x0"] < group[-1]["x1"])])))
                  for group in groups)
    return parts if " ".join(part[2] for part in parts) == text else ()


def _scripted_text(chars: list[dict], line_text: str) -> str | None:
    """Text with the superscripts and subscripts in the line marked. None when there are no scripts or it is not certain (the line
    text is used as is).

    A PDF carries no information about scripts, so they are inferred from position: characters smaller than the line's ordinary
    characters (`_SCRIPT_SIZE_RATIO` or less) whose baseline is off by `_SUPERSCRIPT_RISE` or more upwards or `_SUBSCRIPT_DROP` or
    more downwards (relative to the ordinary character size). Slightly less small characters (`_SCRIPT_FAR_SIZE_RATIO` or less,
    browser printing) only when off by `_SCRIPT_FAR_SHIFT` or more. Small characters on the same baseline (units, annotations) are
    not scripts. The line text is not rebuilt; marks are only inserted into the text pdfminer built: this is done only when the
    characters laid left to right equal the characters of the line text, order included. If they differ (characters of a
    neighbouring line mixed in, or a line where overlapping accents come in a different order) the line is left alone — keeping
    character order and spacing unchanged takes precedence over marking scripts (Pinned by: test_pdf.py::TestSuperscriptByPosition).
    """
    visible = [char for char in chars if char["text"].strip() and char.get("size")]
    if len(visible) < 3:
        return None
    largest = max(char["size"] for char in visible)
    if min(char["size"] for char in visible) > largest * _SCRIPT_FAR_SIZE_RATIO:
        return None  # a line of uniform size (most lines)

    body_size = statistics.median(char["size"] for char in visible)
    body = [char for char in visible if char["size"] > body_size * _SCRIPT_SIZE_RATIO]
    if len(body) * 2 < len(visible):
        return None

    # pdfminer's character box extends below the baseline by the font's descent. It differs per font but is around 0.2 times the size.
    def baseline(char: dict) -> float:
        return char["bottom"] - 0.2 * char["size"]

    body_baseline = statistics.median(baseline(char) for char in body)
    marked: list[tuple[str, str | None]] = []  # (character, script mark), left to right
    for char in sorted(visible, key=lambda char: char["x0"]):
        mark = None
        rise = body_baseline - baseline(char)  # how far it is raised (pt)
        if char["size"] <= body_size * _SCRIPT_SIZE_RATIO:
            mark = SUPERSCRIPT if rise >= body_size * _SUPERSCRIPT_RISE else SUBSCRIPT if rise <= -body_size * _SUBSCRIPT_DROP else None
        elif char["size"] <= body_size * _SCRIPT_FAR_SIZE_RATIO and abs(rise) >= body_size * _SCRIPT_FAR_SHIFT:
            mark = SUPERSCRIPT if rise > 0 else SUBSCRIPT
        marked.extend((letter, mark) for letter in char["text"] if not letter.isspace())
    # Spans containing Hangul or Hanja are not marked. When a designed document aligns small text or units next to a heading to the
    # top, it is positionally identical to a superscript and gave '12,345^{백만원}' (Pinned by:
    # test_pdf.py::TestSuperscriptByPosition::test_small_raised_korean_words_are_not_marked).
    start = 0
    while start < len(marked):
        end = start + 1
        while end < len(marked) and marked[end][1] == marked[start][1]:
            end += 1
        if marked[start][1] and any(_SPACED_CHAR.match(letter) for letter, _ in marked[start:end]):
            marked[start:end] = [(letter, None) for letter, _ in marked[start:end]]
        start = end
    if not any(mark for _, mark in marked) or [letter for letter, _ in marked] != [letter for letter in line_text if not letter.isspace()]:
        return None

    marks = iter(mark for _, mark in marked)
    pieces = []
    for letter in line_text:
        mark = None if letter.isspace() else next(marks)
        pieces.append(mark_script(letter, mark) if mark else letter)
    return resolve_scripts("".join(pieces))


def _is_hangul(text: str) -> bool:
    return len(text) == 1 and "가" <= text <= "힣"


def _figure_boxes(page: object, view: tuple[float, float, float, float]) -> list[tuple[float, float, float, float, float]]:
    """Display areas of the images on the page (pt, clipped to the visible area and the clip region) and their native resolution
    scale. Icon-sized ones are discarded, and images drawn on top of each other at the same position count as one."""
    clips = _image_clips(page) if page.images else []
    shown = []
    for image in page.images:
        x0, top = max(image["x0"], view[0]), max(image["top"], view[1])
        x1, bottom = min(image["x1"], view[2]), min(image["bottom"], view[3])
        clip = _clip_of(image, clips)
        if clip is not None:
            # The clip region is in pdfminer coordinates (0 at the bottom), so shift by how much the image box shrank at its top and bottom.
            x0, x1 = max(x0, clip[0]), min(x1, clip[2])
            top, bottom = max(top, image["top"] + image["y1"] - clip[3]), min(bottom, image["bottom"] - (clip[1] - image["y0"]))
        if x1 <= x0 or bottom <= top:
            continue
        source_width = (image.get("srcsize") or (0, 0))[0] or 0
        shown.append((x0, top, x1, bottom, source_width / max(image["x1"] - image["x0"], 1.0)))

    boxes = {}
    for x0, top, x1, bottom, native in _tiled_background(shown, view):
        if x1 - x0 < _MIN_FIGURE_POINTS or bottom - top < _MIN_FIGURE_POINTS:
            continue
        key = (round(x0), round(top), round(x1), round(bottom))
        boxes[key] = (x0, top, x1, bottom, max(native, boxes.get(key, (0, 0, 0, 0, 0.0))[4]))
    return sorted(boxes.values(), key=lambda box: (box[1], box[0]))


class _ClipRecorder(PDFDevice):
    """pdfminer device that collects (picture box, the clip region at that moment) every time a picture is drawn (coordinates are
    pdfminer's page coordinates).

    The clip region is the rectangle enclosing the path points (including the control points of curves), so it is never smaller than
    the real clip region — conservatively, only as much as can safely be cut is cut. The BBox of a Form XObject is a clip too. A clip
    region of None means no clipping.
    """

    def __init__(self, rsrcmgr: object) -> None:
        super().__init__(rsrcmgr)
        self.clips: list[tuple[float, float, float, float] | None] = [None]  # current clip of each content stream being drawn
        self.images: list[tuple[tuple[float, float, float, float], tuple[float, float, float, float] | None]] = []

    def begin_figure(self, name: str, bbox: object, matrix: object) -> None:
        if tuple(bbox) == (0, 0, 1, 1) and tuple(matrix) == MATRIX_IDENTITY:
            self.clips.append(self.clips[-1])  # an image (pdfminer opens an image as a figure with a unit box too)
        else:
            x0, y0, x1, y1 = bbox
            corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
            self.clips.append(_clip_intersection(self.clips[-1], _points_box(corners, mult_matrix(matrix, self.ctm))))

    def end_figure(self, name: str) -> None:
        if len(self.clips) > 1:
            self.clips.pop()

    def render_image(self, name: str, stream: object) -> None:
        self.images.append((_points_box([(0, 0), (1, 0), (1, 1), (0, 1)], self.ctm), self.clips[-1]))


class _ClipInterpreter(PDFPageInterpreter):
    """pdfminer interpreter that tracks `W` / `W*` clipping along with the graphics state (q, Q) and reports it to `_ClipRecorder`.
    For text drawing the device does nothing."""

    def init_state(self, ctm: object) -> None:
        super().init_state(ctm)
        self.clip = self.device.clips[-1]

    def get_current_state(self) -> tuple:
        return (*super().get_current_state(), self.clip)

    def set_current_state(self, state: tuple) -> None:
        super().set_current_state(state[:3])
        if len(state) > 3:
            self.clip = self.device.clips[-1] = state[3]

    def do_W(self) -> None:
        self._clip_to_path()

    def do_W_a(self) -> None:
        self._clip_to_path()

    def _clip_to_path(self) -> None:
        points = [(segment[index], segment[index + 1]) for segment in self.curpath
                  for index in range(1, len(segment) - 1, 2)]
        if points:
            self.clip = self.device.clips[-1] = _clip_intersection(self.clip, _points_box(points, self.ctm))


def _points_box(points: list[tuple[float, float]], matrix: object) -> tuple[float, float, float, float]:
    moved = [apply_matrix_pt(matrix, point) for point in points]
    return (min(x for x, _ in moved), min(y for _, y in moved), max(x for x, _ in moved), max(y for _, y in moved))


def _clip_intersection(clip: tuple[float, float, float, float] | None,
                       box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    if clip is None:
        return box
    return (max(clip[0], box[0]), max(clip[1], box[1]), min(clip[2], box[2]), min(clip[3], box[3]))


def _image_clips(page: object) -> list[tuple[tuple[float, float, float, float], tuple[float, float, float, float] | None]]:
    """(picture box, clip region) for every picture on the page (pdfminer coordinates, drawing order). The content stream is
    interpreted once more (only on pages with pictures). An empty list if it cannot be read — the unclipped boxes are used as is.

    pdfminer and pdfplumber discard clip paths (`W n`), so in a PDF that places a photo large and clips it with a rectangle to draw
    only the visible part (browser `object-fit: cover`, `overflow: hidden`) the picture box became the original placement box. The
    neighbouring body text was cropped into the picture, and when the original covered the page it was filtered out as background
    and the photo disappeared (Pinned by: test_pdf_images.py::TestClippedImages).
    """
    try:
        recorder = _ClipRecorder(page.pdf.rsrcmgr)
        _ClipInterpreter(page.pdf.rsrcmgr, recorder).process_page(page.page_obj)
    except Exception:  # noqa: BLE001 — clipping is additional information. If interpretation fails, nothing is clipped, as before
        return []
    return recorder.images


def _clip_of(image: dict, clips: list) -> tuple[float, float, float, float] | None:
    """Clip region of the first picture record whose box equals that of the pdfplumber image (within 0.5pt). A used record is removed
    from the list."""
    for index, (box, clip) in enumerate(clips):
        if all(abs(a - b) <= 0.5 for a, b in zip(box, (image["x0"], image["y0"], image["x1"], image["y1"]), strict=True)):
            del clips[index]
            return clip
    return None


def _tiled_background(boxes: list[tuple[float, float, float, float, float]],
                      view: tuple[float, float, float, float]) -> list[tuple[float, float, float, float, float]]:
    """List in which a group of images joined edge to edge (gaps or overlaps within `_TILE_GAP`) is replaced by a single union box
    when the group is strip-shaped (`_is_strips`) and its union covers at least `_BACKGROUND_RATIO` of the visible area. The rest is
    unchanged.

    In a PDF where the scanner stored the page as 20 horizontal strips with an OCR text layer on top, each strip was 5% of the page
    and thus not a background, and 20 figures per page were inserted between the text (2,000 vision calls for 100 pages, with content
    duplicating the text layer). Side-by-side photos with gaps between them do not touch and stay pictures, and a photo laid over a
    background lies inside it (not edge to edge) and stays a picture. This list becomes `_Page.figures`, so `_page_images`,
    `_is_scanned` and `_untrusted_text_layer_pages` use the same decision (Pinned by: test_image.py::TestPdfImages::
    test_scan_cut_into_abutting_strips_under_a_korean_text_layer_is_one_background and 2 more).
    """
    view_area = max((view[2] - view[0]) * (view[3] - view[1]), 1.0)
    if len(boxes) < 2 or sum(_box_area(box[:4]) for box in boxes) < view_area * _BACKGROUND_RATIO:
        return boxes

    order = sorted(range(len(boxes)), key=lambda index: boxes[index][1])
    parent = list(range(len(boxes)))

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for position, index in enumerate(order):
        for other in islice(order, position + 1, None):
            if boxes[other][1] > boxes[index][3] + _TILE_GAP:
                break
            if _abuts(boxes[index], boxes[other]):
                parent[root(other)] = root(index)

    groups: dict[int, list[int]] = {}
    for index in range(len(boxes)):
        groups.setdefault(root(index), []).append(index)
    result = []
    for group in groups.values():
        tiles = [boxes[index] for index in group]
        if (len(tiles) > 1 and _is_strips(tiles)
                and _union_area([tile[:4] for tile in tiles]) >= view_area * _BACKGROUND_RATIO):
            result.append((min(tile[0] for tile in tiles), min(tile[1] for tile in tiles), max(tile[2] for tile in tiles),
                           max(tile[3] for tile in tiles), max(tile[4] for tile in tiles)))
        else:
            result.extend(tiles)
    return result


def _is_strips(tiles: list[tuple[float, ...]]) -> bool:
    """Whether a group of touching images is strip-shaped: every piece fills the whole width (horizontal strips) or height (vertical
    strips) of the group to within `_TILE_GAP`.

    Four photos filling the page 2×2 without gaps were merged into one background, and on a page with more than 100 characters of
    text all the photos disappeared (Pinned by: test_pdf.py::test_photo_collage_filling_the_page_keeps_its_photos). The pieces a
    scanner splits a page into are strips that fill the page width.
    Pieces lying inside another piece (decorative pieces laid over a strip) are not considered — a designed background with a
    left-half piece laid over every page-width strip became 10 pictures (Pinned by:
    test_pdf.py::test_background_strips_with_pieces_laid_inside_them_stay_one_background).
    Known accepted gap: scan pieces stored as a grid are not merged (one picture per piece).
    """
    shapes = [tuple(tile[:4]) for tile in tiles]

    # Pieces that enclose each other (same position within 1pt) are both kept — removing the redrawn strip too would leave no piece
    outer = [tile for tile in shapes if not any(_encloses(other, tile) and not _encloses(tile, other) for other in shapes)]
    x0, top = min(tile[0] for tile in outer), min(tile[1] for tile in outer)
    x1, bottom = max(tile[2] for tile in outer), max(tile[3] for tile in outer)
    return (all(tile[0] - x0 <= _TILE_GAP and x1 - tile[2] <= _TILE_GAP for tile in outer)
            or all(tile[1] - top <= _TILE_GAP and bottom - tile[3] <= _TILE_GAP for tile in outer))


def _abuts(box: tuple[float, ...], other: tuple[float, ...]) -> bool:
    """Whether the two boxes meet edge to edge: their ranges overlap on one axis and on the other axis the difference between end
    and start is within `_TILE_GAP`. Not when one lies inside the other or they overlap heavily (a photo over a background)."""
    def overlaps(low: int, high: int) -> bool:
        return min(box[high], other[high]) - max(box[low], other[low]) > 0

    def touches(low: int, high: int) -> bool:
        return abs(other[low] - box[high]) <= _TILE_GAP or abs(box[low] - other[high]) <= _TILE_GAP

    return (overlaps(0, 2) and touches(1, 3)) or (overlaps(1, 3) and touches(0, 2))


def _union_area(boxes: list[tuple[float, ...]]) -> float:
    """Area of the union of the boxes (overlapping parts counted once)."""
    edges = sorted({edge for box in boxes for edge in (box[1], box[3])})
    area = 0.0
    for low, high in zip(edges, edges[1:], strict=False):
        spans = sorted((box[0], box[2]) for box in boxes if box[1] <= low and box[3] >= high)
        covered, reach = 0.0, float("-inf")
        for x0, x1 in spans:
            covered += max(0.0, x1 - max(x0, reach))
            reach = max(reach, x1)
        area += covered * (high - low)
    return area


class _PageRenderer:
    """Render pages and picture areas to PNG with pypdfium2. Instead of decoding the image source it draws what is on screen, so the
    result is what a person sees regardless of compression (JPX, JBIG2, CCITT), colour space, masks and clipping."""

    def __init__(self, data: bytes, options: ConvertOptions) -> None:
        self._document = _open_pdfium(data)
        self._scale = options.pdf_render_scale
        self._budget = None if options.max_images is None else options.max_images * _RENDER_BUDGET_FACTOR
        self._cached: tuple[tuple[int, float], object] | None = None
        self.skipped = 0

    def render(self, page_index: int, box: tuple[float, float, float, float] | None = None,
               native_scale: float = 0.0) -> bytes | None:
        if self._budget is not None:
            if self._budget <= 0:
                self.skipped += 1
                return None
            self._budget -= 1
        scale = min(max(self._scale, native_scale), _MAX_FIGURE_SCALE) if box is not None else self._scale
        try:
            image = self._render(page_index, box, scale)
        except pdfium.PdfiumError:
            return None
        if image is None:
            return None

        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    def _render(self, page_index: int, box: tuple[float, float, float, float] | None, scale: float) -> object:
        """The whole page (box is None) or a picture area as a PIL image.

        When the whole page is within the pixel cap, the page is drawn once and cropped for each picture on the same page. For a
        large page beyond the cap (posters, drawings) only the picture area is drawn — previously the whole page was drawn at up to
        4x scale to crop a single picture, using 9.4GB on an 8000pt page, and for even larger pages the bitmap could not be created
        and the picture was dropped without a warning. In either case the scale is lowered when the drawn area exceeds the cap.
        """
        if box is not None and self._cached is not None and self._cached[0] == (page_index, scale):
            return self._cached[1].crop(tuple(round(value * scale) for value in box))

        with _PDFIUM_LOCK:
            page = self._document[page_index]
            try:
                width, height = page.get_size()  # visible area, rotation applied — same as the picture coordinates (relative to _view_box)
                if box is None:
                    return _render_bitmap(page, _capped_scale(scale, width * height))
                if width * height * scale * scale <= _MAX_RENDER_PIXELS:
                    self._cached = ((page_index, scale), _render_bitmap(page, scale))
                    return self._cached[1].crop(tuple(round(value * scale) for value in box))

                left, top = max(box[0], 0.0), max(box[1], 0.0)
                right, bottom = min(box[2], width), min(box[3], height)
                if right - left < 1 or bottom - top < 1:
                    return None
                crop = (left, height - bottom, width - right, top)  # amounts to cut off from the page edges (left, bottom, right, top)
                return _render_bitmap(page, _capped_scale(scale, (right - left) * (bottom - top)), crop)
            finally:
                page.close()

    def close(self) -> None:
        self._cached = None
        with _PDFIUM_LOCK:
            self._document.close()


def _capped_scale(scale: float, area: float) -> float:
    """Lower the scale to fit the pixel cap when drawing `area` (pt²) at `scale` would exceed it."""
    return min(scale, (_MAX_RENDER_PIXELS / max(area, 1.0)) ** 0.5)


def _render_bitmap(page: object, scale: float, crop: tuple[float, float, float, float] = (0, 0, 0, 0)) -> object:
    """Make a PIL copy that does not share the bitmap's memory and close the bitmap. Call inside `_PDFIUM_LOCK`."""
    bitmap = page.render(scale=scale, crop=crop)
    try:
        return bitmap.to_pil().copy()
    finally:
        bitmap.close()


def _page_images(page: _Page, renderer: _PageRenderer, extraction: Extraction) -> list[tuple[tuple[float, ...], str]]:
    """List of ((top, x0, x1, bottom), placeholder). For an image page without text the whole page is registered, otherwise the
    picture areas are cropped and registered."""
    if _is_scanned(page):
        data = renderer.render(page.number - 1)
        return [((0.0, 0.0, page.width, page.height), extraction.add_image(data, kind=ImageKind.PAGE))] if data else []

    images = []
    left, top = page.view[:2]
    for *box, native_scale in page.figures:
        if (page.lines or page.sideways) and _covers_page(page, box):
            continue
        # The origin of a rendered picture is the top-left of the visible area (_view_box).
        shown = (box[0] - left, box[1] - top, box[2] - left, box[3] - top)
        data = renderer.render(page.number - 1, shown, native_scale)
        if data:
            images.append(((box[1], box[0], box[2], box[3]), extraction.add_image(data)))
    return images


def _is_scanned(page: _Page) -> bool:
    """A page whose text exists only as an image. Also includes a page-covering image with a text layer of a few characters (a
    stamp) on top, a page with no text at all and only outlines (a designed PDF whose text was converted to shapes), and a page
    whose document text layer cannot be trusted (`_untrusted_text_layer_pages`)."""
    if page.text_untrusted:
        return True
    if page.tables:
        return False
    if not page.lines and not page.sideways:
        return page.has_images or page.has_outlines
    text_chars = sum(len(line.text) for line in page.lines) + sum(len(text) for text in page.sideways)
    return text_chars <= _SCAN_MAX_TEXT_CHARS and any(_covers_page(page, box) for *box, _ in page.figures)


def _untrusted_text_layer_pages(pages: list[_Page]) -> list[_Page]:
    """In a document whose text layer does not hold the body, the pages to hand over whole as images even though they have text.

    The text layer of a PDF scanned from a Korean book contained only Latin and digit fragments (116 of 181 pages exceed 100
    characters). They were treated as pages with text and the page-covering scan image was discarded as background, so the Korean
    body disappeared without a warning. A single page cannot tell (a page with a lot of English code looks like a normal text page),
    so the decision is made per document. The layer is distrusted only when all of the following hold (deliberately narrow — the text
    layer is not discarded, but a wrong call costs a vision model call per page and the same content appears twice):

    - There is no Hangul anywhere in the document's text layer. If even one page has some, the text layer does hold Hangul, and a
      Latin-only page is a page that really is in English.
    - The pages with a page-covering image are more than half of the pages with text. A document where pictures cover only the cover
      and divider pages is not a scan.
    - The median text area of those pages is below `_UNTRUSTED_LAYER_MAX_TEXT_AREA`. In an English scan with proper OCR the text
      fills the page.

    Pages where a ruled table was found and simply extracted pages (no line positions) are not handed over. Known accepted gap:
    English documents with sparse text over a background picture (slides) are caught, and a text layer in which Hangul was only
    partly recognised is not caught (Pinned by: test_image.py::TestPdfImages,
    test_scan_whose_text_layer_holds_only_latin_fragments_is_sent_whole and 5 more).
    """
    written = [page for page in pages if page.lines or page.tables or page.sideways]
    if any(_HANGUL.search(text) for page in written
           for text in (*(line.text for line in page.lines), *(table.text for table in page.tables), *page.sideways)):
        return []

    covered = [page for page in written
               if page.lines and not page.simple and any(_covers_page(page, box) for *box, _ in page.figures)]
    if not covered or len(covered) * 2 <= len(written):
        return []
    if statistics.median(_text_area_ratio(page) for page in covered) >= _UNTRUSTED_LAYER_MAX_TEXT_AREA:
        return []
    return [page for page in covered if not page.tables]


def _text_area_ratio(page: _Page) -> float:
    """Area taken up by body lines / area of the visible region."""
    view_area = max((page.view[2] - page.view[0]) * (page.view[3] - page.view[1]), 1.0)
    return sum((line.x1 - line.x0) * (line.bottom - line.top) for line in page.lines) / view_area


def _covers_page(page: _Page, box: list[float] | tuple[float, ...]) -> bool:
    view_area = max((page.view[2] - page.view[0]) * (page.view[3] - page.view[1]), 1.0)
    return (box[2] - box[0]) * (box[3] - box[1]) >= view_area * _BACKGROUND_RATIO


def _without_glyph_outlines(page: object) -> object:
    """Page without the small curves that draw characters as outlines (for finding ruled tables). `page` itself when there are none.

    A document made with a design tool draws the same text once more as filled
    polygons over the text layer. The vertical edges of the strokes line up across the whole table width a little over 1pt apart, so
    when pdfplumber chains nearby vertical lines and snaps them to one position, all cell borders were pulled to one place in the
    middle of the page and the table became a single cell ('표1. 기준점(Baseline) 유형' scattered over every row, Pinned by:
    test_pdf.py::TestRuledTables::test_ruled_table_overdrawn_with_glyph_outlines_keeps_its_columns). Only curves that are below
    `_GLYPH_OUTLINE_MAX` in both width and height and are not straight lines (both sides thicker than `_STRAIGHT_MAX`) are removed —
    cell borders drawn as curves broken per row (width 0, height 9pt) are straight lines and remain. Checkbox detection still looks
    at the curves of the original page.
    """
    outlines = {id(curve) for curve in page.curves
                if max(curve["width"], curve["height"]) < _GLYPH_OUTLINE_MAX
                and min(curve["width"], curve["height"]) > _STRAIGHT_MAX}
    if not outlines:
        return page
    return page.filter(lambda obj: id(obj) not in outlines)


def _without_unadvanced_spaces(page: object) -> object:
    """Page without the space characters lying under the next character (for ruled-table cell text). `page` itself when there are
    none.

    A document with tightened character spacing draws a space character and then draws the next character before the space's start
    point — the space does not advance, so there is no visible gap. pdfplumber lays out cell characters in x order, so that space
    ended up after the first character of the next word, giving '•새 로운' and '측 정원리' (25 places in the tables of one
    manual, Pinned by: test_quality.py::
    test_pdf_table_cell_space_drawn_under_the_next_letter_does_not_split_the_word). It is removed only when the very next character
    in drawing order (character list order) starts on the same line before the space's start point and covers that start point.
    Measuring by overlapping width removed the normal spaces of a Latin font whose character boxes are wider than the advance (every
    character overlapping by 6pt), turning 'CI / CD' into 'CI /CD', and in right-to-left text, where the next character sits
    separately to the left of the space, the start point is not covered and the space remains. The spacing at a removed position is
    decided again by the cell character gaps (`_CELL_TEXT`). Body lines are read in drawing order and do not have this problem, so
    this is used only for finding tables.
    """
    chars = page.chars
    hidden = {id(char) for char, after in zip(chars, chars[1:], strict=False)
              if char["text"].isspace() and after["text"].strip() and abs(after["top"] - char["top"]) <= 1
              and after["x0"] < char["x0"] < after["x1"]}
    if not hidden:
        return page
    return page.filter(lambda obj: obj.get("object_type") != "char" or id(obj) not in hidden)


def _without_highlights(page: object) -> object:
    """Page without highlight rectangles (for finding tables). `page` itself when there are none.

    LibreOffice draws a highlight as borderless filled rectangles per run / font span, and Hangul spans and digit/Latin spans differ
    in height. pdfplumber used their edges as rules and a highlighted paragraph became a table split into a cell per word, like
    '| 재생용기는 일반 | HDPE 80% + | … | 1 | k | g | 당 |' (Pinned by: test_pdf_highlights.py::TestHighlightedParagraphs). A highlight
    is a rectangle that is filled without a border, covers the characters of one line, is at most `_HIGHLIGHT_MAX_HEIGHT` times that
    character size tall, and whose left edge matches the first covered character and whose right edge is right after the last
    character (up to a filled trailing space). A rectangle covering only the space between filled spans (or no characters) is removed
    together with them when it is on the same line as a same-coloured highlight touching it on the left or right and keeps the height
    condition. Cell shading and striped rows are wider than the characters by the cell's left padding and remain, and a rectangle
    for which another row has a rectangle with the same left and right edges but different covered text (cell shading aligned to a
    column — the widest cell can fit its text exactly) and the cells of a filled table row (`_without_cell_rows`) are kept as well.
    Known accepted gap: the full lines of a justified paragraph highlighted as one run per line remain, but they form a one-column
    box and do not become a table.
    """
    chars = sorted((char for char in page.chars if char["text"]), key=lambda char: char["top"] + char["bottom"])
    if not chars:
        return page
    middles = [(char["top"] + char["bottom"]) / 2 for char in chars]
    tallest = max(char["size"] for char in chars) * _HIGHLIGHT_MAX_HEIGHT
    covered = []
    for rect in page.rects:
        if not rect.get("fill") or rect.get("stroke") or not 0 < rect["bottom"] - rect["top"] <= tallest:
            continue
        inside = [char for char in chars[bisect_left(middles, rect["top"]):bisect_right(middles, rect["bottom"])]
                  if rect["x0"] <= (char["x0"] + char["x1"]) / 2 <= rect["x1"]]
        covered.append((rect, inside, "".join(char["text"] for char in inside).strip()))

    # The only evidence for column shading is a cell that fills its row without gaps (touching the neighbouring cell's shading, with
    # no unfilled text on the same line). Hangul characters all have the same width, so short highlights at the start of paragraph
    # lines ('고 ', '다 ') or highlights filled per ruled-table cell ('장비', '홍보') had the same left and right edges on two lines and
    # remained as column shading, producing a fake table '| 고 | 31 |' or shifting columns because the highlight edges became
    # vertical rules
    shaded = {id(char) for _, inside, _ in covered for char in inside}
    by_right = sorted((rect for rect, _, _ in covered), key=lambda rect: rect["x1"])
    by_left = sorted((rect for rect, _, _ in covered), key=lambda rect: rect["x0"])
    rights, lefts = [rect["x1"] for rect in by_right], [rect["x0"] for rect in by_left]
    columns: dict[tuple[int, int], set[str]] = {}
    for rect, _, text in covered:
        line = chars[bisect_left(middles, rect["top"]):bisect_right(middles, rect["bottom"])]
        touching = (*by_right[bisect_left(rights, rect["x0"] - _HIGHLIGHT_EDGE_MAX):bisect_right(rights, rect["x0"] + _HIGHLIGHT_EDGE_MAX)],
                    *by_left[bisect_left(lefts, rect["x1"] - _HIGHLIGHT_EDGE_MAX):bisect_right(lefts, rect["x1"] + _HIGHLIGHT_EDGE_MAX)])
        if (text and not any(char["text"].strip() and id(char) not in shaded for char in line)
                and any(other is not rect and abs(other["top"] - rect["top"]) <= _CELL_ALIGN
                        and abs(other["bottom"] - rect["bottom"]) <= _CELL_ALIGN for other in touching)):
            columns.setdefault((round(rect["x0"]), round(rect["x1"])), set()).add(text)
    marks, empty = [], []
    for rect, inside, text in covered:
        if not text:
            empty.append(rect)
            continue
        size = max(char["size"] for char in inside)
        edge = min(size * _HIGHLIGHT_EDGE_RATIO, _HIGHLIGHT_EDGE_MAX)
        tail = rect["x1"] - max(char["x1"] for char in inside)
        if (len(columns.get((round(rect["x0"]), round(rect["x1"])), ())) <= 1
                and rect["bottom"] - rect["top"] <= size * _HIGHLIGHT_MAX_HEIGHT
                and max(char["top"] for char in inside) - min(char["top"] for char in inside) <= size / 2
                and abs(min(char["x0"] for char in inside) - rect["x0"]) <= edge
                and -edge <= tail <= size * _HIGHLIGHT_TAIL_RATIO):
            marks.append((rect, inside))
    marks = _without_cell_rows(page, marks)
    if not marks:
        return page

    # A rectangle without characters (other than spaces) is a highlight only when it overlaps vertically with a same-coloured
    # highlight touching it on the left or right. Without the colour check, the neighbouring vertical rule that Chrome draws as a
    # filled rectangle (cell padding is 1px, so within 1.5pt of the highlight) was removed as well
    by_right = sorted((rect for rect, _ in marks), key=lambda mark: mark["x1"])
    by_left = sorted((rect for rect, _ in marks), key=lambda mark: mark["x0"])
    rights, lefts = [mark["x1"] for mark in by_right], [mark["x0"] for mark in by_left]
    dropped = {id(rect) for rect, _ in marks}
    for rect in empty:
        touching = (*by_right[bisect_left(rights, rect["x0"] - _HIGHLIGHT_EDGE_MAX):bisect_right(rights, rect["x0"] + _HIGHLIGHT_EDGE_MAX)],
                    *by_left[bisect_left(lefts, rect["x1"] - _HIGHLIGHT_EDGE_MAX):bisect_right(lefts, rect["x1"] + _HIGHLIGHT_EDGE_MAX)])
        if any(rect["top"] < mark["bottom"] and mark["top"] < rect["bottom"]
               and rect["bottom"] - rect["top"] <= (mark["bottom"] - mark["top"]) * 1.5
               and rect.get("non_stroking_color") == mark.get("non_stroking_color") for mark in touching):
            dropped.add(id(rect))
    return page.filter(lambda obj: id(obj) not in dropped)


def _without_cell_rows(page: object, marks: list[tuple[dict, list[dict]]]) -> list[tuple[dict, list[dict]]]:
    """Remove the cells of a filled table row from `marks` (highlight candidate rectangle, covered characters)
    (`_without_highlights`).

    In a Chrome table whose column widths fit the text, filling only the header row gives cell rectangles that fit the text exactly
    and look the same as highlights (Pinned by: test_pdf_highlights.py::TestChromeTables). They are cells when a line has two or more
    candidates, the line has no unfilled word, and the text of every candidate aligns in a column — left, right or centre — with an
    unfilled word within `_CELL_ROW_REACH` lines above or below. Consecutive lines filling the same text are not compared with each
    other (a highlighted paragraph repeating the same line looked like cell rows and became a table). A line mixed with unfilled
    words is a paragraph — judging by column alignment alone, the first candidate of a line coincided with the left margin and the
    second with one of dozens of surrounding words within 1pt, and a highlighted paragraph became a fake table. A line whose top and
    bottom edges differ or whose candidates are apart from each other (LibreOffice's Hangul and digit spans, highlights that fill
    only the text width in each ruled-table cell) is not a cell row either — the highlight edges became vertical rules and shifted
    the columns (Pinned by: test_pdf_highlights.py::TestScatteredHighlightsInAParagraph). Known accepted gap: a filled row whose
    columns do not align with the text of other rows (a one-row table, empty cells below the columns) is still removed as highlights.
    """
    # Lines are grouped by the middle height of the rectangles. Grouping by the top of the characters put Hangul and digit spans on
    # different lines and bypassed the height comparison
    lines: dict[float, list[tuple[dict, list[dict]]]] = {}
    first_middle = 0.0
    for mark in sorted(marks, key=lambda mark: mark[0]["top"] + mark[0]["bottom"]):
        middle = (mark[0]["top"] + mark[0]["bottom"]) / 2
        if not lines or middle - first_middle > max(char["size"] for char in mark[1]) / 2:
            first_middle, top = middle, min(char["top"] for char in mark[1])
            lines[top] = []
        lines[top].append(mark)
    if all(len(line) < 2 for line in lines.values()):
        return marks

    painted = {(round(char["x0"], 1), round(char["top"], 1)) for _, inside in marks for char in inside}
    words = [word for word in page.extract_words(return_chars=True)
             if (round(word["chars"][0]["x0"], 1), round(word["chars"][0]["top"], 1)) not in painted]
    cells = set()
    for top, line in lines.items():
        size = max(char["size"] for _, inside in line for char in inside)
        reach = size * _CELL_ROW_REACH
        near = [word for word in words if size / 2 < abs(word["top"] - top) <= reach]
        if (len(line) >= 2 and _tiled_row(line) and not any(abs(word["top"] - top) <= size / 2 for word in words)
                and all(_aligned_with_any(inside, near) for _, inside in line)):
            cells.update(id(rect) for rect, _ in line)
    return [mark for mark in marks if id(mark[0]) not in cells]


def _tiled_row(line: list[tuple[dict, list[dict]]]) -> bool:
    """The cell shadings of one row have the same top and bottom edges and touch the neighbouring cell. LibreOffice highlights differ
    in height between Hangul spans and digit/Latin spans, and highlights filled per ruled-table cell cover only the text width, so
    there are gaps between the cells."""
    first = line[0][0]
    rects = sorted((rect for rect, _ in line), key=lambda rect: rect["x0"])
    return (all(abs(rect["top"] - first["top"]) <= _CELL_ALIGN and abs(rect["bottom"] - first["bottom"]) <= _CELL_ALIGN
                for rect in rects)
            and all(right["x0"] - left["x1"] <= _HIGHLIGHT_EDGE_MAX for left, right in pairwise(rects)))


def _aligned_with_any(inside: list[dict], words: list[dict]) -> bool:
    left, right = min(char["x0"] for char in inside), max(char["x1"] for char in inside)
    return any(abs(word["x0"] - left) <= _CELL_ALIGN or abs(word["x1"] - right) <= _CELL_ALIGN
               or abs(word["x0"] + word["x1"] - left - right) <= _CELL_ALIGN * 2 for word in words)


def _real_tables(page: object) -> list[tuple[tuple[float, float, float, float], list[list[str]], list[tuple]]]:
    """Filter layout boxes out of the ruled tables: page frames (a box that covers the page but has hardly any cells), outer boxes
    that enclose another table, and boxes whose content is a single column.

    The characters of a filtered-out box are read as body flow. Returns (bbox, cell matrix, list of cell boxes).
    """
    page_area = max(float(page.width) * float(page.height), 1.0)
    regular = [_closed_at_open_ends(page, _closed_at_the_bottom(page, table)) for table in page.find_tables(_TABLE_SETTINGS)]
    open_sided = [table for table in _open_sided_tables(page)
                  if not any(_is_inner_part(table.bbox, closed.bbox) for closed in regular)]
    found = _with_shaded_rows(page, open_sided + [table for table in regular
                                                  if not any(_encloses(other.bbox, table.bbox) for other in open_sided)])
    candidates = []
    for table in found:
        x0, top, x1, bottom = table.bbox
        is_grid = len(table.rows) > _FRAME_MAX_ROWS and len(table.columns) >= 2
        if (x1 - x0) * (bottom - top) >= page_area * _PAGE_FRAME_RATIO and not is_grid:
            continue
        cells = _cell_rows(table)
        merged = _merge_split_header(cells)
        filled_columns = {index for row in merged for index, cell in enumerate(row) if cell and cell.strip()}
        if len(filled_columns) >= 2:
            header_rows = len(cells) - len(merged) + 1  # 2 if a two-level header was merged
            _mark_filled_cells(table, cells, header_rows)
            rows = [[cell or "" for cell in row] for row in (*merged[:1], *cells[header_rows:])]
            candidates.append((table.bbox, rows, list(table.cells), table))

    kept, absorbed = [], set()
    for bbox, rows, boxes, table in candidates:
        inner = [other for other in candidates if other[0] is not bbox and _encloses(bbox, other[0])]
        if inner:
            rows = _with_inner_tables(table, rows, inner)
            if rows is None:
                continue
            absorbed.update(id(other[0]) for other in inner)
            kept.append((bbox, [(bbox, rows, boxes)]))
        elif len(rows) == len(table.rows):
            kept.append((bbox, _split_column_sections(table, bbox, rows)))
        else:
            kept.append((bbox, [(bbox, rows, boxes)]))
    return [piece for bbox, pieces in kept if id(bbox) not in absorbed for piece in pieces]


def _closed_at_the_bottom(page: object, table: object) -> object:
    """If the bottom row has a place that did not become a cell and text inside the table lies in no cell, the table found again
    after adding a horizontal line at the bottom of the table. `table` itself when there is nothing to change.

    A vertically merged cell split at the page end (rowspan in Chrome printing) has no bottom border on that page and did not become
    a cell. Its text ('그룹5 담당: 팀5') fell out as a paragraph outside the table and the first cell of those rows was empty (Pinned by:
    test_pdf_table_columns.py::TestMergedCellOpenAtThePageEnd). When the cell text is longer than the last row and continues beyond
    the bottom of the table (text of the same column continuing within the line spacing), only that column is extended down to the
    end of the text and closed there — closing at the table bottom split the text so that only the first part was in the cell and
    the rest ('담당: 팀10') became a paragraph outside the table. The table is replaced only when exactly one table is found again,
    the row and column counts are the same, and all the text that fell out is now in cells. For the open-topped empty cell on the
    next page see `_carry_open_cells`.
    """
    if not table.rows or all(cell is not None for cell in table.rows[-1].cells):
        return table
    x0, top, x1, bottom = table.bbox
    in_cells = _BoxIndex([cell for cell in table.cells if cell]).holds
    stray = [char for char in page.chars if char["text"].strip() and x0 < (char["x0"] + char["x1"]) / 2 < x1
             and top < (char["top"] + char["bottom"]) / 2 < bottom and not in_cells(char)]
    if not stray:
        return table

    # When the cell text continues beyond the bottom of the table (merged-cell text longer than the last row), only that column is
    # extended down to the end of the continuing lines and closed there
    edges = _column_edges(table)
    middle = (min(char["x0"] for char in stray) + max(char["x1"] for char in stray)) / 2
    index = max(0, min(bisect_right(edges, middle) - 1, len(edges) - 2))
    left, right = edges[index], edges[index + 1]
    end = max(char["bottom"] for char in stray)
    for char in sorted((char for char in page.chars if char["text"].strip() and (char["top"] + char["bottom"]) / 2 >= bottom
                        and left < (char["x0"] + char["x1"]) / 2 < right), key=lambda char: char["top"]):
        if char["top"] - end > (char["bottom"] - char["top"]) * 0.5:
            break
        stray.append(char)
        end = max(end, char["bottom"])
    lowest = max(bottom, end + 1)
    closing = [_rule_line(a, bottom, b, bottom) for a, b in ((x0, left), (right, x1)) if b > a]  # close while leaving that column open
    lines = {"explicit_horizontal_lines": [*closing, _rule_line(left, lowest, right, lowest)],
             "explicit_vertical_lines": [_rule_line(x, bottom, x, lowest) for x in (left, right)] if lowest > bottom else []}

    page_left, page_top, page_right, page_bottom = page.bbox
    box = (max(x0 - 1, page_left), max(top - 1, page_top), min(x1 + 1, page_right), min(lowest + 1, page_bottom))
    found = page.crop(box).find_tables({**_TABLE_SETTINGS, **lines})
    if (len(found) != 1 or len(found[0].rows) != len(table.rows) or len(found[0].columns) != len(table.columns)
            or not all(map(_BoxIndex([cell for cell in found[0].cells if cell]).holds, stray))):
        return table
    return found[0]


def _closed_at_open_ends(page: object, table: object) -> object:
    """If the vertical rules continue past the bottom (top) of the table at every column boundary and there is text in between, the
    table found again after adding a horizontal line at the end of the continuing vertical lines (a row split at the page end).
    `table` itself when there is nothing to change.

    Chrome printing draws every cell of a row split at the page end without a bottom border, and the remainder on the next page
    without a top border. That row did not become cells, so its first part fell out as a paragraph outside the table, and the
    remainder on the next page also became a paragraph above the table, so the header was not carried to the next page's table
    (Pinned by:
    test_pdf_table_columns.py::TestRowSplitAcrossPages::test_row_whose_cells_are_all_open_at_the_page_end_stays_in_the_table).
    For a vertically merged cell open in one column only see `_closed_at_the_bottom`. The table is replaced only when exactly one
    table is found again, the column count is the same, the rows grow by the number of ends added, and all the text in between is in
    cells. How this row is continued on the next page is in `_carry_table_headers`.
    """
    if not table.rows:
        return table
    x0, top, x1, bottom = table.bbox
    edges = _column_edges(table)
    verticals = [edge for edge in page.vertical_edges if edge["bottom"] - edge["top"] > 1]
    lowest = _common_reach(verticals, edges, bottom, 1)
    highest = _common_reach(verticals, edges, top, -1)
    bands = [band for band in ((bottom, lowest), (highest, top)) if band[1] - band[0] >= _CHECKBOX_SIDE[0]]
    stray = [char for char in page.chars if char["text"].strip() and x0 < (char["x0"] + char["x1"]) / 2 < x1
             and any(low < (char["top"] + char["bottom"]) / 2 < high for low, high in bands)]
    if not stray:
        return table

    lines = [_rule_line(x0, y, x1, y) for y in (lowest, highest) if any(y in band for band in bands)]
    page_left, page_top, page_right, page_bottom = page.bbox
    box = (max(x0 - 1, page_left), max(min(top, highest) - 1, page_top), min(x1 + 1, page_right),
           min(max(bottom, lowest) + 1, page_bottom))
    found = page.crop(box).find_tables({**_TABLE_SETTINGS, "explicit_horizontal_lines": lines})
    if (len(found) != 1 or len(found[0].rows) != len(table.rows) + len(lines) or len(found[0].columns) != len(table.columns)
            or not all(map(_BoxIndex([cell for cell in found[0].cells if cell]).holds, stray))):
        return table
    return found[0]


def _common_reach(verticals: list[dict], edges: list[float], at: float, direction: int) -> float:
    """The end reached by all the vertical rules that start at height `at` at each column boundary (`edges`) and continue downwards
    (`direction` 1) or upwards (-1). `at` if any boundary has no continuing line."""
    reach = math.inf if direction > 0 else -math.inf

    for x in edges:
        ends = [edge["bottom"] if direction > 0 else edge["top"] for edge in verticals if abs(edge["x0"] - x) <= 1.5
                and abs((edge["top"] if direction > 0 else edge["bottom"]) - at) <= 1.5]
        if not ends:
            return at
        reach = min(reach, max(ends)) if direction > 0 else max(reach, min(ends))

    return reach if edges else at


def _split_column_sections(table: object, bbox: tuple[float, float, float, float],
                           rows: list[list[str]]) -> list[tuple[tuple[float, float, float, float], list[list[str]], list[tuple]]]:
    """Split a ruled table (`table`, cell text `rows`) into groups of rows with different column boundaries. It is split only where
    two groups, each keeping the same column boundaries for `_SECTION_MIN_ROWS` rows or more, touch and neither side's boundaries are
    all contained in the other's; in a split table the columns that have no cell in that group (columns covered by merging) are
    removed. Without a split the single table is returned as is (one (bbox, rows, list of cell boxes)).

    pdfplumber read two touching tables (a 6-column '경비 내역' table right below a 4-column '인적 사항' grid) as one grid combining
    the column boundaries of both, so empty columns were inserted and the lower table's header became a data row (Pinned by:
    test_pdf_table_columns.py::TestAbuttingTablesWithDifferentColumns).
    Header rows and total rows that span columns are part of rows with different boundaries and are not split off.
    """
    table_rows = table.rows  # a property that regroups all the cells on every access
    if any(len(row.cells) != len(rows[0]) for row in table_rows):
        return [(bbox, rows, list(table.cells))]
    edges = [tuple(round(edge) for edge in sorted({edge for cell in row.cells if cell is not None for edge in (cell[0], cell[2])}))
             for row in table_rows]
    runs: list[list[int]] = []  # consecutive row numbers with the same boundaries

    for index, edge in enumerate(edges):
        if runs and _same_edges(edges[runs[-1][0]], edge):
            runs[-1].append(index)
        else:
            runs.append([index])

    sections = [list(runs[0])]

    for previous, run in zip(runs, runs[1:], strict=False):
        apart = (len(previous) >= _SECTION_MIN_ROWS and len(run) >= _SECTION_MIN_ROWS
                 and not _edges_within(edges[previous[0]], edges[run[0]])
                 and not _edges_within(edges[run[0]], edges[previous[0]]))
        if apart:
            sections.append(list(run))
        else:
            sections[-1].extend(run)
    if len(sections) == 1:
        return [(bbox, rows, list(table.cells))]

    split = []

    for section in sections:
        cells = [[table_rows[index].cells[column] for column in range(len(rows[0]))] for index in section]
        columns = [column for column in range(len(rows[0])) if any(line[column] is not None for line in cells)]
        boxes = [cell for line in cells for cell in line if cell is not None]
        area = (min(box[0] for box in boxes), min(box[1] for box in boxes), max(box[2] for box in boxes), max(box[3] for box in boxes))
        split.append((area, [[rows[index][column] for column in columns] for index in section], boxes))
    return split


def _same_edges(edges: tuple[int, ...], others: tuple[int, ...]) -> bool:
    return len(edges) == len(others) and all(abs(edge - other) <= 1 for edge, other in zip(edges, others, strict=True))


def _edges_within(edges: tuple[int, ...], others: tuple[int, ...]) -> bool:
    """Whether all column boundaries in `edges` are boundaries of `others` (within 1pt)."""
    return all(any(abs(edge - other) <= 1 for other in others) for edge in edges)


def _with_inner_tables(table: object, rows: list[list[str]], inner: list[tuple]) -> list[list[str]] | None:
    """If the ruled table `table` (cell text `rows`) that encloses other ruled tables (`inner` = candidates of `_real_tables`) is a
    form containing tables within a table, the cell text with the inner tables spread out in place of the outer cell; otherwise None
    (the outer table is a layout box and is discarded).

    It counts as a form only when every inner table has an outer cell containing it, the row of that cell has other cell text
    (labels) and all of it is `_NESTED_LABEL_MAX` characters or less (deliberate, conservative — one cell holding inner tables per
    row). When the inner table holds all the text of that cell (lines above and below the table become rows before and after), the
    row is expanded to the inner table's row count and the label is repeated on every row ('학력 | 기간 | 학교명 | …'); otherwise (the
    inner table was found for only some rows and text remains beside it, or several tables in one cell) the outer cell's text is
    kept as is. Either way the inner table is not emitted separately. In a résumé printed by Chrome the outer table was discarded as
    'a box enclosing another table', so '성명' and '홍길동' were scattered into paragraphs and '학력' was separated from its table
    (Pinned by: test_pdf_nested_tables.py::TestNestedTables::test_inner_table_found_apart_keeps_the_outer_label_on_its_rows,
    test_inner_table_found_only_in_part_is_kept_as_the_outer_cell_text, test_unit_line_under_the_inner_table_becomes_a_row_after_it).
    """
    if len(rows) != len(table.rows):
        return None  # a table whose header was merged or whose inner-table cell rows were removed
    holders: dict[int, tuple[int, list[tuple]]] = {}  # row → (column, inner tables in that cell)

    for other in inner:
        place = next(((row, column) for row, line in enumerate(table.rows) for column, cell in enumerate(line.cells)
                      if cell is not None and _encloses(cell, other[0])), None)
        if place is None:
            return None
        column, held = holders.setdefault(place[0], (place[1], []))
        if column != place[1]:
            return None
        held.append(other)

    for row, (column, _) in holders.items():
        labels = [text for index, text in enumerate(rows[row]) if index != column and text.strip()]
        if not labels or any(len(text) > _NESTED_LABEL_MAX for text in labels):
            return None

    spread: list[list[str]] = []

    for row, cells in enumerate(rows):
        column, held = holders.get(row, (0, []))
        around = _text_around(table.page.chars, table.rows[row].cells[column], held[0]) if len(held) == 1 else None
        if around is None:
            spread.append(cells)
            continue
        width = 1 + next((count for count, cell in enumerate(table.rows[row].cells[column + 1:]) if cell is not None),
                         len(cells) - column - 1)
        above, below = around
        inner_rows = [*([[above]] if above else []), *held[0][1], *([[below]] if below else [])]
        spread.extend([*cells[:column], *inner_row, *cells[column + width:]] for inner_row in inner_rows)

    width = max(len(cells) for cells in spread)
    return [[*cells, *[""] * (width - len(cells))] for cells in spread]


def _text_around(chars: list[dict], cell: tuple, inner: tuple) -> tuple[str, str] | None:
    """Text inside the outer cell `cell` that lies outside the cells of the inner table (`inner` = a candidate of `_real_tables`):
    (text above the table, text below the table). None if there are characters beside the table (the inner table was found only in
    part). A unit line ('(단위: 천원)') or caption becomes a row before or after the inner table."""
    bbox, _, inner_cells, _ = inner
    outside = [char for char in chars
               if char["text"].strip() and _inside_any(char, [cell]) and not _inside_any(char, inner_cells)]
    if any(bbox[1] <= (char["top"] + char["bottom"]) / 2 <= bbox[3] for char in outside):
        return None
    lines: dict[int, set[float]] = {}  # line → the inner-table columns (left edge) the line's characters lie in

    # A line lying across two or more columns of the inner table is not a unit line or caption but a row that was not found as table
    for char in outside:
        middle = (char["x0"] + char["x1"]) / 2
        columns = lines.setdefault(round(char["top"] / _TABLE_ROW_TOLERANCE), set())
        columns.update(x0 for x0, _, x1, _ in inner_cells if x0 <= middle <= x1)
    if any(len(columns) >= 2 for columns in lines.values()):
        return None

    above = _text_outside([char for char in outside if char["bottom"] < bbox[1] + 1], cell, [])
    below = _text_outside([char for char in outside if char["top"] > bbox[3] - 1], cell, [])
    return above, below


def _mark_filled_cells(table: object, rows: list[list[str | None]], header_rows: int) -> None:
    """Put `_FILL_MARK` into the empty cells of a ruled table that are marked only by a fill colour (`rows` is the cell text from
    `_cell_rows`, modified in place).

    In a Gantt-chart schedule (the January–February cells of the '요구사항 분석' row filled blue) the cells have no text, so the whole
    schedule disappeared (Pinned by: test_pdf.py::
    TestCellsMarkedOnlyByFill::test_empty_cells_filled_with_a_colour_are_marked). Only cells that satisfy all of the following are
    marked (deliberate, conservative): an empty cell below the header rows (`header_rows`) ("", not a None hidden by merging), a
    fill rectangle in a mark colour (`_is_mark_fill`) covers at least `_FILL_MIN_COVER` of the cell area, the same row has an
    unfilled cell (not a whole-row background, stripes or a whole-table background), the cells below the header in the same column
    are not all filled (not whole-column shading), and somewhere in the table two neighbouring cells of the same row are filled in
    a run (a bar — not scattered cell emphasis). Header-row shading is excluded because header rows are not examined (Pinned by:
    test_shading_that_is_not_a_mark_leaves_empty_cells_empty). Known accepted gap: a column filled in every data row (a schedule
    over the whole period), a schedule that fills a whole row, pale grey bars and tables whose bars are all one cell wide are not
    marked, while pale coloured 'do not fill in' cells in a table that has cells filled in a run are marked. Cells filled with a
    picture (image) are not examined.
    """
    x0, top, x1, bottom = table.bbox
    fills = [rect for rect in table.page.rects
             if rect["x0"] < x1 and rect["x1"] > x0 and rect["top"] < bottom and rect["bottom"] > top  # only rectangles touching the table
             and rect.get("fill") and _is_mark_fill(rect.get("non_stroking_color"))]
    data = table.rows[header_rows:]
    if not fills or len(data) < 2 or len(rows) != len(table.rows):
        return

    covered = [[cell is not None and _fill_cover(cell, near) >= _FILL_MIN_COVER for cell in row.cells]
               for row, near in zip(data, _fills_by_row(data, fills), strict=True)]
    shaded_columns = {column for column in range(len(covered[0])) if all(row[column] for row in covered)}
    marks: set[tuple[int, int]] = set()
    for index, row in enumerate(data):
        if all(covered[index][column] for column, cell in enumerate(row.cells) if cell is not None):
            continue
        marks.update((header_rows + index, column) for column, cell in enumerate(rows[header_rows + index])
                     if cell == "" and covered[index][column] and column not in shaded_columns)

    # Without a single bar of two neighbouring cells filled in a run it is not a schedule (scattered cell emphasis)
    if not any((row, column + 1) in marks for row, column in marks):
        return
    for row, column in marks:
        rows[row][column] = _FILL_MARK


def _is_mark_fill(colour: object) -> bool:
    """Whether a fill colour counts as a cell mark: a chromatic colour (RGB components differ by at least `_FILL_HUE_MIN`, colours
    close to white excluded) or a colour as dark as `_FILL_GREY_MAX` or darker. Only numeric grey, RGB and CMYK colours are
    considered (unknown colour spaces and patterns are False).

    Light grey is not considered (deliberate): forms commonly paint 'do not fill in' cells light grey, and ■ would read as a
    'selected cell' (pinned by: test_pdf.py::TestCellsMarkedOnlyByFill::test_shading_that_is_not_a_mark_leaves_empty_cells_empty,
    the light-grey case).
    """
    if not isinstance(colour, (tuple, list)) or not colour or not all(isinstance(value, (int, float)) for value in colour):
        return False
    if len(colour) == 4:
        cyan, magenta, yellow, black = colour
        colour = tuple((1 - value) * (1 - black) for value in (cyan, magenta, yellow))
    if min(colour) >= _FILL_WHITE_MIN:
        return False
    return max(colour) - min(colour) >= _FILL_HUE_MIN or max(colour) <= _FILL_GREY_MAX


def _fills_by_row(rows: list[object], fills: list[dict]) -> list[list[dict]]:
    """For each table row, the fill rectangles that reach that row's height. Sweeps top to bottom and keeps only the ones that
    reach — looking at every rectangle for every cell was quadratic time on a table with thousands of filled cells
    (pinned by: test_pdf_resource_limits.py::TestManyCellsOnOnePage)."""
    ordered = sorted(fills, key=lambda rect: rect["top"])
    result, active, position = [], [], 0
    for row in sorted(range(len(rows)), key=lambda index: rows[index].bbox[1]):
        _, top, _, bottom = rows[row].bbox
        while position < len(ordered) and ordered[position]["top"] < bottom:
            active.append(ordered[position])
            position += 1
        active = [rect for rect in active if rect["bottom"] > top]
        result.append((row, _by_area(active)))
    return [near for _, near in sorted(result, key=lambda item: item[0])]


def _fill_cover(cell: tuple[float, float, float, float], fills: list[dict]) -> float:
    """The share of the cell box's (x0, top, x1, bottom) area covered by the fill rectangles (overlapping rectangles are summed
    and clamped at 1).

    Once fully covered, the remaining rectangles are not added — 90,000 cells under thousands of rectangles covering the whole
    table summed all of them per cell and took 78 seconds (fuzzing, pinned by:
    test_pdf_resource_limits.py::TestCellsReachingFarOffThePage). The caller passes the largest rectangles first (`_by_area`)."""
    x0, top, x1, bottom = cell
    area = max((x1 - x0) * (bottom - top), 1e-6)
    inside = 0.0

    for rect in fills:
        inside += max(0.0, min(x1, rect["x1"]) - max(x0, rect["x0"])) * max(0.0, min(bottom, rect["bottom"]) - max(top, rect["top"]))
        if inside >= area:
            return 1.0
    return min(inside / area, 1.0)


def _by_area(rects: list[dict]) -> list[dict]:
    """The rectangles ordered largest first (so `_fill_cover` stops early)."""
    return sorted(rects, key=lambda rect: (rect["x1"] - rect["x0"]) * (rect["bottom"] - rect["top"]), reverse=True)


def _with_shaded_rows(page: object, tables: list[object]) -> list[object]:
    """The table list found again after adding, to ruled tables whose cell boundaries came from cell shading (rectangles that
    are filled but have no border), the unshaded rows that continue on the same column boundaries. `tables` as is when
    there is nothing to change.

    pdfplumber also uses the sides of filled rectangles as rules. In a striped table filled cell by cell (Chrome
    `tr:nth-child(even)`, Word 'Light Shading') only the shaded rows became a ruled table and the unshaded rows became
    separate paragraphs, so the row order was shuffled and the numbers scattered. A table that shades only the header cells
    and draws only a bottom border under the data rows (Chrome th background) became a table of the header alone. A boxed
    table whose vertical lines exist only on the header row (Word 'Medium Shading 1') had its unshaded rows merged into one
    cell (pinned by: test_pdf_shaded_tables.py::TestShadedTables). Only for tables with cell shading, the page is split into
    bands by that table's column boundaries and vertical lines are added only to bands whose cell text fits the cell
    boundaries (`_grown_shaded_table`).
    """
    shades = _by_area([rect for rect in page.rects
                       if rect.get("fill") and not rect.get("stroke") and _is_shade(rect.get("non_stroking_color"))
                       and min(rect["x1"] - rect["x0"], rect["bottom"] - rect["top"]) > _SHADE_MIN_SIDE])
    if not shades:
        return tables
    rules: list[tuple[float, float, float]] | None = None  # collected the first time they are needed
    result = list(tables)

    for table in sorted(tables, key=lambda table: table.bbox[1]):
        if not any(other is table for other in result):
            continue  # merged into an earlier table
        if not any(cell and _fill_cover(cell, shades) >= _FILL_MIN_COVER for cell in table.cells):
            continue
        rules = _horizontal_rules(page) if rules is None else rules
        grown = _grown_shaded_table(page, table, result, rules)
        if grown is None:
            continue
        new, replaced = grown
        position = min(index for index, other in enumerate(result) if any(other is old for old in replaced))
        result = [other for other in result if not any(other is old for old in replaced)]
        result.insert(position, new)
    return _with_merged_column_fragments(page, result, shades)


def _with_merged_column_fragments(page: object, tables: list[object], shades: list[dict]) -> list[object]:
    """The table list found again after joining, among shaded table fragments, those whose column boundaries are a subset of
    another fragment's (a fragment missing its leading column because of a vertically merged cell) with that wider fragment
    into one table. `tables` as is when there is nothing to change.

    A vertically merged cell in a striped table takes the fill of the row it starts on (rowspan under Chrome
    `tr:nth-child(even)`). A fragment starting at a shaded merged cell has 4 columns and a fragment beside an unshaded merged
    cell has 3 columns with the first one missing, so the growing step (`_grown_shaded_table`), which joins only fragments
    with the same column boundaries, could not join the two. The table grown from the 3-column fragment overlapped the
    4-column fragment and rows came out twice, or it could not be grown and the '2팀' and '개발팀' rows fell out of the
    table (pinned by: test_pdf_shaded_tables.py::TestShadedTablesWithMergedCells). Only when the wide fragment (the one with
    the most column boundaries) and a fragment having only some of those boundaries are within `_SHADED_ROW_REACH` row
    heights, the region covering both is split again by the wide fragment's column boundaries (`_regridded_fragments`).
    Fragments with the same column boundaries are left alone (so the in-between bands the growing step rejected are not
    revived).
    """
    shaded = [table for table in tables if any(cell and _fill_cover(cell, shades) >= _FILL_MIN_COVER for cell in table.cells)]
    if len(shaded) < 2 and not any(_has_holes(table) for table in shaded):
        return tables
    result = list(tables)
    row_height = _row_pitch(shaded)

    for widest in sorted(shaded, key=lambda table: -len(_column_edges(table))):
        edges = _column_edges(widest)
        if not any(other is widest for other in result) or len(edges) < 3:
            continue
        members = [widest]
        grew = True

        while grew:
            top, bottom = min(member.bbox[1] for member in members), max(member.bbox[3] for member in members)
            near = [other for other in shaded
                    if any(other is kept for kept in result) and not any(other is member for member in members)
                    and _edges_inside(_column_edges(other), edges) and other.bbox[1] - bottom <= row_height * _SHADED_ROW_REACH
                    and top - other.bbox[3] <= row_height * _SHADED_ROW_REACH]
            members += near
            grew = bool(near)
        if not any(len(_column_edges(member)) < len(edges) or _has_holes(member) for member in members):
            continue

        merged = _regridded_fragments(page, members, edges, shades, row_height)
        if merged is None:
            continue
        position = min(index for index, other in enumerate(result) if any(other is member for member in members))
        result = [other for other in result if not any(other is member for member in members)]
        result.insert(position, merged)
    return result


def _row_pitch(tables: list[object]) -> float:
    """The row height of the table fragments: the median gap between the top and bottom sides of the cell boxes (sides within
    1pt count as one). Rows of merged cells are tall.

    Using the median of pdfplumber's row heights gave two row heights on a table where a vertically merged cell spans every
    row, so the header band right above also covered the caption above the table and the header was not accepted
    (pinned by: test_pdf_round44.py::TestZebraTableWhoseMergedGroupsStartOnShadedRows)."""
    heights: list[float] = []
    for y in sorted({y for table in tables for cell in table.cells if cell for y in (cell[1], cell[3])}):
        if not heights or y - heights[-1] > 1:
            heights.append(y)
    gaps = [lower - upper for upper, lower in pairwise(heights)]
    return statistics.median(gaps) if gaps else statistics.median(row.bbox[3] - row.bbox[1] for table in tables for row in table.rows)


def _has_holes(table: object) -> bool:
    """Whether the cell boxes of a ruled table fail to cover the whole table box (there are gaps with no cell).

    When every vertically merged group of a striped table starts on a shaded row, the single table found from the sides of
    the filled rectangles alone has gaps from the second column onward on the unshaded rows. There was no separate wide and
    narrow fragment, so nothing was split again and those rows scattered outside the table one number at a time
    (pinned by: test_pdf_round44.py::TestZebraTableWhoseMergedGroupsStartOnShadedRows)."""
    x0, top, x1, bottom = table.bbox
    covered = sum((cell[2] - cell[0]) * (cell[3] - cell[1]) for cell in table.cells if cell)
    return covered < (x1 - x0) * (bottom - top) * 0.98


def _edges_inside(edges: list[float], others: list[float]) -> bool:
    """Whether there are two or more column boundaries in `edges` and each matches some boundary of `others` within 1pt."""
    return len(edges) >= 2 and all(any(abs(edge - other) <= 1 for other in others) for edge in edges)


def _regridded_fragments(page: object, members: list[object], edges: list[float], shades: list[dict],
                         row_height: float) -> object | None:
    """The single table found again over the region covering the table fragments (`members`) using the column boundaries
    `edges`. None when it cannot be found.

    The row boundaries are the fragments' row boundaries plus the top and bottom sides of the filled rectangles inside that
    region. The header band right above (`_is_header_band`) and the closing band right below (`_band_cells` +
    `_ends_the_text`, the last unshaded row) are added by the same criteria as the growing step. Vertical lines are drawn at
    every column boundary over the full height of the region; horizontal lines are drawn, per column, only at row boundaries
    that the column's text does not straddle and that are not inside a filled rectangle covering that column — a merged cell
    ('영업본부' shaded over 3 rows, '기술본부' whose text straddles a row boundary by more than `_MERGED_TEXT_CROSSING`)
    stays one cell and its value is repeated on the rows below (`_cell_rows`).
    If any line of the region crosses a column boundary at word spacing (`_band_chars`), it is not a table.
    """
    left, right = edges[0], edges[-1]
    top, bottom = min(member.bbox[1] for member in members), max(member.bbox[3] for member in members)
    above, below = (top - row_height, top), (bottom, bottom + row_height)
    if _is_header_band(page, above, edges) or (_band_cells(page, above, edges) is not None
                                               and _ends_the_text(page, above, -1, left, right, row_height)):
        top = above[0]  # a year header ('2024') is a number cell, so the header-shape test misses it
    if _band_cells(page, below, edges) is not None and _ends_the_text(page, below, 1, left, right, row_height):
        bottom = below[1]
    if _band_chars(page, (top, bottom), edges) is None:
        return None

    covers = [shade for shade in shades if shade["x0"] >= left - 1 and shade["x1"] <= right + 1
              and shade["top"] >= top - 1 and shade["bottom"] <= bottom + 1]
    heights = sorted({top, bottom, *(y for member in members for row in member.rows for y in (row.bbox[1], row.bbox[3])),
                      *(y for shade in covers for y in (shade["top"], shade["bottom"]))})
    boundaries = [y for index, y in enumerate(heights) if index == 0 or y - heights[index - 1] > _TABLE_ROW_TOLERANCE]
    chars = [char for char in page.chars if char["text"].strip() and left < (char["x0"] + char["x1"]) / 2 < right]

    def crossed(y: float, x0: float, x1: float) -> bool:  # whether boundary y lies inside a single cell in this column
        return (any(x0 < (char["x0"] + char["x1"]) / 2 < x1
                    and min(y - char["top"], char["bottom"] - y) > (char["bottom"] - char["top"]) * _MERGED_TEXT_CROSSING
                    for char in chars)
                or any(shade["x0"] <= x0 + 1 and shade["x1"] >= x1 - 1 and shade["top"] + 1 < y < shade["bottom"] - 1
                       for shade in covers))

    verticals = [_rule_line(x, top, x, bottom) for x in edges]
    horizontals = [_rule_line(x0, y, x1, y) for y in boundaries for x0, x1 in zip(edges, edges[1:], strict=False)
                   if y in (top, bottom) or not crossed(y, x0, x1)]

    page_left, page_top, page_right, page_bottom = page.bbox
    box = (max(left - 1, page_left), max(top - 1, page_top), min(right + 1, page_right), min(bottom + 1, page_bottom))
    found = page.crop(box).find_tables({**_TABLE_SETTINGS, "explicit_vertical_lines": verticals,
                                        "explicit_horizontal_lines": horizontals})
    if len(found) != 1 or len(_column_edges(found[0])) != len(edges):
        return None
    return found[0]


def _is_shade(colour: object) -> bool:
    """Whether a fill colour counts as cell shading: a numeric grey, RGB or CMYK colour that is not white (`_FILL_WHITE_MIN`)."""
    if not isinstance(colour, (tuple, list)) or not colour or not all(isinstance(value, (int, float)) for value in colour):
        return False
    if len(colour) == 4:
        cyan, magenta, yellow, black = colour
        colour = tuple((1 - value) * (1 - black) for value in (cyan, magenta, yellow))
    return min(colour) < _FILL_WHITE_MIN


def _horizontal_rules(page: object, edges: list[dict] | None = None) -> list[tuple[float, float, float]]:
    """The page's horizontal rules (lines and rectangle sides, or `edges`) as a list of (height, left end, right end), with
    pieces that continue at the same height (within 1pt) joined."""
    rules: list[tuple[float, float, float]] = []
    edges = [edge for edge in page.edges if edge["orientation"] == "h"] if edges is None else edges
    for edge in sorted(edges, key=lambda edge: (edge["top"], edge["x0"])):
        if rules and abs(edge["top"] - rules[-1][0]) <= 1 and edge["x0"] <= rules[-1][2] + 1:
            rules[-1] = (rules[-1][0], rules[-1][1], max(rules[-1][2], edge["x1"]))
        else:
            rules.append((edge["top"], edge["x0"], edge["x1"]))
    return rules


def _thin_rules(page: object) -> list[tuple[float, float, float]]:
    """The page's thin horizontal rules (horizontal lines, and filled rectangles no taller than `_SHADE_MIN_SIDE` — Chrome
    borders) as a joined list (`_horizontal_rules`). The sides of shaded row rectangles are left out — they would make every
    row of a striped table look ruled."""
    thin = [{"top": (line["top"] + line["bottom"]) / 2, "x0": line["x0"], "x1": line["x1"]} for line in page.lines
            if line["bottom"] - line["top"] <= 1]
    thin += [{"top": (rect["top"] + rect["bottom"]) / 2, "x0": rect["x0"], "x1": rect["x1"]} for rect in page.rects
             if rect["bottom"] - rect["top"] <= _SHADE_MIN_SIDE and rect["x1"] - rect["x0"] > _SHADE_MIN_SIDE]
    return _horizontal_rules(page, thin)


def _shade_bands(page: object) -> list[tuple[float, float, float, float]]:
    """(top, bottom, left end, right end) of the page's filled rectangles (no border, not white, both sides longer than
    `_SHADE_MIN_SIDE`)."""
    return [(rect["top"], rect["bottom"], rect["x0"], rect["x1"]) for rect in page.rects
            if rect.get("fill") and not rect.get("stroke") and _is_shade(rect.get("non_stroking_color"))
            and min(rect["x1"] - rect["x0"], rect["bottom"] - rect["top"]) > _SHADE_MIN_SIDE]


def _column_edges(table: object) -> list[float]:
    """The vertical boundaries of a ruled table's cell boxes (left to right, boundaries within 1pt count as one)."""
    edges: list[float] = []
    for x in sorted({x for cell in table.cells if cell for x in (cell[0], cell[2])}):
        if not edges or x - edges[-1] > 1:
            edges.append(x)
    return edges


def _grown_shaded_table(page: object, table: object, tables: list[object],
                        rules: list[tuple[float, float, float]]) -> tuple[object, list[object]] | None:
    """The table found again after growing a ruled table with cell shading (`table`) by bands on the same column boundaries,
    and the tables it replaces. None when there is nothing to change.

    Upward and downward, a band runs up to (1) another table fragment with the same column boundaries (the next shaded row of
    the stripes), (2) a horizontal rule covering the table width (a row's bottom border), or, when neither exists, (3) one
    band as tall as the fragment's row height (the last unshaded row); it continues only while each band's cell text fits the
    column boundaries (`_band_cells`). Rows with merged cells inside the table get the same test. Such bands and rows get
    vertical lines only when, in two or more columns, the cell text matches the same column's text on another row at the left
    end, right end or centre (`_aligned_cells`) — a body paragraph or first-column note under a shaded header, or a centred
    merged title row, does not become cells. As an exception, when the topmost closed band has non-numeric text in every
    column (`_is_header_band`) it is accepted as the header — the left-aligned text of an unshaded header did not match the
    right-aligned number columns and the whole growing step was given up (pinned by: test_pdf_shaded_tables.py::
    TestShadedTables::test_unshaded_header_over_banded_rows_with_right_aligned_numbers_stays_one_table). Vertical lines are
    drawn in each band exactly at the cell boundaries, only that region is searched again, and the table is replaced only
    when exactly one table comes out.
    """
    edges = _column_edges(table)
    if len(edges) < 3:
        return None
    left, right = edges[0], edges[-1]
    fragments = [other for other in tables if _same_column_edges(_column_edges(other), edges)]
    # rows spanned by a merged cell are tall, so the last open band also covered the paragraph below the table ('끝.')
    row_height = _row_pitch(fragments)
    reach = row_height * _SHADED_ROW_REACH
    spanning = sorted(y for y, x0, x1 in rules if x0 <= left + 2 and x1 >= right - 2)
    chain, bands = [table], []  # the table fragments joined so far, (top, bottom, whether it is an open band)
    top, bottom = table.bbox[1], table.bbox[3]

    # continue the bands downward (+1) and upward (-1)
    for direction in (1, -1):
        edge = bottom if direction > 0 else top
        while True:
            nearby = [(fragment.bbox[1] if direction > 0 else fragment.bbox[3], fragment) for fragment in fragments
                      if not any(fragment is other for other in chain)]
            nearby = [(y, fragment) for y, fragment in nearby if -1 <= (y - edge) * direction <= reach]
            nearby += [(y, None) for y in spanning if 1 < (y - edge) * direction <= reach]
            if not nearby:
                band = (edge, edge + row_height) if direction > 0 else (edge - row_height, edge)
                if _band_cells(page, band, edges) is not None and _ends_the_text(page, band, direction, left, right, row_height):
                    bands.append((*band, True))  # added to the region only when accepted (below)
                elif direction < 0 and _band_cells(page, band, edges) is not None:
                    bands += _unshaded_head(page, band, edges, row_height)
                break
            y, fragment = min(nearby, key=lambda item: (item[0] - edge) * direction)
            if (y - edge) * direction > 1:
                band = (edge, y) if direction > 0 else (y, edge)
                if _band_cells(page, band, edges) is None:
                    break
                bands.append((*band, False))
            if fragment is None:
                edge = y
            else:
                chain.append(fragment)
                edge = fragment.bbox[3] if direction > 0 else fragment.bbox[1]
        if direction > 0:
            bottom = edge
        else:
            top = edge

    # rows with every cell present are the reference for column alignment; rows with merged cells are, like the bands,
    # candidates to split again
    columns = len(edges) - 1
    references, merged = [], []  # cell text of the reference rows, (band, cell text, whether it is an open band)
    for fragment in chain:
        for row in fragment.rows:
            band = (row.bbox[1], row.bbox[3])
            cells = _band_cells(page, band, edges)
            if len(row.cells) == columns and all(cell is not None for cell in row.cells):
                references.append(cells)
            elif cells is not None:
                merged.append((band, cells, False))
    between = [((band_top, band_bottom), _band_cells(page, (band_top, band_bottom), edges), is_open)
               for band_top, band_bottom, is_open in bands]
    candidates = [*merged, *between]

    def aligned(entry: tuple) -> bool:
        return _aligned_cells(entry[1], [*references, *(other[1] for other in candidates if other is not entry)])

    # when the topmost closed band is header-shaped it is accepted without column alignment — a left-aligned header does not
    # match right-aligned number columns
    heading = next((entry for entry in between if not entry[2] and abs(entry[0][0] - top) <= 1
                    and _is_header_band(page, entry[0], edges)), None)
    if any(not entry[2] and entry is not heading and not aligned(entry) for entry in between):
        return None  # a band between shaded fragments is not a table row
    accepted = [(entry[0], entry[2]) for entry in candidates if entry is heading or aligned(entry)]
    if not accepted:
        return None

    # draw the column-boundary vertical lines only in the accepted bands (plus the outer horizontal lines for open bands) and
    # search only that region again
    top = min([top, *(band[0] for band, is_open in accepted if is_open)])
    bottom = max([bottom, *(band[1] for band, is_open in accepted if is_open)])
    verticals = [_rule_line(x, band_top, x, band_bottom) for (band_top, band_bottom), _ in accepted for x in edges]
    horizontals = [_rule_line(left, y, right, y) for (band_top, band_bottom), is_open in accepted if is_open
                   for y in (band_top, band_bottom)]
    page_left, page_top, page_right, page_bottom = page.bbox
    box = (max(left - 1, page_left), max(top - 1, page_top), min(right + 1, page_right), min(bottom + 1, page_bottom))
    if box[0] >= box[2] or box[1] >= box[3]:
        return None
    found = page.crop(box).find_tables({**_TABLE_SETTINGS, "explicit_vertical_lines": verticals,
                                        "explicit_horizontal_lines": horizontals})
    if len(found) != 1 or len(_column_edges(found[0])) != len(edges):
        return None
    return found[0], chain


def _unshaded_head(page: object, band: tuple[float, float], edges: list[float],
                   row_height: float) -> list[tuple[float, float, bool]]:
    """The band list when the unshaded row (`band`) above the table's topmost shaded fragment and the header band above it
    are accepted together as open bands. Otherwise [].

    Only one open band was accepted above and one below, so in a striped table whose header and first data row are both
    unshaded (Chrome `tbody tr:nth-child(even)`) the header continued above the first row at line spacing and the first row
    was not accepted. The header scattered into '번호 지점 / 금액 / 상태' and the first row became a jumbled paragraph,
    worse than with no shading at all (pinned by: test_pdf_round44.py::TestZebraTableUnderAnUnshadedHeaderAndFirstRow).
    Accepted only when the band above is header-shaped (`_is_header_band`) and the text above it is set apart."""
    head = (band[0] - row_height, band[0])
    if not _is_header_band(page, head, edges) or not _ends_the_text(page, head, -1, edges[0], edges[-1], row_height):
        return []
    return [(*band, True), (*head, True)]


def _ends_the_text(page: object, band: tuple[float, float], direction: int, left: float, right: float, row_height: float) -> bool:
    """Whether, beyond the text of the open band at the end of a table (`band`, outward direction `direction`), the next text
    within the table width is more than half a row height away. Text that continues past the band at the same line spacing
    (a list under a shaded title box) is not the table's last row."""
    chars = [char for char in page.chars if char["text"].strip() and char["x1"] > left and char["x0"] < right]
    inside = [char for char in chars if band[0] < (char["top"] + char["bottom"]) / 2 < band[1]]
    if direction > 0:
        end = max(char["bottom"] for char in inside)
        return all(char["top"] - end >= row_height / 2 for char in chars if char["top"] >= band[1] - 1)
    end = min(char["top"] for char in inside)
    return all(end - char["bottom"] >= row_height / 2 for char in chars if char["bottom"] <= band[0] + 1)


def _same_column_edges(edges: list[float], others: list[float]) -> bool:
    return len(edges) == len(others) and all(abs(edge - other) <= 1 for edge, other in zip(edges, others, strict=True))


def _rule_line(x0: float, top: float, x1: float, bottom: float) -> dict:
    """A line to add to table finding (a line object for pdfplumber `explicit_*_lines`)."""
    return {"object_type": "line", "x0": x0, "x1": x1, "top": top, "bottom": bottom, "width": x1 - x0, "height": bottom - top}


def _band_cells(page: object, band: tuple[float, float], edges: list[float]) -> list[tuple[float, float] | None] | None:
    """(left end, right end) per column of the text in a horizontal band (`band` = top, bottom) split by the column boundaries
    (`edges`). Only when there is text in two or more columns, all the text lies inside the band and the table width, and no
    line crosses a column boundary at word spacing; otherwise None (`_band_chars`)."""
    cells = _band_chars(page, band, edges)
    if cells is None:
        return None
    return [(min(char["x0"] for char in cell), max(char["x1"] for char in cell)) if cell else None for cell in cells]


def _is_header_band(page: object, band: tuple[float, float], edges: list[float]) -> bool:
    """Whether the band (`band`) is shaped like a header row: text in every column and not a single number cell."""
    cells = _band_chars(page, band, edges)
    if cells is None or not all(cells):
        return False
    texts = ("".join(char["text"] for char in sorted(cell, key=lambda char: char["x0"])) for cell in cells)
    return not any(_NUMBER_CELL.match(text) for text in texts)


def _band_chars(page: object, band: tuple[float, float], edges: list[float]) -> list[list[dict]] | None:
    """The characters per column for `_band_cells` (the column containing each character's centre). None unless the same
    conditions hold.

    A character that reaches into the band by less than `_BAND_SLACK_RATIO` of its height belongs to the neighbouring row and
    is not counted — the character boxes of a Word number column touched the band of the row below by 0.06pt, that band was
    rejected as 'text sticking out of the band', and the unshaded rows merged into one cell (pinned by:
    test_pdf_shaded_tables.py::TestShadedTables::test_numbers_that_barely_touch_the_next_row_do_not_merge_its_cells).
    """
    top, bottom = band
    left, right = edges[0], edges[-1]
    tops, ordered, tallest = _chars_by_top(page)
    chars = [char for char in ordered[bisect_left(tops, top - tallest):bisect_left(tops, bottom)]
             if char["x1"] > left and char["x0"] < right
             and min(char["bottom"], bottom) - max(char["top"], top) >= (char["bottom"] - char["top"]) * _BAND_SLACK_RATIO]
    for char in chars:
        slack = (char["bottom"] - char["top"]) * _BAND_SLACK_RATIO
        if (char["top"] < top - slack or char["bottom"] > bottom + slack or char["x0"] < left - 1 or char["x1"] > right + 1):
            return None

    cells: list[list[dict]] = [[] for _ in edges[1:]]  # characters per column (the column containing the centre)

    for char in chars:
        middle = (char["x0"] + char["x1"]) / 2
        cells[max(0, min(bisect_right(edges, middle) - 1, len(cells) - 1))].append(char)

    # when the text of two neighbouring columns on the same line sits at word spacing on either side of a column boundary, the
    # text crosses that boundary
    for index in range(1, len(cells)):
        for char in cells[index - 1]:
            size = char.get("size") or (char["bottom"] - char["top"])
            if any(abs(other["top"] - char["top"]) <= size / 2 and other["x0"] - char["x1"] < size * _CELL_GAP_MIN_RATIO
                   for other in cells[index]):
                return None
    if sum(1 for cell in cells if cell) < 2:
        return None
    return cells


def _chars_by_top(page: object) -> tuple[list[float], list[dict], float]:
    """The page's non-blank characters in order of their top edge, as (list of top edges, list of characters, tallest character
    height). Built once per page (`_band_chars`).

    Scanning every character of the page for every band took 48 seconds on a large page with many shaded bands and tables
    (1500pt square, 50,000 characters, 196 tables) (pinned by:
    test_pdf_shaded_tables.py::test_many_bands_on_a_page_with_many_letters_take_linear_time).
    """
    index = page.__dict__.get("_chars_by_top")
    if index is None:
        ordered = sorted((char for char in page.chars if char["text"].strip()), key=lambda char: char["top"])
        index = ([char["top"] for char in ordered], ordered, max((char["bottom"] - char["top"] for char in ordered), default=0.0))
        page.__dict__["_chars_by_top"] = index
    return index


def _aligned_cells(cells: list[tuple[float, float] | None] | None,
                   others: list[list[tuple[float, float] | None] | None]) -> bool:
    """Whether, in two or more columns, the band's cell text (`cells`) matches the same column's text on the other rows
    (`others`) at the left end, right end or centre."""
    if cells is None:
        return False
    matched = 0

    for column, cell in enumerate(cells):
        if cell is None:
            continue
        if any(other is not None and other[column] is not None
               and min(abs(other[column][0] - cell[0]), abs(other[column][1] - cell[1]),
                       abs(sum(other[column]) - sum(cell)) / 2) <= _TABLE_ROW_TOLERANCE
               for other in others):
            matched += 1
    return matched >= 2


def _is_inner_part(part: tuple[float, ...], closed: tuple[float, ...]) -> bool:
    """Whether `part`, found as an open-sided table, is an inner part of `closed`, a table found from rules alone: same height
    and sharing exactly one of the left and right ends.

    When the first column of a table closed on all sides is vertically merged and some row merges columns 1-2 horizontally
    (a header '구분' spanning two columns, a '합계' (total) row), the vertical line between columns 1 and 2 does not run the
    full height of the table, so the horizontal lines that stop short of the merged cell look like a 'table open on the
    left' to `_open_sided_tables`. If the partial table found that way from column 2 onward is kept, the whole table that
    encloses it is discarded as a layout box in `_real_tables` and the category column came out as lines outside the table
    (pinned by: test_pdf.py::TestRuledTables::
    test_closed_table_with_merged_first_column_under_a_two_column_header_is_kept_whole).
    The condition is narrowed to match how `_open_sided_tables` filters regions against each other (deliberate): tables
    that share no end or share both ends, and tables of a different height (a table placed inside a box), are left as
    before — an outer box enclosing an inner table may be layout.
    """
    same_height = abs(closed[1] - part[1]) <= 2 and abs(closed[3] - part[3]) <= 2
    return same_height and _encloses(closed, part) and (abs(closed[0] - part[0]) <= 2) != (abs(closed[2] - part[2]) <= 2)


def _cell_rows(table: object) -> list[list[str | None]]:
    """The cell text of a table. The value of a vertically merged cell is repeated on the rows below too (same rule as
    markdown.TableGrid: repeat vertically, first cell only horizontally).

    pdfplumber gives None for positions hidden by a merge. A cell box spanning several rows is a vertical merge, so the None
    values on the lower rows of that column are filled with the first row's value. None values produced by a horizontal merge
    are left as they are (header merging uses them)
    (pinned by: test_quality.py::test_pdf_table_with_dotted_cell_borders_keeps_rows_and_repeats_merged_cell).

    A cell enclosing two or more other cells is not a merge but a cell holding an inner table (the inner table touches the
    outer border and is caught as one table). Only the text outside the inner cells is kept in that cell and it is not
    repeated, and the first row of such a cell is dropped when it has no such text and holds only values repeated on the row
    below (deliberate). The whole text of the inner table was repeated on every inner row and tripled the character count of
    a LibreOffice résumé (pinned by: test_pdf_nested_tables.py::TestNestedTables::
    test_inner_table_caught_as_cells_of_the_outer_table_is_not_repeated).

    The middle of a row or column is measured not from `row.bbox`/`column.bbox` but from the band where all the cells of
    that row or column overlap (the latest-starting cell to the earliest-ending cell) (deliberate). The box swells by the
    merged cell (a first row containing a cell that spans three rows, a first column containing a '합계' (total) cell that
    spans two columns), its middle moves into another row or column, and the repetition was lost in tables with both
    vertical and horizontal merges (pinned by: test_pdf.py::TestRuledTables::
    test_vertical_merge_is_repeated_in_every_row_when_another_row_merges_columns).
    """
    rows = table.extract(**_CELL_TEXT)
    _read_sideways_cells(table, rows)
    _join_soft_wraps(table, rows)
    row_middles = [_shared_middle(row.cells, 1, 3) for row in table.rows]
    column_middles = [_shared_middle(column.cells, 0, 2) for column in table.columns]
    containers: list[int] = []  # rows where a cell holding an inner table starts

    for cell in table.cells:
        x0, top, x1, bottom = cell
        spanned = [index for index, middle in enumerate(row_middles) if top <= middle <= bottom]
        column = next((index for index, middle in enumerate(column_middles) if x0 <= middle <= x1), None)
        if len(spanned) < 2 or column is None or column >= len(rows[spanned[0]]) or not rows[spanned[0]][column]:
            continue
        inner = [other for other in table.cells if other != cell and _encloses(cell, other)]
        if len(inner) >= 2:
            rows[spanned[0]][column] = _text_outside(table.page.chars, cell, inner)
            containers.append(spanned[0])
            continue
        for index in spanned[1:]:
            if rows[index][column] is None:
                rows[index][column] = rows[spanned[0]][column]

    return [row for index, row in enumerate(rows) if index not in containers or not _repeated_below(rows, index)]


def _text_outside(chars: list[dict], cell: tuple, inner: list[tuple]) -> str:
    """The text inside cell `cell` that lies outside the inner cells (`inner`) (a caption above the inner table, etc.). Each
    line is joined left to right."""
    outside = [char for char in chars if _inside_any(char, [cell]) and not _inside_any(char, inner)]
    lines: list[list[dict]] = []

    for char in sorted(outside, key=lambda char: (char["top"], char["x0"])):
        if lines and abs(char["top"] - lines[-1][0]["top"]) <= _TABLE_ROW_TOLERANCE:
            lines[-1].append(char)
        else:
            lines.append([char])

    return "\n".join(text for text in (_text_from_chars(line) for line in lines) if text)


def _repeated_below(rows: list[list[str | None]], index: int) -> bool:
    """Whether every text cell of row `index` is repeated in the same column of the row right below (a row holding only values
    repeated by a vertical merge)."""
    if index + 1 >= len(rows):
        return False
    return all(not cell or (column < len(rows[index + 1]) and rows[index + 1][column] == cell)
               for column, cell in enumerate(rows[index]))


def _shared_middle(cells: list[tuple | None], low: int, high: int) -> float:
    """The middle of the band where all the cells of one row (or column) overlap. `low` and `high` are the positions of the
    start and end coordinates in a cell box."""
    cells = [cell for cell in cells if cell is not None]
    return (max(cell[low] for cell in cells) + min(cell[high] for cell in cells)) / 2


def _join_soft_wraps(table: object, rows: list[list[str | None]]) -> None:
    """Join the lines in ruled-table cell text that wrapped because of the cell width (modifies `rows` in place).

    A `<br>` went in for every line of cell text, so one cell of an old-versus-new provisions comparison table was broken at
    every visual line, like '…임직원이 업무를 수<br>행함에…' (pinned by:
    test_pdf_cell_wraps.py::TestSoftWrappedLinesInRuledCells). A wrapped line is one where the cell has no room left for the
    first word of the next line (`_soft_wrapped`), and it is joined with a space. A cell with a single Hangul or Hanja
    character on every line ('인/적/사/항') is a vertically stacked word and is joined without spaces.
    Deliberate: a generator that wraps Korean per character (breaking in the middle of a word) is also joined with a space —
    there is no telling which kind it is, in that case '수 행함에' is broken exactly as much as the former '수<br>행함에',
    and with the word-level wrapping that is the default in Word and Hangul (HWP) the result is the original text. Cells
    containing a picture (the picture's position is decided from the line height of the cell text,
    `_place_images_in_cells`), cells containing rotated characters, and cells where the lines regrouped from characters do
    not match the lines of the cell text are left untouched.
    """
    images = [(image["x0"], image["top"], image["x1"], image["bottom"]) for image in table.page.images]
    bbox = table.bbox  # a property recomputed from all the cell boxes, so calling it per character is quadratic time
    chars = sorted((char for char in table.page.chars if char["text"].strip() and _inside_any(char, [bbox])),
                   key=lambda char: (char["top"] + char["bottom"]) / 2)
    middles = [(char["top"] + char["bottom"]) / 2 for char in chars]  # per cell, bisect to only the characters in its vertical range

    for row_index, row in enumerate(table.rows):
        for column_index, cell in enumerate(row.cells):
            text = rows[row_index][column_index] if column_index < len(rows[row_index]) else None
            if cell is None or not text or "\n" not in text or any(_overlaps(cell, image) for image in images):
                continue
            band = chars[bisect_left(middles, cell[1]):bisect_right(middles, cell[3])]
            lines = _char_lines([char for char in band if _inside_any(char, [cell])])
            parts = text.split("\n")
            if any(_is_sideways(char) for line in lines for char in line) or len(lines) != len(parts) or any(
                    _text_from_chars(line).replace(" ", "") != part.replace(" ", "") for line, part in zip(lines, parts, strict=True)):
                continue
            rows[row_index][column_index] = _joined_cell_lines(cell, lines, parts)


def _char_lines(chars: list[dict]) -> list[list[dict]]:
    """Group characters into lines whose top coordinates are within `_TABLE_ROW_TOLERANCE` (top to bottom, left to right
    inside a line)."""
    lines: list[list[dict]] = []

    for char in sorted(chars, key=lambda char: (char["top"], char["x0"])):
        if lines and abs(char["top"] - lines[-1][0]["top"]) <= _TABLE_ROW_TOLERANCE:
            lines[-1].append(char)
        else:
            lines.append([char])
    return [sorted(line, key=lambda char: char["x0"]) for line in lines]


def _joined_cell_lines(cell: tuple, lines: list[list[dict]], parts: list[str]) -> str:
    """Join the lines of a cell (`cell`; character groups `lines`, text `parts`) with a space for wrapped lines and a line
    break for real line breaks (`_join_soft_wraps`)."""
    if len(parts) > 1 and all(len(part.strip()) == 1 and _SPACED_CHAR.match(part.strip()) for part in parts):
        return "".join(part.strip() for part in parts)

    # Only left-aligned cells are considered (a first-line indent is fine). The lines of a centred cell
    # ('위와 같이 신청합니다. / 2024년 7월 25일 / 신청인') are real line breaks even though, measured against the widest line,
    # there seems to be no room left.
    starts = [min(char["x0"] for char in line) for line in lines]
    if max(starts[1:]) - min(starts[1:]) > _TABLE_ROW_TOLERANCE or starts[0] < min(starts[1:]) - _TABLE_ROW_TOLERANCE:
        return "\n".join(parts)
    margin = max(0.0, min(starts) - cell[0])  # assume the same cell padding on the right as on the left
    right = cell[2] - margin
    text = parts[0]

    for line, following, part in zip(lines, lines[1:], parts[1:], strict=False):
        text += (" " if _soft_wrapped(line, following, right) else "\n") + part
    return text


def _soft_wrapped(line: list[dict], following: list[dict], right: float) -> bool:
    """Whether line `line` of cell text wrapped because of the cell width: the next line does not start with a list item
    (`_BARE_LIST_NUMBER`, `_BULLET_START`), has the same font size, and its first word (plus a space) does not fit between the
    end of this line and the right end of the cell (`right`)."""
    size = line[-1]["size"]
    head = _text_from_chars(following)
    if abs(following[0]["size"] - size) > 0.5 or _BULLET_START.match(head) or _BARE_LIST_NUMBER.match(head):
        return False
    word: list[dict] = []

    for char in following:
        if word and char["x0"] - word[-1]["x1"] > _LAPARAMS["word_margin"] * max(char["width"], char["height"]):
            break
        word.append(char)
    return right - line[-1]["x1"] < word[-1]["x1"] - word[0]["x0"] + size * 0.5


def _overlaps(box: tuple, other: tuple) -> bool:
    """Whether two boxes (x0, top, x1, bottom) overlap."""
    return box[0] < other[2] and other[0] < box[2] and box[1] < other[3] and other[1] < box[3]


def _read_sideways_cells(table: object, rows: list[list[str | None]]) -> None:
    """Replace the text of cells whose characters are all rotated 90 degrees with the text read in the rotated direction
    (modifies `rows` in place).

    A category name written sideways in a vertically merged cell ('기계 설비') was read top to bottom like horizontal text and
    became '비설<br>계기' (pinned by:
    test_pdf.py::TestRuledTables::test_text_rotated_inside_a_cell_is_read_in_its_own_direction). A cell with even one
    horizontal character mixed in (the axis title and ticks of a chart caught as a table) is left as is — there is no way to
    decide in what order to join text of two directions in one cell.
    """
    sideways = [char for char in table.page.chars if char["text"].strip() and _is_sideways(char)]
    if not sideways:
        return

    for row_index, row in enumerate(table.rows):
        for column_index, cell in enumerate(row.cells):
            if cell is None or not any(_inside_any(char, [cell]) for char in sideways):
                continue
            chars = [char for char in table.page.chars if _inside_any(char, [cell])]
            if all(_is_sideways(char) for char in chars if char["text"].strip()):
                rows[row_index][column_index] = "\n".join(_sideways_paragraphs(chars))


def _open_sided_tables(page: object) -> list[object]:
    """Find tables without left and right borders again at their full width.

    A shape common in Korean reports: horizontal lines are drawn across the full table width and vertical lines only between
    the inner columns. Finding cells from the rules made only the columns between vertical lines a table, and the first and
    last columns scattered outside it. When there are three or more horizontal lines of the same width with vertical lines
    inside them but no vertical line at the ends of the horizontal lines, only that region is cropped and the table is
    searched for with both ends of the horizontal lines given as vertical borders
    (pinned by: test_quality.py::test_pdf_table_without_outer_borders_keeps_its_first_and_last_columns).

    When the first (or last) column is vertically merged there are two kinds of horizontal-line width (the full width, and
    the width that stops short of the merged cell), so a region also arises from the narrow width. The widest region is
    searched first, and a region inside a region where a table was found that shares only one end with it is part of the same
    table and is skipped. Searching it separately made the whole table that encloses the partial table get discarded as a
    layout box in `_real_tables`, and the category column went outside the table (the horizontal counterpart of
    `_outermost_spans`. Pinned by:
    test_pdf.py::TestRuledTables::test_open_sided_table_with_vertically_merged_first_column_is_kept_whole).
    Other overlapping regions (sharing no end, or sharing both ends) are searched separately as before and filtered by
    `_real_tables` — the lines of a chart drawing became a large table and repeated the tick labels on every row (public
    paper PDF).
    """
    # Pages with many vector shapes (graphs in a paper) have tens of thousands of lines. Rescanning all of them for every line
    # takes seconds per page (600 million comparisons on a 27-page paper), so vertical lines are sorted by x and horizontal
    # lines by height and only the needed range is examined.
    edges = [edge for edge in page.edges if not _is_invisible_edge(edge)]
    verticals = _chained_verticals([edge for edge in edges if edge["orientation"] != "h"])
    positions = [edge["x0"] for edge in verticals]

    def verticals_between(low: float, high: float) -> list[dict]:
        return verticals[bisect_left(positions, low):bisect_right(positions, high)]

    def at_column_border(x: float, top: float) -> bool:
        return any(edge["top"] - 1 <= top <= edge["bottom"] + 1 for edge in verticals_between(x - 1, x + 1))

    horizontals: dict[tuple[int, int], list[float]] = {}
    for left, right, top in _joined_rules([edge for edge in edges if edge["orientation"] == "h"], at_column_border):
        horizontals.setdefault((round(left), round(right)), []).append(top)

    page_left, page_top, page_right, page_bottom = page.bbox
    areas = []  # (region to crop, left end, right end)
    for (left, right), rule_tops in horizontals.items():
        if len(rule_tops) < 3 or right - left < _MIN_FIGURE_POINTS:
            continue
        rule_tops.sort()
        sides = [verticals_between(side - 2, side + 2) for side in (left, right)]
        # The vertical range of the inner vertical lines is the table's height. When several tables of the same width are
        # stacked, each range is examined separately.
        spans = {(round(edge["top"]), round(edge["bottom"])) for edge in verticals_between(left + 2, right - 2)
                 if left + 2 < edge["x0"] < right - 2}
        for top, bottom in _outermost_spans(spans):
            rules = bisect_right(rule_tops, bottom + 2) - bisect_left(rule_tops, top - 2)
            if rules < 3:
                continue
            closed = [side for side in sides
                      if any(edge["top"] <= top + 2 and edge["bottom"] >= bottom - 2 for edge in side)]
            if len(closed) == 2:
                continue
            box = (max(left - 1, page_left), max(top - 1, page_top), min(right + 1, page_right), min(bottom + 1, page_bottom))
            if box[0] >= box[2] or box[1] >= box[3]:
                continue  # lines drawn off the page
            areas.append((box, left, right))

    # region number → the tables found in that region. The widest region is searched first, but they are returned in region order
    found: dict[int, list[object]] = {}
    for index in sorted(range(len(areas)), key=lambda index: -_box_area(areas[index][0])):
        box, left, right = areas[index]
        outers = [areas[other][0] for other, tables in found.items() if tables]
        if any(_encloses(outer, box) and (abs(outer[0] - box[0]) <= 2) != (abs(outer[2] - box[2]) <= 2) for outer in outers):
            continue
        found[index] = page.crop(box).find_tables({**_TABLE_SETTINGS, "explicit_vertical_lines": [left, right]})

    # Horizontal lines whose ends are off by about 1pt fall into two width groups, so the same table is found in two regions.
    # Keeping both made `_real_tables` see each as 'a box enclosing another table' and discard both, and a two-column
    # definition table ('총배출량 | 순배출량', a design-tool guidebook) scattered line by line (pinned by:
    # test_pdf.py::TestRuledTables::test_open_sided_table_found_from_two_rule_widths_is_kept_once)
    tables: list[object] = []
    for table in (table for index in sorted(found) for table in found[index]):
        if not any(all(abs(a - b) <= 2 for a, b in zip(table.bbox, kept.bbox, strict=True)) for kept in tables):
            tables.append(table)
    return tables


def _box_area(box: tuple[float, float, float, float]) -> float:
    return (box[2] - box[0]) * (box[3] - box[1])


def _is_invisible_edge(edge: dict) -> bool:
    """Whether this is a side of a rectangle that is only filled white and has no border. It is invisible on white paper and so
    is not a table's outer border.

    In an open-sided table with a white filled rectangle under every header cell, the outer sides of
    those rectangles looked like vertical lines closing the left and right on the header row, so the full-width horizontal
    lines were not joined and the first column (vertically merged '가격 기반') and the last column ('적용순위') scattered
    outside the table
    (pinned by: test_pdf.py::TestRuledTables::test_open_sided_table_with_white_filled_header_cells_keeps_its_outer_columns).
    Known accepted gap: the sides of white cells laid over a filled background are dropped even though they are visible —
    only `_open_sided_tables` uses this test, and ruled-table finding uses those sides as before.
    """
    return (edge.get("object_type") == "rect_edge" and not edge.get("stroke") and bool(edge.get("fill"))
            and _is_white(edge.get("non_stroking_color")))


def _chained_verticals(verticals: list[dict]) -> list[dict]:
    """The vertical line pieces at the same x joined into one line where they overlap or are broken by no more than
    `_VERTICAL_JOIN_GAP` (in x order).

    A generator that draws the vertical lines separately per cell breaks them by 0.25pt at each row boundary. Measuring the
    vertical range per piece made the table height look like a single row, no range held three or more horizontal lines, and
    the open-sided table was not found (pinned by: test_pdf.py::TestRuledTables::
    test_open_sided_table_whose_column_rules_are_drawn_row_by_row_keeps_its_outer_columns). Keeping pieces drawn on top of
    each other at the same place (a header cell border and a column separator) apart produced overlapping candidate regions
    and a legal-register table scattered into per-cell paragraphs. Known accepted gap: the lifelines of a sequence diagram
    drawn twice like a shadow also become one line, so a drawing that used to cancel itself out as two overlapping
    candidates remains as a small fake table.
    """
    chained: list[dict] = []

    for edge in sorted(verticals, key=lambda edge: (round(edge["x0"], 1), edge["top"])):
        last = chained[-1] if chained else None
        if (last is not None and round(last["x0"], 1) == round(edge["x0"], 1)
                and edge["top"] - last["bottom"] <= _VERTICAL_JOIN_GAP):
            last["bottom"] = max(last["bottom"], edge["bottom"])
        else:
            chained.append({"x0": edge["x0"], "top": edge["top"], "bottom": edge["bottom"]})

    chained.sort(key=lambda edge: edge["x0"])
    return chained


def _joined_rules(edges: list[dict], at_column_border: Callable[[float, float], bool]) -> list[tuple[float, float, float]]:
    """(x0, x1, top) of the horizontal lines. The horizontal lines of an open-sided table drawn broken per cell are returned
    with their pieces joined into one line.

    Some generators draw a table's horizontal lines broken per cell. Grouping widths piece by piece left an open-sided table
    with not a single full-width horizontal line, and the first and last columns stayed outside the table (pinned by:
    test_pdf.py::TestRuledTables::
    test_open_sided_table_whose_rules_are_drawn_cell_by_cell_keeps_its_outer_columns). The joining conditions are kept narrow.
    - At the same height the end of the previous piece and the start of the next piece touch (a gap of at most
      `_RULE_JOIN_GAP`, an overlap of at most `_RULE_JOIN_OVERLAP`), and there is a vertical line (a column boundary) at that
      place. Joining dotted lines (0.4pt pieces, 0.45pt gaps) turned dotted cell boundaries into full-width horizontal lines
      and split the table
      (pinned by: test_quality.py::test_pdf_table_with_dotted_cell_borders_keeps_rows_and_repeats_merged_cell).
    - There is no vertical line at either end of the joined line (the sides are open). When there is a vertical line at an
      end the pieces are left as they are — the borders of chart panels placed side by side were joined into one table and
      even the figure captions ended up in cells (public paper PDF).
    """
    rules: list[list[dict]] = []  # joined pieces (left to right)
    # (height, end x) → (end x, order, line) of the lines that ended there, in end-x order. When looking for a touching line,
    # only the range of end x that can be joined is examined — scanning the list from the start took 457 seconds for 45,000
    # lines stacked at the same place (a 2KB PDF) (pinned by: test_pdf_resource_limits.py::TestStackedRules).
    ends: dict[tuple[float, int], list[tuple[float, int, list[dict]]]] = {}
    for order, edge in enumerate(sorted(edges, key=lambda edge: edge["x0"])):
        level, start = round(edge["top"], 1), round(edge["x0"])
        rule = None
        for x in (start, start + 1, start - 1):
            ended = ends.get((level, x), [])
            at = bisect_left(ended, (edge["x0"] - _RULE_JOIN_GAP,))
            if at < len(ended) and ended[at][0] <= edge["x0"] + _RULE_JOIN_OVERLAP and ended[at][0] < edge["x1"]:
                if at_column_border(edge["x0"], edge["top"]):
                    rule = ended.pop(at)[2]
                break
        if rule is None:
            rule = [edge]
            rules.append(rule)
        else:
            rule.append(edge)
        insort(ends.setdefault((level, round(edge["x1"])), []), (edge["x1"], order, rule))

    joined = []
    for rule in rules:
        first, last = rule[0], rule[-1]
        if len(rule) > 1 and not at_column_border(first["x0"], first["top"]) and not at_column_border(last["x1"], last["top"]):
            joined.append((first["x0"], last["x1"], first["top"]))
        else:
            joined.extend((edge["x0"], edge["x1"], edge["top"]) for edge in rule)
    return joined


def _outermost_spans(spans: set[tuple[int, int]]) -> list[tuple[int, int]]:
    """Drop vertical ranges that lie inside another range. A contained range is part of the same table (the '상반기|하반기' line
    of a two-level header exists only on the lower row).

    Searching it separately produced a lower fragment table, the whole table enclosing the fragment was discarded as a layout
    box in `_real_tables`, and '구분' and '2025년' scattered outside the table
    (pinned by: test_quality.py::test_pdf_open_sided_table_with_two_level_header_is_kept_whole).
    """
    outermost: list[tuple[int, int]] = []
    for span in sorted(spans, key=lambda span: span[0] - span[1]):  # longest range first
        if not any(other[1] - other[0] > span[1] - span[0] and other[0] - 2 <= span[0] and span[1] <= other[1] + 2
                   for other in outermost):
            outermost.append(span)
    return outermost


def _merge_split_header(rows: list[list[str | None]]) -> list[list[str | None]]:
    """Merge a two-level header ('1월·2월' under '문의') into one row, '문의 1월 | 문의 2월'.

    pdfplumber gives None for cells that disappeared in a merge and "" for truly empty cells. The rows are treated as a
    header only when the first row has a horizontal merge (None), the second row below it has text, and both rows have two
    or more filled cells (a single-cell title row is handled separately by render_table).
    When an unmerged column has text of its own in the second row, that is data, not a sub-header. The first data row of a
    one-level header ('연락처' spanning two columns) was attached to the header and became '이름 홍길동'
    (pinned by: test_quality.py::test_pdf_one_level_header_spanning_two_columns_keeps_the_first_data_row).
    When the second row has only one cell under a merged cell (`_undivided_span`), it is the next row, not a sub-header.
    """
    if len(rows) < 3:
        return rows
    first, second = rows[0], rows[1]
    spanned = [index for index, cell in enumerate(first) if cell is None and index > 0]
    # the merged cells and the cell where the merge starts (a horizontal merge is a run of None)
    covered = {*spanned, *(index - 1 for index in spanned)}
    if (not spanned or sum(1 for cell in first if cell) < 2 or sum(1 for cell in second if cell) < 2
            or not any(index < len(second) and second[index] for index in spanned)
            or any(second[index] and second[index] != first[index]
                   for index in range(min(len(first), len(second))) if index not in covered)
            or _undivided_span(first, second)):
        return rows

    filled, last = [], ""
    for cell in first:
        last = cell if cell is not None else last
        filled.append(last or "")
    merged = [" ".join(dict.fromkeys(part.strip() for part in (filled[index], second[index] or "") if part and part.strip()))
              for index in range(min(len(first), len(second)))]
    return [merged, *rows[2:]]


def _undivided_span(first: list[str | None], second: list[str | None]) -> bool:
    """Whether, among the horizontally merged cells of the first row, there is one under which the second row has only a
    single (undivided) cell whose text differs from the merged cell.

    A two-level header ('1월·2월' under '문의') divides into two or more cells under the merged cell. In a form grid, the
    vertical lines of the item rows below make the '등록번호' cell look like a merge spanning two grid columns, and the
    '상호' cell below it spans the same two columns — merging it as a sub-header gave
    '등록번호 상호 | 123-45-67890 (주)라마바' (pinned by: test_pdf_form_grids.py::TestFormGridHeader).
    """
    width = min(len(first), len(second))
    for start in range(width):
        end = start + 1
        while end < width and first[end] is None:
            end += 1
        if first[start] is None or end - start < 2:
            continue
        below = [cell for cell in second[start:end] if cell and cell.strip() and cell != first[start]]
        if len(below) == 1:
            return True
    return False


def _encloses(outer: tuple[float, ...], inner: tuple[float, ...]) -> bool:
    return (outer[0] - 1 <= inner[0] and outer[1] - 1 <= inner[1]
            and inner[2] <= outer[2] + 1 and inner[3] <= outer[3] + 1)


def _render_page(page: _Page, heading_levels: dict[float, int], line_gap: float,
                 images: list[tuple[tuple[float, ...], str]] | None = None) -> str:
    """Join the page's paragraphs, tables and pictures in reading order. `images` is a list of ((top, x0, x1, bottom),
    placeholder)."""
    items: list[tuple[float, str, bool]] = []
    spans: list[tuple[float, float]] = []  # horizontal ranges (x0, x1) in the same order as items
    sizes: list[tuple[float, int]] = []  # (bottom coordinate, line count) in the same order as items. Tables and pictures are (bottom, 0)
    boxes: list[tuple[float, float, float, float]] = []  # (top, bottom, x0, x1) of paragraphs. Tables and pictures are not included
    for paragraph in _group_paragraphs(page.lines, line_gap):
        level = heading_levels.get(_heading_size(paragraph), 0) if _is_heading(paragraph, heading_levels) else 0
        text = f"{'#' * level} {' '.join(line.text for line in paragraph.lines)}" if level else _paragraph_text(paragraph)
        items.append((paragraph.top, text, False))
        spans.append((min(line.x0 for line in paragraph.lines), max(line.x1 for line in paragraph.lines)))
        sizes.append((paragraph.lines[-1].bottom, len(paragraph.lines)))
        boxes.append((paragraph.top, paragraph.lines[-1].bottom, *spans[-1]))

    # The flow order is kept, and a table or picture is inserted before the first paragraph that starts below it. When that
    # paragraph is a label placed to the left of the table or picture, it is inserted after it. A picture inside a ruled-table
    # cell is put in that cell.
    images = _place_images_in_cells(page.tables, images or [])
    blocks = [((table.top, table.x0, table.x1, table.bottom), table.text) for table in page.tables] + images
    follows = _picture_rows(images)
    # pictures joined as a row are placed left to right at the height of the row's first picture
    starts = {text: box[0] for box, text in images}

    for text in follows:
        start = text
        while start in follows:
            start = follows[start]
        starts[text] = starts[start]

    def order(block: tuple[tuple[float, ...], str]) -> tuple[float, float]:
        return (starts[block[1]], block[0][1]) if block[1] in follows else (block[0][0], -math.inf)

    for (block_top, x0, x1, block_bottom), block_text in sorted(blocks, key=order):
        position = next((index for index, (top, _, is_block) in enumerate(items)
                         if not is_block and top >= block_top), len(items))
        if block_text in follows:
            position = next(index for index, (_, text, is_block) in enumerate(items)
                            if is_block and text == follows[block_text]) + 1
        elif (position < len(items) and _in_other_column(spans[position], x0, x1, spans)
                and _columns_apart(spans[position], (block_top, block_bottom, x0, x1), boxes)):
            position = _position_in_column(items, spans, block_top, x0, x1)
        elif ((position == len(items) or not _in_other_column(spans[position], x0, x1, spans))
              and _after_other_column(items, spans, position, (block_top, block_bottom, x0, x1), boxes)):
            position = _position_in_column(items, spans, block_top, x0, x1)
        else:
            while (position < len(items) and not items[position][2]
                   and _is_side_label(items[position][0], *sizes[position], spans[position], (block_top, block_bottom, x0, x1), spans)):
                position += 1
        items.insert(position, (block_top, block_text, True))
        spans.insert(position, (x0, x1))
        sizes.insert(position, (block_bottom, 0))
    return _shortened_leaders("\n\n".join(text for text in [*(text for _, text, _ in items), *page.sideways,
                                                            "\n".join(page.form_values)] if text.strip()))


def _shortened_leaders(text: str) -> str:
    """The page text with dot leaders (`_LEADER_RUN`) shortened to a single ellipsis (' … ', without the space on that side at
    the start or end of a line).

    Deliberate: the dot leaders (tab fill) of a table of contents or price list are only a drawing that connects positions,
    yet left as they are more than 1,300 dots remained on one page and used over five times as much of the character limit
    (1,696 characters) as the DOCX of the same document (325 characters) (pinned by:
    test_pdf_toc_and_checkboxes.py::TestDotLeaderPageNumbers::test_dot_leaders_are_shortened_to_one_ellipsis). The leader
    test (`_leader_rows`) is done on the original line text, not the shortened text. Only PDF is shortened — DOCX and HWP
    draw leaders as tab fill and have no characters for them.
    """
    def shortened(match: re.Match[str]) -> str:
        before = "" if match.start() == 0 or text[match.start() - 1] == "\n" else " "
        after = "" if match.end() == len(text) or text[match.end()] == "\n" else " "
        return f"{before}…{after}"

    return _LEADER_RUN.sub(shortened, text)


def _picture_rows(images: list[tuple[tuple[float, ...], str]]) -> dict[str, str]:
    """In a row of `_PICTURE_ROW_MIN` or more pictures placed side by side at the same height (top and bottom ends within
    `_TABLE_ROW_TOLERANCE`) without overlapping horizontally: placeholder of a picture to put right after the one on its
    left → placeholder of the picture just before it (on the left).

    In a photo grid of four or more columns (product guide, product line-up) the caption under a photo is wider than half
    the photo width and looked like 'body text of another column' (`_in_other_column`) to the neighbouring photo, so photos
    and captions were mixed up arbitrarily (with 2 or 3 columns each row reads as photos → captions). Only the first picture
    of a row gets a position looked up and the rest follow it (pinned by: test_pdf_images.py::TestPictureGrid). Two pictures
    side by side may be the pictures of two columns and are left as before.
    """
    follows: dict[str, str] = {}
    pictures = sorted(images, key=lambda image: (image[0][0], image[0][1]))
    used: set[int] = set()

    for index, ((top, _, _, bottom), _) in enumerate(pictures):
        if index in used:
            continue
        row = [other for other, ((other_top, _, _, other_bottom), _) in enumerate(pictures)
               if other not in used and abs(other_top - top) <= _TABLE_ROW_TOLERANCE
               and abs(other_bottom - bottom) <= _TABLE_ROW_TOLERANCE]
        row.sort(key=lambda other: pictures[other][0][1])
        boxes = [pictures[other][0] for other in row]
        if len(row) < _PICTURE_ROW_MIN or any(right[1] < left[2] for left, right in zip(boxes, boxes[1:], strict=False)):
            continue
        used.update(row)
        follows.update((pictures[right][1], pictures[left][1]) for left, right in zip(row, row[1:], strict=False))
    return follows


def _place_images_in_cells(tables: list[_Table], images: list[tuple[tuple[float, ...], str]]) -> list[tuple[tuple[float, ...], str]]:
    """Put the placeholder of each picture lying inside one cell box of a ruled table (with `_CELL_IMAGE_SLACK` allowed)
    between the lines of that cell's text at the picture's height (`_with_pictures`) and rebuild the table text. Returns the
    list of remaining pictures. Tables whose row and column ranges could not be determined (tables with empty `bands` or
    `spans`) and borderless tables are left as they are.

    Photos inside cells (photo registers, product lists) piled up after the table with their cells left empty, so there was
    no telling which row a photo belonged to. The other formats put the placeholder inside the cell (pinned by:
    test_pdf_images.py::TestPicturesInTableCells). A picture in a vertically merged cell is put only on the first row of the
    merge.
    """
    left: list[tuple[tuple[float, ...], str]] = []
    placed: dict[tuple[int, int, int, int], list[tuple[float, str]]] = {}  # (table, row, column, cell) → [(picture top, placeholder)]

    for box, placeholder in images:
        top, x0, x1, bottom = box
        found = None
        for table_index, table in enumerate(tables):
            if not (table.bands and table.spans):
                continue
            for cell_index, cell in enumerate(table.cells):
                if not _in_cell((x0, top, x1, bottom), cell):
                    continue
                row = next((index for index, band in enumerate(table.bands) if abs(band[0] - cell[1]) <= 1.0), None)
                column = next((index for index, span in enumerate(table.spans) if abs(span[0] - cell[0]) <= 1.0), None)
                if row is not None and column is not None:
                    found = (table_index, row, column, cell_index)
                    break
            if found:
                break
        if found is None:
            left.append((box, placeholder))
            continue
        placed.setdefault(found, []).append((top, placeholder))

    for (table_index, row, column, cell_index), pictures in placed.items():
        table = tables[table_index]
        table.rows[row][column] = _with_pictures(table.rows[row][column] or "", table.cell_lines.get(cell_index, ()), pictures)

    for table_index in {key[0] for key in placed}:
        tables[table_index].text = _table_text(tables[table_index])
    return left


def _table_text(table: _Table) -> str:
    """The table text rebuilt from the table's cell text (`rows`). For a ruled table continued from the previous page, the top
    row is pulled out as a title only when it is a horizontally merged cell — in the cell text a cell covered by a merge is
    also "", so the covered columns of the top row are found from the cell boxes and passed as None (`merged_titles_only` of
    `render_table`)."""
    if not table.continued:
        return render_table(table.rows)
    rows = [list(row) for row in table.rows]

    if table.bands and table.spans and math.isfinite(table.bands[0][0]):
        top = table.bands[0][0]
        starts = [cell[0] for cell in table.cells if abs(cell[1] - top) <= 1.0]
        rows[0] = [cell if any(abs(start - span[0]) <= 1.0 for start in starts) else None
                   for cell, span in zip(rows[0], table.spans, strict=False)]
    return render_table(rows, merged_titles_only=True)


def _with_pictures(text: str, tops: tuple[float, ...], pictures: list[tuple[float, str]]) -> str:
    """Insert picture placeholders into cell text `text` between the lines at each picture's height (`tops` is the top
    coordinate of each line of the cell text). When the line counts do not match (a cell whose text was split differently)
    they go at the end of the cell text."""
    lines = text.split("\n") if text else []
    if len(lines) != len(tops):
        tops = (-math.inf,) * len(lines)

    for top, placeholder in sorted(pictures, key=lambda picture: picture[0], reverse=True):
        lines.insert(sum(1 for line_top in tops if line_top < top), placeholder)
    return "\n".join(lines)


def _is_side_label(top: float, bottom: float, lines: int, span: tuple[float, float], block: tuple[float, float, float, float],
                   spans: list[tuple[float, float]]) -> bool:
    """Whether a paragraph (top and bottom `top`/`bottom`, line count `lines`, horizontal range `span`) is a label placed to
    the left of a table or picture (`block` = top, bottom, x0, x1): at most `_SIDE_LABEL_MAX_LINES` lines, within the height
    of the table or picture, its right end before the left end of the table or picture, and not body text of another column
    (`_in_other_column`).

    Deliberate: 'AS-IS' and 'TO-BE' placed vertically centred to the left of a table start below the top of the table, were
    pushed after the table and read like the title of the next table (pinned by:
    test_pdf.py::TestBlockPlacement::test_label_beside_a_table_comes_before_it,
    test_wide_paragraph_left_of_a_table_is_not_taken_for_its_label). Text placed on the right goes after the table, in
    reading order.
    """
    block_top, block_bottom, x0, x1 = block
    return (lines <= _SIDE_LABEL_MAX_LINES and block_top - 1 <= top and bottom <= block_bottom + 1 and span[1] <= x0 + 1
            and not _in_other_column(span, x0, x1, spans))


def _in_other_column(span: tuple[float, float], x0: float, x1: float, spans: list[tuple[float, float]]) -> bool:
    """Whether a paragraph (horizontal range `span`) is body text of a different column from the table or picture (x0~x1): it
    does not overlap horizontally and is at least `_COLUMN_MIN_WIDTH_RATIO` times as wide as the table or picture. Narrow
    text placed separately beside a table (a category name, a note marker) is not another column.

    A narrow paragraph is also another column when its left end matches such other-column body text (one of `spans`). A
    short subheading of the left column ('2. 관련 연구') looked like narrow text beside a table, and a table lower in the
    right column was inserted before it (pinned by: test_pdf.py::TestBlockPlacement::
    test_table_in_the_right_column_is_not_inserted_before_a_short_heading_of_the_left_column). The body text of a
    single-column page overlaps the table horizontally, so narrow text beside a table is handled as before.
    """
    def is_body(other: tuple[float, float]) -> bool:
        return not (other[0] < x1 and x0 < other[1]) and other[1] - other[0] >= (x1 - x0) * _COLUMN_MIN_WIDTH_RATIO

    left, right = span
    if left < x1 and x0 < right:
        return False
    return is_body(span) or any(is_body(other) and abs(other[0] - left) <= _TABLE_ROW_TOLERANCE for other in spans)


def _columns_apart(span: tuple[float, float], block: tuple[float, float, float, float],
                   boxes: list[tuple[float, float, float, float]]) -> bool:
    """Whether the columns really divide between a paragraph that `_in_other_column` took for another column (horizontal range
    `span`) and the table or picture (`block` = top, bottom, x0, x1). They divide when any paragraph (`boxes` = top, bottom,
    x0, x1) either (1) overlaps the table or picture horizontally, does not overlap `span`, and is at least
    `_COLUMN_MIN_WIDTH_RATIO` times as wide as the table or picture (body text of the table's or picture's column), or (2)
    does not overlap the table or picture horizontally but overlaps it in height (text beside the table or picture,
    including slides that put a picture beside text).

    With neither, the table or picture sits alone on its line in a single-column flow and is left in height order. On a
    single-column page the only paragraphs overlapping a centred narrow table or picture are page-width paragraphs that also
    overlap the left-aligned paragraph below it, yet that left-aligned paragraph was taken for another column and the table
    or picture was moved after the paragraph at the top of the page (or to the end of the page), away from its subheading
    (pinned by: test_pdf.py::TestBlockPlacement::
    test_centred_narrow_table_on_a_one_column_page_stays_under_its_heading).
    """
    top, bottom, x0, x1 = block
    for other_top, other_bottom, left, right in boxes:
        overlaps_block = left < x1 and x0 < right
        if overlaps_block and not (left < span[1] and span[0] < right) and right - left >= (x1 - x0) * _COLUMN_MIN_WIDTH_RATIO:
            return True
        if not overlaps_block and other_top < bottom and top < other_bottom:
            return True
    return False


def _after_other_column(items: list[tuple[float, str, bool]], spans: list[tuple[float, float]], position: int,
                        block: tuple[float, float, float, float], boxes: list[tuple[float, float, float, float]]) -> bool:
    """Whether the paragraph right before the position chosen by height (`position`, the end of the page or before narrow text
    such as a page number) is body text of another column, so the table or picture (`block` = top, bottom, x0, x1) has to
    be moved into the flow of its own column. On a two-column page a table at the very bottom of the left column was
    attached after the body text of the right column (the end of the page) (pinned by:
    test_pdf.py::TestBlockPlacement::test_table_at_the_bottom_of_the_left_column_stays_in_the_left_column and 1 more).

    It is moved only when that paragraph comes down beside the table or picture or starts before the paragraph above in the
    table's or picture's column (a caption row above two side-by-side tables starting at the same height is handled as
    before), and there is a height above the table or picture where two columns flow side by side (text to the right of a
    picture has no text of the picture's column at that height and is read after the picture as before —
    test_pdf_pictures_beside_text.py::TestTextBesideAPicture).
    """
    top, _, x0, x1 = block
    previous = next((index for index in range(position - 1, -1, -1) if not items[index][2]), None)
    if previous is None:
        return False
    box = boxes[sum(1 for _, _, is_block in items[:previous] if not is_block)]
    if not _in_other_column(box[2:], x0, x1, spans):
        return False
    column = [(other_top, bottom) for other_top, bottom, left, right in boxes if other_top < top and left < x1 and x0 < right]
    if not column or (box[1] <= top and box[0] >= max(column)[0] - _TABLE_ROW_TOLERANCE):
        return False

    beside = sorted((other_top, bottom) for other_top, bottom, left, right in boxes
                    if not (left < x1 and x0 < right) and _in_other_column((left, right), x0, x1, spans))
    tops, lowest = [other_top for other_top, _ in beside], list(accumulate((bottom for _, bottom in beside), max))
    return any((reach := bisect_left(tops, bottom)) and lowest[reach - 1] > other_top for other_top, bottom in column)


def _position_in_column(items: list[tuple[float, str, bool]], spans: list[tuple[float, float]],
                        block_top: float, x0: float, x1: float) -> int:
    """When the position chosen by height is a paragraph of another column, the position that puts the table or picture in the
    flow of its own column (the paragraphs overlapping it horizontally).

    On a two-column page a table of the right column cut in before a paragraph of the left column that was 'the first
    paragraph starting below the table'. It goes after the last paragraph above it in the same column (after any table or
    picture already placed there), or, when there is no such paragraph, before the first paragraph below it in the same
    column (pinned by: test_pdf.py::TestBlockPlacement).
    """
    column = [index for index, (_, _, is_block) in enumerate(items)
              if not is_block and spans[index][0] < x1 and x0 < spans[index][1]]
    above = [index for index in column if items[index][0] < block_top]
    if not above:
        # before the topmost paragraph, not the first in reading order (a page where a slide's picture caption was read
        # after the row of cards)
        return min(column, key=lambda index: items[index][0]) if column else len(items)

    # after the tables and pictures already placed in the same column. A table of another column (the lower rows of a table
    # that broke over from the previous column) is not skipped
    position = above[-1] + 1
    while position < len(items) and items[position][2] and spans[position][0] < x1 and x0 < spans[position][1]:
        position += 1
    return position


def _group_paragraphs(lines: list[_Line], line_gap: float) -> list[_Paragraph]:
    paragraphs: list[_Paragraph] = []
    for line in lines:
        if paragraphs and _continues(paragraphs[-1].lines[-1], line, line_gap):
            paragraphs[-1].lines.append(line)
        else:
            paragraphs.append(_Paragraph([line]))
    return paragraphs


def _continues(previous: _Line, line: _Line, line_gap: float) -> bool:
    """Whether `line` is in the same paragraph as `previous`. `line_gap` is the ratio of the usual line gap to the line height."""
    if _LONE_BULLET.match(previous.text):
        # A line holding only a bullet character is one line with the next line at the same height.
        return abs((previous.top + previous.bottom) / 2 - (line.top + line.bottom) / 2) <= previous.height
    if previous.size and line.size and abs(previous.size - line.size) > 0.15 * max(previous.size, line.size):
        return False
    gap = line.top - previous.bottom
    allowed = previous.height * (line_gap if line_gap else 0.5) * 1.5 + 1.0
    horizontally_overlaps = line.x0 < previous.x1 and previous.x0 < line.x1
    return -previous.height * 0.3 <= gap <= allowed and horizontally_overlaps


def _paragraph_text(paragraph: _Paragraph) -> str:
    parts: list[str] = []
    for line in paragraph.lines:
        if parts and _LONE_BULLET.match(parts[-1]):
            parts[-1] = f"{parts[-1]} {line.text}"
        else:
            parts.append(line.text)
    return "\n".join(parts)


def _heading_size(paragraph: _Paragraph) -> float:
    return round(max(line.size for line in paragraph.lines) * 2) / 2


def _is_heading(paragraph: _Paragraph, heading_levels: dict[float, int] | None = None, body_size: float = 0.0) -> bool:
    """A paragraph that is short, mostly letters, does not start with a bullet, and is clearly larger than the body text."""
    text = " ".join(line.text for line in paragraph.lines)
    size = max(line.size for line in paragraph.lines)
    if not size or len(paragraph.lines) > 2 or len(text) > 100 or _BULLET_START.match(text):
        return False
    word_chars = len(_WORD_CHAR.findall(text))
    if word_chars < 2 or word_chars < len(text.replace(" ", "")) * 0.5:
        return False
    if heading_levels is not None:
        return _heading_size(paragraph) in heading_levels
    return bool(body_size) and size / body_size >= _HEADING_MIN_RATIO


def _heading_levels(pages: list[_Page], body_size: float, line_gap: float) -> dict[float, int]:
    """Heading size (in 0.5pt steps) → level. The heading sizes in the document are mapped, largest first, to #, ## and ###
    (### from the fourth size onward)."""
    sizes = {
        _heading_size(paragraph)
        for page in pages if not page.simple
        for paragraph in _group_paragraphs(page.lines, line_gap)
        if _is_heading(paragraph, body_size=body_size)
    }
    return {size: min(rank, 3) for rank, size in enumerate(sorted(sizes, reverse=True), start=1)}


def _join_ruled_header_lines(page: _Page) -> None:
    """Join a header stacked in several lines per cell in the band between two horizontal rules into one line per cell (same
    top coordinate) so that it forms one row.

    In a three-line table with only top and bottom rules (financial statements), the two-line header ('2025년 / (제12기)')
    and the vertically centred one-line cell beside it ('과목') have a different height on every line and were not grouped
    into one row — the header became separate paragraphs and the first data row became the header (pinned by:
    test_pdf_table_columns.py::TestTwoLineHeaderBetweenRules). Joined only when the band is at most 4.5 lines high, every line
    in the band is short, the cells formed by grouping horizontally overlapping lines number three or more (one of them
    multi-line), and the row right under the lower rule has three or more pieces.

    In a two-level header ('2023년' spanning the two columns '남성·여성') the upper cell does not overlap the lower cells
    horizontally, so it was not joined per cell. The upper cell is prefixed to each cell below it
    ('2023년 남성 | 2023년 여성', the same form as `_merge_split_header` for ruled tables, `_grouped_columns`).
    The rule under the header may be drawn stopping short of a vertically merged cell ('구분'), so it is accepted when it
    covers at least half within the width of the upper rule (pinned by:
    test_pdf_table_columns.py::TestGroupedHeaderBetweenRules).
    """
    if page.simple or len(page.rules) < 2:
        return
    max_cell = page.width * _TABLE_CELL_MAX_RATIO
    for (upper, left, right), (lower, lower_left, lower_right) in zip(page.rules, page.rules[1:], strict=False):
        if (lower_left < left - 2 or lower_right > right + 2 or (lower_right - lower_left) * 2 < right - left
                or (abs(right - lower_right) > 2 and abs(left - lower_left) > 2) or lower - upper < _SHADE_MIN_SIDE * 3):
            continue
        inside = [line for line in page.lines if line.top >= upper - 1 and line.bottom <= lower + 1
                  and line.x0 >= left - 2 and line.x1 <= right + 2]
        if (len(inside) < _TABLE_MIN_COLUMNS or any(line.x1 - line.x0 > max_cell for line in inside)
                or lower - upper > statistics.median(line.height for line in inside) * 4.5):
            continue
        grouped = _grouped_columns(inside)
        if grouped is None:
            continue
        grouped_ids = {id(line) for column in grouped for line in column}
        columns = sorted([*_stacked_columns([line for line in inside if id(line) not in grouped_ids]), *grouped],
                         key=lambda column: column[-1].x0)
        if len(columns) < _TABLE_MIN_COLUMNS or (not grouped and all(len(column) == 1 for column in columns)):
            continue
        below = _visual_rows([line for line in page.lines if line.top >= lower - 1
                              and line.top - lower <= inside[0].height * 2.5 and line.x0 >= left - 2 and line.x1 <= right + 2])
        if not below or len(below[0]) < _TABLE_MIN_COLUMNS:
            continue

        top = min(line.top for line in inside)
        joined = [_Line(top=top, bottom=max(line.bottom for line in column), x0=column[-1].x0 if grouped_cell else
                        min(line.x0 for line in column), x1=column[-1].x1 if grouped_cell else max(line.x1 for line in column),
                        text=" ".join(line.text for line in column), size=statistics.median(line.size for line in column))
                  for column in columns for grouped_cell in [any(column is cell for cell in grouped)]]
        taken = {id(line) for line in inside}
        # placed where the first header line was in reading order
        first = next(index for index, line in enumerate(page.lines) if id(line) in taken)
        rest = [line for line in page.lines if id(line) not in taken]
        page.lines = [*rest[:first], *joined, *rest[first:]]


def _grouped_columns(lines: list[_Line]) -> list[list[_Line]] | None:
    """The cells of a two-level header: for each upper cell (`upper`) lying wholly above another line in the band, when two or
    more of the lines below have it as the upper cell whose centre is nearest and the upper cell's centre lies between the
    centres of those lines, one [upper cell, lower line] cell is made per lower line.
    An upper cell with a single line below is a stacked two-line cell and is not handled here (`_stacked_columns`). When an
    upper cell got two or more lower lines but its centre is not between them, it is not a two-level header: None (nothing
    is joined). An empty list when there is no upper cell."""
    def centre(line: _Line) -> float:
        return (line.x0 + line.x1) / 2

    upper = [line for line in lines if any(other.top >= line.bottom - 1 for other in lines)]
    below: dict[int, list[_Line]] = {id(line): [] for line in upper}
    for line in lines:
        above = [other for other in upper if other is not line and other.bottom <= line.top + 1]
        if above and not any(line is other for other in upper):
            below[id(min(above, key=lambda other: abs(centre(other) - centre(line))))].append(line)

    columns: list[list[_Line]] = []
    for line in upper:
        cells = sorted(below[id(line)], key=lambda cell: cell.x0)
        if len(cells) < 2:
            continue
        if not centre(cells[0]) < centre(line) < centre(cells[-1]):
            return None
        columns += [[line, cell] for cell in cells]
    return columns


def _stacked_columns(lines: list[_Line]) -> list[list[_Line]]:
    """The cells formed by grouping lines whose horizontal ranges overlap (left to right, top to bottom inside a cell)."""
    columns: list[list[_Line]] = []
    for line in sorted(lines, key=lambda line: line.x0):
        if columns and line.x0 < max(other.x1 for other in columns[-1]):
            columns[-1].append(line)
        else:
            columns.append([line])
    return [sorted(column, key=lambda line: line.top) for column in columns]


def _extract_aligned_tables(page: _Page) -> None:
    """Find tables that have no rules and only aligned cells, turn them into Markdown tables, and take their lines out of the
    body flow."""
    used: set[int] = set()
    for run in _aligned_table_runs(page):
        header = run[0]
        grid = [[""] * len(header) for _ in run]
        columns = [_row_columns(row, header) for row in run]
        for row_index, row in enumerate(run):
            for line, column in zip(row, columns[row_index], strict=True):
                cell = grid[row_index][column]
                same_row = abs(line.top - row[0].top) <= _TABLE_ROW_TOLERANCE  # words spread over the cell width (_spread_over_one_cell)
                joint = " " if cell and same_row else "<br>"  # a lower line is a wrapped line of the cell text (_continues_cell)
                grid[row_index][column] = f"{cell}{joint}{line.text}" if cell else line.text
        if _wrapped_header_line(run, grid):
            grid[0] = [f"{upper} {lower}".strip() for upper, lower in zip(grid[0], grid[1], strict=True)]
            run, grid, columns = [run[0] + run[1], *run[2:]], [grid[0], *grid[2:]], [columns[0] + columns[1], *columns[2:]]
        cells = [line for row in run for line in row]
        placed = [[line for row, row_columns in zip(run, columns, strict=True)
                   for line, other in zip(row, row_columns, strict=True) if other == column] for column in range(len(header))]
        page.tables.append(_Table(top=header[0].top, text=render_table(grid), bottom=max(line.bottom for line in cells),
                                  x0=min(line.x0 for line in cells), x1=max(line.x1 for line in cells), rows=grid,
                                  spans=tuple((min(line.x0 for line in lines), max(line.x1 for line in lines)) for lines in placed),
                                  lines=run))
        used.update(id(line.source or line) for line in cells)  # for split pieces (_split_merged_cells) the original line is removed
        used.update(id(original) for line in cells for original in line.joined)
    if used:
        page.lines = [line for line in page.lines if id(line) not in used]


def _wrapped_header_line(run: list[list[_Line]], grid: list[list[str]]) -> bool:
    """Whether the second row (`run[1]`) of a borderless table is a line wrapped from the header cells: every column with a
    filled cell also has a filled header cell, neither row has a numeric value cell (`_FAR_VALUE`) while the data rows below
    do, and the gap to the header is at most `_HEADER_WRAP_GAP_RATIO` times the line height and narrower than half the gap
    to the row below.

    The second line of a two-line header ('판매량 / (대)', '전년 동기 / 대비 증감률') became the first data row
    '| | (대) | (백만원) |' and only half of each column name remained (pinned by:
    test_pdf_table_columns.py::TestBorderlessTwoLineHeader). A row at the same pitch as the data rows is not a wrapped line
    (test_row_at_the_same_pitch_as_the_data_rows_stays_a_row in the same class). Known accepted gap: in the two-line header
    of a table with no numeric values the second line stays a row as before.
    """
    if len(run) < 4 or not any(grid[1]):
        return False
    if any(lower and not upper for upper, lower in zip(grid[0], grid[1], strict=True)):
        return False
    if any(_FAR_VALUE.match(cell.replace(" ", "")) for cell in [*grid[0], *grid[1]] if cell):
        return False
    if not any(_FAR_VALUE.match(cell.replace(" ", "")) for row in grid[2:] for cell in row if cell):
        return False
    upper_bottom, lower_top = max(line.bottom for line in run[0]), min(line.top for line in run[1])
    gap, below = lower_top - upper_bottom, min(line.top for line in run[2]) - max(line.bottom for line in run[1])
    height = statistics.median(line.height for line in run[0])
    return gap <= height * _HEADER_WRAP_GAP_RATIO and gap < below / 2


def _row_columns(row: list[_Line], header: list[_Line]) -> list[int]:
    """The column of each piece of a borderless table row (`row`, which may have wrapped lines of cell text joined on).

    Pieces at the same height are decided among themselves with `_column_of`, and a wrapped line (below the first line)
    follows the column of the piece above whose left end matches or which overlaps it horizontally — treating a wrapped line
    as one row with the first-line pieces made the wrapped line in the gap before a centred header ('회의실') look as if
    the column on the right was taken by another piece of the same row, and it was attached to the first-column cell
    ('장소<br>회의실') (pinned by: test_pdf_table_columns.py::
    TestCentredHeadersOverLeftAlignedCells::test_wrapped_line_of_a_cell_in_the_gap_before_a_centred_header_stays_in_the_cell).
    """
    columns: list[int] = []

    for index, line in enumerate(row):
        level = [other for other in row if abs(other.top - line.top) <= _TABLE_ROW_TOLERANCE]
        column = _column_of(line, header, level)
        above = [(other, columns[number]) for number, other in enumerate(row[:index])
                 if other.top < line.top - _TABLE_ROW_TOLERANCE and other.x0 < line.x1 and line.x0 < other.x1]
        if above and column not in {other_column for _, other_column in above}:
            column = min(above, key=lambda pair: abs(pair[0].x0 - line.x0))[1]
        columns.append(column)

    return columns


def _carry_table_headers(pages: list[_Page]) -> None:
    """Put the header row of the previous page's table on top of a table that continues onto the next page without repeating
    its header (modifies in place).

    A Markdown table uses its first row as the header, so from the second page on the first data row became the header and
    the column names were lost (pinned by: test_pdf_table_columns.py::TestTableContinuedOnTheNextPage). A table is taken as
    continued only when all of the following hold (deliberate, conservative): there are no lines or pictures outside the
    margin area (`_in_margin`) either below the bottom table of the page right before or above the top table of this page;
    the columns match (ruled tables: same column count and each column's horizontal range overlaps only the same column of
    the other, `_same_columns`; borderless tables: each line piece overlaps only one column of the previous table,
    `_regrid` — an empty cell in the first data row leaves the column count short); and the first row of this page's table
    differs from the previous table's header. When the first row of this page's table has no numeric value cell and the
    second row does, it is taken as its own header and nothing is put on top (another table with the same column shape,
    pinned by: test_new_table_with_its_own_header_at_the_top_of_the_next_page_keeps_its_header). The previous table must
    have three or more rows and its header must have two or more short cell texts, no numeric value cell, and must not
    appear again in the data rows (`_looks_like_header`) — the frame box filling the page on every slide was caught as a
    ruled table and the previous page's body text was put on top as a header
    (test_page_filling_frames_of_slides_do_not_get_the_previous_slide_as_a_header). From the third page on, the carried-over
    header is carried over again. Year and date cells in a header (`_YEAR_OR_DATE`) are not counted as numeric value cells
    (`_has_value_text`) — '과목 | 주석 | 2024.12.31 | 2023.12.31' of a financial statement was not seen as a header, and
    '성과 지표 | 2023 | 2024' of a different table at the top of the next page was not seen as its own header and became a
    data row under the previous table's header. For a two-column table the header is carried over only when the second cell
    is a numeric value in more than half of the previous table's data rows — the first row ('성명 | 홍길동') of an
    application form filled on every page with a ruled 'label | value' grid was put on top of the next page's table as its
    header (pinned by: test_pdf_table_columns.py::TestHeaderCarriedOverWithYearsAndForms). Known accepted gap: when a table
    whose values are all text starts with its own header, the previous table's header is put on top of it. A two-column
    table whose values are text ('연도 | 주요 내용') does not get its header carried over. When every column of the previous
    table is open below (a row split at the end of the page, `_closed_at_open_ends`), the first row of this page is the rest
    of that row and is not taken as its own header (pinned by:
    TestRowSplitAcrossPages::test_row_whose_cells_are_all_open_at_the_page_end_stays_in_the_table).
    """
    for previous, page in zip(pages, pages[1:], strict=False):
        if page.number != previous.number + 1 or not previous.tables or not page.tables:
            continue
        last = max(previous.tables, key=lambda table: table.bottom)
        first = min(page.tables, key=lambda table: table.top)
        if not (last.rows and first.rows and last.spans):
            continue
        if (any(line.top > last.bottom and not _in_margin(line, previous) for line in previous.lines)
                or any(figure[1] > last.bottom for figure in previous.figures)
                or any(line.bottom < first.top and not _in_margin(line, page) for line in page.lines)
                or any(figure[3] < first.top for figure in page.figures)):
            continue

        if (not first.lines and not last.lines and len(first.spans) == len(last.spans)
                and _same_columns(last.spans, first.spans)):
            # The next-page piece of a row split at the end of the page is a top row with a single filled cell among empty
            # ones, so it was pulled out as a table title and became a paragraph (pinned by:
            # test_pdf_table_columns.py::TestRowSplitAcrossPages). A ruled table gives merged cells as None, so only those
            # are taken as a title.
            first.continued = True
            _carry_open_cells(last, first)
            first.text = _table_text(first)

        # In a borderless table the first row acts as the header, so an empty cell leaves it short of columns; the line pieces
        # are fitted to the previous table's columns again
        if first.lines:
            grid = _regrid(first.lines, last.spans)
        else:
            grid = first.rows if len(first.spans) == len(last.spans) and _same_columns(last.spans, first.spans) else None
        header = last.rows[0]
        if grid is None or len(last.rows) < _TABLE_MIN_ROWS or not _looks_like_header(header, [*last.rows[1:], grid[0]]):
            continue
        # a row split at the end of the previous page: the first row is the rest of it, not a header
        split = len(last.open_below) == len(last.spans)
        if len(grid) > 1 and not split and not _has_value_text(grid[0]) and _has_number_text(grid[1]):
            continue
        if len(header) == 2 and sum(1 for row in last.rows[1:] if _has_number_text(row[1:])) * 2 <= len(last.rows) - 1:
            # a two-column table with no numeric value column may be a 'label | value' form grid — there is no telling whether
            # the first row is a header
            continue
        first.rows = [header, *grid]
        first.text = _table_text(first)
        if first.bands:
            first.bands = ((-math.inf, -math.inf), *first.bands)  # the carried-over header row has no position on the page
        if first.lines:
            first.spans = last.spans  # fitted to the previous table's columns, so the next page is fitted to them too


def _carry_open_cells(last: _Table, first: _Table) -> None:
    """For each column where the bottom cell of the previous page's table (`last`) is open below (`open_below`, a vertically
    merged cell split at the end of the page), fill that cell's text into the rows of the continued table (`first`) where
    the column is empty, starting from the top data row (a repeated header is skipped) (stops at the first cell with text,
    modifies in place).

    Chrome draws the text of a rowspan cell split at the end of the page only on the previous page, so the first cell of the
    next page's rows was empty (pinned by: test_pdf_table_columns.py::
    TestMergedCellOpenAtThePageEnd::test_rows_of_the_cell_continued_on_the_next_page_get_its_text).

    Deliberate: when every column is open it is not a merged cell but a row split at the end of the page
    (`_closed_at_open_ends`) and nothing is filled — filling the previous page's values ('5', '500') into the empty cells of
    the rest of the row on the next page would make the same row come out twice, and the amount could be counted twice
    (pinned by: test_pdf_table_columns.py::
    TestRowSplitAcrossPages::test_row_whose_cells_are_all_open_at_the_page_end_stays_in_the_table).
    """
    if len(last.open_below) == len(last.spans):
        return
    start = 1 if first.rows and last.rows and first.rows[0] == last.rows[0] else 0
    for column in last.open_below:
        value = last.rows[-1][column] if column < len(last.rows[-1]) else ""
        if not value:
            continue
        for row in first.rows[start:]:
            if column >= len(row) or row[column]:
                break
            row[column] = value


def _looks_like_header(row: list[str], rows: list[list[str]]) -> bool:
    """Whether the row looks like a header row: two or more cells have text, all of them at most `_HEADER_CELL_MAX_CHARS`
    characters (line breaks excluded), no cell holds only a numeric value, and no cell text appears again in the same column
    of the other rows (`rows`, the previous table's data rows and the first row of the table to be continued).

    The last condition filters out the same frame on every page (a slide layout caught as a ruled table) — the frame's first
    cell (the section title) continues vertically and appears with the same text in the data rows and in the next page's
    frame. The header cells of a data table do not appear again in its data rows. A header repeated on every page is also
    filtered out here."""
    filled = [cell.strip() for cell in row if cell and cell.strip()]
    return (len(filled) >= 2 and not _has_value_text(row)
            and all(len(cell.replace("<br>", "")) <= _HEADER_CELL_MAX_CHARS for cell in filled)
            and not any(cell and cell.strip() and index < len(other) and (other[index] or "").strip() == cell.strip()
                        for other in rows for index, cell in enumerate(row)))


def _regrid(rows: list[list[_Line]], spans: tuple[tuple[float, float], ...]) -> list[list[str]] | None:
    """The cell text of a borderless table's rows (line pieces) fitted to another table's column ranges (`spans`). Each piece
    must overlap exactly one column (a piece spanning several columns is tried split into its wide-gap pieces,
    `_Line.parts`), and when two pieces land in one cell they are joined with `<br>` only when the second is a wrapped line
    placed below the row's first line. None if anything does not fit."""
    def columns(x0: float, x1: float) -> list[int]:
        return [index for index, span in enumerate(spans) if min(x1, span[1]) - max(x0, span[0]) > 0]

    grid = []
    for row in rows:
        cells = [""] * len(spans)
        for line in row:
            pieces = line.parts if line.parts and len(columns(line.x0, line.x1)) > 1 else ((line.x0, line.x1, line.text),)
            for x0, x1, text in pieces:
                found = columns(x0, x1)
                if len(found) != 1 or (cells[found[0]] and line.top - row[0].top <= _TABLE_ROW_TOLERANCE):
                    return None
                cells[found[0]] = f"{cells[found[0]]}<br>{text}" if cells[found[0]] else text
        grid.append(cells)
    return grid


def _same_columns(spans: tuple[tuple[float, float], ...], others: tuple[tuple[float, float], ...]) -> bool:
    """Whether each column's horizontal range in the two tables overlaps only the column at the same position (by more than 1pt)."""
    def overlaps(span: tuple[float, float], other: tuple[float, float]) -> bool:
        return min(span[1], other[1]) - max(span[0], other[0]) > 1.0

    return all(overlaps(span, others[index])
               and not any(overlaps(span, other) for position, other in enumerate(others) if position != index)
               for index, span in enumerate(spans))


def _has_number_text(row: list[str]) -> bool:
    """Whether a row of cell text has a cell holding only a numeric value (the cell-text counterpart of `_has_number_cell`)."""
    return any(_NUMBER_CELL.match((cell or "").strip()) for cell in row)


def _has_value_text(row: list[str]) -> bool:
    """Whether a row of cell text has a numeric value cell that is not a year or date (`_YEAR_OR_DATE`). Used for the header
    test (`_carry_table_headers`)."""
    return any(_NUMBER_CELL.match(text) and not _YEAR_OR_DATE.match(text) for text in ((cell or "").strip() for cell in row))


def _aligned_table_runs(page: _Page) -> list[list[list[_Line]]]:
    """The groups of rows to treat as borderless tables. A row is the line pieces at the same height (left to right), and the
    first row is the header.

    Tables are first searched for with rows grouped from pieces whose tops match, then only the lines not in a table are
    regrouped by vertical overlap (`staggered` of `_visual_rows`) and searched once more. A table already found is regrouped
    from its own lines alone only when neighbouring rows overlap vertically (half rows) (`_regrouped_half_rows`) — grouping
    by overlap from the start merged staggered figure labels into rows and broke existing tables (public paper PDF).
    """
    if page.simple or len(page.lines) < _TABLE_MIN_ROWS * _TABLE_MIN_COLUMNS:
        return []
    max_cell = page.width * _TABLE_CELL_MAX_RATIO
    rows = _with_ruled_wrapped_cells(_visual_rows(page.lines), page.rules)
    runs = [_regrouped_half_rows(run, max_cell) for run in _table_runs(rows, max_cell, page.bands)]
    in_table = {id(original) for run in runs for row in run for line in row for original in (line, *line.joined)}
    rest = [line for line in page.lines if id(line) not in in_table]
    # The second pass accepts only tables in which staggered pieces were actually grouped. A table that arose merely because
    # removing the table lines made the rows group differently is not accepted.
    # A wrapped line of cell text (_continues_cell) starts below the row's first line and does not count as a staggered piece.
    runs += [run for run in _table_runs(_visual_rows(rest, staggered=True), max_cell)
             if any(abs(line.top - row[0].top) > _TABLE_ROW_TOLERANCE and line.top < row[0].bottom
                    for row in run for line in row)]
    in_table = {id(original) for run in runs for row in run for line in row for original in (line, *line.joined)}
    runs += _split_line_table_runs([line for line in page.lines if id(line) not in in_table], max_cell)

    # on a two-column page, tables that mix table rows of the two columns are dropped and searched for again inside each column
    columns = _table_columns(page)
    if columns is not None:
        runs = [run for run in runs if not _mixes_two_columns(run, *columns)]
        runs += _column_table_runs(page, runs, columns, max_cell)
    rows = _visual_rows(page.lines)
    runs = [_cut_at_closing_rule(run, page.rules) for run in runs]

    # The original line (source) of a split piece is a used line too. When it was left out, the table below took that row
    # again while growing upward and it came out in two tables
    # (pinned by: test_pdf_table_columns.py::TestRowsOfASplitTableAreNotTakenAgain)
    taken = {id(original) for run in runs for row in run for line in row for original in (line, line.source) if original is not None}
    return [_split_merged_cells(run, rows, taken, max_cell, page.rules) for run in runs]


def _cut_at_closing_rule(run: list[list[_Line]], rules: list[tuple[float, float, float]]) -> list[list[_Line]]:
    """The rows of a borderless table (`run`) cut at its bottom border (a table-width horizontal line). `run` as is when there
    is no place to cut.

    The totals block on the right under the bottom border of an invoice's item table ('공급가액 1,002,940') lined up with
    the columns and was absorbed as rows of the item table (pinned by:
    test_pdf_label_value_rows.py::TestTotalsBelowAnItemTable). It is cut only when that is the only table-width horizontal
    line between data rows (the one under the header excluded) and the rows below it all leave the first column empty and
    fill at most half of the columns — a table with a rule on every row, and a totals row with '합계' (total) written in the
    first column, are left as they are.
    """
    left, right = min(line.x0 for row in run for line in row), max(line.x1 for row in run for line in row)
    spanning = [y for y, x0, x1 in rules if x0 <= left + _TABLE_ROW_TOLERANCE and x1 >= right - _TABLE_ROW_TOLERANCE]
    header = run[0]

    def ruled(index: int) -> bool:  # whether there is a table-width horizontal line between run[index - 1] and run[index]
        return any(max(line.bottom for line in run[index - 1]) - 1 <= y <= min(line.top for line in run[index]) + 1 for y in spanning)

    if not spanning:
        return run
    cuts = [index for index in range(2, len(run)) if ruled(index)]
    if len(cuts) != 1 or cuts[0] < _TABLE_MIN_ROWS:
        return run
    tail = run[cuts[0]:]
    columns = [{_column_of(line, header, row) for line in row} for row in tail]
    if all(0 not in used and len(used) <= len(header) // 2 for used in columns):
        return run[:cuts[0]]
    return run


def _split_merged_cells(run: list[list[_Line]], rows: list[list[_Line]], taken: set[int],
                        max_cell: float, rules: list[tuple[float, float, float]]) -> list[list[_Line]]:
    """In a found borderless table (`run`) and the rows right above and below it, split the pieces in which pdfminer merged
    neighbouring cells at the wide gaps inside the line (`_Line.parts`) and search for the table again. Only rows above and
    below that continue at the row pitch and lie within the table's horizontal range are considered. The table is replaced
    only when exactly one table is found again, it holds every line of the original table, the columns do not decrease, and
    every column containing a split piece has cells on two or more rows. Otherwise `run` as is.

    The cells of narrow columns (32pt) are only the cell padding (12pt) apart, so pdfminer, which joins up to twice the
    character width, made '120 118' and '정원 현원 결원 비고' single pieces. The numbers went one column to the left and
    the header row fell off the table (pinned by: test_pdf_table_columns.py::
    TestNarrowColumnGaps). Splitting happens only where a table was already found (deliberate) — body lines whose word
    spacing was widened by justification are not a table and are not split
    (test_justified_prose_with_wide_word_spaces_is_left_alone). Lines containing a piece wider than the maximum table cell
    width, and lines of consecutive single Hangul or Hanja character pieces (letter-spaced '구    분',
    `_letter_spaced_parts`), are not split. Known accepted gap: rows whose cells are as close as ordinary word spacing
    (LibreOffice's 3px cell padding) and so have no wide gap are left as they are.
    """
    def splittable(line: _Line) -> bool:
        return _splittable(line, max_cell)

    # Only rows above and below that continue at the row pitch and lie within the table's horizontal range are considered.
    # A body line reaching into the neighbouring column is not a table header.
    # Rows below are considered too because when a row whose cells were merged differently ('광주 | 5,955 | 10.8% 감소')
    # came in the middle of a table, the table was cut above it and the remaining rows, fewer than the minimum row count,
    # became paragraphs in jumbled order (pinned by: test_pdf_round44.py::TestBorderlessTableWithARowMergedDifferently).
    # Downward, a horizontal line is not crossed — the totals block under the bottom border of an item table is not a table
    # (`_cut_at_closing_rule`). A short piece that starts inside the table is accepted even when it sticks out to the right —
    # in a table whose last column (비고, remarks) rarely has a value, a long value in that column ('2월 미집행') went past
    # the right end of the preceding rows and the table was cut from that row on
    # (pinned by: test_pdf_round46.py::TestRightAlignedHeaderMergedWithANearlyEmptyColumn)
    left = min(line.x0 for row in run for line in row) - _TABLE_ROW_TOLERANCE
    right = max(line.x1 for row in run for line in row) + _TABLE_ROW_TOLERANCE

    def joins(row: list[_Line]) -> bool:
        return not any(id(line) in taken or line.x0 < left or line.x0 > right
                       or (line.x1 > right and line.x1 - line.x0 > max_cell) for line in row)

    def spans(table_row: list[_Line]) -> list[int]:
        # the positions in `rows` that hold the original lines of the table row (for a cell joined from several lines, those lines)
        members = {id(original) for line in table_row for original in (line, *line.joined)}
        return [index for index, row in enumerate(rows) if any(id(line) in members for line in row)]

    above: list[list[_Line]] = []
    start = min(spans(run[0]), default=None)
    if start is not None:
        below = run[0]
        for row in reversed(rows[:start]):
            if not joins(row) or below[0].top - max(line.bottom for line in row) > row[0].height * 2.5:
                break
            above.insert(0, row)
            below = row

    after: list[list[_Line]] = []
    end = max(spans(run[-1]), default=None)
    if end is not None:
        upper = run[-1]
        for row in rows[end + 1:]:
            floor, ceiling = max(line.bottom for line in upper), min(line.top for line in row)
            if (not joins(row) or ceiling - floor > upper[0].height * 2.5
                    or any(floor - 1 <= y <= ceiling + 1 and x0 < right and x1 > left for y, x0, x1 in rules)):
                break
            after.append(row)
            upper = row

    region = [line for row in [*above, *run, *after] for line in row]
    if not any(splittable(line) for line in region):
        return run

    # A cell joined from several lines (`_centred_band_row` etc.) is put back as its original lines. Joined cells differ in
    # height from row to row, so regrouping by top edge would scatter one row line by line. They are joined again by the
    # same rules when the table is searched for again
    pieces: list[_Line] = []
    for line in region:
        if splittable(line):
            pieces += [_Line(top=line.top, bottom=line.bottom, x0=x0, x1=x1, text=text, size=line.size, source=line)
                       for x0, x1, text in line.fine_parts or line.parts]
        else:
            pieces += [original for original in line.joined if not original.joined] or [line]

    found = _table_runs(_visual_rows(pieces), max_cell)
    if len(found) != 1 or len(found[0][0]) < len(run[0]):
        return run
    # Original lines that the re-found table holds inside a cell joined from several lines (`_row_with_lead_lines` etc.) also
    # count as held. Only after the right-aligned header '3월 비고' was split did the lead line of the first row's remark get
    # joined to '예정', so '예정' was counted as missing and the header and first row stayed outside the table (pinned by:
    # test_pdf_round47.py::TestWrappedNoteUnderARightAlignedMergedHeader)
    kept = {id(original) for row in found[0] for line in row for original in (line, *line.joined)}
    in_run = {id(line) for row in run for line in row}
    if not all(id(piece) in kept for piece in pieces if id(piece.source or piece) in in_run):
        return run

    # A column containing a split piece must have cells on other rows too. A column produced by splitting justified text
    # inside a cell exists only on that row
    header = found[0][0]
    used = Counter(column for row in found[0] for column in {_column_of(line, header, row) for line in row})
    if any(used[_column_of(line, header, row)] < 2 for row in found[0] for line in row if line.source is not None):
        return run
    return found[0]


def _splittable(line: _Line, max_cell: float) -> bool:
    """A line that can be treated as neighbouring cells merged by pdfminer and split at its wide gaps (`_Line.parts`, or
    `fine_parts`, which also looks at narrow gaps without a space character, when present): it has gaps, every piece is at
    most the maximum table cell width, and there are no consecutive single Hangul or Hanja character pieces
    (`_letter_spaced_parts`).

    The piece width is examined rather than the whole line width (the same criterion as `_split_line_table_runs`) because
    the header '1분기 2분기 3분기 4분기', merged across narrow cell gaps, exceeded 25% of the page width, could not be split
    and became a paragraph outside the table (pinned by: test_pdf_table_columns.py::
    TestHeaderMergedWiderThanACell).

    `fine_parts` is examined too because the gap between a right-aligned number cell and a left-aligned text cell in a
    Chrome table (cell padding 6px + 6px = 9pt, 0.75 times a 12pt character, no space character) fell short of
    `_CELL_GAP_RATIO` (0.8 times) and '18 2023 신규' became one cell (pinned by:
    test_pdf_table_columns.py::TestNarrowColumnGaps::test_right_aligned_numbers_next_to_a_left_aligned_note_column).
    Splitting only inside an already-found table is unchanged."""
    parts = line.fine_parts or line.parts
    return (bool(parts) and all(x1 - x0 <= max_cell for x0, x1, _ in parts)
            and not _letter_spaced_parts(parts))


def _letter_spaced_parts(parts: list[tuple[float, float, str]]) -> bool:
    """Whether the line pieces contain two or more consecutive single Hangul or Hanja character pieces — that is letter-spaced
    text ('구    분') and is not split into cells.

    Blocking on a single one-character piece made it impossible to split '값 단위' in a Chrome three-line table (pdfminer
    merged the 1.3em between cells into one piece), and the header and first row became paragraphs outside the table
    (pinned by: test_pdf_table_columns.py::TestOneLetterHeaderCell)."""
    return any(len(left) == 1 and len(right) == 1 and _SPACED_CHAR.match(left) and _SPACED_CHAR.match(right)
               for (_, _, left), (_, _, right) in zip(parts, parts[1:], strict=False))


def _split_line_table_runs(lines: list[_Line], max_cell: float) -> list[list[list[_Line]]]:
    """The borderless tables found once more after splitting, among the lines not in a table (`lines`), the lines that merged
    neighbouring cells (`_splittable`).

    In a table whose columns are 1 to 1.6 times the font size apart, pdfminer (char_margin 2.0) merged neighbouring cells
    into one piece, the piece count differed from row to row and the table was not found, and with an empty cell the text
    boxes were grouped per column and the rows were shuffled (pinned by: test_pdf_table_columns.py::
    TestBorderlessTableWithNarrowGaps). A table whose first cell wraps to two lines and whose rows are out of step with the
    other, vertically centred cells is also found here (`_with_centred_wrapped_cells`). The split positions are
    `_Line.fine_parts`, which also looks at narrow gaps without a space character (`parts` when absent), and a merged line
    is split even when it exceeds the maximum table cell width as long as each piece is within that width. Only tables
    containing a split piece or a merged two-line cell are accepted; all the pieces of a split line must be in the same
    table, every column containing a split piece must have cells on two or more rows (the same criterion as
    `_split_merged_cells`), every split piece must have a piece on another row whose left end, right end or centre matches,
    and the pieces of every row must sit at one height (`_aligned_in_another_row`, `_on_one_baseline`) — justified body text
    in short lines has column starts that shift from word to word and does not become a table
    (test_pdf_table_columns.py::TestNarrowColumnGaps::
    test_justified_prose_with_wide_word_spaces_is_left_alone).
    """
    pieces: list[_Line] = []
    for line in lines:
        parts = line.fine_parts or line.parts
        if (parts and all(x1 - x0 <= max_cell for x0, x1, _ in parts)
                and not _letter_spaced_parts(parts)):
            pieces += [_Line(top=line.top, bottom=line.bottom, x0=x0, x1=x1, text=text, size=line.size, source=line)
                       for x0, x1, text in parts]
        else:
            pieces.append(line)
    split = Counter(id(piece.source) for piece in pieces if piece.source is not None)
    found = []

    for run in _table_runs(_with_centred_wrapped_cells(_visual_rows(pieces)), max_cell):
        taken = Counter(id(line.source) for row in run for line in row if line.source is not None)
        if not (taken or any(line.joined for row in run for line in row)) or any(split[source] != count
                                                                                 for source, count in taken.items()):
            continue
        header = run[0]
        used = Counter(column for row in run for column in {_column_of(line, header, row) for line in row})
        if all(used[_column_of(line, header, row)] >= 2 and _aligned_in_another_row(line, row, run)
               for row in run for line in row if line.source is not None) and all(map(_on_one_baseline, run)):
            found.append(run)
    return found


def _on_one_baseline(row: list[_Line]) -> bool:
    """Whether the pieces of a row sit at one height (top coordinates within 1pt). Wrapped lines of cell text (more than half a
    line height below the top line) are excluded.

    Two text boxes with different line spacing (an invoice's company details on the left and 'item value' on the right)
    were grouped into one row while 2 to 4pt out of step on every row and became a three-column table (pinned by:
    test_pdf_label_value_rows.py::TestLeftBlockIsNotAWrappedCell — guarded together with `_CENTRED_CELL_RATIO`. That test
    breaks only when both are missing).
    """
    top = min(line.top for line in row)
    return all(line.top - top <= 1 or line.top - top > line.height / 2 for line in row)


def _with_ruled_wrapped_cells(rows: list[list[_Line]], rules: list[tuple[float, float, float]]) -> list[list[_Line]]:
    """The rows with, between two horizontal rules (the rule under the header and the bottom border of a three-line table),
    first-cell text wrapped over several lines grouped into one row with the value row beside it (`_wrapped_label_row`).
    `rows` as is when there are fewer than two rules.

    In a three-line table whose values are aligned to the vertical centre or the bottom, the lead line of the first cell
    was left on its own above the value row, so the table was cut before that row or '| 값) | 900 | 950 |' became the
    header, and the remaining rows scattered cell by cell after the paragraph below the table. With top alignment too, the
    wrapped line of the last row has no following row below and became a paragraph outside the table (pinned by:
    test_pdf_ruled_wrapped_cells.py::TestWrappedFirstCellsInAThreeRuleTable). Nothing is grouped outside the rules — in a
    table without cell padding the line spacing cannot tell whether a wrapped line belongs to the row above or below, and
    the paragraph right above the table could be attached to a header cell. Outside the rules, `_table_runs` groups them only
    under the header of a found table and only when the row gap and line gap clearly differ (`_row_with_lead_lines`).
    """
    if len(rules) < 2:
        return rows
    gap = _paragraph_line_gap(rows, rules)
    merged: list[list[_Line]] = []
    index = 0

    while index < len(rows):
        found = _wrapped_label_row(rows, index, rules, gap)
        if found is None:
            merged.append(rows[index])
            index += 1
            continue
        row, index = found
        merged.append(row)
    return merged


def _paragraph_line_gap(rows: list[list[_Line]], rules: list[tuple[float, float, float]]) -> float | None:
    """The median line gap where single-piece rows continue with matching left ends outside the horizontal rules (above the
    top rule, below the bottom rule). None when there is none.

    The line gap of a paragraph equals the line gap of cell text wrapped with the same font and line spacing
    (`_wrapped_label_row`)."""
    top, bottom = min(y for y, _, _ in rules), max(y for y, _, _ in rules)
    gaps = sorted(line.top - previous.bottom for previous, line in ((above[0], below[0]) for above, below in pairwise(rows)
                                                                    if len(above) == 1 and len(below) == 1)
                  if (previous.bottom < top and line.bottom < top or previous.top > bottom)
                  and abs(line.x0 - previous.x0) <= _TABLE_ROW_TOLERANCE and 0 <= line.top - previous.bottom <= line.height * 2)
    return gaps[len(gaps) // 2] if gaps else None


def _wrapped_label_row(rows: list[list[_Line]], start: int, rules: list[tuple[float, float, float]],
                       paragraph_gap: float | None = None) -> tuple[list[_Line], int] | None:
    """(row, next position) grouping, from `rows[start]`, the first-cell text lines (two or more single-piece lines with the
    same left end, attached at line spacing) with one value row.

    Attached at line spacing means the line gap is at most half the line height, or at most the line gap of the paragraphs
    outside the rules (`paragraph_gap`, `_paragraph_line_gap`) + 1pt. In a table exported by LibreOffice with a Korean font
    the line gap is 0.8 times the line height (9.7pt for 12pt characters), so the first-cell lines were not joined and the
    table was read column by column (pinned by:
    test_pdf_ruled_wrapped_cells.py::TestWrappedFirstCellsWithWideLineSpacing).

    The value row is either a row whose cell text continues at the same height as one of the first-cell text lines, or a row
    of values only that does not overlap the first-cell text horizontally. They are grouped only when the middle of the
    value row lies within the height of the first-cell text lines, horizontal rules above and below enclose the group at the
    table width (with no rule inside the group), and the gaps to the rows before and after the group are wider than the line
    gap inside the group. Otherwise None.
    Exception: when a values-only row sits between the first-cell text lines and its middle is within `_CENTRED_CELL_RATIO`
    of the line height from the middle of the first-cell text lines (vertical centring — with top or bottom alignment the
    value is at the same height as a first-cell line), the gaps before and after are not examined,
    and if lines were collected up to the first line of the next row, only the leading lines whose middle matches are used —
    in a LibreOffice table whose cells have zero top and bottom padding the gap between rows equals the line gap, so the gap
    could not separate them (pinned by: test_cells_without_vertical_padding in the same class). Known accepted gap: when
    such a table is aligned to the top or bottom there is no telling which row a wrapped line belongs to, and nothing is
    grouped.
    """
    first = rows[start][0]
    reach = max(first.height / 2 + 0.5, -1.0 if paragraph_gap is None else paragraph_gap + 1.0)
    labels: list[_Line] = []
    label_ends: list[int] = []  # per line, the position of the next row after using up to that line
    anchor: list[_Line] | None = None
    anchor_end, beside = start, False  # position after the value row, whether the value is at the same height as a first-cell text line
    end = start

    for row in rows[start:]:
        line = row[0]
        if abs(line.x0 - first.x0) <= _TABLE_ROW_TOLERANCE:
            if labels and not 0 <= line.top - labels[-1].bottom <= reach:
                break
            if len(row) > 1:
                if anchor is not None or any(other.x0 < line.x1 for other in row[1:]):
                    break
                anchor, anchor_end, beside = row[1:], end + 1, True
            labels.append(line)
            label_ends.append(end + 1)
        elif (anchor is None and labels and all(other.x0 > max(label.x1 for label in labels) for other in row)
              and labels[-1].top < line.top):
            anchor, anchor_end = row, end + 1
        else:
            break
        end += 1
    if anchor is None or len(labels) < 2:
        return None

    # vertical centring: the first-cell text lines whose middle matches the middle of the value row (two or more, reaching
    # past the value row)
    centre = (anchor[0].top + anchor[0].bottom) / 2
    centred = next((count for count in range(len(labels), 1, -1) if label_ends[count - 1] >= anchor_end
                    and abs((labels[0].top + labels[count - 1].bottom) / 2 - centre) <= first.height * _CENTRED_CELL_RATIO), None)
    if beside:
        # a value at the same height as a first-cell line looks the same as 'bottom alignment + the first line of the next
        # row', so it is no evidence of centring
        centred = None
    if centred is not None:
        labels, end = labels[:centred], label_ends[centred - 1]
    top, bottom = labels[0].top, labels[-1].bottom
    left, right = first.x0, max(line.x1 for line in (*labels, *anchor))
    spanning = [y for y, x0, x1 in rules if x0 <= left + _TABLE_ROW_TOLERANCE and x1 >= right - _TABLE_ROW_TOLERANCE]
    above = max((y for y in spanning if y < top), default=None)
    below = min((y for y in spanning if y > bottom), default=None)
    if not top <= centre <= bottom or above is None or below is None or any(top < y < bottom for y in spanning):
        return None

    # the wrapped lines belong to this row only when the gaps to the rows before and after the group (inside the rules) are
    # wider than the line gap inside the group
    inside = max(line.top - previous.bottom for previous, line in pairwise(labels))
    margin = inside + max(1.0, inside / 2)
    before = rows[start - 1] if start and centred is None else None
    after = rows[end] if end < len(rows) and centred is None else None
    if before is not None and max(line.bottom for line in before) > above and top - max(line.bottom for line in before) < margin:
        return None
    if after is not None and min(line.top for line in after) < below and min(line.top for line in after) - bottom < margin:
        return None

    cell = _Line(top=top, bottom=bottom, x0=first.x0, x1=max(line.x1 for line in labels),
                 text="<br>".join(line.text for line in labels), size=first.size,
                 joined=tuple(original for line in labels for original in (*line.joined, line)))
    return [cell, *anchor], end


def _with_centred_wrapped_cells(rows: list[list[_Line]]) -> list[list[_Line]]:
    """The rows with each row whose remaining cells sit vertically centred at the height between cell text wrapped to two
    lines (A, B) grouped into one row.

    Chrome centres cells vertically, so in a row whose first cell has two lines the text of the other cells sits between the
    two lines of the first cell. In rows grouped by top coordinate this became three rows, [A] · [other cells] · [B], and the
    table was cut (pinned by: test_pdf_table_columns.py::TestBorderlessTableWithNarrowGaps::
    test_first_cell_wrapped_beside_vertically_centred_cells_stays_in_its_row). Only when two single-piece rows have the same
    left end and are attached at line spacing (at most half the line height, with 0.5pt of slack — in a Chrome table with
    4px cell padding the gap is exactly half the line height and fell out through floating-point error, pinned by:
    test_pdf_table_columns.py::TestWideWrappedFirstColumn), the row between them has two or more pieces that do not overlap
    the two horizontally, and its middle height is within `_CENTRED_CELL_RATIO` of the line height from the middle of the
    two lines, A and B are merged into one piece 'A<br>B' (at the height of the middle row, with the original lines in
    `joined`).
    """
    merged: list[list[_Line]] = []
    index = 0

    while index < len(rows):
        found = _centred_wrapped_row(rows, index)
        if found is not None:
            merged.append(found[0])
            index = found[1]
            continue
        merged.append(rows[index])
        index += 1
    return merged


def _centred_wrapped_row(rows: list[list[_Line]], index: int) -> tuple[list[_Line], int] | None:
    """When the three rows from `rows[index]` are a row whose remaining cells are vertically centred at the height between cell
    text wrapped to two lines (A, B), that single row and the next position; otherwise None (conditions in
    `_with_centred_wrapped_cells`)."""
    if not (index + 2 < len(rows) and len(rows[index]) == 1 and len(rows[index + 2]) == 1 and len(rows[index + 1]) >= 2):
        return None
    first, middle, second = rows[index][0], rows[index + 1], rows[index + 2][0]
    centre = (middle[0].top + middle[0].bottom) / 2
    if not (abs(first.x0 - second.x0) <= _TABLE_ROW_TOLERANCE and 0 <= second.top - first.bottom <= first.height / 2 + 0.5
            and first.top < middle[0].top < second.top
            and abs((first.top + second.bottom) / 2 - centre) <= first.height * _CENTRED_CELL_RATIO
            and not any(line.x0 < max(first.x1, second.x1) and min(first.x0, second.x0) < line.x1 for line in middle)):
        return None
    cell = _Line(top=middle[0].top, bottom=middle[0].bottom, x0=min(first.x0, second.x0), x1=max(first.x1, second.x1),
                 text=f"{first.text}<br>{second.text}", size=first.size, joined=(*first.joined, first, *second.joined, second))
    return sorted([cell, *middle], key=lambda line: line.x0), index + 3


def _centred_band_row(rows: list[list[_Line]], index: int, header: list[_Line]) -> tuple[list[_Line], int] | None:
    """In a borderless table, when the mutually attached lines from `rows[index]` are a single vertically centred row whose
    cells wrap to different numbers of lines, that row with the lines joined per column and the next position; otherwise
    None.

    When several cells wrap to different numbers of lines, as in a formula table, the line heights are out of step from cell
    to cell and the lines are staggered within one line height. The visual rows scattered into cell pieces, the table split
    in two, and the wrapped pieces ('함량', 'eco-cost') went to other columns (pinned by: test_pdf_round46.py::
    TestCentredFormulaRowWithCellsWrappedToDifferentHeights). A row in which only one cell wraps is handled by
    `_centred_wrapped_row`. It is accepted only when: the gaps between lines are at most half the line height or they
    overlap; there is a place where the top edges of two neighbouring lines differ by less than 0.75 times the line height (a
    stagger that the consecutive lines of one cell's text cannot produce); the pieces collected per header column do not
    overlap each other and continue at line spacing; their middles all match the middle of the whole row
    (`_CENTRED_CELL_RATIO`); there are at least `_TABLE_MIN_COLUMNS` columns; and one-line cells and multi-line cells occur
    together — otherwise they are several rows whose heights differ slightly from font to font (pinned by:
    test_pdf.py::TestRepeatedMarginsAndBorderlessTables::
    test_borderless_table_row_is_grouped_when_fonts_put_the_cells_at_different_heights).
    """
    band = [rows[index]]
    end = index + 1
    height = min(line.height for line in rows[index])
    while end < len(rows):
        floor = max(line.bottom for row in band for line in row)
        if (rows[end][0].top - floor > height / 2 + 0.5
                or any(abs(line.height - height) > height * 0.2 for line in rows[end])):
            break
        band.append(rows[end])
        end += 1
        if len(band) > _CENTRED_BAND_ROWS:
            return None
    if len(band) < 2 or not any(lower[0].top - upper[0].top < height * 0.75 for upper, lower in pairwise(band)):
        return None

    # collect the lines of cell text per column. Pieces in the same column of one visual row are one line of that cell
    # ('플라스틱 소재')
    groups: dict[int, list[list[_Line]]] = {}
    for row in band:
        pieces: dict[int, list[_Line]] = {}
        for line in row:
            pieces.setdefault(_column_of(line, header), []).append(line)
        for column, same_line in pieces.items():
            groups.setdefault(column, []).append(same_line)
    counts = {len(lines) for lines in groups.values()}
    if len(groups) < _TABLE_MIN_COLUMNS or len(counts) < 2 or min(counts) != 1:
        return None

    top, bottom = min(line.top for row in band for line in row), max(line.bottom for row in band for line in row)
    cells = []
    for column in sorted(groups):
        lines = sorted((_joined_line(same_line, " ") for same_line in groups[column]), key=lambda line: line.top)
        if any(not 0 <= lower.top - upper.bottom + 1 <= upper.height / 2 + 1.5 for upper, lower in pairwise(lines)):
            return None
        if abs((lines[0].top + lines[-1].bottom - top - bottom) / 2) > height * _CENTRED_CELL_RATIO:
            return None
        cells.append(_joined_line(lines, "<br>"))
    return sorted(cells, key=lambda line: line.x0), end


def _joined_line(lines: list[_Line], separator: str) -> _Line:
    """The line joining the pieces into one cell text (the line itself when there is only one). Pieces of the same line are
    passed left to right and wrapped lines top to bottom. The original lines go in `joined`."""
    if len(lines) == 1:
        return lines[0]
    return _Line(top=min(line.top for line in lines), bottom=max(line.bottom for line in lines), x0=min(line.x0 for line in lines),
                 x1=max(line.x1 for line in lines), text=separator.join(line.text for line in lines), size=lines[0].size,
                 joined=tuple(original for line in lines for original in (*line.joined, line)))


def _row_with_lead_lines(rows: list[list[_Line]], index: int, previous: list[_Line], header: list[_Line],
                         row_gap: float | None) -> tuple[list[_Line], int] | None:
    """In a borderless table, when the single-piece lines from `rows[index]` are the lead lines of one cell's text in the row
    right below (the upper lines of bottom-aligned wrapped cell text left on their own above the value row), the row with
    the lead lines joined onto that cell and the next position; otherwise None.

    With bottom-aligned cells, as when Excel exports to PDF, the lead line of a cell wrapped to two lines is left on its own
    above the value row, so the table split at that row and the first row of the second fragment became a header (when it
    was the first data row, the header fell off into per-cell paragraphs; pinned by: test_pdf_round45.py::
    TestWrappedRowsOfABorderlessTable). Taking lines as lead lines is the reverse view of a wrapped line of the cell text on
    the line above (`_continues_cell`): the lines are clearly closer to each other and to the row below than the gap between
    rows (before the first data row, the gap to the row above), they are that far from the row above, and they overlap the
    cell in the same column of the row below horizontally without reaching into the column on the right (`_wraps_into`).
    After a row with a number cell, when the row below has no number cell it may be a caption above a new table's header and
    is not joined (the same idea as `_is_section_row`).
    """
    lead = [rows[index][0]]
    end = index + 1
    while end < len(rows) and len(rows[end]) == 1 and rows[end][0].top - lead[-1].bottom <= lead[-1].height / 2 + 0.5:
        lead.append(rows[end][0])
        end += 1
    if end >= len(rows) or len(rows[end]) < 2:
        return None
    row = rows[end]
    if _has_number_cell(previous) and not _has_number_cell(row):
        return None

    above = lead[0].top - max(line.bottom for line in previous)
    inner = [lower.top - upper.bottom for upper, lower in pairwise(lead)] + [min(line.top for line in row) - lead[-1].bottom]
    reference = above if row_gap is None else min(row_gap, above)
    height = lead[0].height
    if min(inner) < 0 or max(inner) > reference * _WRAP_GAP_RATIO or reference - max(inner) < height * _WRAP_GAP_MIN_RATIO:
        return None
    if any(abs(line.x0 - lead[0].x0) > _TABLE_ROW_TOLERANCE for line in lead):
        return None

    columns = [_column_of(cell, header, row) for cell in row]
    for line in lead:
        candidates = {_column_of(line, header)} | {
            column for cell, column in zip(row, columns, strict=True) if abs(cell.x0 - line.x0) <= _TABLE_ROW_TOLERANCE}
        if not any(_wraps_into(line, row, columns, header, column) for column in candidates):
            return None

    target = next(cell for cell in row if cell.x0 < lead[0].x1 and lead[0].x0 < cell.x1)
    cell = _Line(top=target.top, bottom=target.bottom, x0=min(target.x0, *(line.x0 for line in lead)),
                 x1=max(target.x1, *(line.x1 for line in lead)), text="<br>".join([*(line.text for line in lead), target.text]),
                 size=target.size, joined=(*(original for line in lead for original in (*line.joined, line)), *target.joined, target))
    return sorted([cell if line is target else line for line in row], key=lambda line: line.x0), end + 1


def _in_the_band_of(lines: list[_Line], row: list[_Line], bands: list[tuple[float, float, float, float]]) -> bool:
    """Whether `lines` (the wrapped lines below the last row) are inside the same shaded band as `row`: there is a filled
    rectangle that covers the top of the row and the bottom of the lines and covers the row's horizontal range.

    In a striped table whose whole rows are shaded the fill tells the extent of a row, yet the wrapped line of the last row
    ('운영') had no following row below and became a paragraph under the table (pinned by:
    test_pdf_round45.py::TestWrappedLastRowOfAStripedTable). In a table without fill it stays a line under the table as
    before (accepted gap)."""
    if not lines or not bands:
        return False
    top, bottom = min(line.top for line in row), max(line.bottom for line in lines)
    left, right = min(line.x0 for line in (*row, *lines)), max(line.x1 for line in (*row, *lines))
    return any(band_top - 1 <= top and bottom <= band_bottom + 1 and band_left - 1 <= left and right <= band_right + 1
               for band_top, band_bottom, band_left, band_right in bands)


def _aligned_in_another_row(line: _Line, row: list[_Line], run: list[list[_Line]]) -> bool:
    """Whether another row has a fragment whose left edge, right edge or centre matches within `_TABLE_ROW_TOLERANCE` (column alignment)."""
    return any(min(abs(other.x0 - line.x0), abs(other.x1 - line.x1), abs(other.x0 + other.x1 - line.x0 - line.x1) / 2)
               <= _TABLE_ROW_TOLERANCE for others in run if others is not row for other in others)


def _column_table_runs(page: _Page, runs: list[list[list[_Line]]], columns: tuple[float, float],
                       max_cell: float) -> list[list[list[_Line]]]:
    """Borderless tables found on a two-column page (`columns` is the left column end and right column start from
    `_table_columns`) by grouping rows only from the lines inside one page column (excluding the lines of the tables
    already found, `runs`).

    When rows are grouped from the lines at the same height across the whole page, a body line of the neighbouring
    column slips into every row of a table that sits in one column, the row fails the 'short fragment' criterion, and
    the table scattered into one paragraph per cell (Pinned by: test_pdf.py::TestBorderlessTableInOneColumn). Inside a
    column the table criteria (`_table_runs`) are unchanged.
    """
    left_end, right_start = columns
    # The original lines of a cell joined from several lines are in a table too. Left in `rest`, the lines of a row whose
    # cells wrap to several lines (`_centred_band_row`) made a second table inside the column and came out twice
    # (Pinned by: test_audit_pdf_tail.py::TestJoinedCellLinesOnATwoColumnPage::
    # test_lines_of_a_joined_row_are_not_a_second_table).
    in_table = {id(original) for run in runs for row in run for line in row for original in (line, *line.joined)}
    rest = [line for line in page.lines if id(line) not in in_table]
    found: list[list[list[_Line]]] = []
    for side in ([line for line in rest if line.x1 <= right_start], [line for line in rest if line.x0 >= left_end]):
        found += [_regrouped_half_rows(run, max_cell) for run in _table_runs(_visual_rows(side), max_cell)]
    return found


def _mixes_two_columns(run: list[list[_Line]], left_end: float, right_start: float) -> bool:
    """Whether a table found across the whole page (`run`) mixes the rows of two tables, one in each column of a
    two-column page: lines exist in both columns with none spanning the gap between them, and some row has lines in
    only one column (the row heights of the two tables are out of step).

    With one table in each column, rows of the two tables at the same height were grouped into one row and mixed like
    '공문 | 71.2 | … | 형식 | 평균 | …', and the header rows, whose columns did not line up, scattered outside the
    table. Such a table is dropped and searched again inside each column (`_column_table_runs`, Pinned by: test_pdf.py::
    TestBorderlessTableInOneColumn::test_tables_in_both_columns_under_a_centred_title_keep_their_own_rows). A
    single-column table that spans both columns has cells in both columns in every row, or a cell spanning the gap, so
    it is left alone (Deliberate — two side-by-side tables whose row heights are also identical cannot be separated).
    """
    def side(line: _Line) -> str:
        if line.x1 <= right_start:
            return "left"
        return "right" if line.x0 >= left_end else "across"

    sides = [{side(line) for line in row} for row in run]
    if any("across" in row for row in sides) or not set().union(*sides) >= {"left", "right"}:
        return False
    return any(len(row) == 1 for row in sides)


def _table_columns(page: _Page) -> tuple[float, float] | None:
    """The (left column end, right column start) of the two page columns that `_column_table_runs` looks at. None if
    the page is not two-column.

    Lines wider than a table cell and at most half the page width are split at the page centre into two columns; the
    lines of each column must lie beside the other (within the height from the top to the bottom of the other column,
    `_beside_each_other`), at least `_COLUMN_LINES_MIN` per column. On a single-column page the last lines of
    paragraphs (left) and the wide '비고' (remarks) cells of a table below (right) were judged to be two columns, and
    the '비고' column broke away from the table (Pinned by: test_pdf.py::TestBorderlessTableInOneColumn::
    test_paragraph_ends_above_a_table_with_a_wide_note_column_are_not_two_columns).

    If the left column end still passes the right column start, lines spanning the page centre (centred title, authors,
    figure caption) are removed and the measurement is repeated. A centred line above the two columns or in the middle
    of the page was counted as a right-column line, the two-column verdict switched off, and the table inside a column
    scattered (Pinned by: test_table_in_a_column_under_a_centred_title_or_caption_is_a_table,
    test_table_in_a_column_below_a_figure_caption_in_the_middle_of_the_page_is_a_table). A page that works with the
    first measurement uses that value — so that the column lines of a two-column page whose gap is off the page centre
    are not removed as spanning lines.
    """
    wide = page.width * _TABLE_CELL_MAX_RATIO
    body = [line for line in page.lines if wide <= line.x1 - line.x0 <= page.width / 2]
    lefts = [line for line in body if line.x0 + line.x1 < page.width]
    rights = [line for line in body if line.x0 + line.x1 >= page.width]
    if min(len(lefts), len(rights)) < _COLUMN_LINES_MIN:
        return None
    lefts, rights = _beside_each_other(lefts, rights), _beside_each_other(rights, lefts)
    if min(len(lefts), len(rights)) < _COLUMN_LINES_MIN:
        return None

    left_end, right_start = max(line.x1 for line in lefts), min(line.x0 for line in rights)
    if left_end < right_start:
        return left_end, right_start

    # Remove the lines spanning the page centre and measure again
    middle = page.width / 2
    lefts = [line for line in lefts if line.x1 <= middle]
    rights = [line for line in rights if line.x0 >= middle]
    if min(len(lefts), len(rights)) < _COLUMN_LINES_MIN:
        return None
    left_end, right_start = max(line.x1 for line in lefts), min(line.x0 for line in rights)
    return (left_end, right_start) if left_end < right_start else None


def _regrouped_half_rows(run: list[list[_Line]], max_cell: float) -> list[list[_Line]]:
    """If two neighbouring rows of a table grouped by top coordinate (`run`) overlap vertically (cells of the same row
    split into halves by a difference in font height), regroup the table's lines by vertical overlap (`staggered` of
    `_visual_rows`) and replace it with the single table found. If the regrouped result is not one table made of all
    of those lines, leave it as is.

    In a table whose number cells and cells starting with a digit ('1차') sat a few pt higher than the Hangul cells,
    '1차·날짜' and '담당 부서·대상' became different rows, yet the half rows also satisfied the table criteria, so the
    second search that regroups by overlap (only lines not in a table) never ran (Pinned by:
    test_pdf.py::TestRepeatedMarginsAndBorderlessTables::test_borderless_table_split_into_half_rows_by_font_heights_is_regrouped).
    """
    def overlapping(upper: list[_Line], lower: list[_Line]) -> bool:
        top, bottom = min(line.top for line in lower), max(line.bottom for line in upper)
        height = min(min(line.height for line in upper), min(line.height for line in lower))
        return bottom - top >= height * _ROW_OVERLAP_RATIO

    if not any(overlapping(upper, lower) for upper, lower in zip(run, run[1:], strict=False)):
        return run

    lines = [line for row in run for line in row]
    regrouped = _table_runs(_visual_rows(lines, staggered=True), max_cell)
    if len(regrouped) != 1 or sum(len(row) for row in regrouped[0]) != len(lines):
        return run
    return regrouped[0]


def _table_runs(rows: list[list[_Line]], max_cell: float,
                bands: list[tuple[float, float, float, float]] = ()) -> list[list[list[_Line]]]:
    """The groups of rows in `rows` (top to bottom) that meet the borderless table criteria.

    A data row below the header may have one wide cell — when that cell fits inside its own column
    (`_wide_cell_in_its_column`). Such a row is not counted towards the minimum number of table rows (same as a section
    row). The minimum must be met, as before, by rows with short cells only (Deliberate, the conservative criterion is
    unchanged). In a Chrome table whose '과제명' (task name) column, 45% of the page width, wrapped onto two lines, the
    table was cut at every wide-cell row and the rows were mixed column by column (Pinned by:
    test_pdf_table_columns.py::TestWideWrappedFirstColumn).

    `bands` are the filled rectangles of the page (`_Page.bands`). A wrapped line below the last row is joined only
    when it lies in the same filled band as that row (`_in_the_band_of`).
    """
    def short_and_separate(row: list[_Line]) -> bool:
        return all(line.x1 - line.x0 <= max_cell for line in row) and _is_separate(row)

    def gap_below(previous: list[_Line], row: list[_Line]) -> float:
        return row[0].top - max(line.bottom for line in previous)

    def accepts(row: list[_Line], previous: list[_Line], header: list[_Line], lead: float = 0.0) -> bool:
        # A row after the first data row with fewer fragments than the minimum number of table columns must match the
        # previous row in font size. The header may be larger than the data, so the first data row is not checked, and
        # neither is a row with all fragments present (an OCR text layer varies in size from row to row, which cut the
        # rows of a price list). `lead` is the height of the leading lines of cell text placed separately above the
        # row (`_row_with_lead_lines`). The gap between rows is measured from above those lines
        columns = [_column_of(line, header, row) for line in row]
        return (len(row) >= 2 and (short_and_separate(row) or _wide_cell_in_its_column(row, header, max_cell))
                and (len(set(columns)) == len(columns) or _spread_over_one_cell(row, columns, header))
                and gap_below(previous, row) - lead <= previous[0].height * 2.5
                and (previous is header or len(row) >= _TABLE_MIN_COLUMNS or _sizes_match(row, previous))
                and (len(run) < 3 or not _out_of_line(row, run, header)))

    runs: list[list[list[_Line]]] = []
    index = 0
    taken = 0  # Position after the rows already grouped into a table. Not crossed when extending upwards
    while index < len(rows):
        header = rows[index]
        if len(header) < _TABLE_MIN_COLUMNS or not short_and_separate(header):
            index += 1
            continue
        run = [header]
        following = index + 1
        row_gap: float | None = None  # Gap between the rows of this table (the smallest). Tells wrapped cell lines apart
        sections = 0  # Number of section rows without values. Not counted towards the minimum number of table rows
        full_wide = 0  # Rows with a wide cell that still fill the minimum number of table columns (also in `sections`)
        full_short = 0  # Data rows that fill the minimum number of table columns with short cells only
        centred_band = False  # Whether there is a centred row whose cells wrapped onto differing line counts (`_centred_band_row`)
        while following < len(rows):
            row, previous = rows[following], run[-1]
            if len(row) == 1 and row_gap is not None:
                # Wrapped lines of cell text (there may be several) are joined to the cell only when more table rows
                # follow below. A short note set right below the last row ('* 잠정치', '출처: 인사팀') was attached to
                # the first cell
                # The bottom of the growing row is carried along and the columns of its cells are worked out from what
                # is already known about each cell (`known`, `_cell_columns`): measuring both again for every wrapped
                # line took time cubic in the number of lines under one row (Pinned by: test_audit_pdf_tail.py::
                # TestManyWrappedLinesUnderOneRow).
                merged, ahead = previous, following
                floor, known = max(line.bottom for line in merged), {}
                while (ahead < len(rows) and len(rows[ahead]) == 1
                       and _continues_cell(rows[ahead][0], merged, header, rows[ahead][0].top - floor, row_gap, known)):
                    floor = max(floor, rows[ahead][0].bottom)
                    merged, ahead = [*merged, *rows[ahead]], ahead + 1
                if ahead > following and ((ahead < len(rows) and accepts(rows[ahead], merged, header))
                                          or _in_the_band_of(merged[len(previous):], previous, bands)):
                    run[-1], following = merged, ahead
                    continue
            band = _centred_band_row(rows, following, header)
            if band is not None and accepts(band[0], previous, header, band[0][0].top - min(line.top for line in row)):
                run.append(band[0])
                centred_band = True
                gap = min(line.top for line in row) - max(line.bottom for line in previous)
                row_gap = gap if row_gap is None else min(row_gap, gap)
                following = band[1]
                continue
            if len(row) == 1:
                # In a row whose cell text wraps onto two lines while the other cells are vertically centred or bottom-aligned,
                # the leading line (and, when centred, the trailing line) stands apart
                wrapped = (_centred_wrapped_row(rows, following)
                           or _row_with_lead_lines(rows, following, previous, header, row_gap))
                if wrapped is not None and accepts(wrapped[0], previous, header, wrapped[0][0].top - row[0].top):
                    run.append(wrapped[0])
                    sections += not short_and_separate(wrapped[0])
                    full_wide += not short_and_separate(wrapped[0]) and len(wrapped[0]) >= _TABLE_MIN_COLUMNS
                    gap = row[0].top - max(line.bottom for line in previous)
                    row_gap = gap if row_gap is None else min(row_gap, gap)
                    following = wrapped[1]
                    continue
            if (len(row) == 1 and following + 1 < len(rows) and accepts(rows[following + 1], row, header)
                    and (_is_section_row(row[0], previous, rows[following + 1], header, row_gap, max_cell)
                         or _is_lone_value_row(row[0], previous, rows[following + 1], header, row_gap, max_cell))):
                run.append(row)
                following += 1
                sections += 1
                continue
            if not accepts(row, previous, header):
                break
            run.append(row)
            sections += not short_and_separate(row)  # A wide-cell row is not counted towards the minimum either
            full_wide += not short_and_separate(row) and len(row) >= _TABLE_MIN_COLUMNS
            full_short += short_and_separate(row) and len(row) >= _TABLE_MIN_COLUMNS
            gap = gap_below(previous, row)
            row_gap = gap if row_gap is None else min(row_gap, gap)
            following += 1
        # A wide-cell row with all cells filled counts towards the minimum only when at least one data row fills all
        # cells with short cells only. In a task table of two or three rows where rows with a wrapped task name and
        # long rows were mixed, there was only one short row, so the table was not found and scattered column by column
        # (owner and due date ended up after the paragraph below the table, Pinned by:
        # test_pdf_round46.py::TestSmallTableWithAWrappedCellAndAWideCell). Not counted when the only short rows have
        # cells missing — the '가. 일 시 …' item lines of an official letter looked like three cells because of the
        # letter-spaced item names and became a table (test_item_lines_of_an_official_letter_stay_lines).
        # Counted only when the table is closed both above and below by a single-fragment line (title, paragraph) or a
        # wide gap — taking the top part, cut at a row whose cells are merged differently, as a table first blocked
        # the path that splits that row and searches the whole table again (`_split_merged_cells`), breaking up a
        # large table (test_top_of_a_table_cut_at_a_row_merged_differently_is_not_taken), and a piece open at the top
        # took the wrapped second line of cell text as its header, scattering the rows above into paragraphs
        # (test_wrapped_rows_of_a_top_aligned_table_are_not_cut_at_a_second_line).
        # A centred staggered row is itself evidence of a table, so the header and that row alone make a table
        # (header + one formula row)
        counted = len(run) - sections
        closed = (following < len(rows) and len(rows[following]) == 1
                  and gap_below(run[-1], rows[following]) <= run[-1][0].height * 2.5
                  and (index == 0 or len(rows[index - 1]) == 1 or gap_below(rows[index - 1], header) > header[0].height * 2.5))
        widened = full_short and closed and counted + full_wide >= _TABLE_MIN_ROWS
        if counted < _TABLE_MIN_ROWS and (counted < 2 or not (widened or centred_band)):
            index += 1
            continue

        above = _table_rows_above(rows[taken:index], run, max_cell)
        corner = None if above or index <= taken else _blank_corner_header(rows[index - 1], run, max_cell)
        runs.append([*above, *([corner] if corner else []), *run])
        index = taken = following
    return runs


def _blank_corner_header(row: list[_Line], run: list[list[_Line]], max_cell: float) -> list[_Line] | None:
    """If `row`, right above a found borderless table (`run`), is a header row whose top-left cell is blank, that row
    (with an empty cell fragment inserted at the front); otherwise None.

    When the header of the first column is left blank, as in a quarterly table, the header has one fragment fewer than
    the data rows and did not fit the table's columns. The header fell apart into one paragraph per cell and the first
    data row became the header (Pinned by: test_pdf_table_columns.py::TestBorderlessTableWithBlankCorner). Accepted
    only: a row with one fragment fewer than the table's first row, all short, starting after the first-column cell
    ends, each fragment matching in turn, from the second cell of the first row, by left edge, right edge or centre
    (`_TABLE_ROW_TOLERANCE`), and attached at row spacing.
    """
    first = run[0]
    if len(row) != len(first) - 1 or len(row) < 2 or not _is_separate(row) or any(line.x1 - line.x0 > max_cell for line in row):
        return None
    if row[0].x0 <= first[0].x1 or first[0].top - max(line.bottom for line in row) > row[0].height * 2.5:
        return None
    if not all(min(abs(cell.x0 - line.x0), abs(cell.x1 - line.x1), abs(cell.x0 + cell.x1 - line.x0 - line.x1) / 2)
               <= _TABLE_ROW_TOLERANCE for line, cell in zip(row, first[1:], strict=True)):
        return None
    corner = first[0]
    return [_Line(top=row[0].top, bottom=row[0].bottom, x0=corner.x0, x1=corner.x0, text="", size=row[0].size), *row]


def _wide_cell_in_its_column(row: list[_Line], header: list[_Line], max_cell: float) -> bool:
    """Whether the fragments of the row do not overlap, exactly one exceeds the maximum table cell width, and that
    fragment fits inside its own header column: it ends before the next header cell starts and starts after the
    previous header cell ends (`_table_runs`). A wide fragment that crosses columns (a body line, a fragment merging
    two cells) is not accepted."""
    wide = [line for line in row if line.x1 - line.x0 > max_cell]
    if len(wide) != 1 or not _is_separate(row):
        return False
    column = _header_column(wide[0], header)
    return ((column + 1 >= len(header) or wide[0].x1 < header[column + 1].x0)
            and (column == 0 or wide[0].x0 > header[column - 1].x1))


def _spread_over_one_cell(row: list[_Line], columns: list[int], header: list[_Line]) -> bool:
    """Whether the several fragments in the row's first column (the item-name column; `columns` is the column of each
    fragment) are the text of one cell spread across the cell width by justification ('운영      경비'): apart from
    the first column there is one fragment per column, the first-column fragments are all short Hangul/Hanja words
    (`_SPREAD_WORD`), and they end before the next header cell starts. The header had one column too few and the
    header row fell out of the table (Pinned by: test_pdf_forms_and_amounts.py::
    TestDistributedLabelsInABorderlessTable). The cell text is joined with spaces between fragments of the same row
    (`_extract_aligned_tables`). Restricting this to the first column and requiring it to end before the next header
    cell is deliberate — a text cell in a column with a blank header stuck to the cell of the neighbouring column and
    the row was mashed together (generated borderless text table).
    """
    first = [line for line, column in zip(row, columns, strict=True) if column == 0]
    if len(first) < 2 or len(header) < 2 or len(set(columns)) != len(columns) - len(first) + 1:
        return False
    return all(_SPREAD_WORD.match(line.text) for line in first) and first[-1].x1 < header[1].x0


def _sizes_match(row: list[_Line], previous: list[_Line]) -> bool:
    """Whether the font sizes of two rows (median of the fragment sizes) are within `_TABLE_SIZE_RATIO`. A row of
    unknown size (0) is treated as matching.

    The two fragments of the left and right section headings (larger type) of a two-column band below a table were
    attached as the last row of the table, giving '| 3. 결과 | | 4. 결론 |'
    (Pinned by: test_pdf.py::TestBorderlessTableEnd::test_headings_of_two_columns_below_a_table_are_not_a_table_row).
    Not applied to rows with at least the minimum number of table columns (`_table_runs`) — the price list of an OCR
    book, whose font size varies from row to row, was cut
    (Pinned by: test_pdf.py::TestBorderlessTableEnd::test_full_rows_of_varying_font_size_stay_in_the_table).
    """
    sizes = [statistics.median(line.size for line in lines) for lines in (row, previous)]
    return min(sizes) <= 0 or max(sizes) <= min(sizes) * _TABLE_SIZE_RATIO


def _continues_cell(line: _Line, previous: list[_Line], header: list[_Line], gap: float, row_gap: float,
                    known: dict | None = None) -> bool:
    """Whether a single-fragment line (`line`) is the wrapped continuation of cell text in the row right above (`previous`).

    When cell text wraps onto two lines, the wrapped line becomes a single-fragment row, the table is cut there, and if
    fewer than 3 rows remain the values scattered one per line ('Ⅷ. 법인세비용차감전순이익(손실)' in an income
    statement). It is treated as a wrapped line only when it sits clearly closer than the gap between rows, both in
    ratio and in size (`_WRAP_GAP_RATIO`, `_WRAP_GAP_MIN_RATIO`), overlaps the cell above in the same column
    horizontally, has the same size, and does not reach the cell of a column to the right. A single-fragment line at
    ordinary row spacing (a note below the table, a category row) ends the table as before. A row of two or more
    fragments is a separate row as before (Pinned by: test_pdf.py::TestBorderlessTableWithWrappedCell).

    For a cell of the row above, the column is the one decided by looking at the fragments of the same row
    (`_column_of`); for the wrapped line, if the column decided from the header positions differs from the cell above,
    the column of a cell above whose left edge matches is also tried — a wrapped line ('회의실') lying in the gap
    before a centred header was counted as the first column and the table was cut
    (Pinned by: test_pdf_table_columns.py::TestCentredHeadersOverLeftAlignedCells::
    test_wrapped_line_of_a_cell_in_the_gap_before_a_centred_header_stays_in_the_cell).

    `known` is what a caller that asks about one growing row again and again keeps between calls (`_cell_columns`).
    """
    if not 0 <= gap <= row_gap * _WRAP_GAP_RATIO or row_gap - gap < line.height * _WRAP_GAP_MIN_RATIO:
        return False

    columns = [_column_of(cell, header, previous) for cell in previous] if known is None else _cell_columns(previous, header, known)
    candidates = {_column_of(line, header)} | {
        column for cell, column in zip(previous, columns, strict=True) if abs(cell.x0 - line.x0) <= _TABLE_ROW_TOLERANCE}
    return any(_wraps_into(line, previous, columns, header, column) for column in sorted(candidates))


def _cell_columns(row: list[_Line], header: list[_Line], known: dict) -> list[int]:
    """The same list as `[_column_of(cell, header, row) for cell in row]` for a row that grows: `known` (empty at first)
    keeps what was worked out in the previous call, and `row` is the row of that call with cells added at the end.

    `_column_of` looks at the other cells of the row for two things only: whether an earlier cell occupies the header
    column of the cell (`taken` in `_gap_column` — true exactly when one of the two cells of that header column that end
    leftmost, the cell itself aside, ends before the cell starts), and whether another cell goes to the same column under
    the gap rule. So the gap rule is asked with a row of just that leftmost cell and the cell itself, again only for the
    header column in which an added cell became one of the two leftmost, and the second question is answered by
    counting. Asking `_column_of` for every cell of the row again for every further wrapped line took tens of seconds
    with several hundred lines under one row (Pinned by: test_audit_pdf_tail.py::TestManyWrappedLinesUnderOneRow; the
    equality with `_column_of` by test_columns_of_a_growing_row_are_those_of_column_of).
    """
    heads: list[int] = known.setdefault("heads", [])  # `_header_column` of each cell
    gaps: list[int] = known.setdefault("gaps", [])  # `_gap_column` of each cell in this row
    leftmost: dict[int, list[_Line]] = known.setdefault("leftmost", {})  # header column → the two cells ending leftmost
    seen = len(heads)
    moved: set[int] = set()  # header columns whose leftmost cells changed: `taken` may have changed for their cells
    for cell in row[seen:]:
        heads.append(_header_column(cell, header))
        gaps.append(heads[-1])
        ends = leftmost.setdefault(heads[-1], [])
        if len(ends) < 2 or cell.x1 < max(other.x1 for other in ends):
            if len(ends) == 2:
                ends.remove(max(ends, key=lambda other: other.x1))
            ends.append(cell)
            moved.add(heads[-1])
    for index in range(0 if moved else seen, len(row)):
        cell, head = row[index], heads[index]
        if index >= seen or head in moved:
            earlier = next((other for other in leftmost[head] if other is not cell and other.x1 <= cell.x0), None)
            gaps[index] = _gap_column(cell, header, [cell] if earlier is None else [earlier, cell], head)
    shared = Counter(gaps)
    return [head if shared[gap] > 1 else gap for head, gap in zip(heads, gaps, strict=True)]


def _wraps_into(line: _Line, previous: list[_Line], columns: list[int], header: list[_Line], column: int) -> bool:
    """Whether `line` overlaps the cell in column `column` of the row above (`previous`, with the column of each
    fragment in `columns`) horizontally, has the same size, and does not reach a column to the right (`_continues_cell`)."""
    later = [cell.x0 for index, cell in enumerate(header) if index > column]
    later += [cell.x0 for cell, other in zip(previous, columns, strict=True) if other > column]
    if later and line.x1 > min(later):
        return False
    return any(other == column and cell.x0 < line.x1 and line.x0 < cell.x1 and abs(cell.size - line.size) <= 0.5
               for cell, other in zip(previous, columns, strict=True))


def _is_section_row(line: _Line, previous: list[_Line], following: list[_Line], header: list[_Line],
                    row_gap: float | None, max_cell: float) -> bool:
    """Whether a single-fragment line (`line`) is a section row without values ('자산', '부채') inside a borderless table.

    The section rows of a balance sheet cut the table, so the header fell apart into one paragraph per cell, and a
    subtotal row with an empty note cell ('Ⅰ. 유동자산') could not be a header candidate, so the amounts scattered to
    the end of the page
    (Pinned by: test_pdf.py::TestBorderlessTableEnd::test_section_rows_without_values_stay_in_the_table).
    It is treated as a section row only when it is short and lies within the first column (ends before the cells of
    the second column start), matches the following row in font size (`_sizes_match` — in an equation document a
    subheading between rows that happened to look like a table was sucked into the table), and the gaps above and
    below are at most `_SECTION_ROW_GAP_RATIO` times the gap between the rows of this table (before the first data
    row, the smaller of the gaps above and below). Whether the following row continues the table is checked by the
    caller, and a section row is not counted towards the minimum number of table rows (in an equation document a
    subheading between two rows that happened to line up made a 3-row table).
    A caption set further apart between tables is not a section row
    (Pinned by: test_short_line_set_apart_between_two_tables_is_not_a_section_row).
    After a row with a numeric value cell, if the following row has no numeric value cell at all, that row is the
    header of a new table, so the line is not accepted as a section row — in a document with tight paragraph spacing a
    caption, note or sentence between two tables sat at exactly the row spacing and the two tables became one
    (Pinned by: test_short_line_at_row_spacing_before_the_header_of_a_next_table_is_not_a_section_row). A table whose
    values are all text cannot use this check.
    """
    later = [cell.x0 for cell in (*header, *previous) if _column_of(cell, header) > 0]
    if (line.x1 - line.x0 > max_cell or _column_of(line, header) != 0 or not later or line.x1 >= min(later)
            or not _sizes_match([line], following)):
        return False
    if _has_number_cell(previous) and not _has_number_cell(following):
        return False
    return _at_row_spacing(line, previous, following, row_gap)


def _is_lone_value_row(line: _Line, previous: list[_Line], following: list[_Line], header: list[_Line],
                       row_gap: float | None, max_cell: float) -> bool:
    """Whether a single-fragment line (`line`) is a row inside a borderless table with only one cell filled, not in the
    first column (a subtotal amount).

    A value in the first column was accepted as a section row (`_is_section_row`), but a single value in the second
    column or later cut the table, so the amount fell out as a paragraph and the first data row of the second piece
    became a header (Pinned by: test_pdf_round45.py::TestSubtotalRowInsideABorderlessTable). Accepted only when it is
    short, matches a cell of the same column in the header or the row above or below by left edge, right edge or
    centre (a cell placed in its own column), matches the following row in font size, and the gaps above and below are
    at row spacing (`_at_row_spacing`). If the row after a number cell has no number cell, it is the header of a new
    table and is not accepted (same as a section row).
    The header is also checked because a subtotal in a column that is empty in every other row (비고, remarks) found no
    cell to align with above or below and cut the table (Pinned by:
    test_pdf_round46.py::TestLoneSubtotalInAnOtherwiseEmptyLastColumn).
    """
    column = _column_of(line, header)
    if line.x1 - line.x0 > max_cell or column == 0 or not _sizes_match([line], following):
        return False
    if _has_number_cell(previous) and not _has_number_cell(following):
        return False
    if not any(_column_of(cell, header, row) == column and _edge_aligned(cell, line)
               for row in (header, previous, following) for cell in row):
        return False
    return _at_row_spacing(line, previous, following, row_gap)


def _at_row_spacing(line: _Line, previous: list[_Line], following: list[_Line], row_gap: float | None) -> bool:
    """Whether the gaps above and below `line` are at most `_SECTION_ROW_GAP_RATIO` times the gap between the table's
    rows (before the first data row, the smaller of the gaps above and below)."""
    gaps = (line.top - max(cell.bottom for cell in previous), min(cell.top for cell in following) - line.bottom)
    reference = min(gaps) if row_gap is None else row_gap
    return min(gaps) >= 0 and max(gaps) <= reference * _SECTION_ROW_GAP_RATIO + 1


def _edge_aligned(line: _Line, other: _Line) -> bool:
    """Whether the left edges, right edges or centres of two fragments match within `_TABLE_ROW_TOLERANCE`."""
    return min(abs(line.x0 - other.x0), abs(line.x1 - other.x1), abs(line.x0 + line.x1 - other.x0 - other.x1) / 2) <= _TABLE_ROW_TOLERANCE


def _out_of_line(row: list[_Line], run: list[list[_Line]], header: list[_Line]) -> bool:
    """Whether `row` is a row of another table that does not fit the columns of the borderless table being built
    (`run`, a header and two or more data rows): it has two or more fragments outside the first column and none of
    them matches any cell of the same column in the data rows by left edge, right edge or centre (`_edge_aligned`).

    Two borderless tables with different column counts, set one row spacing apart, were merged into one table and the
    cells of the second landed in the wrong columns ('| 부서 | 인원 | | 비고 |', Pinned by:
    test_pdf_round45.py::TestBorderlessTablesWithDifferentColumnsNextToEachOther). A centred header does not match
    left-aligned cells, so the header is left out of the reference, and the check runs only when there are two or more
    reference data rows (the first column is not checked because it often holds indented sub-items).
    The reference is the `_OUT_OF_LINE_ROWS` data rows right above — looking at the whole table for every row takes
    quadratic time on a long table."""
    later = [line for line in row if _column_of(line, header, row) > 0]
    if len(later) < 2:
        return False
    placed: dict[int, list[_Line]] = {}
    for other in run[max(1, len(run) - _OUT_OF_LINE_ROWS):]:
        for line in other:
            placed.setdefault(_column_of(line, header, other), []).append(line)
    return not any(_edge_aligned(line, other) for line in later for other in placed.get(_column_of(line, header, row), ()))


def _has_number_cell(row: list[_Line]) -> bool:
    """Whether the row has a cell holding only a numeric value."""
    return any(_NUMBER_CELL.match(line.text.strip()) for line in row)


def _is_separate(row: list[_Line]) -> bool:
    """Whether the fragments of the row do not overlap horizontally."""
    return all(right.x0 >= left.x1 - 1 for left, right in zip(row, row[1:], strict=False))


def _has_one_wide_cell_at_most(row: list[_Line], max_cell: float) -> bool:
    """Whether the fragments can be cells: they do not overlap and at most one exceeds the maximum table cell width
    (one wide cell such as 품명 (item name) or 비고 (remarks))."""
    return _is_separate(row) and sum(1 for line in row if line.x1 - line.x0 > max_cell) <= 1


def _table_rows_above(above: list[list[_Line]], run: list[list[_Line]], max_cell: float) -> list[list[_Line]]:
    """The rows of the same table that continue right above a table found with the conservative criteria (`run`) — the
    tail of `above`. [] if there are none.

    In rows where one cell (품명, item name) is wide the 'short fragment' criterion breaks, so the top part of the table
    scattered column by column and the bottom part, made of short rows only, became the table, turning a data row in
    the middle into the header. The criterion itself is left as is (Deliberate, conservative); only the rows above that
    are attached to a table found with it are accepted: they continue at row spacing, have two or more fragments and at
    most one wide fragment. The topmost row, which becomes the new header, must meet the old criterion unchanged (3 or
    more short fragments), and the fragments of every row below it must fall into different columns of that header
    (Pinned by:
    test_pdf.py::TestBorderlessTableWithWideCells::test_table_found_among_short_rows_starts_at_the_header_above_the_wide_rows).
    """
    found = 0  # Number of rows to accept (from the end of `above`)
    below = run[0]
    for number, row in enumerate(reversed(above), start=1):
        gap = below[0].top - max(line.bottom for line in row)
        if len(row) < 2 or not _has_one_wide_cell_at_most(row, max_cell) or gap > row[0].height * 2.5:
            break
        is_header = len(row) >= _TABLE_MIN_COLUMNS and all(line.x1 - line.x0 <= max_cell for line in row)
        if is_header and all(len({_column_of(line, row) for line in lower}) == len(lower)
                             for lower in [*above[len(above) - number + 1:], *run]):
            found = number
        below = row
    return above[len(above) - found:] if found else []


def _join_row_fragments(page: _Page) -> None:
    """Join fragments that sit at the same height but were split apart by pdfminer into one line of the body flow (not a table).

    - Letter-spaced title ('제 안 서'): every character became a line, giving a run of one-character paragraphs. A row
      of same-size single Hangul/Hanja character fragments at even intervals is joined into one line without spaces.
    - 'label  value' two-column layout: pdfminer grouped the labels and the values separately, so the pairs were split
      like '연락처\n주소\n\n010-…\n서울…'. Joined into one 'label value' line only when rows of two fragments continue
      for 2 or more rows, the left fragment is short (by the table cell criterion), the starts of the two columns (for
      values the ends are accepted too) match row by row, the two columns are close (`_LABEL_GAP_RATIO`; a column of
      right-aligned numeric values may be far — `_right_aligned_numbers`), and the right column does not continue in
      the row right above or below the group.
      Even when the left column of a two-column layout has a few short lines, the right column continues above and
      below, so they are not joined. Three or more fragments use the borderless table criteria unchanged (Deliberate,
      conservative criterion). Pinned by: test_pdf.py::TestFlowJoins.
    - Groups of table-like rows that did not become a table (`_table_like_rows`): the fragments of each row are joined
      left to right into one line.
    - A page number or amount set apart from the end of the dot leader in a table of contents or price list
      (`_leader_rows`): appended to the leader line.
    - An equation and the equation number '(1)' set apart to its right (`_equation_number_rows`): appended to the equation.
    - A list number, bullet or footnote number set apart from the item text (`_marker_rows`): prepended to the first
      line of the item starting at the same height, and placed at the position of the item text.
    """
    if page.simple or len(page.lines) < 2:
        return
    rows = _visual_rows(page.lines)
    joined: list[list[_Line]] = [row for row in rows if _is_letter_spaced(row)]
    joined += _label_value_rows(rows, page.width)
    joined += _equation_number_rows(rows, {id(line) for row in joined for line in row})
    joined += _leader_rows(rows, {id(line) for row in joined for line in row})
    order = {id(line): index for index, line in enumerate(page.lines)}
    # Where to put each joined line (the position of its fragment that came first in the flow). A group of table-like
    # rows was read scattered column by column, so within a group the positions are handed out from the front in
    # top-to-bottom row order.
    places = [min(order[id(line)] for line in row) for row in joined]
    cells: set[int] = set()  # Fragments of table-like rows
    for run in _table_like_rows(rows, page.width, {id(line) for row in joined for line in row}):
        joined += run
        places += sorted(min(order[id(line)] for line in row) for row in run)
        cells.update(id(line) for row in run for line in row)
    marked = _marker_rows(page.lines, {id(line) for row in joined for line in row})
    joined += marked
    # The numbers come bunched together before the item text in the flow. Put them at the position of the item text
    places += [order[id(item)] for _, item in marked]
    markers = {id(marker) for marker, _ in marked}
    if not joined:
        return

    # Line id → the line to put in its place (None removes it)
    replaced: dict[int, _Line | None] = {id(line): None for row in joined for line in row}
    for row, place in zip(joined, places, strict=True):
        separator = "" if _is_letter_spaced(row) else " "
        # The size of a table-like row is the median of its fragments. Following the largest fragment (a big ∑ sign in
        # an equation) turned the row into a heading (public paper PDF).
        sizes = [line.size for line in row]
        size = statistics.median_low(sizes) if id(row[0]) in cells else row[-1].size if id(row[0]) in markers else max(sizes)
        replaced[id(page.lines[place])] = _Line(top=min(line.top for line in row), bottom=max(line.bottom for line in row),
                                                x0=row[0].x0, x1=max(line.x1 for line in row),
                                                text=_row_text(row, separator), size=size)
    page.lines = [replaced.get(id(line), line) for line in page.lines if replaced.get(id(line), line) is not None]


def _equation_number_rows(rows: list[list[_Line]], used: set[int]) -> list[list[_Line]]:
    """The rows of two fragments whose right fragment is an equation number ('(1)', '(2.3)', `_EQUATION_NUMBER`) lying
    more than a line height away from the left fragment (the equation) — excluding lines in `used`.

    A centred equation and the equation number at the right edge are lines beside each other, so an equation read
    after the paragraph below could not be moved up into place (a line with a line beside it is not moved,
    `_read_lone_lines_in_place`) and the numbers were grouped separately and bunched at the end of the page (Pinned
    by: test_pdf_reading_order.py::TestEquationNumbers). Joined into one line, the equation and its number go into
    place together.
    """
    return [row for row in rows if len(row) == 2 and not any(id(line) in used for line in row)
            and _EQUATION_NUMBER.match(row[1].text.strip()) and not _EQUATION_NUMBER.match(row[0].text.strip())
            and row[1].x0 - row[0].x1 > row[0].height]


def _row_text(row: list[_Line], separator: str) -> str:
    """Join the text of a row's fragments. A fragment starting below the middle of the previous one (a wrapped line of
    a value cell, `_with_wrapped_values`) is joined with a line break — line breaks inside a paragraph are not joined
    with a space (docs/decisions/pdf.md)."""
    text = row[0].text

    for previous, line in zip(row, row[1:], strict=False):
        text += ("\n" if line.top > previous.top + previous.height / 2 else separator) + line.text
    return text


def _leader_rows(rows: list[list[_Line]], used: set[int]) -> list[list[_Line]]:
    """Groups made of a line ending in a dot leader (`_LEADER_END`), the short value fragment (page number, amount)
    that ends the row right next to it (within font size × `_LEADER_GAP_RATIO`), and the fragments continuing to the
    left of the leader line within the same distance (item text set apart). Only the same row (`_visual_rows`) and the
    same font size are considered (groups containing a line in `used` are excluded). Without including the item text,
    a table of contents whose leaders are drawn separately was split into two lines, '제1장 서론' and '…… 3'
    (test_dot_leaders_drawn_apart_from_the_titles_stay_on_one_line).

    A one-digit page number in a table of contents is further from the end of the leader than twice the character
    width (pdfminer char_margin), so it was grouped separately, and in the text box flow the numbers alone bunched
    after the items as '…\n\n1\n2\n3', so one could not tell which item each page belonged to. The same happened with
    a right-aligned amount in a price list ('3,000원') (Pinned by:
    test_pdf_toc_and_checkboxes.py::TestDotLeaderPageNumbers). A value close to the leader is already joined by pdfminer.

    A single fragment that includes the page number after the leader ('서론 ……… 1', `_LEADER_TAIL`) is also treated as
    the end fragment, and joining extends to a short (at most the same width) same-size fragment set apart to the left
    within font size × `_LEADER_VALUE_MAX_RATIO` (a chapter/section number column, '제1장', '1.1') — in a table of
    contents whose number column is more than 3 times the font size away from the item text, the numbers alone bunched
    separately (Pinned by: test_pdf_toc_and_checkboxes.py::
    test_section_numbers_set_apart_from_the_titles_of_a_table_of_contents_stay_on_their_lines).
    """
    pairs = []

    for row in rows:
        if len(row) < 2:
            continue
        line, following = row[-2], row[-1]
        size = max(line.size, 1.0)
        if (_LEADER_END.search(line.text) and following.x1 - following.x0 <= size * _LEADER_VALUE_MAX_RATIO
                and abs(line.size - following.size) <= 0.5 and 0 <= following.x0 - line.x1 <= size * _LEADER_GAP_RATIO):
            start = len(row) - 2
        elif _LEADER_TAIL.search(following.text) and not _LEADER_END.search(line.text):
            start, size = len(row) - 1, max(following.size, 1.0)  # Item text, leader and page number in one fragment
        else:
            continue

        # In a table of contents with separately drawn leaders ('제1장 서론' + '……' + '3'), also join the item text
        # continuing closely to the left
        while start > 0 and 0 <= row[start].x0 - row[start - 1].x1 <= size * _LEADER_GAP_RATIO:
            start -= 1

        # A chapter/section number column set further apart ('제1장', '1.1') is also joined if short and the same size
        while (start > 0 and 0 <= row[start].x0 - row[start - 1].x1 <= size * _LEADER_VALUE_MAX_RATIO
               and row[start - 1].x1 - row[start - 1].x0 <= size * _LEADER_VALUE_MAX_RATIO
               and abs(row[start - 1].size - size) <= 0.5 and not _LEADER_TAIL.search(row[start - 1].text)):
            start -= 1

        if start < len(row) - 1 and not any(id(piece) in used for piece in row[start:]):
            pairs.append(row[start:])
    return pairs


def _marker_rows(lines: list[_Line], used: set[int]) -> list[list[_Line]]:
    """The [marker, first line] pairs of a list number or bullet set apart from its item text and the first line of
    that item (excluding lines in `used`).

    A PDF exported by LibreOffice from Word lists (List Number, List Bullet) places the numbers apart from the item
    text, so pdfminer grouped the numbers together, giving '1.\n\n2.\n\n3.\n\n<all the item text>'. The numbers in the
    footnote area behaved the same ('1\n2\n3\n\nK-IFRS …'). The line that starts close to the right of the marker
    (`_LIST_NUMBER`, `_LONE_BULLET`; within `_MARKER_GAP_RATIO` times the line height) at the same height (vertical
    centres differ by at most half the smaller line height) is the first line of that item. A bare number is accepted
    only as a footnote number: the previous or next number sits right above or below at the same left edge
    (`_numbered_in_turn`). If no line starts at the same height as the marker (figure tick labels, a number between
    lines) it is left alone (Pinned by: test_pdf.py::TestListMarkers).
    """
    def is_marker(line: _Line) -> bool:
        return bool(_LIST_NUMBER.match(line.text) or _LONE_BULLET.match(line.text) or _BARE_NUMBER.match(line.text))

    markers = [line for line in lines if id(line) not in used and is_marker(line)]
    texts = sorted((line for line in lines if id(line) not in used and not is_marker(line)) if markers else (),
                   key=lambda line: line.top)
    tops = [line.top for line in texts]
    found: list[list[_Line]] = []
    taken: set[int] = set()

    for marker in markers:
        middle = (marker.top + marker.bottom) / 2
        # Excludes lines more than 10 times the marker height
        near = texts[bisect_left(tops, middle - marker.height * 6):bisect_right(tops, middle + marker.height)]
        item = min((line for line in near if id(line) not in taken
                    and marker.x1 - 1 <= line.x0 <= marker.x1 + marker.height * _MARKER_GAP_RATIO
                    and abs((line.top + line.bottom) / 2 - middle) <= min(line.height, marker.height) / 2),
                   key=lambda line: line.x0, default=None)
        if item is None or (_BARE_NUMBER.match(marker.text) and not _numbered_in_turn(marker, lines)):
            continue
        found.append([marker, item])
        taken.add(id(item))
    return found


def _numbered_in_turn(marker: _Line, lines: list[_Line]) -> bool:
    """Whether a line right above or below the bare number `marker` at the same left edge (within 3 line heights) is,
    or starts with, the previous or next number."""
    number = int(marker.text)
    neighbors = {str(number - 1), str(number + 1)}
    return any(line is not marker and abs(line.x0 - marker.x0) <= _TABLE_ROW_TOLERANCE
               and abs(line.top - marker.top) <= marker.height * 3 and line.text.partition(" ")[0] in neighbors
               for line in lines)


def _table_like_rows(rows: list[list[_Line]], page_width: float, used: set[int]) -> list[list[list[_Line]]]:
    """Groups of rows that did not become a table but are laid out like one: rows of 3 or more fragments continue at
    row spacing for `_TABLE_MIN_ROWS` rows or more. With only two rows the cells must line up column by column
    (`_aligned_pair`).

    A table that missed the borderless table criteria because one cell is wide was grouped by pdfminer into one text
    box per column and scattered column by column like '번호 1 2 3 … 품명 … 수량 …'. It is not made into a table (the
    conservative criterion stays); each row is only joined into one line. Conditions (the same idea as 'label value'
    in `_join_row_fragments`):
    - The fragments of a row do not overlap and at most one is wide (over the maximum table cell width). Side-by-side
      body columns have two or more wide lines.
    - If the row right above or below the group has a wide line with the same left edge as a wide fragment of the
      group, the group is not joined. That is the body of the neighbouring column continuing above and below (an
      equation and equation number in the left column + a body line in the right column).
    - Below the cell rows, two-fragment 'label | value spanning several cells' rows aligned to the left edges of the
      first two cells of the first row may follow (`_spanning_row`; only when the group starts with a cell row, has
      `_TABLE_MIN_ROWS` rows or more in total, and the cell rows have no wide fragment). The '참석자 | (list of names
      spanning three cells)' row in the head of meeting minutes cut the group and the '일시·참석자·작성자' labels all
      fell to the end of the page (Pinned by: test_pdf_label_value_rows.py::TestLabelGridWithASpanningRow).
    Pinned by: test_pdf.py::TestBorderlessTableWithWideCells.
    """
    max_cell = page_width * _TABLE_CELL_MAX_RATIO

    def is_cells(row: list[_Line]) -> bool:
        return (len(row) >= _TABLE_MIN_COLUMNS and _has_one_wide_cell_at_most(row, max_cell)
                and not any(id(line) in used for line in row))

    def joins(run: list[list[_Line]], row: list[_Line]) -> bool:
        if run and not _near(run, row):
            return False
        if is_cells(row):
            return True
        # Label-value rows only below a grid whose cells are all short — with a row holding a wide fragment (a notice
        # box beside a list) the table-like shape is a coincidence
        return (bool(run) and not any(id(line) in used for line in row) and _spanning_row(row, run[0], max_cell)
                and all(line.x1 - line.x0 <= max_cell for other in run if is_cells(other) for line in other))

    found: list[list[list[_Line]]] = []
    index = 0
    while index < len(rows):
        run: list[list[_Line]] = []
        while index + len(run) < len(rows) and joins(run, rows[index + len(run)]):
            run.append(rows[index + len(run)])
        cells = [row for row in run if is_cells(row)]
        neighbors = [rows[position] for position in (index - 1, index + len(run)) if 0 <= position < len(rows)]
        beyond = {id(rows[position]): rows[position + step] for position, step in ((index - 1, -1), (index + len(run), 1))
                  if 0 <= position < len(rows) and 0 <= position + step < len(rows)}
        if (len(cells) >= _TABLE_MIN_ROWS or len(cells) < len(run) >= _TABLE_MIN_ROWS
                or (len(run) == 2 and _aligned_pair(run, neighbors, max_cell, beyond))):
            wide = [line.x0 for row in run for line in row if line.x1 - line.x0 > max_cell]
            continued = any(line.x1 - line.x0 > max_cell and abs(line.x0 - x0) <= _TABLE_ROW_TOLERANCE
                            for row in neighbors if _near(run, row) for line in row for x0 in wide)
            if not continued and not _is_beside_a_list(run, neighbors, max_cell):
                found.append(run)
        index += max(len(run), 1)
    return found


def _spanning_row(row: list[_Line], first: list[_Line], max_cell: float) -> bool:
    """Whether `row` is a two-fragment 'label | value' row whose left edges align with the first two cells of the
    first row (`first`) of a group of table-like rows (`_table_like_rows`). The label is short and at least two
    characters. The value may span into later cells or fit inside the second cell (a row whose later cells are empty)."""
    if len(row) != 2 or not _is_separate(row):
        return False
    label, value = row
    return (label.x1 - label.x0 <= max_cell and len(label.text.replace(" ", "")) >= 2
            and abs(label.x0 - first[0].x0) <= _TABLE_ROW_TOLERANCE and abs(value.x0 - first[1].x0) <= _TABLE_ROW_TOLERANCE)


def _aligned_pair(run: list[list[_Line]], neighbors: list[list[_Line]], max_cell: float,
                  beyond: dict[int, list[_Line]] | None = None) -> bool:
    """Whether two table-like rows (`run`) line up cell by cell: they have the same number of fragments, all short (at
    most the maximum table cell width), each fragment of the second row matches the fragment at the same position in
    the first row by left edge, right edge or centre, the rows are at least `_PAIR_ROW_GAP_RATIO` times the first
    row's line height apart, and the first cell is not a text column continuing above and below — that is, a row right
    above or below (`neighbors`) is a single fragment with the same left edge as the first cell that ends before the
    second cell, and such a row exists on both sides or continues in the same shape beyond that row (`beyond`,
    neighbouring row id → the row beyond it).
    A short one-line paragraph like that on one side only ('아래 문단.') does not block it — a two-row table with cell
    padding scattered column by column and that paragraph slipped in between the first and second columns (Pinned by:
    test_pdf_round45.py::TestTwoRowTableNextToAShortParagraph).

    A borderless 2-row table ('적용일 | 날짜 | 한도' in an official letter, the last two rows of a financial statement
    carried over to the next page) falls short of the minimum number of table-like rows and scattered column by column
    (Pinned by:
    test_pdf.py::TestBorderlessTableWithWideCells::test_two_aligned_rows_of_short_pieces_are_read_row_by_row).
    With only two rows it is hard to tell them from fragments that happen to sit at the same height, so they are
    joined into rows only when the cells line up column by column (short fragments placed out of line stay as they
    are, Pinned by: test_two_rows_of_short_pieces_out_of_line_are_left_as_read). Labels wrapped onto two lines in each
    of several side-by-side boxes are not joined even though the cells line up, because the rows are only a line
    spacing apart — joining split the two lines of each box (Pinned by:
    test_two_line_labels_of_boxes_side_by_side_are_left_as_read).
    Two lines of the left column of a two-column table of contents and the right-column items beside them also line
    up, but the left column continues above and below (Pinned by:
    test_two_rows_beside_a_column_continuing_above_them_are_left_as_read). If the row above or below has several
    fragments (another row of the table) or is a single fragment placed at the second cell or later (wrapped cell
    text), it does not block.
    Known accepted gap: a 2-row table whose rows touch without cell padding (the default padding of a Word table) is
    still read column by column. Not making it a table is the same as for 3 rows or more (conservative criterion).
    """
    def aligned(upper: _Line, lower: _Line) -> bool:
        return min(abs(upper.x0 - lower.x0), abs(upper.x1 - lower.x1),
                   abs(upper.x0 + upper.x1 - lower.x0 - lower.x1) / 2) <= _TABLE_ROW_TOLERANCE

    first, second = run

    def continues_first_cell(row: list[_Line], reference: list[list[_Line]]) -> bool:
        return (_near(reference, row) and len(row) == 1 and abs(row[0].x0 - first[0].x0) <= _TABLE_ROW_TOLERANCE
                and row[0].x1 < first[1].x0)

    gap = min(line.top for line in second) - max(line.bottom for line in first)
    column = [row for row in neighbors if continues_first_cell(row, run)]
    beyond = beyond or {}
    return (len(first) == len(second) and gap >= max(line.height for line in first) * _PAIR_ROW_GAP_RATIO
            and all(line.x1 - line.x0 <= max_cell for line in (*first, *second))
            and all(aligned(upper, lower) for upper, lower in zip(first, second, strict=True))
            and not (len(column) == 2 or any(id(row) in beyond and continues_first_cell(beyond[id(row)], [row])
                                             for row in column)))


def _is_beside_a_list(run: list[list[_Line]], neighbors: list[list[list[_Line]]], max_cell: float) -> bool:
    """Whether a group of table-like rows (`run`) is a box placed beside a list: the rows right above and below the
    group both continue, without a wide fragment, at the same left edges as the narrow fragments of the group (the
    list continues outside the group and only the wide fragments are at the group's height).

    A notice box placed beside a few rows of a '제N조 제목' (Article N, title) list became the third fragment of those
    rows and slipped in line by line between the list rows, like '제2조 정의 이 규정은 2026년…' (Pinned by:
    test_pdf.py::TestBorderlessTableWithWideCells::
    test_side_note_beside_part_of_a_two_column_list_is_not_joined_into_its_rows). Looking at only one of above/below
    would catch a table with a total row ('합계 … 금액') attached below, so both sides are checked. A group that is
    caught is left in pdfminer's flow as before (`_table_like_rows` did not exist).
    """
    narrow = [line.x0 for row in run for line in row if line.x1 - line.x0 <= max_cell]

    def continues_list(row: list[_Line]) -> bool:
        return (len(row) >= 2 and _near(run, row) and all(line.x1 - line.x0 <= max_cell for line in row)
                and all(any(abs(line.x0 - x0) <= _TABLE_ROW_TOLERANCE for x0 in narrow) for line in row))

    return len(neighbors) == 2 and all(continues_list(row) for row in neighbors)


def _read_columns_in_turn(page: _Page) -> None:
    """On a page where the lines of two side-by-side columns were read interleaved line by line, reorder the lines as
    left column, then right column.

    When the gap between lines exceeds `line_margin` (0.5) times the line height, pdfminer makes a text box per line
    and pairs the nearest text boxes first. In the side-by-side text boxes of two-column documents and presentations
    with line spacing of 150% or more, the left and right lines at the same height were paired first and read as
    'left 1, right 1, left 2 …' (130% and 140% give one text box per column and are fine). Raising the fixed
    `line_margin` cannot fix this — 1.0 fixes 160% but leaves 200% as it was. So it is corrected from the positions of
    the lines, independent of line spacing (Pinned by: test_pdf.py::TestWidelySpacedColumns).
    - The trigger is kept narrow (Deliberate): places where two lines adjacent in reading order are apart
      horizontally, both wider than a table cell and close in height occur `_COLUMN_MIX_MIN` times or more, and the
      ends of those left lines are all to the left of the starts of the right lines (one gap between columns). Rows
      with a short fragment (page number in a table of contents, bullet, article number) and three or more columns are
      not touched. Borderless tables and 'label value' rows have already been removed or made into one line.
    - On meeting a line that crosses the gap between the columns (full-width heading or paragraph), the left column
      and right column collected so far are emitted and then that line. The order inside a column and what comes
      before and after a crossing line stay as pdfminer decided.
    - A page where this does not trigger is checked for interleaving by paragraph (`_read_paragraph_columns_in_turn`).
    """
    if page.simple:
        return
    wide = page.width * _TABLE_CELL_MAX_RATIO
    mixed = [sorted(pair, key=lambda line: line.x0) for pair in zip(page.lines, page.lines[1:], strict=False)
             if _is_beside(*pair) and min(line.x1 - line.x0 for line in pair) >= wide]
    if len(mixed) < _COLUMN_MIX_MIN:
        _read_paragraph_columns_in_turn(page)
        return
    left_end, right_start = max(left.x1 for left, _ in mixed), min(right.x0 for _, right in mixed)
    if left_end >= right_start:
        return

    below_top = _below_columns_read_line_by_line(page, left_end, right_start)
    ordered: list[_Line] = []
    left: list[_Line] = []
    right: list[_Line] = []
    for line in (line for line in page.lines if line.top < below_top):
        in_left, in_right = line.x1 <= right_start, line.x0 >= left_end
        if in_left != in_right:
            (left if in_left else right).append(line)
        else:
            ordered += [*left, *right, line]
            left, right = [], []
    page.lines = [*ordered, *left, *right, *(line for line in page.lines if line.top >= below_top)]


def _below_columns_read_line_by_line(page: _Page, left_end: float, right_start: float) -> float:
    """The height at which the text below the two columns reordered by `_read_columns_in_turn` starts. Infinity if
    there is none. The same decision as the paragraph-level path (`_below_both_columns`; for two columns under a pair
    of subheadings with fewer than `_COLUMN_LINES_MIN` wide lines, `_heading_below_headed_columns`).

    A single-column heading ('Ⅴ. 결론') or short paragraph below two columns with line spacing of 160% or more is a
    left line within the column width, so it was attached to the left column and came out before the right column
    (Pinned by: test_pdf.py::TestWidelySpacedColumns::
    test_heading_below_widely_spaced_columns_read_line_by_line_comes_after_the_right_column,
    test_heading_below_short_headed_columns_read_line_by_line_comes_after_the_right_column).
    """
    blocks = [(table.x1, table.bottom) for table in page.tables] + [(figure[2], figure[3]) for figure in page.figures]
    below_top = _below_both_columns(page.lines, left_end, right_start, blocks)
    wide = page.width * _TABLE_CELL_MAX_RATIO
    lefts = [line for line in page.lines if line.x1 <= right_start and wide <= line.x1 - line.x0]
    rights = [line for line in page.lines if line.x0 >= left_end and wide <= line.x1 - line.x0]
    if lefts and rights and max(len(lefts), len(rights)) < _COLUMN_LINES_MIN:
        heading_size = _headed_columns(page.lines, lefts, rights, left_end, right_start)
        if heading_size is not None:
            below_top = min(below_top, _heading_below_headed_columns(page.lines, left_end, right_start, heading_size))
    return below_top


def _read_paragraph_columns_in_turn(page: _Page) -> None:
    """Reorder, column by column, two side-by-side columns that were read interleaved by paragraph (pages where
    `_read_columns_in_turn` did not trigger).

    For two columns with wide paragraph spacing (about one line of space after a paragraph, a blank line between
    paragraphs), pdfminer makes a text box per paragraph and pairs the nearest text boxes first, so the paragraphs of
    the right column (including the rest of a sentence carried over from the end of the left column) slipped in
    between the paragraphs of the left column. The adjacent lines are far apart in height, so the line-level condition
    (`_is_beside`) does not catch it (Pinned by: test_pdf.py::TestWidelySpacedColumns::
    test_two_columns_with_wide_paragraph_spacing_are_read_column_by_column). The conditions are kept narrow (Deliberate):
    - Columns: lines wider than a table cell and at most half the page width number `_COLUMN_LINES_MIN` or more on one
      side and 2 or more on the other, with one gap between them. If a column contains lines side by side (a table of
      3 or more columns), or places where two lines adjacent in reading order sit side by side in the same row occur
      `_COLUMN_MIX_MIN` times or more (row-wise layout — a '조건 | 지표' list), nothing is touched.
    - A group of lines that returns to the left column after right-column lines were read is treated as the same
      column only when it continues right below the left column (gap at most `_COLUMN_CONTINUE_RATIO` times the line
      height). Unless the next group is right-column lines starting above it, its left edge must also match the left
      column — outdented text below the two columns ('FAQ') was pulled up into the left column. If it does not
      continue, the flow is cut there and the left column and right column collected so far are emitted (two columns
      above and below a full-width figure or table stay as they are). However, if the non-continuing left-column group
      starts above the bottom of the right column read before it (beside the right column), only the earlier left
      column is emitted and the right column is placed after that group — in a two-column paper that pdfminer gave in
      right column → left column order, the right column came out first (Pinned by:
      test_pdf.py::TestWidelySpacedColumns::test_right_column_read_before_the_left_column_is_put_after_it). If that
      group continues past the bottom of the right column to a line set far apart, it is split there
      (`_set_apart_below`).
    - Lines below both columns (`_below_both_columns`) are placed after the two columns in flow order.
    Known accepted gap: pages with many short lines interleaved line by line, and pages that return to a paragraph
    with an indented first line, stay as they are.
    """
    columns = _paragraph_columns(page)
    if columns is None:
        return
    left_end, right_start, heading_size = columns
    blocks = [(table.x1, table.bottom) for table in page.tables] + [(figure[2], figure[3]) for figure in page.figures]
    below_top = min(_below_both_columns(page.lines, left_end, right_start, blocks),
                    _heading_below_headed_columns(page.lines, left_end, right_start, heading_size))
    below = [line for line in page.lines if line.top >= below_top]

    # (in the left column? — None if crossing, consecutive lines of the same column in reading order)
    runs: list[tuple[bool | None, list[_Line]]] = []
    for line in page.lines:
        if line.top >= below_top:
            continue
        in_left, in_right = line.x1 <= right_start, line.x0 >= left_end
        side = None if in_left == in_right else in_left
        if runs and side is not None and runs[-1][0] == side:
            runs[-1][1].append(line)
        else:
            runs.append((side, [line]))

    ordered: list[_Line] = []
    left: list[_Line] = []
    right: list[_Line] = []
    index = 0
    while index < len(runs):
        in_left, lines = runs[index]
        index += 1
        if in_left is None:
            ordered += [*left, *right, *lines]
            left, right = [], []
            continue
        following = runs[index] if index < len(runs) else (None, lines)
        interleaved = following[0] is False and following[1][0].top < max(line.bottom for line in lines)
        if right and not _continues_column(left if in_left else right, lines, aligned=not (in_left and interleaved)):
            beside = in_left and lines[0].top < max(line.bottom for line in right)  # Left column beside the right column read before it
            rest = _set_apart_below(lines, max(line.bottom for line in right), blocks, right_start) if beside else []
            if rest:  # The rest, continuing set apart below the right column, is handled separately as the next group
                lines = lines[:len(lines) - len(rest)]
                runs.insert(index, (True, rest))
            ordered += left if beside else [*left, *right]
            left, right = [], right if beside else []
        (left if in_left else right).extend(lines)
    page.lines = [*ordered, *left, *right, *below]


def _set_apart_below(lines: list[_Line], bottom: float, blocks: list[tuple[float, float]], right_start: float) -> list[_Line]:
    """The rest of `lines` (a left-column group, in reading order) that starts below `bottom` far apart from the lines
    before it (more than the gap of `_continues_column`). [] if there is none. A table or figure inside the left column
    that ends in between (`blocks` holds (right edge, bottom)) counts as a preceding line — a left column continuing
    below a table became cut-off text (a table in the middle of a two-column paper).
    """
    reached = lines[0].bottom  # Bottom of the preceding lines (and the tables/figures below them)
    for index, line in enumerate(lines[1:], start=1):
        reached = max([reached, *(end for x1, end in blocks if x1 <= right_start and end <= line.top)])
        if line.top >= bottom and line.top - reached > max(lines[index - 1].height, line.height) * _COLUMN_CONTINUE_RATIO:
            return lines[index:]
        reached = max(reached, line.bottom)
    return []


def _below_both_columns(lines: list[_Line], left_end: float, right_start: float,
                        blocks: list[tuple[float, float]]) -> float:
    """The height (top coordinate) at which the text placed below both columns starts. Infinity if there is none.

    A short line within one column width (a subheading or contact line below the two columns) cannot be told from a
    left-column line, so it was attached as a continuation of the left column and came out before the right column
    ('3. 결론' below a LibreOffice two-column section, Pinned by: test_pdf.py::TestWidelySpacedColumns::
    test_short_line_below_both_columns_stays_after_the_right_column). A line on the left-column side is below both
    columns if it satisfies all of the following:
    - Every line of the right column ends above it, and the right column comes down to the height of the preceding
      line of the left column (the bottoms of the two columns agree). For a left-column paragraph on a page where only
      the left column is long (the last page), the right column does not come down that far.
    - It does not fill the column width, and its left edge is at or to the left of the leftmost edge of the left column.
    - It is at least half a line height, and at least twice the median gap between left-column lines, away from the
      preceding line of the left column (spacing between paragraphs or above a subheading).
    In a presentation with wide line spacing the continuation line of the last item of the left column (an indented
    line in parentheses) was pushed below both columns and separated from its item (Pinned by:
    test_last_line_of_the_left_column_below_the_end_of_the_right_column_stays_in_the_left_column) — it is indented and
    the gap is the ordinary line spacing.
    A table or figure inside the left column (`blocks` holds (right edge, bottom)) counts as a preceding line of the
    left column when measuring the bottoms of the two columns — when the right column ended at the same height as the
    text above a left-column table, the caption below the table and the rest of the left column were pushed below both
    columns (Pinned by: test_left_column_continuing_below_its_table_is_not_taken_for_text_below_both_columns). The gap
    is still measured from text lines.
    If the line right below (with no line reaching towards both columns in between) continues while filling the column
    width, this is the closing text of a page whose left column is longer — the subheading and paragraph at the end of
    the left column were pushed after the right column (Pinned by:
    test_heading_and_paragraph_closing_the_longer_left_column_stay_in_the_left_column).
    """
    rights = [line for line in lines if line.x0 >= left_end]
    if not rights:
        return float("inf")

    right_bottom = max(line.bottom for line in rights)
    lefts = sorted((line for line in lines if line.x1 <= right_start), key=lambda line: line.top)
    for index, line in enumerate(lefts):
        above = lefts[:index]
        if not above or line.top < right_bottom - 1 or line.x1 > left_end - line.height:
            continue
        gaps = [lower.top - upper.bottom for upper, lower in zip(above, above[1:], strict=False) if lower.top > upper.bottom]
        gap = line.top - max(other.bottom for other in above)
        left_bottom = max([other.bottom for other in above]
                          + [bottom for x1, bottom in blocks if x1 <= right_start and bottom <= line.top])
        if (line.x0 <= min(other.x0 for other in above) + _TABLE_ROW_TOLERANCE
                and gap >= max(line.height * 0.5, statistics.median(gaps) * 2 if gaps else 0.0)
                and right_bottom >= left_bottom - line.height and not _fills_column_below(line, lefts[index + 1:], lines, left_end)):
            return line.top
    return float("inf")


def _fills_column_below(line: _Line, lower: list[_Line], lines: list[_Line], left_end: float) -> bool:
    """Whether the left-column line right below `line` (with no other line in between; the first line of `lower`)
    fills the column width and wraps at that width onto the next line (the second line of `lower`, same left edge and
    gap at most `_WRAP_LINE_GAP_RATIO` times the line height) — the left-column text continues.

    Without checking that it wraps onto the next line, a single-column subheading and one-line paragraph below both
    columns were pulled up before the right column because that one line happened to be as long as the column width
    (below a LibreOffice two-column section, Pinned by: test_pdf.py::TestWidelySpacedColumns::
    test_one_line_paragraph_under_a_heading_below_both_columns_comes_after_the_right_column). If the single-column
    paragraph has two or more lines, its first line crosses both columns and is not a left-column line. Known accepted
    gap: if the paragraph under a subheading at the end of a page whose left column is longer has only one line, it is
    treated as text below both columns.
    """
    if len(lower) < 2 or lower[0].x1 <= left_end - lower[0].height:
        return False
    wrap_gap = lower[1].top - lower[0].bottom
    if abs(lower[1].x0 - lower[0].x0) > _TABLE_ROW_TOLERANCE or not 0 <= wrap_gap <= lower[0].height * _WRAP_LINE_GAP_RATIO:
        return False
    return not any(line.top < other.top < lower[0].top and other.x1 > left_end for other in lines)


def _paragraph_columns(page: _Page) -> tuple[float, float, float] | None:
    """The (left column end, right column start, smaller font size of the pair of subheadings on top of short two
    columns — infinity for two columns not found through a subheading pair) of the two columns that
    `_read_paragraph_columns_in_turn` reorders. None if the conditions are not met.

    If the left column end passes the right column start, the measurement is repeated with only the lines within the
    height where the two columns are side by side (`_beside_each_other`). The short last line of a single-column
    summary paragraph above two columns was counted as a left-column line and the column verdict switched off (Pinned
    by: test_pdf.py::TestWidelySpacedColumns::
    test_short_last_line_of_a_summary_above_two_columns_does_not_hide_the_columns). A page that works with the first
    measurement is unchanged.

    Lines centred on the page (`_centred_on`; title, author affiliations) are not counted in either column from the
    start. A title slightly right of centre was counted as a right-column line, the left column re-measured with only
    the side-by-side height ended at the first section, and the sections of the short right column slipped in between
    the left column (section order 1 → 4 → 2 → 3, Pinned by: test_pdf_reading_order.py::TestCentredLinesOverTwoColumns).
    """
    wide = page.width * _TABLE_CELL_MAX_RATIO
    body = [line for line in page.lines if wide <= line.x1 - line.x0 <= page.width / 2 and not _centred_on(line, page.width)]
    lefts = [line for line in body if line.x0 + line.x1 < page.width]
    rights = [line for line in body if line.x0 + line.x1 >= page.width]
    if min(len(lefts), len(rights)) < 2:
        return None
    left_end, right_start = max(line.x1 for line in lefts), min(line.x0 for line in rights)
    if left_end >= right_start:
        lefts, rights = _beside_each_other(lefts, rights), _beside_each_other(rights, lefts)
        if min(len(lefts), len(rights)) < 2:
            return None
        left_end, right_start = max(line.x1 for line in lefts), min(line.x0 for line in rights)
        if left_end >= right_start:
            return None

    # Short two columns with few wide lines are columns only when there is a pair of subheadings on top. Such columns
    # are read column by column even if they were read paired line by line (even if they look like a row-wise layout)
    heading_size = float("inf")
    if max(len(lefts), len(rights)) < _COLUMN_LINES_MIN:
        heading_size = _headed_columns(page.lines, lefts, rights, left_end, right_start)
        if heading_size is None:
            return None
    headed = heading_size < float("inf")
    pairs = sum(1 for previous, line in zip(page.lines, page.lines[1:], strict=False) if _side_by_side(previous, line))
    if pairs >= _COLUMN_MIX_MIN and not headed:
        return None
    if (_has_lines_side_by_side([line for line in page.lines if line.x1 <= right_start])
            or _has_lines_side_by_side([line for line in page.lines if line.x0 >= left_end])):
        return None
    return left_end, right_start, heading_size


def _headed_columns(lines: list[_Line], lefts: list[_Line], rights: list[_Line], left_end: float,
                    right_start: float) -> float | None:
    """If two columns with fewer than `_COLUMN_LINES_MIN` wide lines (`lefts`, `rights` are the wide lines of each
    column) are short two columns under a pair of subheadings, the smaller font size of that pair; otherwise None.

    A short two-column section with 4 or fewer wide lines per column ('4.1 국내 | 4.2 해외') misses the column
    verdict, so after the subheadings of the two columns came out side by side, the lines of the two columns came out
    alternating line by line (LibreOffice and reportlab reports, Pinned by: test_pdf.py::TestWidelySpacedColumns::
    test_short_two_columns_under_level_headings_are_read_column_by_column). They are columns only when all of the
    following hold (Deliberate, conservative):
    - On top of the two columns there is a pair of subheadings side by side in the same row, both at least
      `_HEADING_MIN_RATIO` times larger than the body lines of the columns. Body-size subheadings cannot be told from
      the header row of a '조건 | 해당 지표' list and are not considered (Known accepted gap, Pinned by:
      test_short_two_columns_under_body_size_headings_are_left_as_read).
    - The column lines under the subheadings (for the left column, down to above a line crossing both columns) share
      the left edge of the subheading in each column, and the right column lines all start right below the subheading
      (within line height × 3).
    - In each column, lines that fill the column width (right edge within a line height of the longest line of the
      column) make up at least half of that column's lines — it is wrapped text. A list paired row by row has ragged
      line lengths (Pinned by:
      test_rows_of_a_condition_beside_its_indicator_under_a_larger_header_row_are_still_read_row_by_row).

    Instead of a subheading pair, a single subheading on top of the right column is also accepted — when it is at the
    very top of the page (no other line above) and the left column also starts at that height (a left-column
    paragraph carried over from the previous page). In the short two columns on the last page of a two-column paper,
    '참고문헌' (References) of the right column slipped in after the first line of the left column (Pinned by:
    test_pdf_reading_order.py::TestShortColumnsUnderOneHeading). The remaining conditions (left edges of the column
    lines, filling the column width) are the same.
    """
    def fills(column: list[_Line]) -> bool:
        end = max(line.x1 for line in column)
        return sum(1 for line in column if line.x1 >= end - line.height) * 2 >= len(column)

    size = max(line.size for line in (*lefts, *rights))
    for left, right in zip(lines, lines[1:], strict=False):
        if right.x0 < left.x0:
            left, right = right, left
        if not (_side_by_side(left, right) and left.x1 <= right_start and right.x0 >= left_end
                and min(left.size, right.size) >= size * _HEADING_MIN_RATIO):
            continue
        across = [line.top for line in lines if line.top > left.bottom and line.x0 < left_end and line.x1 > right_start]
        under = [line for line in lefts if left.bottom - _TABLE_ROW_TOLERANCE <= line.top < min(across, default=float("inf"))]
        if (len(under) >= 2 and all(line.top >= right.bottom - _TABLE_ROW_TOLERANCE for line in rights)
                and min(line.top for line in rights) - right.bottom <= right.height * _COLUMN_CONTINUE_RATIO
                and all(abs(line.x0 - left.x0) <= _TABLE_ROW_TOLERANCE for line in under)
                and all(abs(line.x0 - right.x0) <= _TABLE_ROW_TOLERANCE for line in rights)
                and fills(under) and fills(rights)):
            return min(left.size, right.size)

    # Top of a page where only the right column starts with a subheading (the left column is a paragraph carried over
    # from the previous page): the subheading is at the very top of the page and the left column also starts at that
    # height. The subheading only needs to be larger than the right-column body (a reference list is set in smaller
    # type than the body)
    heads = [line for line in lines if line.x0 >= left_end and line.size >= max(other.size for other in rights) * _HEADING_MIN_RATIO]
    right = min(heads, key=lambda line: line.top, default=None)
    if (right is not None and len(lefts) >= 2 and all(line.top >= right.top - right.height for line in lines)
            and min(line.top for line in lines if line.x1 <= right_start) <= right.bottom
            and all(line.top >= right.bottom - _TABLE_ROW_TOLERANCE for line in rights)
            and min(line.top for line in rights) - right.bottom <= right.height * _COLUMN_CONTINUE_RATIO
            and all(abs(line.x0 - right.x0) <= _TABLE_ROW_TOLERANCE for line in rights)
            and all(abs(line.x0 - lefts[0].x0) <= _TABLE_ROW_TOLERANCE for line in lefts)
            and fills(lefts) and fills(rights)):
        return right.size
    return None


def _heading_below_headed_columns(lines: list[_Line], left_end: float, right_start: float, heading_size: float) -> float:
    """The height at which a single-column heading below short two columns found through a subheading pair
    (`_headed_columns`) starts. Infinity if there is none.

    Among the left-column-side lines below the end of the right column, a line as large as the subheadings of the two
    columns (within 0.5pt) is the next section, not a continuation of the left column — '5. 결론' below short two
    columns with a longer left column came out before the right column (Pinned by: test_pdf.py::TestWidelySpacedColumns::
    test_heading_below_short_two_columns_with_the_longer_left_column_comes_after_the_right_column). Pages of ordinary
    two columns with a longer left column (the bottom condition of `_below_both_columns`) are not changed — without a
    subheading pair on top of the columns there is no basis for telling whether a line of that size is a subheading
    inside the left column or the next section.
    """
    right_bottom = max((line.bottom for line in lines if line.x0 >= left_end), default=float("inf"))
    return min((line.top for line in lines if line.x1 <= right_start and line.top >= right_bottom
                and line.size >= heading_size - 0.5), default=float("inf"))


def _beside_each_other(lines: list[_Line], others: list[_Line]) -> list[_Line]:
    """The lines of `lines` that lie within the height from the top to the bottom of `others`."""
    top, bottom = min(line.top for line in others), max(line.bottom for line in others)
    return [line for line in lines if line.top >= top - _TABLE_ROW_TOLERANCE and line.bottom <= bottom + _TABLE_ROW_TOLERANCE]


def _has_lines_side_by_side(lines: list[_Line]) -> bool:
    """Whether `lines` contains two lines placed side by side in the same row."""
    lines = sorted(lines, key=lambda line: line.top)
    for index, line in enumerate(lines):
        for other in lines[index + 1:]:
            if other.top >= line.bottom:
                break
            if _side_by_side(line, other):
                return True
    return False


def _side_by_side(first: _Line, second: _Line) -> bool:
    """Whether two lines are in the same row without overlapping horizontally (vertical overlap at least half the
    smaller line height)."""
    overlap = min(first.bottom, second.bottom) - max(first.top, second.top)
    return overlap >= min(first.height, second.height) * 0.5 and (second.x0 >= first.x1 or first.x0 >= second.x1)


def _continues_column(column: list[_Line], lines: list[_Line], aligned: bool) -> bool:
    """Whether `lines` continue right below the column (`column`). With `aligned`, the left edge must match the column too."""
    if not column:
        return False

    gap = lines[0].top - max(line.bottom for line in column)
    if not 0 <= gap <= max(column[-1].height, lines[0].height) * _COLUMN_CONTINUE_RATIO:
        return False
    return not aligned or abs(min(line.x0 for line in lines) - min(line.x0 for line in column)) <= _TABLE_ROW_TOLERANCE


def _read_alternating_sides_in_turn(page: _Page) -> None:
    """Where right-aligned lines sit in between left lines but were read separately like columns (or jumbled), put the
    lines back in top-to-bottom order.

    In a conversation log (left remarks, right-aligned replies), a right-aligned presenter on the line under an agenda
    item, or a right-aligned '※' note between lines, the meaning lies in the top-to-bottom order, but pdfminer's text
    box flow (`boxes_flow`) saw two columns and read all the left lines before the right lines, or jumbled the two
    (Pinned by: test_pdf.py::TestRightAlignedLinesBetweenLines). The conditions are kept narrow (Deliberate) — two
    side-by-side columns or text boxes are not touched (`_alternating_window`).
    Known accepted gap: places with only one right-aligned line, and places where the right lines all have the same
    width (their left edges match too, so they cannot be told from side-by-side text boxes), stay as they are.
    """
    if page.simple:
        return
    lines = page.lines  # Modified in place
    index = 1

    while index < len(lines):
        window = _alternating_window(lines, index) if lines[index].top < lines[index - 1].top else None
        if window is None:
            index += 1
            continue
        start, end = window
        lines[start:end] = sorted(lines[start:end], key=lambda line: line.top)
        index = end


def _alternating_window(lines: list[_Line], index: int) -> tuple[int, int] | None:
    """The range of lines [start, end) to put back in top-to-bottom order, containing the place (`index`) where the
    reading order went back up. None if the conditions are not met.

    - Of the two lines at the turn-back, the one whose right edge is further right is the reference (`edge`) of the
      right-aligned group. The lines of the range are only lines whose right edge matches `edge` and whose left edge is
      at least a line height to the right of the reference left line (right group), or lines whose right edge is to
      the left of `edge` (left group). It stops at a page-width line (starting at the left and ending at `edge`). On
      the side before the turn-back it is extended only while the lines continue top to bottom.
    - The right group has 2 or more lines and ragged left edges (differing by a line height or more). Column body text
      has matching left edges.
    - At least one line of the left group lies at a height between the lines of the right group (alternating). If the
      right group is one block only above or below, they are side-by-side text boxes — the table of contents on a
      cover (left-aligned items under '【목차】'), which looked ragged because of a single centred heading, was mixed
      top to bottom and the team name and date on the left slipped in between the contents (Pinned by:
      test_pdf.py::TestRightAlignedLinesBetweenLines::
      test_left_aligned_list_under_a_centred_heading_is_not_taken_for_right_aligned_lines).
    - The left edges of the left group are all to the left of the right group, and no two lines in the range overlap
      vertically. In the body of two side-by-side columns the lines sit in the same row or the gap between lines is
      narrower than the line height, so they overlap somewhere.
    """
    pair = (lines[index - 1], lines[index])
    seed_right = max(pair, key=lambda line: line.x1)
    seed_left = pair[0] if seed_right is pair[1] else pair[1]
    edge, height = seed_right.x1, seed_right.height
    if seed_left.x1 >= edge - _TABLE_ROW_TOLERANCE or seed_left.x0 + height > seed_right.x0:
        return None

    def side(line: _Line) -> bool | None:  # Right group True, left group False, neither None
        if abs(line.x1 - edge) <= _TABLE_ROW_TOLERANCE:
            return True if line.x0 >= seed_left.x0 + height else None
        return False if line.x1 < edge else None

    start, end = index - 1, index + 1

    while start > 0 and lines[start - 1].top < lines[start].top and side(lines[start - 1]) is not None:
        start -= 1
    while end < len(lines) and side(lines[end]) is not None:
        end += 1

    window = lines[start:end]
    right = [line for line in window if side(line)]
    left = [line for line in window if not side(line)]
    if len(right) < 2 or max(line.x0 for line in right) - min(line.x0 for line in right) < height:
        return None
    first, last = min(line.top for line in right), max(line.top for line in right)
    if not any(first < line.top < last for line in left):
        return None
    if max(line.x0 for line in left) >= min(line.x0 for line in right):
        return None
    ordered = sorted(window, key=lambda line: line.top)
    if any(below.top < above.bottom for above, below in zip(ordered, ordered[1:], strict=False)):
        return None
    return start, end


def _read_lone_lines_in_place(page: _Page) -> None:
    """In a single-column flow, move a line that was read after the lines below it up to the position of its height.

    pdfminer's text box flow (`boxes_flow`) sent a short centred caption after the left-aligned paragraph right below
    it (or after '끝.' below a table), and since a table or figure is inserted before 'the first paragraph starting
    below it', it was pushed along with it (Pinned by: test_pdf_reading_order.py::
    TestCentredCaptionOnOneColumnPage). When the consecutive lines right before a line in reading order all lie below
    that line, it is moved to the front of those lines. To tell this from a place where the flow moved on to another
    column, all of the following are checked (Deliberate, conservative):
    - Neither the line to move nor the lines to skip have another line, table or figure beside them (same row, apart
      horizontally). Lines of two side-by-side columns have a line of the other column beside them.
      A figure beside the line to move is fine — text to the right of a figure was pushed after the subheading and
      table below in the left margin. A caption right below a figure (`_is_figure_caption`) also skips lines to skip
      that have a line beside them (a row of side-by-side cards) (Pinned by: test_pdf_pictures_beside_text.py).
    - The line to move and the lines to skip are one column flow (`_one_flow`).
    Known accepted gap: a page where a group of left-aligned text and a group of tables/captions on the right do not
    overlap and nothing connects them is seen as two columns and left alone. Pages with more than
    `_LONE_LINE_MAX_LINES` lines are also left alone.

    First, a row of lines side by side right above tables (a caption row, `_table_caption_rows`) is gathered left to
    right at the position of the row's first line in reading order, and counts as lines that the line to move may
    skip (even with a line beside them). When two sets of side-by-side captioned tables follow one above the other,
    pdfminer read the left captions and the right captions separately like columns, so a right caption went after the
    subheading of the next section and a table was placed after the wrong caption (Pinned by:
    test_pdf_reading_order.py::TestCaptionsOverSideBySideTables).

    Separately, a line (the page title) that lies alone above every line of the page (no line, table or figure beside
    it), is at least `_HEADING_MIN_RATIO` times the median font size of the page's lines, and spans the page centre or
    starts at the leftmost edge of the page's lines is put first. A centred title of a payslip or two-column document
    was pushed after the left text box and came out after the items, amounts or left column (Pinned by:
    test_pdf_forms_and_amounts.py::TestFarRightAmounts). A subheading at the top spanning only the right column (two
    columns whose left column starts lower) is not moved.
    """
    if page.simple or not 2 <= len(page.lines) <= _LONE_LINE_MAX_LINES:
        return
    lines = page.lines  # Modified in place
    tables = [(table.top, table.bottom, table.x0, table.x1) for table in page.tables]
    figures = [(top, bottom, x0, x1) for x0, top, x1, bottom, _ in page.figures]
    blocks = tables + figures
    crowded = _lines_with_neighbours(lines, blocks)
    beside_text = _lines_with_neighbours(lines, tables)  # A line with only a figure beside it (text right of a figure) may be moved

    # Gather the caption rows above tables
    captions = set()
    for row in _table_caption_rows(lines, tables):
        members = {id(line) for line in row}
        first = next(index for index, line in enumerate(lines) if id(line) in members)
        lines[:] = [line for line in lines if id(line) not in members]
        lines[first:first] = row
        captions |= members

    for index in range(1, len(lines)):
        line = lines[index]
        start = index
        caption = _is_figure_caption(line, figures)

        while (start > 0 and _lies_below(lines[start - 1], line)
               and (caption or id(lines[start - 1]) not in crowded or id(lines[start - 1]) in captions)):
            start -= 1
        if start == index or id(line) in beside_text or (start > 0 and _lies_below(lines[start - 1], line)):
            continue
        if _centred_on(line, page.width) or _one_flow(line, lines[start:index], lines, blocks):
            lines.insert(start, lines.pop(index))

    # Large text alone at the very top of the page (the title) is put first. The condition above does not move it when
    # a line to skip has a line beside it (name and department in one row)
    top = min(lines, key=lambda line: line.top)
    sizes = [line.size for line in lines if line.size]
    spans_middle = top.x0 < page.width / 2 < top.x1 or top.x0 <= min(line.x0 for line in lines) + _TABLE_ROW_TOLERANCE
    if (lines[0] is not top and id(top) not in crowded and spans_middle and sizes
            and top.size >= statistics.median(sizes) * _HEADING_MIN_RATIO
            and all(_lies_below(other, top) for other in lines if other is not top)):
        lines.insert(0, lines.pop(lines.index(top)))


def _read_sidebar_first(page: _Page) -> None:
    """On a page where the left sidebar was read after the whole right body column, put the sidebar first if the page
    title is in the sidebar.

    On a page that sets a left 30% sidebar (name, contact details) beside the body on the right, like a résumé, the
    sidebar lines are narrower than a table cell and do not enter the two-column verdict, and pdfminer read body →
    sidebar, so the name came out at the end of the page (Pinned by: test_pdf_reading_order.py::TestSidebarBesideTheBody).
    Kept narrow (Deliberate): only when the reading order splits cleanly into 'all right-column lines → all left-column
    lines' (if mixed, that is for other rules), the two columns are apart horizontally at side-by-side heights with 3
    or more lines each, and the largest text on the page is in the left column (at least `_HEADING_MIN_RATIO` times
    the largest text of the right column). Pages with tables or figures are not touched because their positions hang
    on the line order. Known accepted gap: a sidebar layout whose page title is in the body column (notes in the left
    margin) is still body → sidebar.
    """
    lines = page.lines
    if page.simple or page.tables or page.figures or len(lines) < 6:
        return
    split = next((index for index in range(1, len(lines)) if lines[index].x0 < lines[index - 1].x0 - _TABLE_ROW_TOLERANCE), None)
    if split is None or split < 3 or len(lines) - split < 3:
        return
    right, left = lines[:split], lines[split:]
    if max(line.x1 for line in left) >= min(line.x0 for line in right):
        return
    if not (min(line.top for line in left) < max(line.bottom for line in right)
            and min(line.top for line in right) < max(line.bottom for line in left)):
        return
    if max(line.size for line in left) >= max(line.size for line in right) * _HEADING_MIN_RATIO:
        page.lines = [*left, *right]


def _table_caption_rows(lines: list[_Line], tables: list[tuple[float, float, float, float]]) -> list[list[_Line]]:
    """The rows of lines placed side by side right above tables (top, bottom, x0, x1), left to right within a row: two
    or more lines at the same height all lie right above some table (gap at most `_CAPTION_GAP_RATIO` times the line
    height), overlapping that table horizontally, with no line continuing right above them (gap at most the line
    height) — the last row of a label-value group above a table ('작성 부서 인재개발팀 / 승인자 박본부장') is not a
    caption. The extent of a borderless table is its cell text, so a caption sticks out a few pt to the left."""
    if not tables:
        return []

    def is_caption(line: _Line) -> bool:
        return (any(-1 <= top - line.bottom <= line.height * _CAPTION_GAP_RATIO and line.x0 < x1 and x0 < line.x1
                    for top, _, x0, x1 in tables)
                and not any(other.x0 < line.x1 and line.x0 < other.x1 and 0 <= line.top - other.bottom <= line.height
                            for other in lines))

    return [row for row in _visual_rows(lines) if len(row) >= 2 and all(is_caption(line) for line in row)]


def _is_figure_caption(line: _Line, figures: list[tuple[float, float, float, float]]) -> bool:
    """Whether the line is a caption placed right below a figure (top, bottom, x0, x1) (gap at most the line height),
    within the figure's horizontal extent."""
    return any(-1 <= line.top - bottom <= line.height and x0 - _TABLE_ROW_TOLERANCE <= line.x0 and line.x1 <= x1 + _TABLE_ROW_TOLERANCE
               for _, bottom, x0, x1 in figures)


def _centred_on(line: _Line, width: float) -> bool:
    """Whether the line is centred on the page (its centre within `_CENTRED_TOLERANCE_RATIO` of the page width from
    the page centre, its width at most half the page width).

    A centred caption or title is not a line of either column of a two-column page — it spans the gap between the
    columns. On a page with no line or figure linking a left-aligned subheading and a centred caption (a figure frame
    drawn as a border only) the caption was pushed to the end of the page (Pinned by: test_pdf_reading_order.py::
    TestCentredCaptionOnOneColumnPage::test_centred_lines_with_nothing_linking_them_to_the_left_aligned_text_stay_in_place).
    """
    return line.x1 - line.x0 <= width / 2 and abs((line.x0 + line.x1) / 2 - width / 2) <= width * _CENTRED_TOLERANCE_RATIO


def _lies_below(other: _Line, line: _Line) -> bool:
    """Whether `other` lies below `line` (an overlap of up to 1pt is tolerated)."""
    return other.top >= line.bottom - 1.0


def _lines_with_neighbours(lines: list[_Line], blocks: list[tuple[float, float, float, float]]) -> set[int]:
    """The ids of the lines that have another line or a table/figure (top, bottom, x0, x1) beside them (same row,
    apart horizontally)."""
    ordered = sorted(lines, key=lambda line: line.top)
    crowded = set()

    for index, line in enumerate(ordered):
        for other in ordered[index + 1:]:
            if other.top >= line.bottom:
                break
            if _side_by_side(line, other):
                crowded.update((id(line), id(other)))

    for line in lines:
        if any(top < line.bottom and line.top < bottom and (x0 >= line.x1 or line.x0 >= x1) for top, bottom, x0, x1 in blocks):
            crowded.add(id(line))
    return crowded


def _one_flow(line: _Line, below: list[_Line], lines: list[_Line], blocks: list[tuple[float, float, float, float]]) -> bool:
    """Whether `line` and the lines lying below it (`below`) are one column flow (`_read_lone_lines_in_place`). They
    are if they overlap horizontally.

    If they are apart, all of the following are checked. (1) On the far side from `below`, `line` is alone within the
    height range of `below` (several lines, like a table of contents on the right of a cover, are a text box), or some
    line, table or figure (top, bottom, x0, x1) on the page covers the gap between the two or overlaps both `line` and
    what is across the gap (single-column body text, a centred figure). (2) Nothing on the two sides of the gap
    overlaps vertically (on a two-column page whose right column holds only a figure and caption, the figure is beside
    the left column). (3) The lines of one column on the other side do not alternate right above and below a line on
    one side (`_interleaved`, two side-by-side text boxes offset by half a line).
    """
    left, right = min(other.x0 for other in below), max(other.x1 for other in below)
    if line.x0 < right and left < line.x1:
        return True

    gap = (right, line.x0) if line.x0 >= right else (line.x1, left)
    spans = [(other.top, other.bottom, other.x0, other.x1) for other in lines] + blocks
    west = [span for span in spans if span[3] <= gap[0] + _TABLE_ROW_TOLERANCE]
    east = [span for span in spans if span[2] >= gap[1] - _TABLE_ROW_TOLERANCE]
    far = west if line.x0 >= right else east  # The side of `below`
    beyond = [span for span in spans if (span[2] >= right - _TABLE_ROW_TOLERANCE if line.x0 >= right
                                         else span[3] <= left + _TABLE_ROW_TOLERANCE)]  # Everything on the far side from `below`
    bottom = max(other.bottom for other in below)
    alone = sum(1 for top, end, _, _ in beyond if top < bottom and line.top < end) <= 1

    def bridges(x0: float, x1: float) -> bool:
        if x0 <= gap[0] and gap[1] <= x1:
            return True
        return x0 < line.x1 and line.x0 < x1 and any(x0 < other_x1 and other_x0 < x1 for _, _, other_x0, other_x1 in far)

    if not alone and not any(bridges(x0, x1) for _, _, x0, x1 in spans):
        return False
    if any(top < other_bottom and other_top < end for top, end, _, _ in east for other_top, other_bottom, _, _ in west):
        return False
    west_lines = [other for other in lines if other.x1 <= gap[0] + _TABLE_ROW_TOLERANCE]
    east_lines = [other for other in lines if other.x0 >= gap[1] - _TABLE_ROW_TOLERANCE]
    return not (_interleaved(east_lines, west_lines) or _interleaved(west_lines, east_lines))


def _interleaved(lines: list[_Line], others: list[_Line]) -> bool:
    """Whether `lines` contains a line with lines of `others` close in height (`_is_beside`) both right above and
    right below it, and those two lines share a left edge (lines of one column) — two text boxes alternating, offset by
    half a line. Lines above and below with different left edges are centred captions."""
    for line in lines:
        middle = line.top + line.bottom
        near = [other for other in others if _is_beside(other, line)]
        above = [other.x0 for other in near if other.top + other.bottom < middle]
        below = [other.x0 for other in near if other.top + other.bottom > middle]
        if any(abs(upper - lower) <= _TABLE_ROW_TOLERANCE for upper in above for lower in below):
            return True
    return False


def _is_beside(previous: _Line, line: _Line) -> bool:
    """Whether two lines do not overlap horizontally and are close in height (vertical centres differ by at most
    `_COLUMN_MIX_NEAR` times the line height)."""
    apart = line.x0 >= previous.x1 or previous.x0 >= line.x1
    distance = abs((previous.top + previous.bottom) - (line.top + line.bottom)) / 2
    return apart and distance <= max(previous.height, line.height) * _COLUMN_MIX_NEAR


def _is_letter_spaced(row: list[_Line]) -> bool:
    """A row of single Hangul/Hanja character fragments of the same size at even intervals (a letter-spaced title)."""
    if len(row) < 2 or not all(len(line.text) == 1 and _SPACED_CHAR.match(line.text) for line in row):
        return False
    if max(line.size for line in row) - min(line.size for line in row) > 0.5:
        return False
    gaps = [right.x0 - left.x1 for left, right in zip(row, row[1:], strict=False)]
    return min(gaps) > 0 and max(gaps) <= min(gaps) * _SPACED_GAP_SPREAD + 1


def _label_value_rows(rows: list[list[_Line]], page_width: float) -> list[list[_Line]]:
    """The rows to treat as a 'label  value' two-column layout (conditions in `_join_row_fragments`). Wrapped value
    lines are returned attached to the row above them.

    Where the pairs break off, the rows are checked once more without the lines of another column that ends to the
    left of the label column (`_without_left_block`) — the company information block on the left of an invoice head
    and the '청구번호 INV-…' (invoice number) block on the right have different line spacing and are out of step, so a
    left line became a row in between or mixed into the same row, the pairs broke off, and the group of labels and the
    group of values were split (Pinned by: test_pdf_label_value_rows.py::TestLabelValueRowsBesideAnotherBlock).
    """
    max_label = page_width * _TABLE_CELL_MAX_RATIO
    found: list[list[_Line]] = []
    index = 0

    while index < len(rows):
        centered, taken = _centered_label_rows(rows, index, max_label)
        if centered:
            found.extend(centered)
            index += taken
            continue
        paired = _pairs_with([], rows[index], max_label)
        run = _label_value_run(rows, index, max_label) if paired else []
        if len(run) < 2 and (paired or len(rows[index]) > 2):
            beside = _without_left_block(rows, index, max_label, page_width)
            if beside is not None:
                found.extend(beside)
                taken = {id(line) for row in beside for line in row}
                index = max(position for position, row in enumerate(rows) if any(id(line) in taken for line in row)) + 1
                continue
        if not paired:
            index += 1
            continue
        widths = [row[1].x1 - row[1].x0 for row in run if len(row) == 2]
        narrow = sum(1 for width in widths if width <= max_label * _VALUE_MAX_RATIO)
        if (narrow < 2 or narrow <= len(widths) - narrow) and not _sectioned_values(run, max_label):
            index += 1
            continue

        if _stands_apart(rows, index, run, page_width):
            found.extend(_with_wrapped_values(run))
        index += len(run)
    return found


def _centered_label_rows(rows: list[list[_Line]], index: int, max_label: float) -> tuple[list[list[_Line]], int]:
    """If, starting at `rows[index]` (one line of the value column), there is a 'label value' group whose labels sit at
    the vertical centre of multi-line value blocks: (rows of a label + its value lines, number of rows consumed).
    Otherwise ([], 0).

    When the labels of a borderless two-column table are vertically centred in their cells (valign=middle), a label
    does not sit in the same row as the first line of its value, so no pairs formed, and either all labels were read
    before all values (Chrome) or one value was placed after the wrong label (LibreOffice) (Pinned by:
    test_pdf_label_value_rows.py::TestLabelsCenteredOnTheirValues). Accepted narrowly: the value lines all share the
    left edge and size; the labels are short lines of the same size to the left of the value column, share a left edge
    and are further apart than the line spacing (two touching lines are one wrapped label); two or more labels each sit
    at the centre of one value block; the gaps between blocks are clearly wider than between the lines inside a block;
    and the rows right above and below do not continue the value column. Margin notes beside evenly spaced body text
    have no gaps between blocks, and with left-column headings of a two-column layout the type is larger or the right
    column continues outside the group. The first row is a single value line, or a pair of a one-line value and its
    label at the same height — when it could not start with a pair, a group whose first item has a one-line value was
    rejected on the grounds that the pair right above continues the value column (Pinned by:
    test_pdf_label_value_rows.py::
    TestLabelsCenteredOnTheirValues::test_value_of_one_line_at_any_place_keeps_each_label_before_its_value).
    """
    first = rows[index]
    if len(first) == 2 and first[0].x1 < first[1].x0 and abs(first[0].top - first[1].top) <= _TABLE_ROW_TOLERANCE:
        first = first[1:]  # 'label value' pair of a one-line value — the label is picked up by the loop below
    elif len(first) != 1:
        return [], 0
    column, size = first[0].x0, first[0].size
    values: list[_Line] = []
    labels: list[_Line] = []
    end = index

    while end < len(rows):
        row = rows[end]
        if end > index and row[0].top - max(line.bottom for line in rows[end - 1]) > row[0].height * 2.5:
            break
        value = [line for line in row if abs(line.x0 - column) <= _TABLE_ROW_TOLERANCE and abs(line.size - size) <= 0.5]
        label = [line for line in row if line.x1 < column and len(line.text.replace(" ", "")) >= 2 and abs(line.size - size) <= 0.5
                 and _short_label([line], max_label) and not _LINE_NUMBER_LABEL.match(line.text)]
        if len(value) > 1 or len(label) > 1 or len(value) + len(label) != len(row):
            break
        values += value
        labels += label
        end += 1

    if len(labels) < 2 or any(abs(label.x0 - labels[0].x0) > _TABLE_ROW_TOLERANCE for label in labels):
        return [], 0
    if any(lower.top - upper.bottom < upper.height for upper, lower in zip(labels, labels[1:], strict=False)):
        return [], 0  # Label lines touching at line spacing are one label wrapped onto two lines ('정성 / 효과')
    for neighbor, gap in ((rows[index - 1] if index else [], lambda row: first[0].top - max(line.bottom for line in row)),
                          (rows[end] if end < len(rows) else [], lambda row: row[0].top - values[-1].bottom)):
        if neighbor and gap(neighbor) <= first[0].height * 2.5 and any(
                abs(line.x0 - column) <= _TABLE_ROW_TOLERANCE for line in neighbor):
            return [], 0  # The value column continues above or below the group (right column of a two-column layout)

    # Split the value blocks at the widest gap between the centres of two neighbouring labels
    blocks: list[list[_Line]] = []
    gaps: list[float] = []  # Gaps between blocks
    start = 0
    for upper, lower in zip(labels, labels[1:], strict=False):
        middle, below = (upper.top + upper.bottom) / 2, (lower.top + lower.bottom) / 2
        splits = [at for at in range(start + 1, len(values))
                  if values[at - 1].bottom >= middle - _TABLE_ROW_TOLERANCE and values[at].top <= below + _TABLE_ROW_TOLERANCE]
        if not splits:
            return [], 0
        at = max(splits, key=lambda at: values[at].top - values[at - 1].bottom)
        gaps.append(values[at].top - values[at - 1].bottom)
        blocks.append(values[start:at])
        start = at
    blocks.append(values[start:])
    inner = max((lower.top - upper.bottom for block in blocks for upper, lower in zip(block, block[1:], strict=False)),
                default=0.0)
    if min(gaps) < inner + size * 0.2:
        return [], 0
    for label, block in zip(labels, blocks, strict=True):
        middle = (block[0].top + block[-1].bottom) / 2
        if abs((label.top + label.bottom) / 2 - middle) > label.height * 0.6 and abs(label.top - block[0].top) > _TABLE_ROW_TOLERANCE:
            return [], 0
    if all(abs(label.top - block[0].top) <= _TABLE_ROW_TOLERANCE for label, block in zip(labels, blocks, strict=True)):
        return [], 0  # Every label sits on the first line of its value — the ordinary 'label value' group handles it
    return [[label, *block] for label, block in zip(labels, blocks, strict=True)], end - index


def _without_left_block(rows: list[list[_Line]], index: int, max_label: float, page_width: float) -> list[list[_Line]] | None:
    """The group of 'label value' rows found again from the rows with the lines of another column removed — lines that
    end to the left of the label column of the last two fragments (a label-value pair) of `rows[index]` (wrapped value
    lines attached). None if neither this row nor the row right below has such a line, or no group forms.

    When the first line of the left block is at the same height as the first pair, that row has three fragments and
    could not start as a pair (Pinned by: test_pdf_label_value_rows.py::
    TestLabelValueRowsBesideAnotherBlock::test_pairs_whose_first_rows_line_up_with_the_left_block_stay_paired). In that
    case, if every row of the group has a left line, it is a three-column layout rather than a left block, so the
    result is None (test_rows_of_three_short_columns_are_not_split_into_a_pair_and_a_left_line in the same class).
    """
    if len(rows[index]) < 2:
        return None
    label = rows[index][-2]

    def kept(row: list[_Line]) -> list[_Line]:
        return [line for line in row if line.x1 > label.x0 - _TABLE_ROW_TOLERANCE]

    if len(kept(rows[index])) != 2 or index + 1 >= len(rows):
        return None
    if len(kept(rows[index])) == len(rows[index]) and len(kept(rows[index + 1])) == len(rows[index + 1]):
        return None
    view = [row for row in map(kept, rows) if row]
    start = next(position for position, row in enumerate(view) if row[0] is label)
    if not _pairs_with([], view[start], max_label):
        return None
    run = _label_value_run(view, start, max_label)
    widths = [row[1].x1 - row[1].x0 for row in run if len(row) == 2]
    narrow = sum(1 for width in widths if width <= max_label * _VALUE_MAX_RATIO)
    if narrow < 2 or narrow <= len(widths) - narrow or not _stands_apart(view, start, run, page_width):
        return None
    sides = {id(line): len(row) > len(kept(row)) for row in rows for line in row}  # Line → whether its row has a left line
    if len(rows[index]) > 2 and all(sides[id(row[0])] for row in run):
        return None
    return _with_wrapped_values(run)


def _label_value_run(rows: list[list[_Line]], index: int, max_label: float) -> list[list[_Line]]:
    """The group of 'label value' rows continuing downwards from `rows[index]` (a label-value pair): pairs in the same
    two columns (`_pairs_with`), wrapped value lines (`_wraps_value`), and label rows without a value between pairs
    (`_is_empty_value_row`). A group that has accepted a wrapped value line accepts further pairs only with a short label."""
    def pairs(run: list[list[_Line]], row: list[_Line], wrapped: bool) -> bool:
        return _pairs_with(run, row, max_label) and (_short_label(row, max_label) or not wrapped)

    def wraps(row: list[_Line]) -> bool:  # what `row` adds to `_has_wrapped_value` of the group (judged against the first row)
        return _has_wrapped_value([run[0], row])

    run = [rows[index]]
    # `_has_wrapped_value(run)`, carried along as rows are added. Asked of the whole group again for every pair with a long
    # label, a group of many such pairs took time with the square of its rows, and with the cube when it is then discarded
    # for its wide values and built again from each of its rows (Pinned by: test_audit_pdf_tail.py::
    # TestManyPairsWithLongLabels).
    wrapped = _has_wrapped_value(run)

    while index + len(run) < len(rows):
        row = rows[index + len(run)]
        if pairs(run, row, wrapped) or _wraps_value(run, row, max_label):
            run.append(row)
            wrapped = wrapped or wraps(row)
        elif (index + len(run) + 1 < len(rows) and _is_empty_value_row(run, row, max_label)
              and pairs([*run, row], rows[index + len(run) + 1], wrapped or wraps(row))):
            wrapped = wrapped or wraps(row) or wraps(rows[index + len(run) + 1])
            run += [row, rows[index + len(run) + 1]]
        else:
            break
    return run


def _stands_apart(rows: list[list[_Line]], index: int, run: list[list[_Line]], page_width: float) -> bool:
    """Whether the group of 'label value' rows from `rows[index]` (`run`) stands alone, with no value column continuing
    above or below, and its two columns are close (a column of right-aligned numeric values, or a group where a pair
    follows again after a wrapped value line, may be far apart)."""
    neighbors = [rows[position] for position in (index - 1, index + len(run)) if 0 <= position < len(rows)]
    column = run[0][1]
    if len(run[-1]) == 1 and run[-1][0].x0 > column.x0 - _TABLE_ROW_TOLERANCE:
        # A group ending in a wrapped value line looks at the next line that overlaps the value column horizontally. In
        # a two-column layout a right-column line, where the left-column paragraph has ended, comes in as a wrapped
        # value line, and the right column continues below it too (left-column subheading rows are skipped).
        right = max(row[1].x1 for row in run if len(row) == 2)
        below = [row for row in rows[index + len(run):] if any(line.x0 < right and column.x0 < line.x1 for line in row)]
        neighbors += below[:1]
    if any(_near(run, row) and any(abs(line.x0 - column.x0) <= _TABLE_ROW_TOLERANCE for line in row) for row in neighbors):
        return False

    # If a 'label value' row follows again after a line where only the value wrapped, it is a table with row structure
    # (in two side-by-side text boxes the left line is never empty). Joined even when the columns are far apart —
    # LibreOffice splits the column widths of a borderless two-column table in half, leaving label and value 30% of
    # the page width apart.
    wrapped = any(len(row) == 1 and len(later) == 2 and abs(later[1].x0 - row[0].x0) <= _TABLE_ROW_TOLERANCE
                  for position, row in enumerate(run) for later in run[position + 1:position + 2])
    return (column.x0 - max(row[0].x1 for row in run if len(row) == 2) <= page_width * _LABEL_GAP_RATIO
            or _right_aligned_numbers(run) or wrapped)


def _wraps_value(run: list[list[_Line]], row: list[_Line], max_label: float) -> bool:
    """Whether `row` is a line wrapped from the last value of a group of 'label value' rows (`run`): a single fragment
    shares the left edge of the value column, is attached to the value line right above at the line spacing inside a
    cell (`_VALUE_WRAP_GAP_RATIO`), has the same size, and does not reach further right than the group's value column.
    The labels of the group must all be short (`_WRAP_LABEL_RATIO`) — in two-column text made of short lines, a
    right-column line where the left-column paragraph had ended came in as a wrapped value line and the two columns
    were joined row by row (generated two-column document).

    A line smaller than the value line above (down to `_VALUE_WRAP_MIN_SIZE_RATIO`) is also accepted, and such a line
    may reach further right than the value column — in a programme schedule the small print '한국제조혁신협회 회장
    김혁신' under '개회사' was not accepted as a wrapped line, so all the times were read before all the sessions
    (Pinned by: test_pdf_label_value_rows.py::TestShortLabelsWithLongerValues::
    test_program_schedule_with_a_smaller_second_line_keeps_each_time_with_its_session). Right-column lines of two
    columns have the same size.

    In a borderless two-column table from LibreOffice or Word, the next line of a value wrapped at the cell width sat
    alone in the value column and cut the group, and the cut group was discarded because of the value-column fragment
    in the neighbouring row, so the group of labels and the group of values were split wholesale (Pinned by:
    test_pdf.py::TestFlowJoins::test_wrapped_value_lines_stay_under_their_label). A wrapped line is accepted only
    inside a group, so a right column continuing above and below (two-column layout) is still filtered out by the
    neighbouring-row condition outside the group (`_stands_apart`).
    """
    if len(row) != 1:
        return False
    line, above = row[0], run[-1][-1]
    values = [pair[1] for pair in run if len(pair) == 2]
    return (all(_short_label(pair, max_label) for pair in run if len(pair) == 2)
            and abs(line.x0 - values[0].x0) <= _TABLE_ROW_TOLERANCE
            and above.size * _VALUE_WRAP_MIN_SIZE_RATIO - 0.5 <= line.size <= above.size + 0.5
            and line.top - above.bottom <= above.height * _VALUE_WRAP_GAP_RATIO and line.top > above.top + above.height / 2
            and (line.x1 <= max(value.x1 for value in values) + line.size * 2 or line.size < above.size - 0.5))


def _short_label(row: list[_Line], max_label: float) -> bool:
    """Whether the label of a 'label value' row is short enough to accept a wrapped value line (`_WRAP_LABEL_RATIO`).
    A wider label with two words or fewer (`_LABEL_MAX_WORDS`) is accepted as well.

    '정부지원연구개발비' and '총 연구개발기간' in an R&D form exceed 15% of the page width, so when the value wrapped,
    the group of labels and the group of values were split wholesale (Pinned by:
    test_pdf_label_value_rows.py::TestLongLabelsWithWrappedValues). The left-column lines of two columns of short
    lines, which the 15% cap used to block, are body text of several words (Pinned by: test_pdf.py::TestFlowJoins::
    test_two_columns_of_short_lines_are_not_joined_through_a_line_only_on_the_right). Known accepted gap: two columns
    made of wide body lines of two words or fewer cannot be told apart.
    """
    label = row[0]
    return label.x1 - label.x0 <= max_label * _WRAP_LABEL_RATIO or len(label.text.split()) <= _LABEL_MAX_WORDS


def _has_wrapped_value(run: list[list[_Line]]) -> bool:
    """Whether a group of 'label value' rows has a wrapped value line (a fragment alone in the value column)."""
    return any(len(row) == 1 and row[0].x0 > run[0][1].x0 - _TABLE_ROW_TOLERANCE for row in run)


def _sectioned_values(run: list[list[_Line]], max_label: float) -> bool:
    """Whether the shape counts as a 'label value' group even with many wide values: there are two or more labels, all
    short (`_short_label`) and not digits only, and a pair follows again right after a line where the value wrapped —
    the label column is empty for the several lines of a value and starts again at the next item.

    In a history (date | multi-line content) or side headings (section title | body paragraph), half or more of the
    rows have a value over half the page width, so the group was discarded and all labels were read before all values
    (Pinned by: test_pdf_label_value_rows.py::TestShortLabelsWithLongerValues). A document with line numbers has
    digit-only labels and is discarded as before (test_line_numbers_beside_wide_lines_are_not_label_value_rows in the
    same class). In two columns where the lines of a narrow side column sit beside the body lines line by line, no pair
    follows again after a wrapped line.
    """
    pairs = [row for row in run if len(row) == 2]
    if len(pairs) < 2 or any(not _short_label(row, max_label) or _LINE_NUMBER_LABEL.match(row[0].text) for row in pairs):
        return False
    return any(len(row) == 1 and len(later) == 2 for row, later in zip(run, run[1:], strict=False))


def _with_wrapped_values(run: list[list[_Line]]) -> list[list[_Line]]:
    """Attach wrapped value lines (single-fragment rows) to the 'label value' row right above. Label rows without a
    value (`_is_empty_value_row`) are left as they are."""
    rows: list[list[_Line]] = []

    for row in run:
        if len(row) == 1 and rows and len(rows[-1]) >= 2 and row[0].x0 >= rows[-1][1].x0 - _TABLE_ROW_TOLERANCE:
            rows[-1] = [*rows[-1], row[0]]
        else:
            rows.append(row)
    return rows


def _right_aligned_numbers(run: list[list[_Line]]) -> bool:
    """Whether the values of a group of 'label value' rows are all numeric values (`_FAR_VALUE`: amounts, ratios,
    dates, page numbers; units such as '원' allowed) with matching right edges.

    Right-aligned amounts in a payslip, receipt or invoice are more than 20% of the page width (`_LABEL_GAP_RATIO`)
    away from the items, so the items bunched together and the amounts bunched together
    (Pinned by: test_pdf_forms_and_amounts.py::TestFarRightAmounts). The right column of two side-by-side forms or text
    boxes is not numeric values only, so it does not qualify (Deliberate — the distance condition is left as is and
    only this shape is additionally accepted, test_quality.py::
    test_pdf_side_by_side_form_xobjects_are_not_interleaved).

    The first row may be a non-numeric header ('항목 | 금액(원)') — when there are two or more numeric values below
    with the same right edge. Because of the header the whole amount column fell after the next section (Pinned by:
    test_pdf_forms_and_amounts.py::TestFarRightAmounts::
    test_amount_column_with_a_header_word_stays_beside_its_items).
    """
    values = [row[1] for row in run if len(row) == 2]
    header = (len(run[0]) == 2 and len(values) >= 3 and len(run[0][1].text) <= _HEADER_CELL_MAX_CHARS
              and not _FAR_VALUE.match(run[0][1].text.replace(" ", "")))
    numbers = values[1:] if header else values
    return all(abs(value.x1 - values[0].x1) <= _TABLE_ROW_TOLERANCE for value in values) and all(
        _FAR_VALUE.match(value.text.replace(" ", "")) for value in numbers)


def _pairs_with(run: list[list[_Line]], row: list[_Line], max_label: float) -> bool:
    """Whether `row` is a label-value pair and fits the same two columns right below the last row of `run` (if `run`
    is empty, only whether it is a pair).

    The label is at least two characters. An identifier printed vertically in the left margin of a paper ('arXiv:…')
    or a drop cap came out as one-character lines and was joined with the body line beside it (public paper PDF). The
    value width is not checked here but over the whole group (`_label_value_rows`) — when one long value cut the
    group, both cut halves were discarded because of the value-column fragment in the neighbouring row (Pinned by:
    test_pdf.py::TestFlowJoins::
    test_label_value_rows_with_one_value_longer_than_half_the_page_keep_the_pairs).
    """
    if len(row) != 2:
        return False
    label, value = row
    if label.x1 - label.x0 > max_label or len(label.text.replace(" ", "")) < 2 or value.x0 < label.x1:
        return False
    if not run:
        return True

    first_label, first_value = run[0]
    # Table of contents without leaders: items indented per level also belong to the same group if they have a
    # right-aligned page number. All the items were read first and then the page numbers came out bunched together
    # (Pinned by: test_pdf_toc_and_checkboxes.py::TestTableOfContentsWithoutLeaders).
    indented = (bool(_BARE_NUMBER.match(value.text)) and bool(_BARE_NUMBER.match(first_value.text))
                and abs(value.x1 - first_value.x1) <= _TABLE_ROW_TOLERANCE
                and abs(label.x0 - first_label.x0) <= max(label.size, first_label.size) * _TOC_INDENT_RATIO)
    return (_near(run, row) and (abs(label.x0 - first_label.x0) <= _TABLE_ROW_TOLERANCE or indented)
            and (abs(value.x0 - first_value.x0) <= _TABLE_ROW_TOLERANCE
                 or abs(value.x1 - first_value.x1) <= _TABLE_ROW_TOLERANCE))


def _is_empty_value_row(run: list[list[_Line]], row: list[_Line], max_label: float) -> bool:
    """Whether `row` is a label row without a value right below a group of 'label value' rows (`run`): a single
    fragment shares the left edge of the label column, is at least two characters, and ends before the value column
    starts. The caller adds it to the group only when the next row is a pair again.

    The valueless '(경유)' row in the head of an official letter, '수신 / (경유) / 제목', cut the group, the pairs fell
    short of 2 rows, and the labels and values came out grouped separately
    (Pinned by: test_pdf.py::TestFlowJoins::test_label_row_with_an_empty_value_between_label_value_rows_keeps_the_pairs).
    """
    label, value = run[0]
    line = row[0]
    return (len(row) == 1 and _near(run, row) and abs(line.x0 - label.x0) <= _TABLE_ROW_TOLERANCE
            and line.x1 - line.x0 <= max_label and len(line.text.replace(" ", "")) >= 2 and line.x1 < value.x0)


def _near(run: list[list[_Line]], row: list[_Line]) -> bool:
    """Whether `row` is the row right above or below `run` (by the row spacing criterion of borderless tables)."""
    top, bottom = min(line.top for line in run[0]), max(line.bottom for line in run[-1])
    height = run[0][0].height
    return row[0].top - bottom <= height * 2.5 and top - max(line.bottom for line in row) <= height * 2.5


def _visual_rows(lines: list[_Line], staggered: bool = False) -> list[list[_Line]]:
    """Group line fragments at the same height into one row (top to bottom, left to right within a row). Same height =
    top coordinates within `_TABLE_ROW_TOLERANCE`.

    With `staggered`, a fragment whose top is out of step is also grouped if it has the same font size as the topmost
    fragment of the row and overlaps it vertically by at least `_ROW_OVERLAP_RATIO` of the smaller line height — only
    between a fragment containing Hangul/Hanja and one that does not. A generator that top-aligns table cells
    (LibreOffice) has a different ascent per font, so the baselines and glyph boxes of the Hangul cells and number
    cells of the same row were 4.5–5pt out of step (11pt font). Looking at the top alone, '사원' and '3,200,000'
    became different rows and the borderless table scattered column by column. Two consecutive lines do not overlap by
    as much as half even at 100% line spacing. Used only for the second search for borderless tables
    (`_aligned_table_runs`); without the character-type condition, staggered fragments of equations in a paper became
    a table (Pinned by: test_pdf.py::TestRepeatedMarginsAndBorderlessTables::
    test_borderless_table_row_is_grouped_when_fonts_put_the_cells_at_different_heights).
    """
    def same_row(first: _Line, line: _Line) -> bool:
        if abs(line.top - first.top) <= _TABLE_ROW_TOLERANCE:
            return True
        overlap = min(first.bottom, line.bottom) - line.top
        return (staggered and bool(first.size) and abs(first.size - line.size) <= 0.5
                and bool(_SPACED_CHAR.search(first.text)) != bool(_SPACED_CHAR.search(line.text))
                and overlap >= min(first.height, line.height) * _ROW_OVERLAP_RATIO)

    rows: list[list[_Line]] = []
    for line in sorted(lines, key=lambda line: (line.top, line.x0)):
        if rows and same_row(rows[-1][0], line):
            rows[-1].append(line)
        else:
            rows.append([line])
    return [sorted(row, key=lambda line: line.x0) for row in rows]


def _column_of(line: _Line, header: list[_Line], row: list[_Line] | tuple[()] = ()) -> int:
    """The column a fragment falls into (`_header_column`). Given the fragments of the same row, `row`, a text fragment
    (one containing letters, with no header matching its left or right edge) that overlaps no header horizontally and
    lies in the gap between two headers is sent to the column of the right header when its centre is closer to it —
    only when no other fragment of the same row falls into that column.

    Deliberate: left-aligned cell text under a narrow header centred over a wide column ('항목', '담당자') has its
    centre left of the header's start, so it went one column too early ('손익 매출액 | |'), mashing the row or
    dropping the header row from the table (Pinned by: test_pdf_table_columns.py::
    TestCentredHeadersOverLeftAlignedCells). A number fragment is a right-aligned value overflowing its header, so it
    is not sent — it is sent only when an earlier fragment of the same row already occupies the left column
    (left-aligned number column: '4,5' in the '주석' (notes) column of a financial statement overlapped the account
    column and the table split in two, Pinned by:
    test_left_aligned_note_numbers_under_a_centred_header_stay_in_its_column). If another fragment of the same row is
    in the right column, this fragment is text spread across the previous cell (justified '운영      경비', Pinned by:
    test_pdf_forms_and_amounts.py::TestDistributedLabelsInABorderlessTable). Known accepted gap: decisions made without
    knowing the row (section rows, accepting rows above) keep the old rule.
    Whether the right column is occupied is judged by the column the other fragments move to under the same gap rule
    (`_gap_column`) — going by header positions alone, '확정' lying in the gap before the '비고' header was counted in
    the '내용' column, so the '내용' cell text stayed in the '구분' column and the header row dropped out of the table
    (Pinned by:
    test_pdf_table_columns.py::TestCentredHeadersOverLeftAlignedCells::test_cells_in_the_gaps_before_every_centred_header).
    """
    column = _header_column(line, header)
    later = _gap_column(line, header, row, column)
    if later == column or any(other is not line and _gap_column(other, header, row) == later for other in row):
        return column
    return later


def _gap_column(line: _Line, header: list[_Line], row: list[_Line] | tuple[()], column: int | None = None) -> int:
    """The column as in `_column_of` but without the check on whether the right column is occupied: if a text fragment
    in the gap between two headers goes to the right header because its centre is closer, that column; otherwise
    `_header_column`. A text fragment goes to the right column regardless of its centre when an earlier fragment of
    the same row occupies the left column — short cell text in a wide column ('관계부처 국장급 12명') has its centre
    closer to the left header and overlapped the first-column text (Pinned by:
    test_pdf_table_columns.py::TestCentredHeadersOverLeftAlignedCells::test_cells_in_the_gaps_before_every_centred_header)."""
    if column is None:
        column = _header_column(line, header)
    if not row or any(abs(cell.x0 - line.x0) <= _TABLE_ROW_TOLERANCE or abs(cell.x1 - line.x1) <= _TABLE_ROW_TOLERANCE
                      for cell in header):
        return column
    later = next((index for index, cell in enumerate(header) if cell.x0 >= line.x1), None)
    if not later or header[later - 1].x1 > line.x0 or later != column + 1:
        return column
    taken = any(other is not line and other.x1 <= line.x0 and _header_column(other, header) == column for other in row)
    words = any(letter.isalpha() for letter in line.text)
    if not words and not taken:
        return column  # A number fragment only when an earlier fragment of the row occupies the left column (left-aligned numbers)
    if words and taken:
        # The left column belongs to the earlier fragment of the same row. Short cell text in a wide column has its
        # centre closer to the left header
        return later

    center = (line.x0 + line.x1) / 2
    left_cell, right_cell = header[later - 1], header[later]
    if (right_cell.x0 + right_cell.x1) / 2 - center >= center - (left_cell.x0 + left_cell.x1) / 2:
        return column
    return later


def _header_column(line: _Line, header: list[_Line]) -> int:
    """The column a fragment falls into: the header whose left edge matches → the header whose right edge matches and
    with no other header overlapped → if exactly one header overlaps horizontally, that header (left-aligned cell text
    spanning only one centred header — Pinned by: test_pdf_table_columns.py::
    TestCentredHeadersOverLeftAlignedCells) → the interval its centre falls into (measured from header starts).

    In a right-aligned table (financial table), when a number is wider than its header its centre crosses into the
    interval of the column to the left, so the value went one column to the left or the header row dropped out of the
    table (Pinned by: test_pdf.py::TestRepeatedMarginsAndBorderlessTables::
    test_borderless_table_with_right_aligned_numbers_wider_than_headers). Where pdfminer merged two columns into one
    fragment ('0.627 ± 0.080 +0.084'), the right edge matches the later column but the fragment also lies over the
    header of the earlier column, so the column is not decided by the right edge (public paper PDF).
    """
    def closest(distance: Callable[[_Line], float]) -> int | None:
        index = min(range(len(header)), key=lambda index: abs(distance(header[index])))
        return index if abs(distance(header[index])) <= _TABLE_ROW_TOLERANCE else None

    left = closest(lambda cell: cell.x0 - line.x0)
    if left is not None:
        return left
    right = closest(lambda cell: cell.x1 - line.x1)
    overlapped = [index for index, cell in enumerate(header) if cell.x0 < line.x1 and line.x0 < cell.x1]
    if right is not None and overlapped in ([], [right]):
        return right
    if len(overlapped) == 1:
        return overlapped[0]

    center = (line.x0 + line.x1) / 2
    column = 0
    for index, cell in enumerate(header):
        if center >= cell.x0 - _TABLE_ROW_TOLERANCE:
            column = index
    return column


def _body_font_size(pages: list[_Page]) -> float:
    """Body font size of the document = the most frequent size weighted by character count (0.5pt steps)."""
    weights: Counter[float] = Counter()
    for page in pages:
        for line in page.lines:
            if line.size:
                weights[round(line.size * 2) / 2] += len(line.text)
    return weights.most_common(1)[0][0] if weights else 0.0


def _typical_line_gap(pages: list[_Page]) -> float:
    """Typical line gap of the document = the most frequent value of (gap between consecutive lines of the same font
    size ÷ line height) (0.05 steps).

    Measuring as a ratio of the height lets the line spacing of headings (large font) and of body text be judged by
    one criterion.
    """
    ratios: Counter[float] = Counter()
    for page in (page for page in pages if not page.simple):
        for previous, line in zip(page.lines, page.lines[1:], strict=False):
            gap = line.top - previous.bottom
            same_size = abs(previous.size - line.size) <= 0.1 * max(previous.size, line.size, 1.0)
            if same_size and 0 <= gap <= previous.height * 1.5:
                ratios[round(gap / previous.height * 20) / 20] += 1
    return ratios.most_common(1)[0][0] if ratios else 0.0


def _remove_repeated_margins(pages: list[_Page]) -> None:
    """Remove the lines at the top and bottom edges of pages that repeat on half or more of the pages.

    Fragments at the same height (a row) are treated as one unit. Looking at each fragment separately removed the
    fragments of a header row repeated on every page of a borderless table spanning several pages, and the amounts and
    dates of bottom rows differing only in digits. So (1) rows grouped into a borderless table are excluded from the
    candidates, (2) the comparison that ignores digits is applied only to single-fragment margin lines, and for a row
    of several fragments only the digits of page-number-shaped fragments are ignored and the rest must match character
    for character (Pinned by: test_pdf.py::TestRepeatedMarginsAndBorderlessTables).

    Kinds of lines that are body text but look repeated are also excluded from the candidates (the pinning tests are
    in the same class).
    - Rows grouped as a 'label  value' two-column layout (`_label_value_rows`): the value (a bare number) of a row
      reaching down into the margin area was removed as a page number ('해지 건수 5' → '해지 건수'), and when rows
      differing only in the value were on every page the whole row was removed
      (test_label_value_rows_in_the_margin_area_keep_their_values).
    - Lines attached to a ruled table outside the margin (`_caption_lines`): in a document whose pages are mostly a
      table, '(단위: 백만원)' above the table and the note below it fell into the top and bottom two rows of the page
      and were removed on every page
      (test_caption_lines_attached_to_a_ruled_table_are_not_removed_as_repeated_lines). Edge rows outside the margin
      are not excluded from the candidates wholesale — a header or page number placed outside the margin would remain
      (public paper and book PDFs, test_repeated_header_outside_the_margin_area_is_still_removed).
    - Values of a body column (`_is_column_value`): a row whose text, the same except for digits, also appears in the
      body of that page (outside the margin and edge rows) at the same position (left edge or right edge)
      `_DATA_REPEATS` times or more. A header or page number appears once per page. A single column (owner names)
      pushed onto following pages by overflowing the print width has the same value at the top and bottom of every
      page, so the edge values were removed (402 of 6,000), and in a number column the values in the margin area were
      removed like page numbers
      (test_values_of_a_one_column_list_at_the_page_edges_are_not_removed_as_repeated_lines).
      The position is checked as well so that the page number of a page with the same number in figure tick labels
      does not remain (public paper PDF).
    """
    if len(pages) < 3:
        return
    title_size = _body_font_size(pages) * _HEADING_MIN_RATIO

    def margin_keys(page: _Page) -> dict[str, list[_Line]]:
        # Rows in the margin area + the top and bottom two rows of the page. Slide-style PDFs have the header outside the margin.
        rows = _visual_rows(page.lines)
        edge = {id(line) for row in _edge_rows(rows) for line in row}
        cells = [line for run in _aligned_table_runs(page) for row in run for line in row]
        protected = {id(line.source or line) for line in cells}
        # A cell joined from several lines stands for its original lines, which are the ones in `page.lines`: unprotected,
        # the first line of a wrapped cell in the top row of a table repeated on every page was removed and the table fell
        # apart (Pinned by: test_audit_pdf_tail.py::TestJoinedCellLinesAtThePageTop::
        # test_wrapped_first_cell_survives_on_every_page).
        protected.update(id(original) for line in cells for original in line.joined)
        protected.update(id(line) for row in _label_value_rows(rows, page.width) for line in row)
        captions = _caption_lines(page)
        body: dict[str, list[list[_Line]]] = {}  # Text with digits masked → body rows (outside the margin and edge rows)
        for row in rows:
            if id(row[0]) not in edge and not all(_in_margin(line, page) for line in row):
                body.setdefault(_row_shape(row), []).append(row)
        keys: dict[str, list[_Line]] = {}
        for row in rows:
            if any(id(line) in protected for line in row) or _is_column_value(row, body.get(_row_shape(row), ())):
                continue
            in_margin = all(_in_margin(line, page) for line in row)
            if not in_margin and (id(row[0]) not in edge or any(id(line) in captions for line in row)):
                continue
            keys.setdefault(_row_key(row, in_margin, title_size), []).extend(row)

            # Page number fragments are also considered on their own. Even if other fragments at the same height (a
            # header differing on odd and even pages, line numbers of a proof) differ from page to page, the page
            # number is removed. The criterion is the same as for a single-fragment line (digits ignored in the
            # margin, character for character at the edge outside the margin).
            for line in row:
                if _PAGE_NUMBER.match(line.text):
                    keys.setdefault(_row_key([line], in_margin, title_size), []).append(line)
        return keys

    per_page = [margin_keys(page) if not page.simple else {} for page in pages]
    repeated = _repeated_keys(per_page)
    # A book has different headers and footers on even and odd pages ('2 자유 문화' / '해적질 3'), so neither reaches
    # half of all pages. They are also counted separately per page parity and, if repeated on 2/3 or more of those
    # pages, removed from those pages
    # (Pinned by: test_pdf.py::TestRepeatedMarginsAndBorderlessTables::
    # test_book_footers_alternating_by_page_side_inside_the_crop_box_are_removed,
    # test_margin_line_on_a_minority_of_one_page_side_is_kept).
    sides = {side: _repeated_keys([keys for page, keys in zip(pages, per_page, strict=True) if page.number % 2 == side],
                                  _SIDE_REPEAT_RATIO)
             for side in (0, 1)}
    for page, keys in zip(pages, per_page, strict=True):
        drop = {id(line) for key in repeated | sides[page.number % 2] for line in keys.get(key, [])}
        page.lines = [line for line in page.lines if id(line) not in drop]


def _row_shape(row: list[_Line]) -> str:
    """The text of a row with digits masked. Rows of the same shape differing only in value ('12,345' and '67,890')
    count as one kind."""
    return "\t".join(_DIGITS.sub("#", line.text) for line in row)


def _is_column_value(row: list[_Line], alike: list[list[_Line]] | tuple) -> bool:
    """Whether `row` is a value of a body column: at least `_DATA_REPEATS` of the body rows of the same shape (`alike`)
    share its left edge or right edge."""
    aligned = sum(1 for other in alike if abs(other[0].x0 - row[0].x0) <= _TABLE_ROW_TOLERANCE
                  or abs(other[-1].x1 - row[-1].x1) <= _TABLE_ROW_TOLERANCE)
    return aligned >= _DATA_REPEATS


def _repeated_keys(per_page: list[dict[str, list[_Line]]], ratio: float = _REPEAT_RATIO) -> set[str]:
    """The keys of repeated lines that appear on `ratio` or more of the pages (at least 3 pages)."""
    counts = Counter(key for keys in per_page for key in keys)
    return {key for key, count in counts.items() if count >= max(3, len(per_page) * ratio)}


def _in_margin(line: _Line, page: _Page) -> bool:
    """Whether the line is in the top or bottom margin area of the page (`_MARGIN_RATIO` of the height of the visible
    area). A print PDF has more paper outside the trim line (CropBox), so measured by the paper height a footer inside
    the trim line fell outside the margin. When the visible area equals the paper, the result is the same as before."""
    _, top, _, bottom = page.text_view or (0.0, 0.0, page.width, page.height)
    band = (bottom - top) * _MARGIN_RATIO
    return line.bottom <= top + band or line.top >= bottom - band


def _caption_lines(page: _Page) -> set[int]:
    """The ids of the lines attached above and below a ruled table at line spacing (`_CAPTION_GAP_RATIO`) — table
    title, unit, notes. Only lines overlapping the table horizontally are considered, stopping where the gap opens up
    in the direction away from the table."""
    attached: set[int] = set()
    for table in page.tables:
        beside = sorted((line for line in page.lines if line.x0 < table.x1 and table.x0 < line.x1), key=lambda line: line.top)
        edge = table.top
        for line in reversed([line for line in beside if line.top < table.top]):
            if edge - line.bottom > line.height * _CAPTION_GAP_RATIO:
                break
            attached.add(id(line))
            edge = min(edge, line.top)

        edge = table.bottom
        for line in (line for line in beside if line.bottom > table.bottom):
            if line.top - edge > line.height * _CAPTION_GAP_RATIO:
                break
            attached.add(id(line))
            edge = max(edge, line.bottom)
    return attached


def _edge_rows(rows: list[list[_Line]]) -> list[list[_Line]]:
    """The top two rows and the bottom two rows of the page (from a list of rows ordered top to bottom)."""
    return rows[:2] + rows[max(len(rows) - 2, 2):]


def _row_key(row: list[_Line], in_margin: bool, title_size: float) -> str:
    """The text of a row used for the repetition check. A single-fragment margin line ignores digits, and an edge line
    outside the margin must match down to the digits — so that chapter titles like "제1장", "제2장" are not removed.
    With several fragments, only the digits of page-number-shaped fragments are ignored.

    Even a margin line must match down to the digits if it is at least title size (`title_size` = body size ×
    `_HEADING_MIN_RATIO`) and not page-number-shaped. Page titles placed in the top margin band ('2025년 3월 영업
    실적') differed only in digits and were removed on every page (Pinned by: test_pdf.py::
    TestRepeatedMarginsAndBorderlessTables::test_large_page_titles_that_differ_only_in_a_number_are_kept).
    """
    if len(row) == 1:
        line = row[0]
        is_title = bool(title_size) and line.size >= title_size and not _PAGE_NUMBER.match(line.text)
        return _page_number_blind(line.text) if in_margin and not is_title else line.text
    return "\t".join(_DIGITS.sub("#", line.text) if _PAGE_NUMBER.match(line.text) else line.text for line in row)


def _page_number_blind(text: str) -> str:
    """The text with '#' masking only the digits of tokens that can be a page number. If the whole text is
    page-number-shaped (`_PAGE_NUMBER`), all of its digits are masked.

    A token is masked only when what remains after removing the digits is punctuation only, a page word
    (`_PAGE_WORDS`) or a single ASCII letter ('3', '3/10', '3쪽', 'p.3', '2024-01-05', 'A-1'). Masking every digit of a
    margin line removed single-fragment value lines differing only in digits from page to page ('소계 12건',
    '소계 7건') as repeated lines. A running header mixing a date and page count ('2024-01-05 보고서 3쪽') has all its
    tokens masked and is still removed (Pinned by: test_pdf.py::
    TestRepeatedMarginsAndBorderlessTables::test_margin_lines_with_a_count_that_differs_per_page_are_kept).

    Value-shaped tokens (`_VALUE_TOKEN`: '3,450,000', '6.8%', '₩1,234', '1.5') are not masked even though they have
    nothing but punctuation besides digits. '실지급액: 3,450,000', '전기이월 잔액: 1,000,000' and '증감률 6.8%' placed
    in the margin of every page were removed as repeated lines (Pinned by:
    test_margin_lines_with_amounts_or_rates_that_differ_per_page_are_kept in the same class).
    """
    if _PAGE_NUMBER.match(text):
        return _DIGITS.sub("#", text)

    def blind(match: re.Match) -> str:
        token = match.group()
        if not _DIGITS.search(token) or _VALUE_TOKEN.search(token):
            return token
        rest = _NON_WORD.sub("", _DIGITS.sub("", token)).lower()
        return _DIGITS.sub("#", token) if not rest or rest in _PAGE_WORDS or (len(rest) == 1 and rest.isascii()) else token

    return _TOKEN.sub(blind, text)


class _BoxIndex:
    """An index of boxes (x0, top, x1, bottom) split into height bands. `holds(obj)` is equivalent to
    `_inside_any(obj, boxes)`.

    On a page with 7,500 cells every character scanned all the cells and one page took tens of seconds (Pinned by:
    test_pdf_resource_limits.py::TestManyCellsOnOnePage). Boxes taller than `_MAX_BANDS` bands (cells reaching far off
    the page) and boxes with non-finite coordinates are not put into bands and are always scanned — making bands for
    the full height of every cell made a 1.6KB PDF use 13GB (Pinned by: TestCellsReachingFarOffThePage)."""

    _BAND = 16.0
    _MAX_BANDS = 128  # 2,048pt — every cell inside a page fits into bands

    def __init__(self, boxes: list[tuple[float, float, float, float]]) -> None:
        self._bands: dict[int, list[tuple[float, float, float, float]]] = {}
        self._tall: list[tuple[float, float, float, float]] = []
        for box in boxes:
            if not (math.isfinite(box[1]) and math.isfinite(box[3])) or box[3] - box[1] > self._BAND * self._MAX_BANDS:
                self._tall.append(box)
                continue
            for band in range(math.floor(box[1] / self._BAND), math.floor(box[3] / self._BAND) + 1):
                self._bands.setdefault(band, []).append(box)

    def holds(self, obj: dict) -> bool:
        center_y = (obj["top"] + obj["bottom"]) / 2
        band = math.floor(center_y / self._BAND) if math.isfinite(center_y) else None
        return _inside_any(obj, self._bands.get(band, [])) or _inside_any(obj, self._tall)


def _inside_any(obj: dict, boxes: list[tuple[float, float, float, float]]) -> bool:
    center_x = (obj["x0"] + obj["x1"]) / 2
    center_y = (obj["top"] + obj["bottom"]) / 2
    return any(x0 <= center_x <= x1 and top <= center_y <= bottom for x0, top, x1, bottom in boxes)


def _metadata(decoded: dict, infos: list[dict]) -> dict[str, str]:
    """The title and author from the document information. String values are decoded again from the original bytes
    (`infos`) instead of the values decoded by pdfplumber (`decoded`)."""
    raw: dict = {}
    for info in infos:
        raw.update(info)

    metadata = {}
    for key, source in (("title", "Title"), ("author", "Author")):
        try:
            value = resolve1(raw.get(source))
        except Exception:  # noqa: BLE001 — like pdfplumber, skip a value that cannot be resolved (reading the document continues)
            value = None
        value = _info_text(value) if isinstance(value, bytes) else decoded.get(source)
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, str) and value.strip():
            metadata[key] = value.strip()
    return metadata


def _info_text(value: bytes) -> str:
    """Decode the bytes of a document information string. The standard is UTF-16BE (BOM) or PDFDocEncoding, but some
    generators write a Korean title as raw UTF-8 or CP949 bytes, which read as PDFDocEncoding became 'íŁœê¸• ì€œëª©'
    (Pinned by: test_pdf.py::TestInfoStrings). If there are high bytes, strict UTF-8 and then strict EUC-KR yielding
    Hangul are tried in that order, and if neither works it is PDFDocEncoding. Latin PDFDocEncoding text almost always
    fails both decodings (a space after 'é', etc.). Accepting the CP949 extension characters as well turned 'Opiełka'
    (ł = 0x9B) into 'Opie쌽a'."""
    if value.startswith((b"\xfe\xff", b"\xff\xfe")):
        return value.decode("utf-16", errors="replace")
    if value.startswith(b"\xef\xbb\xbf"):
        return value[3:].decode("utf-8", errors="replace")  # UTF-8 of PDF 2.0
    if any(byte >= 0x80 for byte in value):
        for encoding in ("utf-8", "euc_kr"):
            try:
                text = value.decode(encoding)
            except UnicodeDecodeError:
                continue
            if encoding == "utf-8" or any("가" <= char <= "힣" for char in text):
                return text
    return decode_text(value)


def _page_list(numbers: list[int]) -> str:
    shown = ", ".join(str(number) for number in numbers[:10])
    return f"{shown} and more" if len(numbers) > 10 else shown
