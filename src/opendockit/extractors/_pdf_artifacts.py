"""Characters of a page that are not its text: undecodable or unmeasured text, watermarks, shadow copies, tilted text,
text drawn outside the page view, and sideways text set apart to be read in its own direction.
"""

from bisect import bisect_left, bisect_right
import statistics

from pdfminer.layout import LTContainer, LTFigure, LTTextLineHorizontal
from pdfminer.pdftypes import (
    PDFStream,
    resolve1,
)

from opendockit.extractors._pdf_chars import _text_from_chars
from opendockit.extractors._pdf_model import _inside_any
from opendockit.extractors._pdf_pdfminer import _LAPARAMS


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


_WATERMARK_SIZE_RATIO = 2.0
# Horizontally written watermark: a line at least this multiple of the page's median character size whose colour components are all
# at least _WATERMARK_LIGHT_MIN (light grey) and which overlaps other characters (`_faint_watermark`). Horizontal text may be a
# heading, so it is judged more narrowly than slanted text (_WATERMARK_SIZE_RATIO).
_WATERMARK_FLAT_RATIO = 3.0
_WATERMARK_LIGHT_MIN = 0.7


# A page where at least this share of the visible characters has zero width is read with pdfium's simple extraction (`_unmeasured`).
_UNMEASURED_RATIO = 0.5
# When the raw length of a content stream is longer than this but the decoded result is empty, decoding failed (an empty Flate
# stream is 8 bytes, 12 bytes even when wrapped in ASCII85).
_MIN_CONTENT_LENGTH = 16


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
