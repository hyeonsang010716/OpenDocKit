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

from collections import Counter
from dataclasses import dataclass, field
from io import BytesIO
from itertools import islice
import logging
import re
import statistics
import threading
import time

from pdfminer.pdftypes import (
    resolve1,
)
from pdfminer.utils import INF as _LAYOUT_INF
import pdfplumber
from pdfplumber.utils.exceptions import PdfminerException
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c

from opendockit.exceptions import CorruptedDocumentError, DocumentTooLargeError, EncryptedDocumentError
from opendockit.extractors._pdf_aligned_tables import _extract_aligned_tables
from opendockit.extractors._pdf_artifacts import (
    _chars_at_line_level,
    _CopyIndex,
    _has_undecodable_contents,
    _is_sideways,
    _is_tilted,
    _part_outside_cells,
    _sideways_paragraphs,
    _text_view,
    _text_without_artifacts,
    _unmeasured,
    _view_box,
    _without_drawing_artifacts,
    _xobject_owners,
)
from opendockit.extractors._pdf_chars import (
    _drawn_text,
    _drop_cap,
    _gap_parts,
    _letter_spaced_text,
    _letter_spacings,
    _scripted_text,
    _text_from_chars,
)
from opendockit.extractors._pdf_continued_tables import _carry_table_headers, _join_ruled_header_lines
from opendockit.extractors._pdf_figures import _figure_boxes, _open_pdfium, _page_images, _PageRenderer, _untrusted_text_layer_pages
from opendockit.extractors._pdf_forms import _annotated_attachments, _form_values, _has_xfa, _named_attachments
from opendockit.extractors._pdf_margins import _body_font_size, _edge_rows, _remove_repeated_margins, _typical_line_gap
from opendockit.extractors._pdf_marks import _mark_checkboxes, _mark_circle_bullets
from opendockit.extractors._pdf_model import (
    _DIGITS,
    _MAX_GROUPED_TEXT_PIECES,
    _PDFIUM_LOCK,
    _REPEAT_RATIO,
    _SIDE_REPEAT_RATIO,
    _BoxIndex,
    _in_margin,
    _info_text,
    _Line,
    _Page,
    _visual_rows,
)
from opendockit.extractors._pdf_paragraphs import _heading_levels, _render_page
from opendockit.extractors._pdf_pdfminer import (
    _DOCUMENT_FONT_RANGE_ENTRIES,
    _FONT_RANGES_CUT,
    _FORM_NESTING_MAX,
    _INFLATE_BUDGET,
    _LAPARAMS,
    _MAX_DRAWN_OBJECTS,
    _REPEATED_FORMS_SKIPPED,
    _TOO_LARGE,
    _charge_page,
    _drawn_objects,
    _InflateBudget,
    _name,
)
from opendockit.extractors._pdf_pieces import _place_xobject_lines, _rejoined_lines
from opendockit.extractors._pdf_placed_runs import _repair_placed_runs
from opendockit.extractors._pdf_reading_order import (
    _read_alternating_sides_in_turn,
    _read_columns_in_turn,
    _read_lone_lines_in_place,
    _read_sidebar_first,
)
from opendockit.extractors._pdf_row_joins import _join_row_fragments
from opendockit.extractors._pdf_ruled_tables import _real_tables, _ruled_table, _with_open_below
from opendockit.extractors._pdf_shaded_tables import _shade_bands, _thin_rules
from opendockit.extractors._pdf_table_filters import (
    _without_glyph_outlines,
    _without_highlights,
    _without_unadvanced_spaces,
    _without_unbounded_shapes,
)
from opendockit.markdown import solid_length
from opendockit.models import ConvertOptions, Extraction, Section, SectionKind


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


_UNGROUPED_LAPARAMS = {**_LAPARAMS, "boxes_flow": None}


_CID = re.compile(r"\(cid:\d+\)")
# Predefined Unicode CMap name → CIDSystemInfo Ordering (_repair_cid_system_info)
_UNICODE_CMAP = re.compile(r"Uni(KS|JIS|GB|CNS)-")
_CID_ORDERINGS = {"KS": b"Korea1", "JIS": b"Japan1", "GB": b"GB1", "CNS": b"CNS1"}
# Orderings a correctly read CIDSystemInfo of such a font can hold (Japan2 and KR are the newer collections of the same scripts).
_KNOWN_CID_ORDERINGS = frozenset((b"Korea1", b"KR", b"Japan1", b"Japan2", b"GB1", b"CNS1", b"Identity"))


# A spot this far apart (relative to the character size) without a space character is also a cell boundary candidate
# (`_Line.fine_parts`). In Chrome the table cell padding (6px + 6px) is 0.75× a 10pt character and no space character is placed
# between cells. Between words there is either a space character (mostly) or a gap of 0.25–0.5×.
_UNSPACED_GAP_RATIO = 0.6


# Dot leader characters left out of the running character count (`_countable_chars`).
_LEADER_DOTS = re.compile(r"[.·∙・…‥]")


# On a page with 0 characters, the pdfium character count has to exceed this multiple of the hidden character count (outside the
# visible area, size 0, copies) to count as text pdfminer missed.
_HIDDEN_TEXT_RATIO = 1.5
# In a document with an undecoded CID font (`_cmap_or_embedded`), a page where pdfium's character count is larger than this multiple
# of the characters pdfminer read (`_read_chars`) plus `_UNDECODED_MIN_CHARS` is read with pdfium's simple extraction. Only that
# font's text was missing while the text of other fonts remained, so the fallback for pages with 0 characters did not kick in.
_UNDECODED_TEXT_RATIO = 1.25
_UNDECODED_MIN_CHARS = 10


# When there are neither characters nor images but this many curve points, the page is taken as one whose characters were converted
# to outlines (a design PDF) and sent whole. One character's outline is dozens of points, so a few words exceed it, and borders and
# dividers (rectangles, straight lines) are not curves and are not counted
# (Pinned by: test_image.py::test_page_of_vector_outlines_without_text_is_sent_whole_for_ocr, test_page_with_only_a_frame_is_not_sent).
_OUTLINE_MIN_POINTS = 200


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
            crammed: set[int] = set()  # pages found with too much text-box grouping while being read (`_PAGE_GROUPING_COST`)
            grids: set[int] = set()  # ruled grid pages (read without text-box grouping, but not reported since cell text goes into the table)
            attachments: dict[object, str] = {}  # attachment file specification (object number or held object) → name
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
                            extraction.time_limited = True
                            try:
                                pages.append(_pdfium_page(fallback, page.page_number - 1))
                            except pdfium.PdfiumError:
                                # A page pdfium cannot open (pdfminer follows `/Kids`, pdfium trusts `/Count`) is an empty page,
                                # not a corrupted document — the pages already read are kept
                                # (Pinned by: test_audit_pdf_head.py::TestPageOnlyPdfminerFinds).
                                pages.append(_Page(number=page.page_number, width=float(page.width), height=float(page.height),
                                                   lines=[], tables=[], has_images=False,
                                                   view=(0.0, 0.0, float(page.width), float(page.height)), simple=True))
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
                            ungrouped_before = budget.ungrouped_layouts
                            budget.new_page()
                            read = _read_page_from(ungrouped if page.page_number in dense | grids else pdf, page, placement_fit)
                            if budget.ungrouped_layouts > ungrouped_before:
                                # Too much text-box grouping (`_PAGE_GROUPING_COST`). The page was read from `pdf` with the bound
                                # in force, so it is read again from there (`crammed` is not in `dense`, which picks `ungrouped`)
                                crammed.add(page.page_number)
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
                                try:
                                    simple = _pdfium_page(fallback, page.page_number - 1)
                                except pdfium.PdfiumError:
                                    simple = read  # pdfium cannot open the page: the pdfminer result stays (see above)
                                floor = (_read_chars(read) * _UNDECODED_TEXT_RATIO + _UNDECODED_MIN_CHARS if undecoded
                                         else read.hidden_chars * _HIDDEN_TEXT_RATIO)
                                if simple is not read and simple.lines and _solid_chars(simple) > floor:
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
                        try:
                            pages.append(_pdfium_page(fallback, page.page_number - 1))
                        except pdfium.PdfiumError:  # neither library reads the page: an empty page (see above)
                            pages.append(_Page(number=page.page_number, width=float(page.width), height=float(page.height),
                                               lines=[], tables=[], has_images=False,
                                               view=(0.0, 0.0, float(page.width), float(page.height)), simple=True))
                        failed.append(page.page_number)
                    finally:
                        page.close()
                    _check_budget(budget)  # an exceeded limit swallowed by broad exception handling (font repair, form reading)
                    # Page analysis is the slowest part of this extractor. Once the character limit is exceeded, the remaining
                    # pages are not read.
                    if extraction.count(_countable_chars(pages[-1], tally), options):
                        break

                # A page with few characters, such as a cover or table of contents, cannot determine the character width on its
                # own. If a later page determined it, only those pages are read again — while the time budget lasts: past it a
                # page keeps its first reading (its words placed without spaces stay joined). Re-reading ignored the budget, so
                # every pending page was analysed a second time after the budget had sent the remaining pages to plain
                # extraction (Pinned by: test_audit_pdf_text.py::TestPendingPagesAfterTheTimeBudget).
                for index, read in enumerate(pages):
                    if placement_fit is None or not read.placement_pending or extraction.exceeded:
                        continue
                    if options.pdf_time_budget is not None and time.perf_counter() - started > options.pdf_time_budget:
                        extraction.time_limited = True
                        break
                    # A page found crowded with text boxes only while being read (`crammed`) was read from `pdf` and is read
                    # from it again, with the grouping cost of the page reset — the same layouts are left ungrouped. Taking it
                    # for `dense` sent it to `ungrouped`, which had not been opened, and the whole document failed (Pinned by:
                    # test_audit_pdf_text.py::TestTooManyTextBoxes::test_a_crowded_page_read_again_keeps_its_first_reading;
                    # the reset: ::test_a_page_read_again_is_not_charged_for_the_forms_of_the_page_read_before_it)
                    page = (ungrouped if read.number in dense | grids else pdf).pages[read.number - 1]
                    ungrouped_before = budget.ungrouped_layouts
                    budget.new_page()
                    try:
                        pages[index] = _read_page(page, placement_fit)
                    except PdfminerException:
                        pass
                    finally:
                        page.close()
                    if budget.ungrouped_layouts > ungrouped_before:
                        crammed.add(read.number)  # the warning also when only the second reading met the bound
            finally:
                if fallback is not None:
                    with _PDFIUM_LOCK:
                        fallback.close()
                if ungrouped is not None:
                    ungrouped.close()
            dense |= crammed  # the same warning for both
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
    if budget.unruled_pages:
        extraction.warnings.append(f"{len(budget.unruled_pages)} PDF page(s) ({_page_list(sorted(budget.unruled_pages))}) have too "
                                   "many ruling lines, so ruled tables were not looked for on them (their text is read as paragraphs).")
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
    unrendered: list[int] = []  # pages with an untrusted text layer whose whole-page picture was not rendered (picture budget)
    has_cid = False
    # Once the limit is exceeded no pictures are rendered (that leads on to image decoding and vision model calls, all of it wasted).
    renderer = _PageRenderer(data, options) if options.extract_images and not extraction.exceeded else None
    try:
        for page in pages:
            images = _page_images(page, renderer, extraction) if renderer else []
            if page.text_untrusted and not images:
                unrendered.append(page.number)
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
    # Only the pages whose picture was rendered were handed over: with `max_images` low (or 0) the render budget runs out, and
    # those pages kept only their text layer, while the warning said every page was handed over as an image (Pinned by:
    # test_image.py::TestPdfImages::test_untrusted_pages_beyond_the_picture_budget_are_not_said_to_be_handed_over).
    handed = [page.number for page in untrusted if page.number not in unrendered]
    if handed:
        extraction.warnings.append(
            f"{len(handed)} PDF page(s) ({_page_list(handed)}) appear to have no body text in their "
            "text layer (no Hangul and sparse characters), so each whole page was handed over as an image.")
    if unrendered:
        extraction.warnings.append(
            f"{len(unrendered)} PDF page(s) ({_page_list(unrendered)}) appear to have no body text in their text layer, but "
            "there were too many pictures to hand them over as images, so only their text layer was read.")
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

    Text read sideways (`page.sideways`) and form values (`page.form_values`) are counted too — both are always written at the end
    of the page. Left out, a document whose text is all rotated by 90 degrees was analysed page after page far beyond the limit
    until the time budget stopped it (Pinned by: test_audit_pdf_text.py::TestLimitCountsSidewaysTextAndFormValues).
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
               for text in [*(line.text for line in lines), *(table.text for table in page.tables), *page.sideways,
                            *page.form_values])


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
    # A MediaBox without area (`[0 0 0 0]`, `[0 0 612 0]`) made a page of height 0, so every character lay outside the visible
    # area and was dropped, and pdfium read no more than that, so nothing took its place. Such a page is read as US Letter, the
    # size pdfium shows it at (Pinned by: test_pdf_page_boxes.py::TestEmptyMediaBox).
    media = getattr(source, "mediabox", None)
    if (isinstance(media, list | tuple) and len(media) == 4 and all(isinstance(value, int | float) for value in media)
            and (media[2] - media[0] <= 0 or media[3] - media[1] <= 0)):
        source.mediabox = source.cropbox = (0.0, 0.0, 612.0, 792.0)
        width, height = (612.0, 792.0) if getattr(page, "rotation", 0) % 180 == 0 else (792.0, 612.0)
        page.mediabox = page.cropbox = page.bbox = (0.0, 0.0, width, height)
        page.flush_cache(["_layout", "_objects"])  # characters already read were placed on the page without area


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
    tables = _real_tables(_without_unadvanced_spaces(_without_highlights(_without_glyph_outlines(_without_unbounded_shapes(page)))))
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
    copy_index = _CopyIndex(dropped, dropped_tops)
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
    shown = _text_view(page)
    for line, owner in zip(layout_lines, owners, strict=True):
        text = " ".join(line.get("text", "").split())
        if not text:
            continue
        spot = (line["x0"], line["x1"], line["top"], line["bottom"])
        if not all(abs(value) < _LAYOUT_INF for value in spot):
            # A line whose characters sit at a position that is not a finite number (a text matrix overflowing to infinity) is
            # drawn nowhere; pdfminer gives it its 'infinite' box (2**31 - 1). Its characters are already dropped (as outside
            # the view or, on a rotated page, for their position: `_without_drawing_artifacts`), but the layout line kept
            # their text and every copy came out as a body line (Pinned by: test_audit_pdf_text.py::
            # TestCharactersAtNonFinitePositions::test_a_rotated_page_is_read_too — on an upright page `_beyond_view` drops
            # such a line as well). No page reaches that far (14,400pt × UserUnit 75,000).
            continue
        if shown and _beyond_view(line, shown[0]):
            continue
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
        has_copies, shadowed = copy_index.at(line)  # removed characters under the line (`_CopyIndex`)
        if has_copies:
            kept = _text_without_artifacts(line, text, inside, kept_lines, shadowed)
            if kept is None:
                continue
            if kept == text:
                overprinted.add(spot)
            text = kept
        kept_lines.setdefault(text, []).append(line)
        text = _scripted_text(inside, text) or text
        text, top, bottom = _drop_cap(inside, text) or (text, line["top"], line["bottom"])
        sizes = [char["size"] for char in inside if char["text"].strip()]
        size = statistics.median(sizes) if sizes else 0.0
        lines.append(_Line(top=top, bottom=bottom, x0=line["x0"], x1=line["x1"], text=_drawn_text(text),
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


def _beyond_view(line: dict, view: tuple[float, float, float, float]) -> bool:
    """Whether a layout line lies where no character kept on the page can belong to it: above or below the visible area, or
    more than 1pt right of it. Every character of such a line is outside the view and dropped (`_without_drawing_artifacts`),
    so the line would be dropped as a copy of removed characters anyway (`_text_without_artifacts`) — but finding that out
    picks the kept characters at its height and scans the removed ones, and 6,000 characters placed apart far right of the
    page (x = 2e9) on one row took that scan for every one of them. The removed ones are indexed now (`_CopyIndex`); the
    kept ones are still picked line by line, so 8,000 characters right of the page on the rows of 40,000 small ones (21KB)
    take 27 seconds without this and 1.9 with (Pinned by: test_audit_pdf_text.py::TestTooManyTextBoxes::
    test_lines_beyond_the_view_are_not_matched_against_the_characters_of_the_page). The left side is not judged: a wide
    kept character starting left of the view could still be centred inside it."""
    return line["bottom"] < view[1] or line["top"] > view[3] or line["x0"] - 1 > view[2]


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


def _page_list(numbers: list[int]) -> str:
    shown = ", ".join(str(number) for number in numbers[:10])
    return f"{shown} and more" if len(numbers) > 10 else shown
