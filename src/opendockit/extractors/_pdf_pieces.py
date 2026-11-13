"""One line drawn as several pieces: the pieces are put back together, overlapping pieces are merged with their characters
in reading order, symbols and footnote marks go where they stand, and the lines of a form placed on the page take their
place.
"""

from bisect import bisect_left, bisect_right, insort
import copy
from dataclasses import dataclass, field
from itertools import islice
import math
import re

from pdfminer.layout import LTChar, LTContainer, LTFigure, LTLayoutContainer, LTTextLineHorizontal

from opendockit.extractors._pdf_chars import _SCRIPT_SIZE_RATIO, _SUPERSCRIPT_RISE
from opendockit.extractors._pdf_model import _MAX_GROUPED_TEXT_PIECES, _WORD_CHAR, _Line


_FOOTNOTE_MARK_PIECE = re.compile(r"^(?:\d{1,3}\)|\*{1,3})$")  # footnote number piece drawn separately ('1)', '*', `_footnote_mark_piece`)


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


_RIGHT_TO_LEFT = re.compile(r"[\u0590-\u08ff\ufb1d-\ufdff\ufe70-\ufefc]")  # Hebrew and Arabic script (`_late_symbols_in_place`)


# When the number of Form XObject line clusters × the number of text lines on the page is larger than this, nothing is moved
# (finding the position takes time proportional to the product of the two; 4 million ≈ 0.3 seconds).
_XOBJECT_PLACEMENT_MAX_WORK = 4_000_000


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
