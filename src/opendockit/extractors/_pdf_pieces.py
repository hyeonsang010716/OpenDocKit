"""One line drawn as several pieces: the pieces are put back together, overlapping pieces are merged with their characters
in reading order, symbols and footnote marks go where they stand, and the lines of a form placed on the page take their
place.
"""

from bisect import bisect_left, bisect_right, insort
from collections.abc import Iterator
import copy
from dataclasses import dataclass, field
from itertools import islice
import math
import re

from pdfminer.layout import LTChar, LTContainer, LTFigure, LTLayoutContainer, LTTextLineHorizontal

from opendockit.extractors._pdf_bidi import _RIGHT_TO_LEFT
from opendockit.extractors._pdf_chars import (
    _SCRIPT_FAR_SHIFT,
    _SCRIPT_FAR_SIZE_RATIO,
    _SCRIPT_SIZE_RATIO,
    _SUBSCRIPT_DROP,
    _SUPERSCRIPT_RISE,
)
from opendockit.extractors._pdf_model import _MAX_GROUPED_TEXT_PIECES, _TABLE_ROW_TOLERANCE, _is_word_char, _Line, _word_chars


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
# A subscript split off as a line of its own (`_lowered_script_piece`): at most this many characters, lowered by less than this
# share of the line's character size. pdfminer splits it off from about 0.42 em; the next line is a full line height (1.2 em or
# more) below.
_LOWERED_SCRIPT_CHARS = 4
_LOWERED_SCRIPT_MAX_DROP = 0.75
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


# A justified line whose word gaps pdfminer split into pieces (`_justified_joints`): gaps between pieces wider than a space
# (`_PIECE_SPACE_GAP_RATIO`) up to this multiple of the character size are looked at (a line of two words in a 15 em column
# reaches 5 em), and all the word gaps of the row, inside the pieces and between them, may differ by this share of the size.
# A gap inside a piece counts from this share of the size (letters touch, a natural space is 0.25 em or more).
_JUSTIFIED_GAP_MAX_RATIO = 8.0
_JUSTIFIED_GAP_SPREAD_RATIO = 0.1
_JUSTIFIED_WORD_GAP_RATIO = 0.15
# The line of the same paragraph above or below a justified row (`_JustifiedLines.spans`): its baseline this many character
# sizes away at most, its left edge within `_TABLE_ROW_TOLERANCE` and its right edge within this share of the size of the
# row's (within `_JUSTIFIED_GAP_SPREAD_RATIO` for a row with a single gap).
_JUSTIFIED_PITCH_MAX_RATIO = 2.5
_JUSTIFIED_EDGE_RATIO = 1.0
# Passes over the split rows (`_justified_joints`): a row joined in one pass is the line of the same paragraph for the row next to
# it in the next, so a run of split rows is joined this many rows away from an unsplit line at most.
_JUSTIFIED_PASSES = 8
# Lines one page may look at in all for the justified rows (`_JustifiedLines`); the rows after that stay apart.
_JUSTIFIED_WORK = 1_000_000


_LIGATURE_MAX = 4  # characters one glyph of right-to-left letters may stand for (`_is_plain_glyph`)


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
    following.update(_justified_joints(pieces, following))
    following.update(_right_to_left_joints(pieces, following))
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
    test_pdf_reading_order.py::TestSymbolsDrawnAfterTheirLine). Words drawn last to first with no space glyph between them came
    out in drawing order the same way: a short word and the word drawn after it, to its left, were one piece ('comes awith
    bright', Pinned by: test_words_drawn_from_right_to_left_without_spaces_are_read_in_place). The whole line is re-laid in x
    order when no character placed to the left of characters drawn before it (the characters to be moved back) is whitespace,
    and the characters do not overlap when laid in x order (`_glyphs_collide` — copies drawn on top of each other, fake bold, a
    mark drawn over its letter). Word characters are moved back only on a line whose characters all advance to the right
    (`_advances_leftwards`): text turned upside down (a page rotated by 180°) runs right to left on the page, and laying it in x
    order reversed it (Pinned by:
    test_pdf.py::TestDrawingArtifacts::test_rotated_page_with_an_off_centre_crop_box_keeps_its_visible_text). Deliberate: a line
    containing right-to-left characters (Hebrew, Arabic) is not touched — for text drawn in logical order a decreasing x is
    correct, and laying it in x order would reverse it (Pinned by: test_right_to_left_text_drawn_in_logical_order_is_not_reversed).
    """
    chars = [obj for obj in piece if isinstance(obj, LTChar)]
    if len(chars) < 2 or _RIGHT_TO_LEFT.search("".join(char.get_text() for char in chars)):
        return None

    moved, reach = [], -math.inf

    for char in chars:
        if char.x1 <= reach + char.size * _PIECE_JOIN_GAP_RATIO:
            moved.append(char)
        reach = max(reach, char.x0)

    if not moved or any(not char.get_text().strip() for char in moved):
        return None
    if any(_word_chars(char.get_text()) for char in moved) and any(_advances_leftwards(char) for char in chars):
        return None  # text turned upside down (a page rotated by 180°) runs right to left on the page and is read in drawing order
    ordered = sorted(chars, key=lambda char: char.x0)
    if _glyphs_collide(ordered):
        return None

    line = LTTextLineHorizontal(word_margin)
    for char in ordered:
        line.add(char)
    return line


def _advances_leftwards(char: LTChar) -> bool:
    """Whether the text of the character does not run to the right on the page: the next character of its string is drawn to
    its left (or straight above or below it).

    The pen moves by `adv` (glyph width × font size × horizontal scaling) along the x axis of the text matrix, whose page x
    component is `matrix[0]`, so the sign of their product is the direction on the page. A matrix turned by 180° or mirrored
    (a < 0) with an ordinary size runs leftwards. So does an upright matrix with a negative font size ('/F1 -11 Tf', the same
    180° turn written without a matrix) or negative horizontal scaling ('-100 Tz', mirrored): the matrix alone looked like
    ordinary text there, and words separated by kerning instead of space glyphs were laid in x order, last word first
    ('Total amount due today' → 'yadot eud tnuoma latoT'; Pinned by:
    test_pdf_reading_order.py::TestSymbolsDrawnAfterTheirLine::test_text_running_leftwards_without_a_turned_matrix_is_not_reversed).
    Both at once (negative size in a 180° matrix) run rightwards again. Text turned by 90° (a = 0) does not run along x at all
    and is left in drawing order whatever its advance (Pinned by:
    test_a_piece_holding_a_glyph_turned_by_a_quarter_is_left_in_drawing_order). A glyph of no width moves nothing and tells no direction, so it does
    not speak for its line: the list bullet of reportlab (code 7F of Helvetica, which pdfminer gives no width) or a zero-width
    mark on an ordinary line whose letters were drawn last to first kept the whole line in drawing order
    ('• Total amount due today' → 'yadoteudtnuomalatoT•'; Pinned by:
    test_pdf_reading_order.py::TestSymbolsDrawnAfterTheirLine::test_a_glyph_without_advance_does_not_keep_its_line_in_drawing_order)."""
    return char.matrix[0] == 0 or char.matrix[0] * char.adv < 0


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
    # A piece or edge character with a coordinate that is not a finite number joins nothing — rounding it raised ValueError and
    # the whole document was reported corrupted (Pinned by: test_audit_pdf_text.py::TestCharactersAtNonFinitePositions)
    edges = [edge if edge is not None and _finite_piece(piece, edge) else None
             for piece, edge in ((piece, _edge_chars(piece)) for piece in pieces)]
    starts: dict[tuple[int, int], list[int]] = {}  # (start x, height band) — same-column pieces of other rows are skipped (thousands of table cells)
    spaces: dict[int, list[LTTextLineHorizontal]] = {}
    for index, piece in enumerate(pieces):
        if edges[index] is not None:
            for x in {round(piece.x0), round(edges[index][0].x0)}:
                starts.setdefault((x, math.floor(piece.y0 / _PIECE_BAND)), []).append(index)
        elif _space_piece(piece) and all(math.isfinite(value) for value in piece.bbox):
            spaces.setdefault(round(piece.x0), []).append(piece)

    # height band → the start x values in that band (sorted). Scanning integer x over a space width never finished at character size 1e9
    columns: dict[int, list[int]] = {}
    for x, band in starts:
        columns.setdefault(band, []).append(x)
    for xs in columns.values():
        xs.sort()

    def after(index: int) -> Iterator[tuple[int, float]]:
        """(piece starting where piece `index` ends, where it ended) for every candidate, nearest band first."""
        piece, last = pieces[index], edges[index][1]
        reach = max(last.size, 1.0) * _PIECE_SPACE_GAP_RATIO
        band = math.floor(piece.y0 / _PIECE_BAND)
        for end in _piece_ends(piece, last, spaces):
            for x in _starts_between(columns, (band - 1, band, band + 1), round(end) - 1, round(end + reach) + 1):
                for near in (band - 1, band, band + 1):
                    for other in starts.get((x, near), ()):
                        yield other, end

    following: dict[int, int] = {}
    taken: set[int] = set()
    for index, piece in enumerate(pieces):
        if edges[index] is None or index in following:
            continue
        last = edges[index][1]
        # A subscript lowered so far that pdfminer made it a line of its own, set exactly into the gap of this line
        # (`_lowered_script`): this piece, the subscript and the piece after it are joined in one go. It is looked for first:
        # the piece after the gap may start close enough to join this piece over the subscript's head ('Index x' and ' runs',
        # leaving the 'i' out). Only a gap that exactly one piece fits is taken, so the pieces after it are looked through once
        scripts = {script for script, end in after(index)
                   if edges[script] is not None and script not in taken and script not in following
                   and not _leads_to(following, script, index) and _lowered_script(last, pieces[script], end)}
        if len(scripts) == 1:
            script = scripts.pop()
            rest = next((rest for rest, script_end in after(script)
                         if rest not in (index, script) and rest not in taken and not _leads_to(following, rest, index)
                         and _closes_the_gap(last, script_end, pieces[rest], edges[rest][0])), None)
            if rest is not None and not any(other not in (index, script, rest)
                                            for other in _starting_within(starts, columns, pieces[script])):
                following[index], following[script] = script, rest
                taken.update((script, rest))
                continue
        other = next((other for other, end in after(index)
                      if other not in taken and not _leads_to(following, other, index)
                      and _continues_piece(piece, last, pieces[other], edges[other][0], end)), None)
        if other is not None:
            following[index] = other
            taken.add(other)
    return following


def _right_to_left_joints(pieces: list[LTTextLineHorizontal], following: dict[int, int]) -> dict[int, int]:
    """Piece number → the piece standing right after it on its row, for the rows that hold Hebrew or Arabic letters and
    the joins `following` does not have yet.

    Word and LibreOffice draw a line of Arabic run by run, starting from the right: each run is to the left of the one
    drawn before it, and pdfminer, which joins only what is drawn side by side, makes a piece of each ('البند', 'ان 15 و',
    '116', 'من جدول الأعمال' for one line). The text of such a line is its letters in the order of the page, read back
    (`_pdf_bidi._logical`), so the whole row has to be one line first: read piece by piece, the words of the line came
    out as paragraphs in the order of the page. `_continues_piece` is too narrow for it — a bracket or a full stop drawn
    alone holds no letter, and a Latin word in the line is set in another font with another bottom edge. Here two
    pieces are joined when one starts where the other ends, a space apart at most (`_PIECE_SPACE_GAP_RATIO`), they
    overlap for most of the height of the smaller (`_PIECE_OVERLAP_RATIO`) and are of like height
    (`_PIECE_HEIGHT_RATIO`), and the chain they make holds a right-to-left letter. Rows without one are not looked at
    (Pinned by: test_pdf_right_to_left.py::TestLinesDrawnRunByRun)."""
    leftwards = [bool(_RIGHT_TO_LEFT.search(piece.get_text())) for piece in pieces]
    if not any(leftwards):
        return {}
    bands: dict[int, list[int]] = {}
    for index, piece in enumerate(pieces):
        if all(math.isfinite(value) for value in piece.bbox) and piece.height > 0 and piece.get_text().strip():
            bands.setdefault(math.floor(piece.y0 / _PIECE_BAND), []).append(index)
    near = {band + step for index, piece in enumerate(pieces) if leftwards[index] and math.isfinite(piece.y0)
            for band in (math.floor(piece.y0 / _PIECE_BAND),) for step in (-1, 0, 1)}
    continued = set(following.values())
    links: dict[int, int] = {}
    for band in sorted(near & bands.keys()):
        for index in bands[band]:
            if index in following or index in links:
                continue
            piece = pieces[index]
            reach = piece.height * _PIECE_SPACE_GAP_RATIO
            best = None
            for other_band in (band - 1, band, band + 1):
                for other in bands.get(other_band, ()):
                    after = pieces[other]
                    if other == index or other in continued or not -piece.height * _PIECE_JOIN_GAP_RATIO <= after.x0 - piece.x1 <= reach:
                        continue
                    shared = min(piece.y1, after.y1) - max(piece.y0, after.y0)
                    low, high = sorted((piece.height, after.height))
                    if shared >= low * _PIECE_OVERLAP_RATIO and low >= high * _PIECE_HEIGHT_RATIO and (
                            best is None or after.x0 < pieces[best].x0):
                        best = other
            if best is not None and not _leads_to({**following, **links}, best, index):
                links[index] = best
                continued.add(best)
    # Only the chains that hold a right-to-left letter are kept: the pieces of a row of Latin text are joined by
    # `_continues_piece` or not at all
    merged = {**following, **links}
    heads = set(merged) - set(merged.values())
    kept: dict[int, int] = {}
    for head in heads:
        chain = [head]
        while chain[-1] in merged and len(chain) <= len(pieces):
            chain.append(merged[chain[-1]])
        if any(leftwards[index] for index in chain):
            kept.update((index, merged[index]) for index in chain[:-1] if index in links)
    return kept


def _justified_joints(pieces: list[LTTextLineHorizontal], following: dict[int, int]) -> dict[int, int]:
    """Piece number → number of the next piece of the same justified line, for the lines whose word gaps pdfminer split
    apart. Pieces already joined (`following`) are left out.

    Justification widens every word gap of a line by the same amount; in a narrow column with long words a gap reaches
    1.1 em and more, and pdfminer (char_margin 2.0 of the characters beside the gap) makes each word a line of its own and
    a text box of its own, so the words of that row came out shuffled and scattered through the paragraph (Pinned by:
    test_pdf.py::TestLinesDrawnInPieces::test_words_of_a_widely_justified_line_are_read_as_one_line). Pieces that far
    apart are otherwise not joined: they cannot be told from the next column or the next cell of a table (Pinned by:
    test_pieces_apart_on_the_same_row_are_left_as_they_are). A row is joined only when all of the following hold
    (Deliberate, narrow):
    - its pieces share a baseline and a character size, and the gaps between them are wider than a space and at most
      `_JUSTIFIED_GAP_MAX_RATIO` times the size;
    - all its word gaps, between the pieces and inside them, are the same (within `_JUSTIFIED_GAP_SPREAD_RATIO` of the
      size) — a gutter or a cell gap differs from the word gaps of the text beside it;
    - a line of the same size right above or below it starts at its left edge and ends at its right edge
      (`_JUSTIFIED_EDGE_RATIO` of the size; with a single gap, which cannot be checked against others, within
      `_JUSTIFIED_GAP_SPREAD_RATIO`): one paragraph line spans all its gaps, which no row of a table or of side-by-side
      columns has. A row joined this way serves as that line for the row next to it (two split rows in a row).
    """
    joined = set(following) | set(following.values())
    edges: dict[int, tuple[LTChar, LTChar]] = {}
    for index, piece in enumerate(pieces):
        edge = _edge_chars(piece)
        if edge is not None and _finite_piece(piece, edge):
            edges[index] = edge
    lines = _JustifiedLines()
    for index, (start, end) in edges.items():
        lines.add(start, end)

    rows = sorted((edges[index][0].y0, edges[index][0].x0, index) for index in edges if index not in joined)
    chains: list[list[int]] = []
    start = 0
    while start < len(rows):  # rows of pieces sharing a baseline, each read left to right
        size = max(edges[rows[start][2]][0].size, 1.0)
        end = start + 1
        while end < len(rows) and rows[end][0] - rows[start][0] <= size * _PIECE_BASELINE_RATIO:
            end += 1
        chain: list[int] = []
        for _, _, index in sorted(rows[start:end], key=lambda row: row[1]):
            if chain:
                previous = chain[-1]
                last, first = edges[previous][1], edges[index][0]
                gap, size = first.x0 - last.x1, max(first.size, 1.0)
                if (abs(last.size - first.size) <= 0.1 and pieces[index].x0 > pieces[previous].x1
                        and size * _PIECE_SPACE_GAP_RATIO < gap <= size * _JUSTIFIED_GAP_MAX_RATIO):
                    chain.append(index)
                    continue
                chains.append(chain)
            chain = [index]
        chains.append(chain)
        start = end

    pending = [(chain, single) for chain in chains if (single := _evenly_gapped(pieces, edges, chain)) is not None]
    found: dict[int, int] = {}
    for _ in range(_JUSTIFIED_PASSES):  # a row joined serves the split row next to it in the next pass
        rest = []
        for chain, single in pending:
            first, last = edges[chain[0]][0], edges[chain[-1]][1]
            if lines.spans(first, last, single):
                found.update(zip(chain, chain[1:], strict=False))
                lines.add(first, last)
            else:
                rest.append((chain, single))
        if len(rest) == len(pending):
            break
        pending = rest
    return found


def _evenly_gapped(pieces: list[LTTextLineHorizontal], edges: dict[int, tuple[LTChar, LTChar]], chain: list[int]) -> bool | None:
    """For a row of pieces (`chain`, left to right, each a wide gap from the one before) whose word gaps, between the pieces
    and inside them, are all the same (`_justified_joints`): whether it has a single gap. None for any other row."""
    if len(chain) < 2:
        return None
    size = max(edges[chain[0]][0].size, 1.0)
    gaps = [edges[right][0].x0 - edges[left][1].x1 for left, right in zip(chain, chain[1:], strict=False)]
    for index in chain:
        chars = sorted((obj for obj in pieces[index] if isinstance(obj, LTChar) and obj.get_text().strip()),
                       key=lambda char: char.x0)
        gaps += [gap for left, right in zip(chars, chars[1:], strict=False)
                 if (gap := right.x0 - left.x1) > size * _JUSTIFIED_WORD_GAP_RATIO]
    if max(gaps) - min(gaps) > size * _JUSTIFIED_GAP_SPREAD_RATIO:
        return None
    return len(gaps) == 1


class _JustifiedLines:
    """The lines a justified row may be checked against (`_justified_joints`), by the rounded left edge of their first
    character: (bottom of the first character, left edge, right edge of the last character, size), sorted by bottom.
    Lines looked at past `_JUSTIFIED_WORK` on one page answer no: a page of copies drawn on top of one another holds
    thousands of lines at one spot."""

    def __init__(self) -> None:
        self._by_start: dict[int, list[tuple[float, float, float, float]]] = {}
        self._work = 0

    def add(self, first: LTChar, last: LTChar) -> None:
        insort(self._by_start.setdefault(round(first.x0), []), (first.y0, first.x0, last.x1, first.size))

    def spans(self, first: LTChar, last: LTChar, single: bool) -> bool:
        """Whether a line of the size of `first` right above or below the row from `first` to `last` (bottoms
        `_JUSTIFIED_PITCH_MAX_RATIO` sizes apart at most) starts at the row's left edge and ends at its right edge."""
        size = max(first.size, 1.0)
        reach = size * _JUSTIFIED_PITCH_MAX_RATIO
        edge = size * (_JUSTIFIED_GAP_SPREAD_RATIO if single else _JUSTIFIED_EDGE_RATIO)
        tolerance = math.ceil(_TABLE_ROW_TOLERANCE)
        for x in range(round(first.x0) - tolerance, round(first.x0) + tolerance + 1):
            lines = self._by_start.get(x, ())
            low = bisect_left(lines, (first.y0 - reach, -math.inf, -math.inf, -math.inf))
            high = bisect_right(lines, (first.y0 + reach, math.inf, math.inf, math.inf))
            self._work += high - low
            if self._work > _JUSTIFIED_WORK:
                return False
            if any(abs(bottom - first.y0) >= size * 0.5 and abs(other_size - first.size) <= 0.1
                   and abs(x0 - first.x0) <= _TABLE_ROW_TOLERANCE and abs(x1 - last.x1) <= edge
                   for bottom, x0, x1, other_size in lines[low:high]):
                return True
        return False


def _starting_within(starts: dict[tuple[int, int], list[int]], columns: dict[int, list[int]],
                     piece: LTTextLineHorizontal) -> list[int]:
    """The pieces with a word character that start within the x range of `piece`, in its height band and the two on either
    side of it."""
    band = math.floor(piece.y0 / _PIECE_BAND)
    bands = tuple(range(band - 2, band + 3))
    return sorted({other for x in _starts_between(columns, bands, math.floor(piece.x0) - 1, math.ceil(piece.x1))
                   for near in bands for other in starts.get((x, near), ())})


def _lowered_script(last: LTChar, script: LTTextLineHorizontal, end: float) -> bool:
    """Whether `script` is a subscript of the line whose piece ends at `end` with the character `last`, which pdfminer split
    off as a line of its own. `_closes_the_gap` tells whether the piece after it continues the same line.

    pdfminer puts two characters on one line only when their boxes overlap by half the smaller height (`line_overlap`), so a
    subscript lowered by about 0.42 em or more (reportlab's `<sub>`: 9 pt under 11 pt, 0.46 em down) became a piece of its
    own and the sentence was split in three around it, the subscript pushed out of order ('Water is H', '2', 'O in
    chemistry.'). The rule is kept narrow (deliberate), so that only a piece set exactly into the gap of one line is taken:
    - `script` is short (1–4 word characters, `_LOWERED_SCRIPT_CHARS`), every character a subscript of `last` as
      `_positional_scripts` tells one (at most 0.8 the size and lowered by `_SUBSCRIPT_DROP`, or at most 0.85 the size and
      lowered by `_SCRIPT_FAR_SHIFT`), and lowered by less than `_LOWERED_SCRIPT_MAX_DROP` of the size;
    - it starts where the piece before ends, and the piece after starts where it ends (`_closes_the_gap`), both as
      `_continues_piece` measures touching pieces;
    - the characters on both sides of the gap have the same size and baseline and are plain characters: one line;
    - it is the only piece that fits the gap, and no other piece starts within its x range two bands above or below it (a
      superscript stacked over it): with the subscript joined and the superscript left out, a formula's pieces would be
      split up differently.
    A raised piece is not taken here: footnote marks are `_footnote_mark_piece`, and bare raised numbers are deliberately not
    joined (Pinned by: test_pdf_reading_order.py::TestSubscriptsOnALineOfTheirOwn)."""
    chars = [obj for obj in script if isinstance(obj, LTChar) and obj.get_text().strip()]
    if not 1 <= len(chars) <= _LOWERED_SCRIPT_CHARS or not all(_is_word_char(char.get_text()) for char in chars):
        return False
    size = max(last.size, 1.0)
    return abs(script.x0 - end) <= size * _PIECE_JOIN_GAP_RATIO and all(
        last.y0 - char.y0 < size * _LOWERED_SCRIPT_MAX_DROP
        and (char.size <= last.size * _SCRIPT_SIZE_RATIO + 0.01 and last.y0 - char.y0 >= size * _SUBSCRIPT_DROP
             or char.size <= last.size * _SCRIPT_FAR_SIZE_RATIO and last.y0 - char.y0 >= size * _SCRIPT_FAR_SHIFT)
        for char in chars)


def _closes_the_gap(last: LTChar, script_end: float, rest: LTTextLineHorizontal, first: LTChar) -> bool:
    """Whether `rest` (leftmost character `first`) continues, right where a lowered subscript ends (`script_end`), the line
    whose character before the subscript is `last` (`_lowered_script`): it touches the subscript, or starts with a space within
    one space width of it, and `first` has the size and baseline of `last`."""
    size = max(last.size, 1.0)
    tolerance = size * _PIECE_JOIN_GAP_RATIO
    gap = rest.x0 - script_end
    if not (abs(gap) <= tolerance or (tolerance < gap <= size * _PIECE_SPACE_GAP_RATIO and first.x0 - rest.x0 > tolerance)):
        return False  # a gap wider than touching is allowed only for a space at the start of `rest`
    same_baseline = (abs(last.y0 - first.y0) <= size * _PIECE_BASELINE_RATIO
                     or (_upright(last) and _upright(first) and abs(last.matrix[5] - first.matrix[5]) <= size * _PIECE_BASELINE_RATIO))
    return abs(last.size - first.size) <= 0.1 and same_baseline and _is_plain_glyph(last.get_text()) and _is_plain_glyph(first.get_text())


def _finite_piece(piece: LTTextLineHorizontal, edge: tuple[LTChar, LTChar]) -> bool:
    """Whether the piece box and its two edge characters have finite coordinates (and a finite size)."""
    return all(math.isfinite(value) for value in (*piece.bbox, *edge[0].bbox, *edge[1].bbox, edge[0].size, edge[1].size))


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
    if not any(_is_word_char(char.get_text()) for char in chars) and not (len(chars) == 1 and _PIECE_SYMBOL.match(chars[0].get_text())):
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
    not. A ligature of Arabic or Hebrew letters is one glyph of several characters ('لا', 'في') and plain as its letters
    are."""
    if len(text) == 1:
        return not "\ue000" <= text <= "\uf8ff"
    return 1 < len(text) <= _LIGATURE_MAX and all(_RIGHT_TO_LEFT.match(letter) for letter in text)


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
        before.setdefault(_xobject_position(placed, group, lines), []).extend(group)
    return [line for index in range(len(placed) + 1) for line in [*before.get(index, []), *placed[index:index + 1]]]


def _xobject_position(placed: list[_Line], group: list[_Line], lines: list[_Line]) -> int:
    """Position at which to insert the lines of an XObject (`group`, its first line first; `lines` are all the page's lines,
    drawn on the page or in XObjects) before which page-text line: right after the text to its left on the same line →
    before the first line below it in the same column → after the last line above it in the same column → before the first line
    below it regardless of column → the end.

    The text to the left is meant for a label beside its value. When the line after it continues it beside the block with
    nothing else to its right (a label has its value there), the block sits beside a paragraph and goes after the whole
    paragraph, as drawn directly on the page:
    a paragraph of three lines in an XObject was wedged into the paragraph beside it after its first line
    (Pinned by: test_quality.py::test_pdf_form_xobject_beside_a_paragraph_does_not_split_it)."""
    first = group[0]

    def same_column(line: _Line) -> bool:
        return line.x0 < first.x1 and first.x0 < line.x1

    middle = (first.top + first.bottom) / 2
    left = [index for index, line in enumerate(placed)
            if line.top <= middle <= line.bottom and line.x1 <= first.x0 + 1]
    if left:
        index = max(left, key=lambda index: placed[index].x1)
        if index + 1 < len(placed) and _continues_beside(placed[index], placed[index + 1], group, lines):
            index += 1
            while index + 1 < len(placed) and _next_line_of(placed[index], placed[index + 1]):
                index += 1
        return index + 1
    below = next((index for index, line in enumerate(placed) if line.top >= first.top and same_column(line)), None)
    if below is not None:
        return below
    above = [index for index, line in enumerate(placed) if line.top < first.top and same_column(line)]
    if above:
        return above[-1] + 1
    return next((index for index, line in enumerate(placed) if line.top >= first.top), len(placed))


def _next_line_of(line: _Line, after: _Line) -> bool:
    """Whether `after` is the next line of the paragraph of `line`: in the same column and below it, at most a line height apart."""
    return after.x0 < line.x1 and line.x0 < after.x1 and 0 <= after.top - line.bottom <= line.height


def _continues_beside(line: _Line, after: _Line, group: list[_Line], lines: list[_Line]) -> bool:
    """Whether `after`, the page-text line after `line` (the text to the left of the XObject block `group`), continues the
    paragraph of `line` beside the block: its middle lies beside a later line of the block, and no other line of the page
    (`lines`, outside the block) lies to its right on its row (then it is a label with its value)."""
    middle = (after.top + after.bottom) / 2
    first = group[0]
    if not (_next_line_of(line, after) and after.x1 <= first.x0 + 1
            and any(block_line.top <= middle <= block_line.bottom for block_line in group[1:])):
        return False
    block = {id(block_line) for block_line in group}
    return not any(other is not after and id(other) not in block and other.top <= middle <= other.bottom
                   and other.x0 >= after.x1 - 1 for other in lines)


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
