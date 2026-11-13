"""Pictures of a page: the areas that hold a figure, the clip they are drawn through, tiled backgrounds, rendering with
pdfium within the pixel budget, and the pages that are a scan or whose text layer cannot be trusted.
"""

from bisect import bisect_left, bisect_right, insort
from io import BytesIO
from itertools import islice
import math
import re
import statistics

from pdfminer.pdfdevice import PDFDevice
from pdfminer.pdfinterp import PDFPageInterpreter
from pdfminer.utils import MATRIX_IDENTITY, apply_matrix_pt, mult_matrix
import pypdfium2 as pdfium

from opendockit.extractors._pdf_model import _MIN_FIGURE_POINTS, _PDFIUM_LOCK, _box_area, _encloses, _Page
from opendockit.models import ConvertOptions, Extraction, ImageKind


# On a page with characters, an image covering at least this share of the area is the background.
_BACKGROUND_RATIO = 0.9
# When images drawn directly on the page adjoin edge to edge (gap/overlap at most this many pt) and their union covers the page,
# they are one background (_tiled_background).
_TILE_GAP = 2.0


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


# A picture is rendered to match its original pixel size, but not beyond this scale (4 = 288dpi). Drawing it smaller than the
# original made the reading of small characters unreliable.
_MAX_FIGURE_SCALE = 4.0
# Maximum number of pixels of the bitmap created by one render (about 5000×5000, 75MB as RGB). Beyond it the scale is lowered. The
# image is sent to the vision model reduced to a long side of `image_max_side` (default 2048), so reading is not affected. Without
# it the bitmap of a large page such as a poster or drawing became several GB
# (Pinned by: test_image.py::TestResourceBounds::test_pdf_figure_on_a_huge_page_renders_only_its_own_area,
# test_pdf_scanned_huge_page_is_rendered_within_the_pixel_cap).
_MAX_RENDER_PIXELS = 25_000_000


# Number of pictures to render per document = max_images × this factor (no limit if max_images is None). Repeated logos are
# filtered out by deduplication after rendering, so some headroom is left.
_RENDER_BUDGET_FACTOR = 2


def _open_pdfium(data: bytes) -> pdfium.PdfDocument:
    with _PDFIUM_LOCK:
        return pdfium.PdfDocument(data)


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

    # Boxes that abut have a side within `_TILE_GAP` of the other's opposite side, so when that is fewer boxes than the ones
    # that follow down to this box's bottom, only the boxes with a side near the opposite side of this one are compared (the
    # same pairs are joined). Comparing every pair took 8 of 10 seconds on a page whose picture was drawn as 3,000 vertical
    # strips, which all share a top (a 12 KB file) (Pinned by:
    # test_audit_pdf_mid.py::TestPictureDrawnAsManyStrips::test_strips_are_compared_with_their_neighbours_only).
    # A coordinate that is not a finite number has no place in this search, so such a page is compared pair by pair as before
    sorted_sides = (None if not all(math.isfinite(value) for box in boxes for value in box[:4]) else
                    [sorted((boxes[index][side], place) for place, index in enumerate(order)) for side in range(4)])
    for position, index in enumerate(order):
        if sorted_sides is None:
            for other in islice(order, position + 1, None):
                if boxes[other][1] > boxes[index][3] + _TILE_GAP:
                    break
                if _abuts(boxes[index], boxes[other]):
                    parent[root(other)] = root(index)
            continue
        end = bisect_right(sorted_sides[1], (boxes[index][3] + _TILE_GAP, len(order)))  # the boxes after it start lower than this box reaches
        near = []
        for side, values in enumerate(sorted_sides):
            opposite = boxes[index][(side + 2) % 4]
            reach = _TILE_GAP + 1 + abs(opposite) * 1e-9  # wider than `_abuts` allows, whatever the rounding
            near.append((values, bisect_left(values, (opposite - reach,)), bisect_right(values, (opposite + reach, len(order)))))
        places = (range(position + 1, end) if end - position <= sum(high - low for _, low, high in near)
                  else [place for values, low, high in near for _, place in values[low:high] if position < place < end])
        for place in places:
            if _abuts(boxes[index], boxes[order[place]]):
                parent[root(order[place])] = root(index)

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
    # A piece that encloses another starts no further right (lower) than it and, being no wider (taller) than the widest
    # (tallest) piece, not far to the left of (above) its end, so only the pieces starting in the narrower of those two ranges
    # are compared instead of every piece (Pinned by:
    # test_audit_pdf_mid.py::TestPictureDrawnAsManyStrips::test_strips_are_compared_with_their_neighbours_only).
    # A coordinate that is not a finite number has no place in this search, so then every piece is compared as before
    ordered = (None if not all(math.isfinite(value) for tile in shapes for value in tile) else
               [sorted(shapes, key=lambda tile, low=low: tile[low]) for low in (0, 1)])
    starts = [[tile[low] for tile in pieces] for low, pieces in enumerate(ordered or ())]
    longest = [max(tile[low + 2] - tile[low] for tile in shapes) for low in (0, 1)]

    def others(tile: tuple[float, ...]) -> list[tuple[float, ...]]:
        if ordered is None:
            return shapes
        ranges = []
        for low in (0, 1):
            reach = 3 + (abs(tile[low]) + abs(tile[low + 2]) + abs(longest[low])) * 1e-9  # wider than `_encloses` allows
            ranges.append((bisect_left(starts[low], tile[low + 2] - longest[low] - reach), bisect_right(starts[low], tile[low] + reach)))
        low = 0 if ranges[0][1] - ranges[0][0] <= ranges[1][1] - ranges[1][0] else 1
        return ordered[low][ranges[low][0]:ranges[low][1]]

    outer = [tile for tile in shapes if not any(_encloses(other, tile) and not _encloses(tile, other) for other in others(tile))]
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
    # The boxes crossing each band are kept in a sorted list that a box enters at its top and leaves at its bottom (the same
    # list as picking them out of all the boxes for every band, which took the number of bands times the number of boxes)
    # (Pinned by: test_audit_pdf_mid.py::TestPictureDrawnAsManyStrips::test_union_area_of_many_stacked_strips_is_swept_once).
    # A coordinate that is not a finite number has no place in this list, so then the boxes are picked for every band as before
    swept = all(math.isfinite(value) for box in boxes for value in box)
    entering: dict[float, list[tuple[float, float]]] = {}
    leaving: dict[float, list[tuple[float, float]]] = {}
    for box in boxes:
        if swept and box[1] < box[3]:
            entering.setdefault(box[1], []).append((box[0], box[2]))
            leaving.setdefault(box[3], []).append((box[0], box[2]))
    spans: list[tuple[float, float]] = []
    for low, high in zip(edges, edges[1:], strict=False):
        if swept:
            for span in leaving.get(low, ()):
                del spans[bisect_left(spans, span)]
            for span in entering.get(low, ()):
                insort(spans, span)
        else:
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
