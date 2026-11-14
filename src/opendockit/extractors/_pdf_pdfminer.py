"""What is replaced in pdfminer and pdfplumber so that a hostile or damaged PDF stays within the limits and an odd font
still gives its text: reference loops, the budget for inflated streams, the count of drawn objects, the size of font
tables, overprinted copies, symbol fonts, fill opacity, Type 3 fonts and embedded CMaps.

Importing this module replaces functions of pdfminer for the whole process.
"""

from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import lru_cache
from io import BytesIO
from itertools import count, islice
import math
import re
import zlib

from pdfminer import layout as pdfminer_layout
from pdfminer import pdffont
from pdfminer.ascii85 import ascii85decode, asciihexdecode
from pdfminer.cmapdb import CMap, CMapParser, FileUnicodeMap
from pdfminer.layout import LTChar, LTLayoutContainer, LTTextBoxVertical
from pdfminer.pdfinterp import PDFGraphicState, PDFPageInterpreter
from pdfminer.pdftypes import (
    LITERALS_ASCII85_DECODE,
    LITERALS_ASCIIHEX_DECODE,
    LITERALS_FLATE_DECODE,
    PDFObjRef,
    PDFStream,
    resolve1,
)
from pdfminer.utils import choplist
from pdfplumber.page import PDFPageAggregatorWithMarkedContent

from opendockit.exceptions import DocumentTooLargeError
from opendockit.extractors._pdf_model import _SYMBOL_BULLETS
from opendockit.extractors._symbols import symbol_glyph


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
    charged: dict[int | tuple[int, int | None], object] = field(default_factory=dict)  # id, or (object number, generation)
    walked: dict[int, object] = field(default_factory=dict)
    font_entries: int = 0  # number of entries font ranges (/W, mapping tables) may still expand to (`_bounded_font_ranges`)
    font_ranges_cut: bool = False
    painted: dict[int, tuple[object, int]] = field(default_factory=dict)  # content stream → paint operator count (`_drawn_objects`)
    forms: dict[tuple[int, int], tuple[object, ...]] = field(default_factory=dict)  # form → drawn object count (`_content_objects`)
    undecoded_fonts: bool = False  # met a CID font whose encoding CMap was not found, so no character decodes (`_cmap_or_embedded`)
    ungrouped_layouts: int = 0  # layouts laid out without text-box grouping for having too many text boxes (`_group_few_textboxes`)
    grouping_cost: int = 0  # text-box grouping cost spent on the page being read (`_PAGE_GROUPING_COST`, reset per page)


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
    """Charges the size the stream's Flate stages will inflate to against the limit. Raises DocumentTooLargeError when exceeded.
    Other filters and broken streams are not measured (pdfminer decodes them as before — ASCII filters shrink the data, image
    filters are not decoded).

    Every Flate stage is charged, not only the first: with `/Filter [/FlateDecode /FlateDecode]` the first stage inflated to a
    few KB and passed, and pdfminer then inflated the second to 200MB under a 10MB limit. A stage that is followed by another
    Flate stage has been measured to fit, so it is decoded by pdfminer's own decoder (predictor included) to get the next stage's
    input; a stream with a single Flate stage is only measured, as before
    (Pinned by: test_audit_pdf_head.py::TestStreamCompressedTwice)."""
    # Keyed by the object's number in the file: the second pdfplumber document (`ungrouped`, pages with very many text pieces)
    # builds new stream objects for the same file objects, and by `id` each stream decoded there was charged a second time (a
    # dense page inflating to 6.4MB was rejected at a 10MB limit). A stream without a number is keyed by `id` as before
    # (Pinned by: test_audit_pdf_head.py::TestStreamReadThroughTheUngroupedDocument).
    key = id(stream) if stream.objid is None else (stream.objid, stream.genno)
    if key in budget.charged or stream.rawdata is None:
        return
    budget.charged[key] = stream
    try:
        data = stream.rawdata
        if stream.decipher:
            data = stream.decipher(stream.objid, stream.genno, data, stream.attrs)
        filters = stream.get_filters()
        for index, (name, params) in enumerate(filters):
            if name in LITERALS_ASCII85_DECODE:
                data = ascii85decode(data)
            elif name in LITERALS_ASCIIHEX_DECODE:
                data = asciihexdecode(data)
            elif name in LITERALS_FLATE_DECODE:
                budget.remaining -= _inflated_size(data, budget.remaining + 1)
                if budget.remaining < 0 or not any(later in LITERALS_FLATE_DECODE for later, _ in filters[index + 1:]):
                    break
                stage = PDFStream({"Filter": name, "DecodeParms": params}, data)
                _decode_unbounded(stage)
                data = stage.data
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
    # Only the first `limit + 1` calls are looked at: every call is at least one object, so with more the estimate is over the limit
    # anyway. `findall` listed every name first — a 65MB stream of five million different names held 500MB
    # (Pinned by: test_audit_pdf_head.py::TestManyDistinctXObjectNames).
    calls = Counter(match[1] for match in islice(_XOBJECT_CALL.finditer(data), limit + 1))
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
        # NaN is keyed as None: it is unequal even to itself, so copies drawn at a NaN position each got a key of their own and
        # were never capped (Pinned by: test_audit_pdf_text.py::TestCharactersAtNonFinitePositions)
        key = (char.get_text(), char.fontname, *(None if value != value else round(value, 1)
                                                 for value in (char.size, char.x0, char.y0)))
        copies[key] += 1
        if copies[key] > _MAX_OVERPRINTS:
            objs.pop()
    return advance


# Symbol font PostScript name (subset prefix, ',Bold' and '-Regular' removed; lower case without spaces) → font name in
# `_symbols.SYMBOL_GLYPHS`.
_SYMBOL_FONT_NAMES = {"symbol": "symbol", "symbolmt": "symbol", "wingdings": "wingdings", "wingdings2": "wingdings 2"}


@lru_cache(maxsize=256)
def _symbol_font(fontname: str) -> str | None:
    """pdfminer font name ('ABCDEF+Wingdings-Regular', 'Wingdings2,Bold') → symbol font table name. None if not a symbol font."""
    name = re.sub(r"^[A-Z]{6}\+", "", fontname)
    return _SYMBOL_FONT_NAMES.get(re.split(r"[,-]", name, maxsplit=1)[0].replace(" ", "").lower())


def _symbol_font_glyph(char: LTChar, font: object) -> None:
    """When a symbol font character comes out in the private use area (U+F020–F0FF), replaces it using that font's table
    (`_symbols.symbol_glyph`).

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


# Cost of pdfminer's text-box grouping (`group_textboxes`) a page may spend, as the sum over the page's layouts — the page itself
# and every drawn Form XObject (`all_texts`) — of the square of their text-box counts (`_group_few_textboxes`). Grouping keeps a
# heap of every pair of boxes: one layout of 1,000 separate boxes took 2 seconds, 1,500 6 seconds, 2,000 14 seconds, and a 68KB
# page whose 6,000 characters lie far off the page (each its own box) 122 seconds inside one page, where the time budget does
# not look. Per layout, a 4.3KB form of 1,400 boxes drawn 15 times spent 54 seconds. The pdfium estimate of line pieces
# (`_survey_page`) did not see either: pdfium keeps positions in single precision, so characters 20pt apart at x = 2e9 fell on
# the same spot. 1,000² ≈ 2 seconds; over the 983 pages of the 566 PDFs of the review corpus the largest page spends 848² (0.72 of
# it, 2.0 seconds to read), the next 320² (0.10). Pinned by: test_audit_pdf_text.py::TestTooManyTextBoxes.
_PAGE_GROUPING_COST = 1_000_000
_group_all_textboxes = LTLayoutContainer.group_textboxes


def _group_few_textboxes(self: LTLayoutContainer, laparams: object, boxes: list) -> list:
    """Replacement of pdfminer's `group_textboxes`: during a conversion, a layout whose square of the box count would take the
    page's grouping cost (`grouping_cost`, reset per page by the caller) beyond `_PAGE_GROUPING_COST` is ordered as pdfminer
    orders it without grouping (`boxes_flow` None: vertical boxes right to left, then horizontal boxes top to bottom and left to
    right) — the boxes are analysed, numbered in that order and no group is returned, so `analyze` sorts them by those numbers.
    The conversion is told (`ungrouped_layouts`) so that the page gets the warning of pages read without text-box grouping. The
    function is not copied (same reason as `_first_seen_order`)."""
    budget = _INFLATE_BUDGET.get()
    if budget is None:
        return _group_all_textboxes(self, laparams, boxes)
    cost = len(boxes) ** 2
    if budget.grouping_cost + cost <= _PAGE_GROUPING_COST:
        budget.grouping_cost += cost
        return _group_all_textboxes(self, laparams, boxes)
    budget.ungrouped_layouts += 1
    for box in boxes:
        box.analyze(laparams)
    ordered = sorted(boxes, key=lambda box: (0, -box.x1, -box.y0) if isinstance(box, LTTextBoxVertical) else (1, -box.y0, box.x0))
    for index, box in enumerate(ordered):
        box.index = index
    return []


LTLayoutContainer.group_textboxes = _group_few_textboxes  # outside a conversion (no budget) it behaves as before
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


def _name(value: object) -> str:
    return str(getattr(value, "name", "") or "")
