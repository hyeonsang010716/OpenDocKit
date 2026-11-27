"""What is replaced in pdfminer and pdfplumber so that a hostile or damaged PDF stays within the limits and an odd font
still gives its text: reference loops, the budget for inflated streams, the count of drawn objects, the size of font
tables, overprinted copies, symbol fonts, fill opacity, Type 3 fonts and embedded CMaps.

Importing this module replaces functions of pdfminer for the whole process.
"""

from collections import Counter
from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import lru_cache
from io import BytesIO
from itertools import count, islice
import math
import re
import struct
import unicodedata
import zlib

from pdfminer import ascii85 as pdfminer_ascii85
from pdfminer import layout as pdfminer_layout
from pdfminer import pdffont, pdftypes
from pdfminer.ascii85 import asciihexdecode
from pdfminer.cmapdb import CMap, CMapParser, FileUnicodeMap
from pdfminer.encodingdb import EncodingDB, name2unicode
from pdfminer.layout import LTChar, LTFigure, LTLayoutContainer, LTPage, LTTextBoxHorizontal, LTTextBoxVertical, LTTextLineHorizontal
from pdfminer.pdfinterp import PDFContentParser, PDFGraphicState, PDFPageInterpreter
from pdfminer.pdftypes import (
    LITERALS_ASCII85_DECODE,
    LITERALS_ASCIIHEX_DECODE,
    LITERALS_FLATE_DECODE,
    PDFObjRef,
    PDFStream,
    resolve1,
)
from pdfminer.utils import Plane, apply_matrix_pt, choplist, decode_text
from pdfplumber.page import PDFPageAggregatorWithMarkedContent

from opendockit.exceptions import DocumentTooLargeError
from opendockit.extractors._pdf_bidi import _RIGHT_TO_LEFT, _is_mark
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


# Work pdfminer's grouping of text lines into text boxes (`group_textlines`) may do on one layout: the objects in the plane cells
# it looks through to find each line's neighbours (`Plane.find`) plus the lines it gathers to merge their boxes (`uniq`), counted
# by `_find_counted` and `_uniq_counted`. The page's own layout may spend this on its own and the Form XObjects drawn on the page
# share another such allowance (reset per page read), as for text-box grouping. Lines within the line margin of each other all
# find each other and every line re-adds the whole box it joins: characters drawn alternately at two places, rising 0.1pt every
# two characters, took 4.3 seconds at 2,000 characters (5.4KB), 15.8 seconds at 4,000 and 62.5 seconds at 8,000, inside one page
# and with no warning (boxes_flow None does not avoid it, and the result is few boxes). Over the 566 PDFs of the review corpus the
# largest layout does 3.5 million (0.1 seconds), p99 27,000; 20 million costs about 0.5 seconds. Pinned by:
# test_audit_pdf_text.py::TestTooMuchLineGrouping.
_LINE_GROUPING_WORK = 20_000_000
# Code points the `cmap` tables of the embedded TrueType fonts of one document may name in all (`_unicode_map_of_the_font`).
_DOCUMENT_CMAP_CODES = 12_000_000


# ASCII85 data is decoded this many characters at a time (a multiple of the five that make four bytes).
_ASCII85_PIECE = 5 * 65_536
_ASCII85_WHITE_SPACE = b" \t\n\r\x0b"  # what the standard library's decoder passes over
_ASCII85_ZEROS = ord("z")  # the abbreviation of four zero bytes: one character where five stand otherwise
_ASCII85_AROUND_A_MARK = b" \t\n\r\x0b\x0c"  # the white space of the library's patterns for the marks of the start and the end


def _ascii85_body(data: bytes) -> bytes:
    """The data without the marks of its start (`<~` or `~`) and its end (`~>` or `~`), as pdfminer's `start_re` and `end_re`
    leave it. Those patterns begin with white space of any length, so a search that fails is tried again from every
    character of a run: 80,000 spaces behind the filter took 12 s. This reads each end once (Pinned by:
    test_pdf_resource_limits.py::TestAscii85InPieces::test_marks_are_taken_off_as_the_patterns_of_the_library_do)."""
    start = data.lstrip(_ASCII85_AROUND_A_MARK)
    if start[:1] == b"<":
        start = start[1:].lstrip(_ASCII85_AROUND_A_MARK)
    if start[:1] == b"~":
        data = start[1:].lstrip(_ASCII85_AROUND_A_MARK)
    end = data.rstrip(_ASCII85_AROUND_A_MARK)
    if end[-1:] == b">":
        end = end[:-1].rstrip(_ASCII85_AROUND_A_MARK)
    if end[-1:] == b"~":
        data = end[:-1].rstrip(_ASCII85_AROUND_A_MARK)
    return data


def ascii85decode(data: bytes) -> bytes:
    """pdfminer's `ascii85decode`, with the data decoded a piece at a time.

    The decoder of the standard library is written in Python and holds some forty bytes for each one it reads: a file of
    10 MB behind the filter took 405 MB to convert and now takes 134 MB, one of 31 MB 1,077 MB and now 250 MB. Five characters
    stand for four bytes, and `z` alone for four zero bytes, so data cut where a five ends decodes to the same bytes piece
    by piece: a piece ends where the characters before it that are not `z` come to a multiple of five. Data the library
    raises on raises in the piece that holds the fault, and data with a `y` (which the library raises on) or no longer
    than a piece is decoded whole as before (Pinned by: test_pdf_resource_limits.py::TestAscii85InPieces)."""
    body = _ascii85_body(data)
    if len(body) <= _ASCII85_PIECE or b"y" in body:
        return pdfminer_ascii85.a85decode(body)
    body = body.translate(None, _ASCII85_WHITE_SPACE)
    pieces, at, length = [], 0, len(body)
    while at < length:
        end = min(at + _ASCII85_PIECE, length)
        if end < length:
            over = (end - at - body.count(b"z", at, end)) % 5   # characters of a five that is cut
            while over:
                end -= 1
                over -= body[end] != _ASCII85_ZEROS
            if end == at:
                return pdfminer_ascii85.a85decode(body)   # fewer than five characters before a `z`: the library raises
        pieces.append(pdfminer_ascii85.a85decode(body[at:end]))
        at = end
    return b"".join(pieces)


pdftypes.ascii85decode = ascii85decode  # `PDFStream.decode` looks the function up in its module


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
    cmap_codes: int = _DOCUMENT_CMAP_CODES  # code points the `cmap` tables of embedded TrueType fonts may still name (`_DOCUMENT_CMAP_CODES`)
    undecoded_fonts: bool = False  # met a CID font whose encoding CMap was not found, so no character decodes (`_cmap_or_embedded`)
    ungrouped_layouts: int = 0  # layouts laid out without text-box grouping for having too many text boxes (`_group_few_textboxes`)
    grouping_cost: int = 0  # text-box grouping cost the Form XObjects of the page being read spent (`_PAGE_GROUPING_COST`, reset per page)
    figure_line_work: int = _LINE_GROUPING_WORK  # line-grouping work left to the Form XObjects of the page being read (`_LINE_GROUPING_WORK`)
    line_work: int | None = None  # line-grouping work left to the layout being grouped (None outside `group_textlines`)
    line_spent_layout: object = None  # the layout whose line grouping gave up last: its boxes are not grouped (`_group_few_textboxes`)
    unruled_pages: set[int] = field(default_factory=set)  # pages whose ruled tables were not looked for (`_MAX_RULE_CROSSINGS`)
    long_texts: dict[int, tuple[object, bool]] = field(default_factory=dict)  # ToUnicode stream → a code of it stands for a long text
    page_glyphs: int = 0  # glyphs pdfminer has drawn on the page being read (`_MAX_READ_GLYPHS`, reset per page)
    page_kept: int = 0  # those it kept (`_MAX_KEPT_GLYPHS`)
    page_text: int = 0  # characters the kept ones stand for (`_MAX_READ_TEXT`)

    def new_page(self) -> None:
        """Resets the per-page allowances of layout analysis (`_PAGE_GROUPING_COST`, `_LINE_GROUPING_WORK`). Pinned by:
        test_audit_pdf_text.py::TestTooManyTextBoxes::test_the_forms_of_a_page_are_not_charged_for_those_of_the_page_before,
        TestTooMuchLineGrouping::test_the_forms_of_a_page_start_with_a_whole_allowance."""
        self.grouping_cost, self.figure_line_work = 0, _LINE_GROUPING_WORK
        self.page_glyphs = self.page_kept = self.page_text = 0


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


def _inline_data_in_linear_time(self: PDFContentParser, pos: int, target: bytes = b"EI") -> tuple[int, bytes]:
    """`PDFContentParser.get_inline_data` with the data gathered in a list and joined once.

    pdfminer adds every piece it reads to the data read so far (`data += …`): a piece per buffer of the stream and two at every
    `E`, so reading an inline image costs its size times its pieces — 5MB of compressed image data (an `E` every 256 bytes) took 8.5
    seconds inside one page, and 20MB would take over two minutes. The steps are pdfminer's, so the data, the position and the
    errors are the same; only the joining is done once. Deliberate exception to not copying pdfminer's functions: the cost is in
    the body of the function, so no wrapper can avoid it (Pinned by: test_pdf_resource_limits.py::TestLargeInlineImages)."""
    self.seek(pos)
    i = 0
    parts = []
    while i <= len(target):
        self.fillbuf()
        if i:
            c = bytes((self.buf[self.charpos],))
            parts.append(c)
            self.charpos += 1
            if (len(target) <= i and c.isspace()) or (i < len(target) and c == bytes((target[i],))):
                i += 1
            else:
                i = 0
        else:
            try:
                j = self.buf.index(target[0], self.charpos)
                parts.append(self.buf[self.charpos:j + 1])
                self.charpos = j + 1
                i = 1
            except ValueError:
                parts.append(self.buf[self.charpos:])
                self.charpos = len(self.buf)
    data = b"".join(parts)[:-(len(target) + 1)]  # strip the last part
    return pos, re.sub(rb"(\x0d\x0a|[\x0d\x0a])$", b"", data)


_inline_data_unbounded = PDFContentParser.get_inline_data
PDFContentParser.get_inline_data = _inline_data_in_linear_time


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
    _charge_repeated_contents(page, budget)


def _charge_repeated_contents(page: object, budget: _InflateBudget) -> None:
    """Charges a content stream listed several times in the page's `/Contents` once more for every further listing: pdfium joins
    the page's streams into one before reading them, so it holds such a stream as often as it is listed, as if each listing were a
    stream of its own. Measured once, a 21KB file listing one stream of 20MB 80 times used 3.5GB in 10.5 seconds under a limit of
    500MB (Pinned by: test_pdf_resource_limits.py::TestContentStreamListedManyTimes)."""
    try:
        streams = _content_streams(page)
    except DocumentTooLargeError:
        raise
    except Exception:  # noqa: BLE001 — page reading meets a broken reference itself and handles it
        return
    listed = Counter(id(stream) for stream in streams)
    if len(listed) == len(streams):
        return
    for stream in {id(stream): stream for stream in streams}.values():
        if listed[id(stream)] > 1:
            try:
                size = len(stream.get_data())
            except DocumentTooLargeError:
                raise
            except Exception:  # noqa: BLE001 — a stream that cannot be decoded is met by page reading as before
                continue
            budget.remaining -= size * (listed[id(stream)] - 1)
    if budget.remaining < 0:
        budget.exceeded = True
        raise DocumentTooLargeError(_TOO_LARGE)


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
# Bytes of content a page may make pdfium read (`_page_drawing`: each content stream as often as `/Contents` lists it, each form's
# content as often as it is drawn). Operators that paint nothing make no object, so a form of a million `q Q` (4.8KB file) drawn n
# times passed the estimate up to n = 500,000; pdfminer took 4.4 seconds at n = 1, 17 at n = 4 and 88 at n = 16, and pdfium reads
# such content at about 60MB a second (256MB: 4.4 seconds). Past this bound nobody opens the page. A page's content stream is rarely
# above a few MB, and a page drawing 128MB is past the drawn-object limit long before (Pinned by:
# test_pdf_resource_limits.py::TestContentThatPaintsNothing).
_MAX_OPENED_CONTENT_BYTES = 128 << 20
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
    """Estimate of the number of drawn objects that drawing the page will create (`_page_drawing`)."""
    return _page_drawing(page, limit, painted, forms)[0]


def _page_drawing(page: object, limit: int, painted: dict[int, tuple[object, int]] | None = None,
                  forms: dict[tuple[int, int], tuple[object, ...]] | None = None) -> tuple[int, int, int, int]:
    """Estimate of the number of drawn objects that drawing the page will create (forms are multiplied by the number of times they
    are drawn; once over `limit` the value where counting stopped), and the bytes of content read to draw them — each content stream
    as often as `/Contents` lists it, and each form's content as often as it is drawn: all its bytes (`_MAX_OPENED_CONTENT_BYTES`),
    the tokens pdfminer reads from them (`_content_cost`, `_MAX_READ_CONTENT_TOKENS`) and the pieces they add to paths
    (`_path_segments`, `_MAX_READ_PATH_SEGMENTS`).

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
        streams = _content_streams(page)
        for stream in streams:
            if id(stream) not in painted:
                painted[id(stream)] = (stream, _paint_operators(stream.get_data(), limit))
        paints = sum(painted[id(stream)][1] for stream in streams)
        calls = _page_calls(streams, limit)
        objects, read = _FormWalk(limit, forms).drawn(calls, resources, 0, paints)
        parents = _parent_resources(page.page_obj)
        if parents is not None and parents is not resources:
            # pdfium takes inherited resources from `/Parent`, pdfminer from the path through the page tree
            other, other_read = _FormWalk(limit, forms).drawn(calls, parents, 0, paints)
            objects, read = max(objects, other), tuple(max(pair) for pair in zip(read, other_read, strict=True))
        for stream in streams:
            read = _plus(read, _reading_cost(stream.get_data()))
        return (objects, *read)
    except DocumentTooLargeError:
        raise
    except Exception:  # noqa: BLE001 — the estimate is a bonus. Page reading meets a broken structure itself and handles it
        return 0, 0, 0, 0



_PAGE_TREE_LEVELS = 1024  # levels of the page tree pdfium follows, through `/Kids` and through `/Parent` (its kMaxPageLevel)


def _parent_resources(page_obj: object) -> object:
    """The resources pdfium gives a page without `/Resources` of its own: those of the nearest node up its `/Parent` chain. None
    when the page has its own (or none is found).

    pdfminer passes inherited attributes down the path through the page tree, pdfium looks them up through `/Parent`, so a page
    whose `/Parent` is not the node listing it drew, in pdfium, the forms of other resources than the drawn-object estimate looked
    at — a form drawing itself twice there took gigabytes (Pinned by:
    test_pdf_resource_limits.py::TestPagesPdfiumNumbersOtherwise::test_resources_inherited_through_parent_are_estimated)."""
    raw = getattr(page_obj, "raw", None)
    if raw is None:
        doc, pageid = getattr(page_obj, "doc", None), getattr(page_obj, "pageid", None)
        raw = resolve1(doc.getobj(pageid)) if doc is not None and isinstance(pageid, int) else None
    if not isinstance(raw, dict) or "Resources" in raw:
        return None
    node, seen = resolve1(raw.get("Parent")), set()
    while isinstance(node, dict) and id(node) not in seen and len(seen) < _PAGE_TREE_LEVELS:
        if "Resources" in node:
            return resolve1(node["Resources"])
        seen.add(id(node))
        node = resolve1(node.get("Parent"))
    return None

def _content_streams(page: object) -> list[PDFStream]:
    """The content streams of the page in the order drawn, a stream listed several times in `/Contents` as often as listed."""
    contents = resolve1(page.page_obj.attrs.get("Contents"))
    streams = [resolve1(part) for part in contents] if isinstance(contents, list) else [contents]
    return [stream for stream in streams if isinstance(stream, PDFStream)]


_CALL_AT_START = re.compile(rb"\s*Do(?=[\s\[(/<%]|$)")  # the `Do` of a call whose name ends the stream before
_NAME_CHARACTERS = re.compile(rb"[^\s/\[\]()<>{}%]+")


def _page_calls(streams: list[PDFStream], limit: int) -> Counter:
    """XObject calls (name → times) of the page's content streams drawn one after the other: the calls of each stream are looked for
    once, and taken as often as the stream is listed, so that the result is that of looking through the streams joined by line
    breaks (pdfminer and pdfium draw them as one stream) — also for a call whose name ends one stream and whose `Do` starts a later
    one. Joining them first held the page's contents again: one stream of 20MB listed 80 times held 1.6GB (Pinned by:
    test_pdf_resource_limits.py::TestContentStreamListedManyTimes)."""
    seen: dict[int, tuple[Counter, bool, bool, bytes | None]] = {}
    for stream in streams:
        if id(stream) not in seen:
            data = stream.get_data()
            ending = data.rstrip()
            slash = ending.rfind(b"/")
            name = ending[slash + 1:] if slash >= 0 else b""
            last = name if name and _NAME_CHARACTERS.fullmatch(name) else None
            seen[id(stream)] = (_xobject_calls(data, limit), not ending, bool(_CALL_AT_START.match(data)), last)
    listed = Counter(id(stream) for stream in streams)
    calls: Counter = Counter()
    for key, times in listed.items():
        for name, drawn in seen[key][0].items():
            calls[name] += drawn * times
    pending = None  # the name ending the streams so far, waiting for its `Do`
    for stream in streams:
        _, blank, starts_with_do, last = seen[id(stream)]
        if blank:
            continue
        if pending is not None and starts_with_do:
            calls[pending] += 1
        pending = last
    return calls


def _xobject_calls(data: bytes, limit: int) -> Counter:
    """XObject calls (name → times) of a content stream. Only the first `limit + 1` calls are looked at: every call is at least one
    object, so with more the estimate is over the limit anyway. `findall` listed every name first — a 65MB stream of five million
    different names held 500MB (Pinned by: test_audit_pdf_head.py::TestManyDistinctXObjectNames)."""
    return Counter(match[1] for match in islice(_XOBJECT_CALL.finditer(data), limit + 1))


# Classes of content bytes for counting tokens (`_content_cost`): white space, delimiters, the rest.
_TOKEN_CLASSES = bytes(32 if byte in b"\x00\t\n\x0c\r " else 100 if byte in b"()<>[]{}/%" else 120 for byte in range(256))


_NOTHING_READ = (0, 0, 0)


def _plus(cost: tuple[int, ...], more: tuple[int, ...]) -> tuple[int, ...]:
    return tuple(one + other for one, other in zip(cost, more, strict=True))


def _times(cost: tuple[int, ...], times: int) -> tuple[int, ...]:
    return tuple(one * times for one in cost)


def _content_cost(data: bytes) -> tuple[int, int]:
    """Bytes of a content stream and the tokens pdfminer reads from them: runs of bytes that are neither white space nor
    delimiters, and each delimiter (`(`, `<`, `[`, `/` …). Counted on a copy with each byte replaced by its class, at the speed of a
    byte copy.

    pdfminer's time goes with the tokens it reads, not with the bytes: per MB without white space a million `q Q` took 3.1 seconds
    (2 million tokens), segments of a long path (`x y l`) 1.1, colours 0.8, curves 0.6, numbers 0.3, white space next to nothing;
    per token they are all within 2 to 5 microseconds. Counted by bytes, a page with a 5MB inline image (0.3 seconds) and one with a
    single map path of 400,000 segments (4.3 seconds) were sent to pdfium's simple extraction and lost their tables. Inline image
    data is not left out by finding `ID … EI` — those letters can stand inside a string, which would hide the operators after it —
    but image data holds few tokens: compressed data about one per 16 bytes (20MB: 1.25 million, read in 0.7 seconds with
    `_inline_data_in_linear_time`), data without a white-space or delimiter byte one (Pinned by:
    test_pdf_resource_limits.py::TestContentThatPaintsNothing)."""
    classes = data.translate(_TOKEN_CLASSES)
    return len(data), classes.count(b" x") + classes.count(b"d") + (classes[:1] == b"x")


# Operators that add a piece to the current path (`m l c v y h re`). The same letters inside strings are counted too (fine for an
# upper bound).
_PATH_OPERATOR = re.compile(rb"(?<![^\s\])>])(?:re|[mlcvyh])(?=[\s\[(/<%]|$)")


def _reading_cost(data: bytes) -> tuple[int, int, int]:
    """Bytes, tokens (`_content_cost`) and path pieces (`_path_segments`) of a content stream."""
    return (*_content_cost(data), _path_segments(data))


def _path_segments(data: bytes) -> int:
    """Pieces added to paths in a content stream (`_MAX_READ_PATH_SEGMENTS` in pdf.py). Only counts."""
    return sum(1 for _ in _PATH_OPERATOR.finditer(data))


def _paint_operators(data: bytes, limit: int) -> int:
    """Number of paint operators in a content stream (`limit + 1` when over `limit`). Only counts, so that a stream with ten million
    of them does not build a list."""
    return sum(1 for _ in islice(_PAINT_OPERATOR.finditer(data), limit + 1))


def _is_form(obj: object) -> bool:
    return isinstance(obj, PDFStream) and _name(obj.attrs.get("Subtype")) == "Form"


def _content_objects(data: bytes, resources: object, limit: int, counted: dict[tuple[int, int], tuple[object, ...]],
                     active: set[int], depth: int, paints: int | None = None) -> int:
    """Estimate of the number of drawn objects one content stream `data` will create (`_drawn_objects`), drawn inside the forms
    `active` (normally none). A form counts as times drawn × (1 form object + the count inside the form) and is counted only once per
    form (`counted`, (form id, id of the resources it uses) → (form, resources, count) — holding the objects prevents id reuse. A form
    without resources inherits those of whatever draws it, so if it draws other XObjects it is counted per such resources, otherwise
    once under resource id 0). A form that comes back to itself is counted as pdfium draws it (`_FormWalk`), and a form deeper than
    `_FORM_NESTING_MAX` counts as over the limit. The number of paint operators already counted is passed in as `paints`."""
    return _FormWalk(limit, counted, active).objects(data, resources, depth, paints)[0]


# pdfium draws a form inside a form down to its 40th level, also when the form comes back to itself: a form that draws itself once
# is drawn 41 times. pdfminer stops at the form that comes back (`parent_stream_ids`), so it draws fewer.
_CYCLE_DRAWS = 41


class _FormWalk:
    """The forms drawn from one content stream, followed down to their contents (`_content_objects`).

    A form that comes back to itself was counted as one object, and pdfium follows such a loop down to its nesting level (40): when
    a form of the loop is drawn twice, every level doubles the forms of the next, so an 800-byte file whose form draws itself twice
    used 3.9GB in 6 seconds and 10.7GB in 20 seconds merely by pdfium opening the page (the same with two forms drawing each other
    twice). Such a loop — a call on the way round drawn more than once, or a second loop met while one is still open (two ways round
    give as many paths as Fibonacci numbers) — is over the limit. A loop whose every call is drawn once is followed by pdfium only as
    deep as its nesting level, so the form it comes back to counts `_CYCLE_DRAWS` times its contents; the forms between are not
    remembered (`counted`), as their count holds the loop only partly (Pinned by:
    test_pdf_resource_limits.py::TestFormsDrawingThemselves)."""

    __slots__ = ("calls", "chain", "counted", "limit", "loop", "outside")

    def __init__(self, limit: int, counted: dict[tuple[int, int], tuple[object, ...]], outside: set[int] | None = None) -> None:
        self.limit, self.counted = limit, counted
        self.outside = outside or set()  # forms the stream is drawn inside, an unknown number of times: coming back is over
        self.chain: dict[int, int] = {}  # form being drawn → its place in the chain of forms being drawn
        self.calls: list[int] = []  # how many times the call drawing each form of the chain draws it
        self.loop: int | None = None  # place in the chain of the form an open loop comes back to

    def objects(self, data: bytes, resources: object, depth: int, paints: int | None = None) -> tuple[int, tuple[int, int, int]]:
        """Drawn objects of the content stream `data`, and what reading it and its forms costs (`_reading_cost`)."""
        total = _paint_operators(data, self.limit) if paints is None else paints
        objects, read = self.drawn(_xobject_calls(data, self.limit), resources, depth, total)
        return objects, _plus(read, _reading_cost(data))

    def drawn(self, calls: Counter, resources: object, depth: int, total: int) -> tuple[int, tuple[int, int, int]]:
        """`total` plus the drawn objects of the XObject calls `calls` (name → times) made with `resources`, and what reading the
        content of the forms costs (`_reading_cost`): each form's content and that of the forms inside it, as often as it is drawn."""
        limit, counted = self.limit, self.counted
        read = _NOTHING_READ
        xobjects = resolve1(resources.get("XObject")) if isinstance(resources, dict) and calls else None

        for raw, times in calls.items():
            name = _NAME_ESCAPE.sub(lambda match: bytes([int(match[1], 16)]), raw)
            form = resolve1(xobjects.get(name.decode("utf-8", "replace"))) if isinstance(xobjects, dict) else None
            if not _is_form(form):
                total += times  # an image or a missing name is one object
                continue
            if id(form) in self.outside:
                return limit + 1, read
            if id(form) in self.chain:
                place = self.chain[id(form)]
                if times > 1 or self.loop is not None or any(drawn > 1 for drawn in self.calls[place + 1:]):
                    return limit + 1, read
                self.loop = place  # its contents are counted again when the form it comes back to is left
                total += 1
                continue
            if depth >= _FORM_NESTING_MAX:
                return limit + 1, read
            inherited = resolve1(form.attrs.get("Resources")) or resources
            key = (id(form), 0) if (id(form), 0) in counted else (id(form), id(inherited))
            inner, inner_read = (counted[key][-1], counted[key][-2]) if key in counted else (None, _NOTHING_READ)
            if inner is None:
                place = len(self.calls)
                self.chain[id(form)] = place
                self.calls.append(times)
                form_data = form.get_data()
                inner, inner_read = self.objects(form_data, inherited, depth + 1)
                del self.chain[id(form)]
                self.calls.pop()
                if self.loop == place:
                    # pdfium goes round the loop down to its nesting level
                    inner, inner_read, self.loop = inner * _CYCLE_DRAWS, _times(inner_read, _CYCLE_DRAWS), None
                if self.loop is None:  # a loop still open comes back above: the count holds only part of it
                    if not _XOBJECT_CALL.search(form_data):
                        key = (id(form), 0)  # the count of a form that draws no other XObject does not depend on the resources
                    counted[key] = (form, inherited, inner_read, inner)
            total += times * (1 + inner)
            read = _plus(read, _times(inner_read, times))
            if total > limit:
                break
        return total, read


# The text a /ToUnicode map gives one code is long when it is written in more characters of the map than this, from its `<` to
# its `>` whatever stands between: 64 characters of text. Over 3,506 maps of 1,626 public PDFs the longest is 49 (one test
# file of a viewer; 23 below it): ligatures, and words a writer drew as one glyph.
_LONG_GLYPH_TEXT = re.compile(rb"<[^<>]{257,}>")
# Dictionaries of resources and forms followed for the fonts of a page (`_gives_a_code_long_text`)
_FONT_RESOURCES_FOLLOWED = 5000


def _gives_a_code_long_text(page: object, budget: _InflateBudget) -> bool:
    """Does a /ToUnicode map of a font of the page - in its resources, those pdfium takes from `/Parent`, or those of a form in
    them - give one code a long text (`_LONG_GLYPH_TEXT`)?

    pdfium's text page holds a character for each unit of that text for each glyph drawn, 0.3 KB each: an 18 KB file whose one
    code stands for 2,000 characters, drawn 2,000 times, took 1.2 GB there, and 4,000 by 4,000 more than 3 GB within a second.
    No text of a document is written so, and pdfium is not let open such a page (Pinned by:
    test_pdf_resource_limits.py::TestGlyphsOfAPage).

    Known accepted gap: False when the fonts cannot be looked through - a map that does not decode, resources that go on past
    `_FONT_RESOURCES_FOLLOWED` dictionaries - as for every estimate taken before a page is read; and what else gives a glyph
    more than one character in pdfium (/ActualText, the letters of a compatibility character) is not looked for."""
    try:
        page_obj = page.page_obj
        pending, seen = [page_obj.resources, _parent_resources(page_obj)], set()
        while pending:
            current = resolve1(pending.pop())
            if not isinstance(current, dict) or id(current) in seen:
                continue
            seen.add(id(current))
            if len(seen) > _FONT_RESOURCES_FOLLOWED:
                return False
            fonts = resolve1(current.get("Font"))
            for font in fonts.values() if isinstance(fonts, dict) else ():
                font = resolve1(font)
                table = resolve1(font.get("ToUnicode")) if isinstance(font, dict) else None
                if not isinstance(table, PDFStream):
                    continue
                if id(table) not in budget.long_texts:
                    budget.long_texts[id(table)] = (table, _LONG_GLYPH_TEXT.search(table.get_data()) is not None)
                if budget.long_texts[id(table)][1]:
                    return True
            xobjects = resolve1(current.get("XObject"))
            for xobject in xobjects.values() if isinstance(xobjects, dict) else ():
                xobject = resolve1(xobject)
                if _is_form(xobject):
                    pending.append(xobject.attrs.get("Resources"))
        return False
    except DocumentTooLargeError:
        raise
    except Exception:  # noqa: BLE001 — the look is a bonus. Page reading meets a broken structure itself and handles it
        return False


# What pdfminer draws on one page before the page is given up. It keeps about 2.5 KB a glyph and takes some 5 microseconds to
# draw one, and the count pdfium gives of a page (`_MAX_PAGE_CHARS` in pdf.py: above 200,000 the page is not given to pdfminer)
# leaves out what lies off the page: one string of a million glyphs (a 1.5 KB file) took 20 s and 2.5 GB. A glyph removed as a
# copy on the same spot (`_MAX_OVERPRINTS`: of bold drawn by printing four times, the fourth) is not kept and counts only for
# the time; the copies that stay count as kept.
_MAX_KEPT_GLYPHS = 250_000
_MAX_READ_GLYPHS = 1_000_000
_MAX_READ_TEXT = 2_000_000


class _TooManyGlyphs(Exception):
    """A page draws more glyphs, or stands for more text, than pdfminer is let read (`_MAX_KEPT_GLYPHS`, `_MAX_READ_GLYPHS`,
    `_MAX_READ_TEXT`)."""


# Bytes of one string of a content stream pdfminer reads before the page is given up (`_TooManyGlyphs`). It reads a string
# whole before it draws its first glyph and keeps some 70 bytes for each: one string of 30 MB (a 30 KB file) took 2.2 GB.
_MAX_STRING_BYTES = 2_000_000
_string_of_any_length = PDFContentParser._parse_string
_hex_string_of_any_length = PDFContentParser._parse_hexstring


def _parse_bounded_string(self: PDFContentParser, s: bytes, i: int) -> int:
    """`_parse_string` of the parser of content streams, which gives a page up when the string being read is longer than
    `_MAX_STRING_BYTES`. The count is of the string as pdfminer itself reads it, across the buffers and the streams of a
    page; strings of the objects of a document are read by another parser and are not bounded here (Pinned by:
    test_pdf_resource_limits.py::TestGlyphsOfAPage)."""
    at = _string_of_any_length(self, s, i)
    if len(self._curtoken) > _MAX_STRING_BYTES and _INFLATE_BUDGET.get() is not None:
        raise _TooManyGlyphs
    return at


def _parse_bounded_hex_string(self: PDFContentParser, s: bytes, i: int) -> int:
    """`_parse_hexstring` bounded like `_parse_bounded_string`: two digits stand for a byte."""
    at = _hex_string_of_any_length(self, s, i)
    if len(self._curtoken) > 2 * _MAX_STRING_BYTES and _INFLATE_BUDGET.get() is not None:
        raise _TooManyGlyphs
    return at


PDFContentParser._parse_string = _parse_bounded_string
PDFContentParser._parse_hexstring = _parse_bounded_hex_string


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
    budget = _INFLATE_BUDGET.get()
    if budget is None:
        return advance
    objs = self.cur_item._objs
    char = objs[-1] if objs else None
    if isinstance(char, LTChar):
        budget.page_glyphs += 1
        if budget.page_glyphs > _MAX_READ_GLYPHS:
            raise _TooManyGlyphs
        _blended_fill(char.graphicstate)
        font = kwargs.get("font", args[1] if len(args) > 1 else None)
        _symbol_font_glyph(char, font)
        _win_ansi_bullet(char, font, kwargs.get("cid", args[5] if len(args) > 5 else None))
        copies = self.cur_item.__dict__.setdefault("_opendockit_copies", Counter())
        # NaN is keyed as None: it is unequal even to itself, so copies drawn at a NaN position each got a key of their own and
        # were never capped (Pinned by: test_audit_pdf_text.py::TestCharactersAtNonFinitePositions::
        # test_copies_at_a_nan_position_are_counted_as_copies)
        key = (char.get_text(), char.fontname, *(None if value != value else round(value, 1)
                                                 for value in (char.size, char.x0, char.y0)))
        copies[key] += 1
        if copies[key] > _MAX_OVERPRINTS:
            objs.pop()
        else:
            budget.page_kept += 1
            budget.page_text += len(char._text)
            if budget.page_kept > _MAX_KEPT_GLYPHS or budget.page_text > _MAX_READ_TEXT:
                raise _TooManyGlyphs
            _set_on_its_letter(objs)
    return advance


# A mark is drawn on the letter before it when it is no wider than this share of its size and its middle lies within the
# letter, give or take this share of the size (`_set_on_its_letter`).
_MARK_WIDTH_RATIO = 0.1
_MARK_REACH_RATIO = 0.15


def _set_on_its_letter(objs: list) -> None:
    """Joins a mark that was just drawn (a vowel sign, a tone mark, an accent: a glyph of marks only, with no width to
    speak of) to the letter drawn before it, when it stands over that letter: the letter carries the text of both and
    the mark is taken out.

    Thai sets a vowel under a letter and a tone mark over both ('อยู่'), each a glyph that moves nothing. pdfminer puts
    two characters on one line when they are closer than twice the width of the wider — never, for two glyphs without
    width — so the line ended between the two marks, and the pieces came out in another order ('…อยู' on one line, the
    tone mark and the rest of the sentence elsewhere). Joined to its letter the mark needs no place of its own.
    Deliberate: not a mark of Hebrew or Arabic, which a program that draws a line in the order of the page draws before
    its letter; those are set on their letter by their place (`_pdf_bidi._visual_units`)
    (Pinned by: test_pdf_actual_text.py::TestMarksSetOnALetter)."""
    if len(objs) < 2:
        return
    mark, letter = objs[-1], objs[-2]
    text = mark.get_text()
    if not isinstance(letter, LTChar) or not _is_mark(text) or _RIGHT_TO_LEFT.search(text) or not letter.get_text().strip():
        return
    size = max(mark.size, 1.0)
    reach = size * _MARK_REACH_RATIO
    if (mark.width <= size * _MARK_WIDTH_RATIO and abs(mark.size - letter.size) <= 0.1
            and letter.x0 - reach <= (mark.x0 + mark.x1) / 2 <= letter.x1 + reach
            and min(mark.y1, letter.y1) - max(mark.y0, letter.y0) > 0):
        letter._text = letter.get_text() + text
        objs.pop()


# Symbol font PostScript name (subset prefix, ',Bold' and '-Regular' removed; lower case without spaces) → font name in
# `_symbols.SYMBOL_GLYPHS`.
_SYMBOL_FONT_NAMES = {"symbol": "symbol", "symbolmt": "symbol", "wingdings": "wingdings", "wingdings2": "wingdings 2",
                      "wingdings3": "wingdings 3", "webdings": "webdings"}


@lru_cache(maxsize=256)
def _symbol_font(fontname: str) -> str | None:
    """pdfminer font name ('ABCDEF+Wingdings-Regular', 'Wingdings2,Bold') → symbol font table name. None if not a symbol font."""
    name = re.sub(r"^[A-Z]{6}\+", "", fontname)
    return _SYMBOL_FONT_NAMES.get(re.split(r"[,-]", name, maxsplit=1)[0].replace(" ", "").lower())


def _symbol_font_glyph(char: LTChar, font: object) -> None:
    """When a symbol font character comes out in the private use area (U+F020–F0FF), or a Wingdings one as a letter of
    Windows-1252, replaces it using that font's table (`_symbols.symbol_glyph`).

    The check boxes in a PDF exported by Word (☑ = F052 and ☐ = F0A3 in Wingdings 2, □ = F06F and ☒ = F078 in Wingdings) are code
    points missing from the shared mapping (`markdown._SYMBOL_FONT_GLYPHS`), so they were deleted and which side had been ticked
    was lost (Pinned by: test_pdf_fonts.py::TestSymbolFontCheckboxes). Bullet code points (`_SYMBOL_BULLETS`) are left alone —
    bullet detection works on those characters and the shared mapping converts them later. Code points not in the table are left
    alone too (left to the shared mapping as before). The font name is 'unknown' when there is no font descriptor (FontName), so
    the font dictionary's BaseFont is checked as well.
    """
    text = char._text
    if len(text) != 1 or text in _SYMBOL_BULLETS or text.isspace():
        return
    basefont = getattr(font, "basefont", None)
    name = _symbol_font(char.fontname) or (_symbol_font(basefont) if isinstance(basefont, str) else None)
    if not name:
        return
    if 0xF020 <= ord(text) <= 0xF0FF:
        glyph = symbol_glyph(name, ord(text))
    elif name != "symbol":
        # A Wingdings font holds no letters, nor does Webdings: where a character of it comes out as one — 'm' for the circle Hangul's PDF
        # sets before an item, '§' and 'ž' for its small squares and dots — the PDF named the glyph by its number read as
        # Windows-1252, and the number is looked up as it is for the private use area. A number the table does not hold
        # is left as it came (Pinned by: test_pdf_fonts.py::TestWingdingsGlyphsReadAsLetters).
        try:
            glyph = symbol_glyph(name, text.encode("cp1252")[0])
        except UnicodeEncodeError:
            glyph = None
    else:
        glyph = None
    if glyph:
        char._text = glyph


# Codes above 32 that WinAnsiEncoding leaves unused: the specification draws a bullet for each (Appendix D, note 6).
_WIN_ANSI_BULLET_CODES = frozenset((0x7F, 0x81, 0x8D, 0x8F, 0x90, 0x9D))
_SYMBOLIC_FONT_FLAG = 4


def _win_ansi_bullet(char: LTChar, font: object, cid: object) -> None:
    """Reads a code that WinAnsiEncoding leaves unused (`_WIN_ANSI_BULLET_CODES`) as the bullet the specification draws for it.

    reportlab writes the bullets of its lists as code 7F in WinAnsiEncoding Helvetica. pdfminer has no character for the code,
    so the bullets came out as `(cid:127)`, were removed and the page was reported as having characters without a Unicode
    mapping (Pinned by: test_pdf_fonts.py::TestWinAnsiUnusedCodesAreBullets). Only a code pdfminer could not map is read so —
    one a ToUnicode entry or the font's /Differences name keeps that character — and only in a font whose /Encoding names
    WinAnsiEncoding without /Differences (`_names_win_ansi`; pdfminer then uses its shared WinAnsi table itself: /Differences
    may give the code a glyph under a name pdfminer does not know, '/G7F' in a subset font). pdfminer reads
    a TrueType font without /Encoding with the same table, but a viewer draws such a font with its built-in encoding or, when
    it is not symbolic, StandardEncoding, where these codes have no glyph: it gets no bullet. A Type 3 font draws its own glyphs
    and a symbolic font (descriptor flag 4) uses the encoding built into the font, so both are left alone (Pinned by:
    test_pdf_fonts.py::TestWinAnsiUnusedCodesAreBullets::test_fonts_that_do_not_draw_the_win_ansi_bullet_get_none). The flags
    may be an indirect object, or a whole number written as a real ('4.0'): read as they were written, they were taken for no
    flags and a symbolic font got bullets (Pinned by: ::test_a_symbolic_font_gets_no_bullet).
    """
    if cid not in _WIN_ANSI_BULLET_CODES or char._text != f"(cid:{cid})":
        return
    if (not isinstance(font, pdffont.PDFSimpleFont) or isinstance(font, pdffont.PDFType3Font)
            or getattr(font, "cid2unicode", None) is not EncodingDB.win2unicode or not getattr(font, _NAMES_WIN_ANSI, False)):
        return
    flags = resolve1(font.descriptor.get("Flags")) if isinstance(font.descriptor, dict) else None
    if isinstance(flags, float) and flags.is_integer():
        flags = int(flags)
    if isinstance(flags, int) and flags & _SYMBOLIC_FONT_FLAG:
        return
    char._text = "•"


_NAMES_WIN_ANSI = "opendockit_names_win_ansi"  # attribute of a pdfminer font (`_names_win_ansi`)
_simple_font_init = pdffont.PDFSimpleFont.__init__


def _names_win_ansi(self: pdffont.PDFSimpleFont, descriptor: object, widths: object, spec: object) -> None:
    """Initializer used instead of `PDFSimpleFont.__init__`: it also notes on the font whether its dictionary names
    WinAnsiEncoding (as /Encoding, or as the /BaseEncoding of an encoding dictionary). pdfminer keeps only the table it chose,
    and it chooses the WinAnsi table for a TrueType font without /Encoding as well (`_win_ansi_bullet`)."""
    _simple_font_init(self, descriptor, widths, spec)
    encoding = resolve1(spec.get("Encoding")) if isinstance(spec, dict) else None
    if isinstance(encoding, dict):
        encoding = resolve1(encoding.get("BaseEncoding"))
    setattr(self, _NAMES_WIN_ANSI, getattr(encoding, "name", None) == "WinAnsiEncoding")


pdffont.PDFSimpleFont.__init__ = _names_win_ansi


# A CFF font program (FontFile3 /Type1C) may bring an encoding of its own: which glyph each code draws. This many bytes of the
# program are looked at (its header, strings, charset and encoding stand at its front in the files seen; a table past the
# end is no table), and a font holds at most this many glyphs.
_CFF_BYTES_MAX = 1024 * 1024
# What is read of a program is bounded, for a program is read each time a file makes pdfminer build its font again (a font
# written into the resources of a page or of a form has no object number to be remembered by): the dictionary that says
# where its tables are is a few hundred bytes, and an encoding names 256 codes, so the glyphs past the first thousand have
# no code.
_CFF_DICTIONARY_BYTES_MAX = 4096
_CFF_GLYPHS_MAX = 1024
_CFF_GLYPH_NAMES = "opendockit_cff_glyph_names"  # attribute of the stream of a program: what `_cff_glyph_names` read from it
_CFF_ROS = (12, 30)  # the operator that makes a font CID-keyed: it has no encoding
_type1_font_init = pdffont.PDFType1Font.__init__


def _type1_font_with_its_own_encoding(self: pdffont.PDFType1Font, rsrcmgr: object, spec: dict) -> None:
    """Initializer used instead of `PDFType1Font.__init__`: a Type 1 font without /Encoding whose program is embedded as CFF
    is read with the encoding built into that program (PDF 32000-1 9.6.6.1, Table 114).

    pdfminer does so for a program in the Type 1 format (FontFile) and reads the CFF one (FontFile3) with StandardEncoding:
    a bank statement whose fonts draw `é` at code E9 read `ProtØgØ`, `DØpts`, `Ł` for `è`, 27 wrong letters a page, and
    nothing said so (Pinned by: test_pdf_fonts.py::TestEncodingBuiltIntoACffFont). A code keeps what it read as before when
    the program names no glyph for it or names it by a name that says no character (`G38`): the letters of ASCII stand at
    their codes in such fonts. /ToUnicode goes first as ever, and a font with /Encoding is read by it.

    A program that names a letter or a digit of ASCII as another character is not believed at all: a form whose fonts name
    the glyph drawn for `A` `five` and the one for `u` `c` hides its text from a reader of names - `Auto Policy Number` read
    `5cb] C]ZWQg Ac[PS` - while its codes are the text (Pinned by: ::test_program_that_renames_the_letters_is_not_believed).
    """
    _type1_font_init(self, rsrcmgr, spec)
    if type(self) is not pdffont.PDFType1Font or not isinstance(spec, dict) or "Encoding" in spec:
        return
    try:
        descriptor = resolve1(spec.get("FontDescriptor"))
        program = resolve1(descriptor.get("FontFile3")) if isinstance(descriptor, dict) else None
        if not isinstance(program, PDFStream) or getattr(resolve1(program.get("Subtype")), "name", None) != "Type1C":
            return
    except Exception:  # noqa: BLE001 — a descriptor that cannot be read says nothing; the font is read as before
        return
    read = getattr(program, _CFF_GLYPH_NAMES, None)
    if read is None:
        # Read once for a program: the same stream stands behind the font however often the font is built
        read = {}, frozenset()
        try:
            head = _program_head(program)
            if head is not None:
                read = _characters_of_glyph_names(_cff_glyph_names(head))
        except DocumentTooLargeError:
            raise
        except Exception:  # noqa: BLE001 — a program that cannot be read says nothing; the font is read as before
            pass
        setattr(program, _CFF_GLYPH_NAMES, read)
    named, nameless = read
    if any(character != chr(code) for code, character in named.items() if code < 0x80 and chr(code).isalnum()):
        return
    characters = {**self.cid2unicode, **named}
    for code in nameless:
        characters.pop(code, None)
    self.cid2unicode = characters


pdffont.PDFType1Font.__init__ = _type1_font_with_its_own_encoding


def _program_head(program: PDFStream) -> bytes | None:
    """The first `_CFF_BYTES_MAX` bytes of a font program, inflated no further and charged to the document's limit: None
    for a stream that is stored otherwise than plain or under one Flate filter without a predictor, with ASCII filters
    before it or none (Distiller's ASCII format writes `[/ASCII85Decode /FlateDecode]`; an ASCII filter only shrinks).

    `get_data` inflates the whole stream and keeps it: a file whose FontFile3 inflates to 300 MB would hold it all for an
    encoding that stands in its first kilobytes (Pinned by:
    test_pdf_fonts.py::TestEncodingBuiltIntoACffFont::test_only_the_head_of_a_program_is_inflated)."""
    data = program.rawdata
    if data is None:
        return None
    if program.decipher:
        data = program.decipher(program.objid, program.genno, data, program.attrs)
    filters = program.get_filters()
    for index, (name, params) in enumerate(filters):
        if name in LITERALS_ASCII85_DECODE:
            data = ascii85decode(data)
        elif name in LITERALS_ASCIIHEX_DECODE:
            data = asciihexdecode(data)
        elif name in LITERALS_FLATE_DECODE and index == len(filters) - 1 and (params or {}).get("Predictor", 1) == 1:
            break
        else:
            return None
    else:
        return data[:_CFF_BYTES_MAX]
    inflater, pieces, size = zlib.decompressobj(), [], 0
    try:
        while data and size < _CFF_BYTES_MAX and not inflater.eof:
            pieces.append(inflater.decompress(data, _CFF_BYTES_MAX - size))
            size += len(pieces[-1])
            data = inflater.unconsumed_tail
    except zlib.error:
        pass  # what was inflated before the broken spot is read, as pdfminer reads a broken stream
    budget = _INFLATE_BUDGET.get()
    key = id(program) if program.objid is None else (program.objid, program.genno)
    if budget is not None and key not in budget.charged:  # a stream measured whole (`_charge`) is charged already
        budget.remaining -= size
        if budget.remaining < 0:
            budget.exceeded = True
            raise DocumentTooLargeError(_TOO_LARGE)
    return b"".join(pieces)


# The endings of the glyph names of the private use area that draw a character in another cut: `oneoldstyle` a 1,
# `ampersandsmall` an ampersand, `Asmall` a small capital A
_GLYPH_CUTS = ("taboldstyle", "oldstyle", "small", "superior", "inferior")


def _characters_of_glyph_names(names: dict[int, str]) -> tuple[dict[int, str], frozenset[int]]:
    """The characters of the glyph names that say text, by code, and the codes whose name says a character that is no text:
    one of the private use area, a surrogate, an unassigned one, U+FFFD, or a control other than a tab or a line end
    (`Asmall` is U+F761, `uni0000` a NUL).

    Such a code has no character: it is not the one of StandardEncoding, which the font does not draw there (`oneoldstyle`
    at E8 read `Ł`), and cleaning would remove the one of the private use area, so either way the page lost the character
    and with the second also the warning of a character without a mapping. But where the name is another cut of the very
    character StandardEncoding has at that code - `oneoldstyle` at the code of `1`, `dollaroldstyle` at `$`, `Asmall` at
    `a` - the code reads as before: it is that character. A name that says no character at all (`G38`) is in neither
    (Pinned by: test_pdf_fonts.py::TestEncodingBuiltIntoACffFont::test_glyph_name_of_no_text_has_no_character)."""
    named: dict[int, str] = {}
    nameless = set()
    for code, name in names.items():
        try:
            characters = name2unicode(name)
        except (KeyError, ValueError):
            continue
        if characters and all(_is_text(one) for one in characters):
            named[code] = characters
        elif not _another_cut_of_the_standard_character(code, name):
            nameless.add(code)
    return named, frozenset(nameless)


def _another_cut_of_the_standard_character(code: int, name: str) -> bool:
    standard = EncodingDB.std2unicode.get(code)
    if standard is None:
        return False
    for cut in _GLYPH_CUTS:
        if name.endswith(cut) and len(name) > len(cut):
            try:
                return name2unicode(name[:-len(cut)]).casefold() == standard.casefold()
            except (KeyError, ValueError):
                return False
    return False


def _is_text(character: str) -> bool:
    kind = unicodedata.category(character)
    return character != "\ufffd" and kind not in ("Co", "Cs", "Cn") and (kind != "Cc" or character in "\t\n\r")


def _cff_glyph_names(program: bytes) -> dict[int, str]:
    """The glyph name of each code in the encoding a CFF font program brings (Adobe Technical Note 5176, sections 12 and 13):
    nothing for a program that uses a predefined encoding - StandardEncoding is what the font is read with already, and
    the Expert sets hold no text - for a CID-keyed one, and for one whose charset is predefined other than ISOAdobe.
    Raises `struct.error`, `IndexError` or `ValueError` when the program ends early or is not laid out as one."""
    _, at = _cff_index(program, program[2])     # the Name INDEX stands behind the header, whose size is its third byte
    dictionaries, at = _cff_index(program, at)
    strings, at = _cff_index(program, at)
    first, last = _cff_object(program, dictionaries, 0)
    if last - first > _CFF_DICTIONARY_BYTES_MAX:
        raise ValueError
    top = _cff_dictionary(program[first:last])
    encoding, charset, glyphs = (top.get(operator, (0,))[-1] for operator in (16, 15, 17))
    if _CFF_ROS in top or encoding in (0, 1) or charset in (1, 2) or not glyphs:
        return {}
    if min(encoding, charset, glyphs) < 0:
        raise ValueError  # a place before the start of the program: counted from its end, it would read something
    (count,) = struct.unpack_from(">H", program, glyphs)
    count = min(count, _CFF_GLYPHS_MAX)
    # The string of each glyph: glyph 0 is .notdef, and the charset names the others
    if charset == 0:
        string_ids = list(range(min(count, _CFF_ISO_ADOBE_GLYPHS)))
    else:
        string_ids, kind, at = [0], program[charset], charset + 1
        if kind == 0:
            string_ids += struct.unpack_from(f">{count - 1}H", program, at)
        elif kind in (1, 2):
            while len(string_ids) < count:
                start, left = struct.unpack_from(">HB" if kind == 1 else ">HH", program, at)
                string_ids += range(start, start + min(left, count - len(string_ids) - 1) + 1)
                at += 3 if kind == 1 else 4
        else:
            raise ValueError

    def name(string_id: int) -> str:
        standard = pdffont.CFFFont.STANDARD_STRINGS
        if string_id < len(standard):
            return standard[string_id]
        first, last = _cff_object(program, strings, string_id - len(standard))
        return program[first:min(last, first + _CFF_NAME_BYTES_MAX)].decode("latin-1")

    found: dict[int, str] = {}
    kind, at = program[encoding], encoding + 1
    if kind & 0x7F == 0:        # the code of each glyph, from glyph 1
        codes = program[at + 1:at + 1 + program[at]]
        at += 1 + program[at]
        for glyph, code in enumerate(codes[:max(len(string_ids) - 1, 0)], 1):
            found[code] = name(string_ids[glyph])
    elif kind & 0x7F == 1:      # ranges of codes, their glyphs in order from glyph 1
        ranges, glyph = program[at], 1
        at += 1
        for index in range(ranges):
            start, left = program[at + 2 * index], program[at + 2 * index + 1]
            for code in range(start, min(start + left, 0xFF) + 1):
                if glyph >= len(string_ids):
                    break
                found[code] = name(string_ids[glyph])
                glyph += 1
            else:
                glyph += max(start + left - 0xFF, 0)  # the codes of the range past 255 take their glyphs too
                continue
            break
        at += 2 * ranges
    else:
        raise ValueError
    if kind & 0x80:             # codes that draw a glyph another code draws already, named by their string
        for index in range(program[at]):
            code, string_id = struct.unpack_from(">BH", program, at + 1 + 3 * index)
            found[code] = name(string_id)
    return found


_CFF_ISO_ADOBE_GLYPHS = 229  # the predefined charset ISOAdobe: glyph n is string n
_CFF_NAME_BYTES_MAX = 128    # a glyph name is at most 127 characters (Adobe Technical Note 5176 and PostScript names)


def _cff_index(program: bytes, at: int) -> tuple[tuple[int, int, int], int]:
    """The INDEX at `at` as (the number of its objects, the size of an offset, where its offsets begin), and where the INDEX
    ends. Only its last offset is read: the objects are looked up one at a time (`_cff_object`), for an INDEX may claim
    65,535 of them."""
    (count,) = struct.unpack_from(">H", program, at)
    if not count:
        return (0, 0, 0), at + 2
    size = program[at + 2]
    if not 1 <= size <= 4:
        raise ValueError
    offsets = at + 3
    base = offsets + (count + 1) * size - 1
    if base + 1 > len(program):
        raise ValueError
    end = base + int.from_bytes(program[offsets + count * size:offsets + (count + 1) * size], "big")
    if end > len(program) or end <= base:
        raise ValueError
    return (count, size, offsets), end


def _cff_object(program: bytes, index: tuple[int, int, int], number: int) -> tuple[int, int]:
    """Where object `number` of an INDEX (`_cff_index`) starts and ends."""
    count, size, offsets = index
    if not 0 <= number < count:
        raise IndexError
    base = offsets + (count + 1) * size - 1
    at = offsets + number * size
    start = int.from_bytes(program[at:at + size], "big")
    end = int.from_bytes(program[at + size:at + 2 * size], "big")
    if not 1 <= start <= end or base + end > len(program):
        raise ValueError
    return base + start, base + end


def _cff_dictionary(data: bytes) -> dict[int | tuple[int, int], tuple[int, ...]]:
    """The whole-number operands of each operator of a CFF DICT (an operand that is a real number is read as 0: the offsets
    looked up here are whole)."""
    found: dict[int | tuple[int, int], tuple[int, ...]] = {}
    operands: list[int] = []
    at = 0
    while at < len(data):
        byte = data[at]
        if byte <= 21:
            operator: int | tuple[int, int] = byte
            if byte == 12:
                operator = (12, data[at + 1])
                at += 1
            found[operator] = tuple(operands) or (0,)
            operands = []
            at += 1
        elif byte == 28:
            operands.append(struct.unpack_from(">h", data, at + 1)[0])
            at += 3
        elif byte == 29:
            operands.append(struct.unpack_from(">i", data, at + 1)[0])
            at += 5
        elif byte == 30:
            at += 1
            while data[at] & 0x0F != 0x0F and data[at] >> 4 != 0x0F:
                at += 1
            at += 1
            operands.append(0)
        elif 32 <= byte <= 246:
            operands.append(byte - 139)
            at += 1
        elif 247 <= byte <= 250:
            operands.append((byte - 247) * 256 + data[at + 1] + 108)
            at += 2
        elif 251 <= byte <= 254:
            operands.append(-(byte - 251) * 256 - data[at + 1] - 108)
            at += 2
        else:
            raise ValueError
    return found


# A marked-content sequence may say what its glyphs stand for (`/Span << /ActualText (…) >> BDC … EMC`, PDF 32000-1 14.9.4).
# The glyphs of one cluster of a script are taken together up to this many, and its text is this long at most; a longer
# sequence is left as its glyphs say (`_stand_for`).
_JOINERS = ("\u200c", "\u200d")
_ACTUAL_TEXT_GLYPHS = 8
_ACTUAL_TEXT_CHARS = 32
_TAGS = "_opendockit_tags"
_begin_tag_flat = PDFPageAggregatorWithMarkedContent.begin_tag
_end_tag_flat = PDFPageAggregatorWithMarkedContent.end_tag


def _begin_tag_nested(self: PDFPageAggregatorWithMarkedContent, tag: object, props: object = None) -> None:
    """Begins a marked-content sequence inside the one that is open, and notes the text it says its glyphs stand for.

    pdfplumber keeps one tag and one MCID, not the sequences inside each other: a `/Span` with an `ActualText` inside
    the content of a table cell (`/P << /MCID 7 >> BDC`) took the MCID away from itself and from everything after it up
    to the end of the cell, so those characters belonged to no cell of a tagged table. A sequence without an MCID of its
    own is part of the one around it (Pinned by: test_pdf_actual_text.py::TestSequencesInsideEachOther)."""
    if _INFLATE_BUDGET.get() is None:
        return _begin_tag_flat(self, tag, props)
    item = getattr(self, "cur_item", None)
    stack = self.__dict__.setdefault(_TAGS, [])
    stack.append((self.cur_tag, self.cur_mcid, _actual_text(props), item, len(item._objs) if item is not None else 0))
    around = self.cur_mcid
    _begin_tag_flat(self, tag, props)
    if self.cur_mcid is None:
        self.cur_mcid = around
    return None


def _end_tag_nested(self: PDFPageAggregatorWithMarkedContent) -> None:
    """Ends the innermost marked-content sequence: its glyphs take the text it said they stand for (`_stand_for`), and
    the tag and MCID of the sequence around it apply again."""
    stack = self.__dict__.get(_TAGS)
    if _INFLATE_BUDGET.get() is None or not stack:
        return _end_tag_flat(self)
    tag, mcid, actual, item, start = stack.pop()
    if actual and item is not None and item is getattr(self, "cur_item", None):
        _stand_for(item._objs, start, actual)
    self.cur_tag, self.cur_mcid = tag, mcid
    return None


def _actual_text(props: object) -> str | None:
    """The `ActualText` of the property list of a marked-content sequence; None when it has none, it is no text, or it
    holds nothing to see.

    Deliberate: a text of spaces and marks without a shape says what a glyph is not, not what it is. InDesign writes a
    soft hyphen (U+00AD) over the space glyph it sets at every place a word may be broken, and over the hyphen of a word
    it did break. Taken as the text, that put a hyphen before every such word ('these -distributions are') on ten
    documents of the benchmark. The glyphs of such a sequence are read as they are drawn (Pinned by:
    test_pdf_actual_text.py::TestActualText::test_a_text_with_nothing_to_see_changes_nothing). The one exception is a
    zero-width joiner or non-joiner alone, which LibreOffice and a browser write over the glyph they set for it: it is
    part of the spelling of a Persian or a Hindi word and is kept where it stands between two letters of such a script
    (Pinned by: test_pdf_actual_text.py::TestActualText::test_a_joiner_inside_a_word_is_kept). The other is one space,
    which a browser writes over the glyph of a space its font gives no name: `_stand_for` takes it for such a glyph
    only."""
    if not isinstance(props, dict):
        return None
    try:
        value = resolve1(props.get("ActualText"))
        text = decode_text(value) if isinstance(value, bytes) else None
    except Exception:  # noqa: BLE001 — a broken entry says nothing; the glyphs are read as they are
        return None
    if text is None or text in _JOINERS or text == " ":
        return text  # a zero-width joiner or non-joiner is part of a spelling (`markdown.without_invisible`)
    if not any(unicodedata.category(letter)[0] not in "CZ" for letter in text):
        return None
    return text


def _stand_for(objs: list, start: int, actual: str) -> None:
    """Gives the glyphs drawn since `objs[start]` the text their marked-content sequence says they stand for.

    A glyph is not always a character. The vowel sign of Hindi drawn before its consonant, the stacked marks of Thai,
    a ligature of Arabic, a letter in the form of its place in the word: a browser and LibreOffice write the glyphs and,
    around them, the characters they stand for. Read from the glyphs alone, the sign before the consonant was a
    character without a name (U+0000), marks were lost or stood apart from their letter, and of the words of a Hindi
    Wikipedia article 70% were found where `pdftotext`, which reads `ActualText`, finds 97%.

    Deliberate: a space is taken only by one glyph whose own text is a control character (`_is_nameless`). Chrome
    (Skia/PDF) draws the space of some fonts (NanumGothic, Apple SD Gothic Neo) as a glyph its ToUnicode map calls
    U+0001 and writes `ActualText ( )` around it: left as the glyph says, every space of the document was lost ('정부는오늘'), where
    pdfium and `pdftotext` read them. A space said of any other glyph says what the glyph is not, as the soft hyphen
    InDesign writes over a space does (`_actual_text`), and changes nothing
    (Pinned by: test_pdf_actual_text.py::TestActualText::test_a_space_is_taken_by_a_glyph_without_a_name).

    One glyph takes the text. Several glyphs on one line become one: the widest keeps its place from top to bottom, is
    as wide as all of them and carries the text, and the others are taken out — the text of a cluster cannot be dealt
    out among its glyphs, which are not in the order of its characters. Deliberate: a sequence of more than
    `_ACTUAL_TEXT_GLYPHS` glyphs or `_ACTUAL_TEXT_CHARS` characters is a line or a paragraph given another wording, not
    a cluster; its glyphs keep their places and their own text, on which the columns of a table are found. An empty
    `ActualText` (a hyphen at the end of a line that is no part of the word) changes nothing either
    (Pinned by: test_pdf_actual_text.py)."""
    chars = [obj for obj in objs[start:] if isinstance(obj, LTChar)]
    if not chars or len(chars) > _ACTUAL_TEXT_GLYPHS or len(actual) > _ACTUAL_TEXT_CHARS:
        return
    if actual == " " and not (len(chars) == 1 and _is_nameless(chars[0].get_text())):
        return
    if "".join(char.get_text() for char in chars) == actual:
        return
    carrier = max(chars, key=lambda char: char.width)
    if len(chars) > 1:
        boxes = [char.bbox for char in chars]
        if not all(math.isfinite(value) for box in boxes for value in box):
            return
        if any(min(char.y1, carrier.y1) - max(char.y0, carrier.y0) <= 0 for char in chars):
            return  # glyphs of more than one line
        carrier.set_bbox((min(box[0] for box in boxes), carrier.y0, max(box[2] for box in boxes), carrier.y1))
        gone = {id(char) for char in chars if char is not carrier}
        objs[start:] = [obj for obj in objs[start:] if id(obj) not in gone]
    carrier._text = actual


def _is_nameless(text: str) -> bool:
    """Whether the text a font gives a glyph is control characters only: no name of a character at all."""
    return bool(text) and all(unicodedata.category(letter) == "Cc" for letter in text)


_add_to_line = LTTextLineHorizontal.add


def _add_keeping_the_reach(self: LTTextLineHorizontal, obj: object) -> None:
    """Adds a character to a line as pdfminer does, but a mark set on a letter does not take the end of the line back.

    pdfminer writes a space where a character starts further right than the one before it ended. A vowel sign or an
    accent drawn as a glyph of its own stands over its letter and ends before the letter does — in a browser's PDF it
    has no width at all — so the letter after it started 'far' from it and a space was written into the word
    ('प्राकृ तिक' for 'प्राकृतिक') (Pinned by: test_pdf_actual_text.py::TestMarksSetOnALetter)."""
    reach = getattr(self, "_x1", None)
    _add_to_line(self, obj)
    if reach is not None and isinstance(obj, LTChar) and obj.x1 < reach and _is_mark(obj.get_text()):
        self._x1 = reach


LTTextLineHorizontal.add = _add_keeping_the_reach
PDFPageAggregatorWithMarkedContent.begin_tag = _begin_tag_nested  # outside a conversion (no budget) both behave as before
PDFPageAggregatorWithMarkedContent.end_tag = _end_tag_nested
PDFPageAggregatorWithMarkedContent.render_char = _render_char_few_copies  # outside a conversion (no budget) it behaves as before


# Cost bound of pdfminer's text-box grouping (`group_textboxes`), counted as the square of a layout's text-box count
# (`_group_few_textboxes`). The page's own layout (the body, and its regrouping after joining pieces, `_rejoined_lines`) may cost
# up to this on its own, and the layouts of every Form XObject drawn on the page (`all_texts`) share another such allowance
# (`grouping_cost`, reset per page read by the caller), so a page runs at most three groupings of up to 1,000 boxes each (the
# body, the body again after joining pieces, its forms together). The bound limits the pairs of boxes pdfminer keeps in its heap,
# not the time: for every pair pdfminer also looks through the plane for a box lying between the two, which costs more the
# further apart the boxes lie and the more of them lie on top of each other. Measured on an Apple M4, one grouping of up to 1,000
# boxes takes 2 seconds when they lie 30pt apart on an A4-sized page and about 4 when they are scattered over a 6,900pt page.
# Known remaining cost (inside one page, where the time budget does not look): a 12KB page under every bound — 990 characters
# scattered over the page, 990 more in a form and one line drawn in two pieces, so that the body is grouped twice — takes 10 to
# 12 seconds (the same on main). A layout whose line grouping gave up is not grouped at all, whatever its box count
# (`line_spent_layout`). Grouping keeps a
# heap of every pair of boxes: one layout of 1,000 separate boxes took 2 seconds, 1,500 6 seconds, 2,000 14 seconds, and a 68KB
# page whose 6,000 characters lie far off the page (each its own box) 122 seconds inside one page, where the time budget does
# not look. Per layout, a 4.3KB form of 1,400 boxes drawn 15 times spent 54 seconds. The pdfium estimate of line pieces
# (`_survey_page`) did not see either: pdfium keeps positions in single precision, so characters 20pt apart at x = 2e9 fell on
# the same spot. One shared page allowance charged the body for the grouping that joining pieces discards and for the forms
# analysed before it, so pages within the bound lost their column order. Over the 983 pages of the 566 PDFs of the review
# corpus the largest page spends 848² (0.72 of it, 2.0 seconds to read), the next 320² (0.10). Pinned by:
# test_audit_pdf_text.py::TestTooManyTextBoxes (the allowance of forms per page:
# ::test_the_forms_of_a_page_are_not_charged_for_those_of_the_page_before,
# ::test_a_page_read_again_is_not_charged_for_the_forms_of_the_page_read_before_it).
_PAGE_GROUPING_COST = 1_000_000
_group_all_textboxes = LTLayoutContainer.group_textboxes


def _group_few_textboxes(self: LTLayoutContainer, laparams: object, boxes: list) -> list:
    """Replacement of pdfminer's `group_textboxes`: during a conversion, a layout whose grouping cost (the square of its box
    count) is beyond its allowance — `_PAGE_GROUPING_COST` for the page's own layout, what is left of the page's shared
    allowance for Form XObjects (`grouping_cost`) — is ordered as pdfminer orders it without grouping (`boxes_flow` None:
    vertical boxes right to left, then horizontal boxes top to bottom and left to right) — the boxes are analysed, numbered in
    that order and no group is returned, so `analyze` sorts them by those numbers. The conversion is told
    (`ungrouped_layouts`) so that the page gets the warning of pages read without text-box grouping. The function is not copied
    (same reason as `_first_seen_order`)."""
    budget = _INFLATE_BUDGET.get()
    if budget is None:
        return _group_all_textboxes(self, laparams, boxes)
    if budget.line_spent_layout is self:
        # Its lines were left a box each (`_group_textlines_within`, which counted the layout already). Such lines lie within
        # the line margin of each other, and boxes lying on top of each other are what pdfminer groups slowest: 500 of them
        # on a page (1.8KB) took 8 seconds, 1,000 in a form (29KB) 52, under the bound below
        # (Pinned by: test_audit_pdf_text.py::TestTooMuchLineGrouping::test_lines_left_ungrouped_are_not_grouped_as_boxes).
        budget.line_spent_layout = None
    else:
        cost = len(boxes) ** 2
        if not isinstance(self, LTFigure):
            if cost <= _PAGE_GROUPING_COST:
                return _group_all_textboxes(self, laparams, boxes)
        elif budget.grouping_cost + cost <= _PAGE_GROUPING_COST:
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


_group_all_textlines = LTLayoutContainer.group_textlines
_find_unbounded = Plane.find
_uniq_unbounded = pdfminer_layout.uniq


class _LineGroupingSpent(Exception):
    """The layout being grouped into text boxes has spent its line-grouping allowance (`_LINE_GROUPING_WORK`)."""


def _charge_line_work(budget: _InflateBudget, work: float) -> None:
    budget.line_work -= work
    if budget.line_work < 0:
        raise _LineGroupingSpent


def _find_counted(self: Plane, bbox: tuple[float, float, float, float]) -> object:
    """`Plane.find` that, inside `group_textlines` during a conversion, first charges the objects in the cells it will look
    through (and, before counting them, the number of cells). The function is not copied (Pinned by:
    test_audit_pdf_text.py::TestTooMuchLineGrouping::test_each_kind_of_work_counts_towards_the_allowance)."""
    budget = _INFLATE_BUDGET.get()
    if budget is not None and budget.line_work is not None:
        x0, y0, x1, y1 = (max(self.x0, bbox[0]), max(self.y0, bbox[1]), min(self.x1, bbox[2]), min(self.y1, bbox[3]))
        if x0 < x1 and y0 < y1:
            _charge_line_work(budget, (math.floor((x1 - x0) / self.gridsize) + 1) * (math.floor((y1 - y0) / self.gridsize) + 1))
            _charge_line_work(budget, sum(len(self._grid.get(cell, ())) for cell in self._getrange(bbox)))
    return _find_unbounded(self, bbox)


# Work charged for each distinct line `group_textlines` adds to a box (`_uniq_counted`), against 1 for each line it only looks
# at: adding updates the box (measured: about 0.5µs, a repeated member about 0.03µs). Pinned by:
# test_audit_pdf_text.py::TestTooMuchLineGrouping::test_each_kind_of_work_counts_towards_the_allowance.
_LINE_ADD_WORK = 10


def _uniq_counted(objs: object) -> object:
    """pdfminer's `uniq` (used in `group_textlines` only) that, inside `group_textlines` during a conversion, charges the
    lines gathered for one box, and `_LINE_ADD_WORK` more for each distinct one, which pdfminer then adds to the box."""
    budget = _INFLATE_BUDGET.get()
    if budget is None or budget.line_work is None:
        return _uniq_unbounded(objs)
    objs = objs if isinstance(objs, list) else list(objs)  # pdfminer passes its list of members
    _charge_line_work(budget, len(objs))
    return _charged_distinct(objs, budget)


def _charged_distinct(objs: list, budget: _InflateBudget) -> object:
    for obj in _uniq_unbounded(objs):
        _charge_line_work(budget, _LINE_ADD_WORK)
        yield obj


def _group_textlines_within(self: LTLayoutContainer, laparams: object, lines: list) -> object:
    """Replacement of pdfminer's `group_textlines`: during a conversion it runs with the work allowance of the layout (its own
    `_LINE_GROUPING_WORK` for the page's layout, the page's shared allowance for Form XObjects). If the allowance is spent,
    every line becomes a text box of its own (linear), those boxes are ordered without text-box grouping
    (`_group_few_textboxes`, `line_spent_layout`), and the page gets the warning of pages read without text-box grouping
    (`ungrouped_layouts`). The function is not copied."""
    budget = _INFLATE_BUDGET.get()
    if budget is None:
        yield from _group_all_textlines(self, laparams, lines)
        return
    figure = isinstance(self, LTFigure)
    budget.line_work = budget.figure_line_work if figure else _LINE_GROUPING_WORK
    try:
        boxes = list(_group_all_textlines(self, laparams, lines))
    except _LineGroupingSpent:
        boxes = None
    finally:
        if figure:
            # What this form left is what the next form of the page may spend (Pinned by:
            # test_audit_pdf_text.py::TestTooMuchLineGrouping::test_the_forms_drawn_on_a_page_share_one_allowance)
            budget.figure_line_work = max(budget.line_work, 0)
        budget.line_work = None
    budget.line_spent_layout = None
    if boxes is None:
        budget.ungrouped_layouts += 1
        budget.line_spent_layout = self
        boxes = []
        for line in lines:
            box = LTTextBoxHorizontal() if isinstance(line, LTTextLineHorizontal) else LTTextBoxVertical()
            box.add(line)
            if not box.is_empty():
                boxes.append(box)
    yield from boxes


# Plane cells the objects handed to one of pdfminer's planes may span together, at most (`_extend_coarsely`). An object is filed
# under every 50pt cell it covers, and the groups of text-box grouping cover the union of their boxes, so one character of size
# 200,000 in a form with a large BBox (859 bytes) took 8.6 seconds, two characters 100,000pt apart 1.6 seconds (both grow with the
# square of the size). 20,000 cells hold 7,000pt × 7,000pt at 50pt — an A0 poster is 3,300 cells. Pinned by:
# test_audit_pdf_text.py::TestTooMuchLineGrouping::test_far_apart_or_huge_characters_do_not_fill_the_plane_cell_by_cell.
_PLANE_CELLS = 20_000
_extend_unbounded = Plane.extend


def _extend_coarsely(self: Plane, objs: object) -> None:
    """`Plane.extend` that, during a conversion and on an empty plane, first widens the cells (doubling the 50pt size) until the
    union of the objects (within the plane) spans at most `_PLANE_CELLS` of them. The cells only index the objects — `find`
    still checks each object's box — so only the order in which `find` gives its objects can change, and only on planes whose
    objects span more than `_PLANE_CELLS` cells, which no page reaches (14,400pt square is 83,000 cells, but text spread over all
    of it is unknown in the corpus). The function is not copied."""
    if _INFLATE_BUDGET.get() is None or self._seq:
        _extend_unbounded(self, objs)
        return
    objs = list(objs)
    inside = [(max(self.x0, obj.x0), max(self.y0, obj.y0), min(self.x1, obj.x1), min(self.y1, obj.y1)) for obj in objs]
    inside = [box for box in inside if all(math.isfinite(value) for value in box) and box[0] <= box[2] and box[1] <= box[3]]
    if inside:
        width = max(box[2] for box in inside) - min(box[0] for box in inside)
        height = max(box[3] for box in inside) - min(box[1] for box in inside)
        while (width // self.gridsize + 2) * (height // self.gridsize + 2) > _PLANE_CELLS:
            self.gridsize *= 2
    _extend_unbounded(self, objs)


LTLayoutContainer.group_textlines = _group_textlines_within  # outside a conversion (no budget) it behaves as before
Plane.find = _find_counted
Plane.extend = _extend_coarsely
pdfminer_layout.uniq = _uniq_counted
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


# The clipping paths a page sets, for the documents whose writer clips each table cell (`_pdf_clip_tables`): during the
# reading of such a document (`_CELL_CLIPS`), each clip that is a rectangle along the axes of the page is noted on the
# page's layout (`_CLIPS`, in pdfminer's coordinates, in drawing order). pdfminer reads the operators `W` and `W*` and
# does nothing; outside such a document that is still all that happens. A page notes `_MAX_CLIPS` at most.
_CELL_CLIPS: ContextVar[bool] = ContextVar("pdf_cell_clips", default=False)
_CLIPS = "_opendockit_clips"
_MAX_CLIPS = 20_000
_CLIP_PATH_MAX = 6  # a rectangle is `re` or `m l l l h` (pdfminer keeps both as five segments), at most with its closing line


def _note_clip(self: PDFPageInterpreter) -> None:
    """`W`, `W*`: notes the box of a rectangular clipping path set on the page itself (not inside a form XObject)."""
    if not _CELL_CLIPS.get():
        return
    try:
        item = getattr(self.device, "cur_item", None)
        path = self.curpath
        if not isinstance(item, LTPage) or not 4 <= len(path) <= _CLIP_PATH_MAX:
            return
        if any(segment[0] not in "mlh" for segment in path):
            return
        points = [apply_matrix_pt(self.ctm, (segment[1], segment[2])) for segment in path if len(segment) == 3]
        xs, ys = sorted({round(x, 2) for x, _ in points}), sorted({round(y, 2) for _, y in points})
        if len(points) < 4 or len(xs) != 2 or len(ys) != 2:
            return  # not a rectangle along the axes
        noted = item.__dict__.setdefault(_CLIPS, [])
        if len(noted) < _MAX_CLIPS:
            noted.append((xs[0], ys[0], xs[1], ys[1]))
    except Exception:  # noqa: BLE001 — a clip that cannot be measured is one that is not noted
        return


PDFPageInterpreter.do_W = _note_clip
PDFPageInterpreter.do_W_a = _note_clip


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


# The most bytes of a TrueType `cmap` table that are read, the most code points its subtables may name in all, and the most
# the tables of one document may (`_InflateBudget.cmap_codes`). A table names ranges, not glyphs, so a few bytes name billions
# of code points, and a font has 65,535 glyphs at most: a font that names every character of the basic plane in four
# subtables names 262,144 code points. A table over its allowance gives the glyphs named before it; with the allowance of
# the document spent, the fonts after it have no map and their text is reported as text of a font without one.
_CMAP_BYTES_MAX = 16_000_000
_CMAP_CODES_MAX = 1_200_000


class _CMapWork:
    """The code points the subtables of one `cmap` table may still name (`_CMAP_CODES_MAX`)."""

    __slots__ = ("left",)

    def __init__(self, left: int) -> None:
        self.left = left

    def take(self, codes: int) -> int:
        """As many of `codes` as are left."""
        taken = max(0, min(codes, self.left))
        self.left -= taken
        return taken


def _cmap_format_0(table: bytes, at: int, work: _CMapWork) -> Iterator[tuple[int, int]]:
    yield from enumerate(struct.unpack_from(f">{work.take(256)}B", table, at + 6))


def _cmap_format_4(table: bytes, at: int, work: _CMapWork) -> Iterator[tuple[int, int]]:
    """Segments of 16-bit codes. A segment with an offset reads its glyphs from the array the offset points at, counted from
    the place of the offset itself; a glyph of 0 there is no glyph, whatever the delta."""
    (doubled,) = struct.unpack_from(">H", table, at + 6)
    segments = doubled // 2
    ends = struct.unpack_from(f">{segments}H", table, at + 14)
    starts = struct.unpack_from(f">{segments}H", table, at + 16 + doubled)
    deltas = struct.unpack_from(f">{segments}h", table, at + 16 + 2 * doubled)
    offsets_at = at + 16 + 3 * doubled
    offsets = struct.unpack_from(f">{segments}H", table, offsets_at)
    for index, (first, last, delta, offset) in enumerate(zip(starts, ends, deltas, offsets, strict=True)):
        codes = work.take(last - first + 1)
        if codes <= 0:
            continue
        if not offset:
            for code in range(first, first + codes):
                yield code, (code + delta) & 0xFFFF
            continue
        glyphs = struct.unpack_from(f">{codes}H", table, offsets_at + 2 * index + offset)
        for code, glyph in enumerate(glyphs, first):
            if glyph:
                yield code, (glyph + delta) & 0xFFFF


def _cmap_format_6(table: bytes, at: int, work: _CMapWork) -> Iterator[tuple[int, int]]:
    first, codes = struct.unpack_from(">HH", table, at + 6)
    yield from enumerate(struct.unpack_from(f">{work.take(codes)}H", table, at + 10), first)


def _cmap_format_10(table: bytes, at: int, work: _CMapWork) -> Iterator[tuple[int, int]]:
    first, codes = struct.unpack_from(">II", table, at + 12)
    yield from enumerate(struct.unpack_from(f">{work.take(codes)}H", table, at + 20), first)


def _cmap_format_12(table: bytes, at: int, work: _CMapWork) -> Iterator[tuple[int, int]]:
    (groups,) = struct.unpack_from(">I", table, at + 12)
    for group in range(min(groups, (len(table) - at - 16) // 12)):
        first, last, glyph = struct.unpack_from(">III", table, at + 16 + 12 * group)
        codes = work.take(last - first + 1)
        if codes <= 0:
            if not work.left:
                return
            continue
        yield from zip(range(first, first + codes), range(glyph, glyph + codes), strict=True)


_CMAP_FORMATS = {0: _cmap_format_0, 4: _cmap_format_4, 6: _cmap_format_6, 10: _cmap_format_10, 12: _cmap_format_12}


def _unicode_map_of_the_font(self: pdffont.TrueTypeFont) -> FileUnicodeMap:
    """What each glyph of an embedded TrueType font is, from the font's own `cmap` table: the last source of a CID font that
    names its glyphs by number and has no ToUnicode (`PDFCIDFont`, Adobe-Identity and Adobe-UCS). Replaces
    `TrueTypeFont.create_unicode_map`.

    pdfminer read the commonest subtable (format 4) wrongly: the offset of a segment counts from the place of that offset in
    its array, and pdfminer counted every one from the start of the array, so only the first segment with an offset was read
    right — of the 2,691 glyphs Arial names, 1,261 came out as another character and nothing said so ('25°C ± 2, price £ 10'
    read '25±C¡²¡2,¡price¡¤¡10': the letters of ASCII stand in a segment without an offset and survive, so the page looks
    read). It also gave a glyph of 0 the delta of its segment, kept the last of the characters that share a glyph (the soft
    hyphen for the hyphen, which cleaning then removes; U+00A0 for the space), raised on a subtable of a format it does not
    know — a table of variation sequences (format 14) stands under the Unicode platform in most current fonts — and walked
    whatever a table named: a subtable is a list of ranges, and a few bytes name billions of code points.

    Here every Unicode subtable (platform 0; platform 3, encodings 1 and 10) of a known format is read, a glyph gets the lowest
    of its code points (the hyphen, the space, the Greek letter before the sign that looks like it), surrogates and code
    points past U+10FFFF are no characters, a subtable of another format is passed over, and a subtable that ends early gives
    what was read before its end. The ranges of one table may name `_CMAP_CODES_MAX` code points in all, those of a document
    `_DOCUMENT_CMAP_CODES` (Pinned by: test_pdf_truetype_cmap.py)."""
    if b"cmap" not in self.tables:
        raise pdffont.TrueTypeFont.CMapNotFound
    offset, length = self.tables[b"cmap"]
    self.fp.seek(offset)
    table = self.fp.read(min(length, _CMAP_BYTES_MAX))
    lowest: dict[int, int] = {}
    budget = _INFLATE_BUDGET.get()
    allowed = _CMAP_CODES_MAX if budget is None else min(_CMAP_CODES_MAX, budget.cmap_codes)
    work = _CMapWork(allowed)
    try:
        (subtables,) = struct.unpack_from(">H", table, 2)
        for index in range(min(subtables, (len(table) - 4) // 8)):
            platform, encoding, at = struct.unpack_from(">HHL", table, 4 + 8 * index)
            if not (platform == 0 or (platform == 3 and encoding in (1, 10))):
                continue
            (number,) = struct.unpack_from(">H", table, at)
            read = _CMAP_FORMATS.get(number)
            if read is None:
                continue
            try:
                for code, glyph in read(table, at, work):
                    if glyph and (glyph not in lowest or code < lowest[glyph]) and _is_character(code):
                        lowest[glyph] = code
            except struct.error:   # this subtable ends early: what it gave before stays, and the next one is read
                continue
    except struct.error:
        pass
    if budget is not None:
        budget.cmap_codes -= allowed - work.left
    if not lowest:
        raise pdffont.TrueTypeFont.CMapNotFound
    unicode_map = FileUnicodeMap()
    unicode_map.cid2unichr = {glyph: chr(code) for glyph, code in lowest.items() if glyph <= 0xFFFF}
    return unicode_map


def _is_character(code: int) -> bool:
    return code < 0xD800 or 0xE000 <= code <= 0x10FFFF and code & 0xFFFE != 0xFFFE


pdffont.TrueTypeFont.create_unicode_map = _unicode_map_of_the_font


def _name(value: object) -> str:
    return str(getattr(value, "name", "") or "")
