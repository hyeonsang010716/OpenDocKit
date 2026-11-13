"""From the characters of a page to its lines: characters that are not text (watermarks, shadow copies, tilted or hidden
text), sideways text, pieces of a line put back together, text placed character by character without spaces, letter
spacing, and superscripts and subscripts.
"""

from bisect import bisect_left, bisect_right, insort
import copy
from dataclasses import dataclass, field
from itertools import islice
import math
import re
import statistics

from pdfminer.layout import LTChar, LTContainer, LTFigure, LTLayoutContainer, LTTextLineHorizontal
from pdfminer.pdftypes import (
    PDFStream,
    resolve1,
)

from opendockit.extractors._inline import SUBSCRIPT, SUPERSCRIPT, mark_script, resolve_scripts
from opendockit.extractors._pdf_model import _MAX_GROUPED_TEXT_PIECES, _SPACED_CHAR, _WORD_CHAR, _inside_any, _Line
from opendockit.extractors._pdf_pdfminer import _LAPARAMS


_FOOTNOTE_MARK_PIECE = re.compile(r"^(?:\d{1,3}\)|\*{1,3})$")  # footnote number piece drawn separately ('1)', '*', `_footnote_mark_piece`)


# Gap within a line (relative to the character size, `_split_merged_cells`) taken as a spot where pdfminer (char_margin 2.0) merged
# neighbouring cells into one piece inside a borderless table. Ordinary word gaps are 0.25–0.5×. Between the cells of a narrow
# column (32pt) the cell padding is 6pt + 6pt, which was 1.2× for 10pt characters.
_CELL_GAP_RATIO = 0.8


# A spot where digit and digit are this far apart without a space character is also a cell boundary candidate (`fine_parts`). The
# 4px + 4px cell padding of a Chrome table is 0.5× a 12pt character, so a right-aligned amount and the next column's percentage
# became the single cell '1,250 65%' (Pinned by: test_pdf_table_columns.py::TestWideWrappedFirstColumn).
# Digits within one number touch each other, and between words there is a space character.
_NUMBER_GAP_RATIO = 0.4
_NUMBER_EDGE = re.compile(r"[\d%‰)]")  # numeric character ending a cell (before the gap)
_NUMBER_START = re.compile(r"[\d(△▲▼+\-−–]")  # numeric character starting a cell (after the gap)


# Upper bound on the gap between characters (relative to the character size) for a line to count as written with widened character
# spacing (Tc), and the difference (pt) within which two gaps count as the same spacing. Ordinary word gaps are 0.25–0.5×, so
# character spacing widened beyond that cannot be told from word gaps (`_letter_spacings`).
_LETTER_SPACING_MAX = 0.5
_SPACING_TOLERANCE = 0.3


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


# A page where at least this share of the visible characters has zero width is read with pdfium's simple extraction (`_unmeasured`).
_UNMEASURED_RATIO = 0.5
# When the raw length of a content stream is longer than this but the decoded result is empty, decoding failed (an empty Flate
# stream is 8 bytes, 12 bytes even when wrapped in ASCII85).
_MIN_CONTENT_LENGTH = 16


_RIGHT_TO_LEFT = re.compile(r"[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufefc]")  # Hebrew and Arabic script (`_late_symbols_in_place`)


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


# When the number of Form XObject line clusters × the number of text lines on the page is larger than this, nothing is moved
# (finding the position takes time proportional to the product of the two; 4 million ≈ 0.3 seconds).
_XOBJECT_PLACEMENT_MAX_WORK = 4_000_000


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
    into one piece with the characters re-laid by x. A merged piece takes the place of its first piece. Only pairs that can overlap
    both ways are examined: the pieces are scanned in x0 order, and of the earlier pieces that still reach the current x0 (`live`,
    sorted by y0) only those starting below the piece's top and less than 2 / `_PIECE_HEIGHT_RATIO` heights below its bottom are
    compared — `_shares_baseline` is False for every other pair (no x overlap, no vertical overlap, or a taller piece than the height
    ratio allows), and the merged groups depend only on the pairs for which it is True. Scanning in y0 order alone compared every
    pair of one row: a single row of 6,000 two-letter pieces (an 8.5KB PDF) took 28 seconds inside one page
    (Pinned by: test_audit_pdf_head.py::TestManyPiecesOnOneBaseline). With a non-finite coordinate the pieces are scanned in y0
    order as before."""
    order = sorted(range(len(pieces)), key=lambda index: pieces[index].y0)
    parent = list(range(len(pieces)))
    texts = [piece.get_text().strip() for piece in pieces]  # rejoining the text for every pair would build text the square of the piece count times

    def root(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    if all(math.isfinite(value) for piece in pieces for value in piece.bbox):
        wide = [index for index in range(len(pieces)) if pieces[index].x1 > pieces[index].x0]  # the others overlap nothing in x
        ends, ended = sorted(wide, key=lambda index: pieces[index].x1), 0
        live: list[tuple[float, int]] = []  # (y0, piece number)
        for index in sorted(wide, key=lambda index: pieces[index].x0):
            piece = pieces[index]
            while pieces[ends[ended]].x1 <= piece.x0:
                del live[bisect_left(live, (pieces[ends[ended]].y0, ends[ended]))]
                ended += 1
            for position in range(bisect_left(live, (piece.y0 - piece.height * 2 / _PIECE_HEIGHT_RATIO,)), len(live)):
                y0, other = live[position]
                if y0 > piece.y1:
                    break
                if _shares_baseline(piece, pieces[other], texts[index], texts[other]):
                    parent[root(other)] = root(index)
            insort(live, (piece.y0, index))
    else:
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
