"""What repeats on every page: headers, footers, page numbers and side captions, found by comparing the edge rows of the
pages, and the body font size and line gap of the document.
"""

from collections import Counter
import re

from opendockit.extractors._pdf_aligned_tables import _aligned_table_runs
from opendockit.extractors._pdf_label_values import _label_value_rows
from opendockit.extractors._pdf_model import (
    _CAPTION_GAP_RATIO,
    _DIGITS,
    _HEADING_MIN_RATIO,
    _REPEAT_RATIO,
    _SIDE_REPEAT_RATIO,
    _TABLE_ROW_TOLERANCE,
    _in_margin,
    _Line,
    _Page,
    _visual_rows,
)


# Piece that looks like a page number ("3", "- 3 -", "3 / 10", "Page 3", "3쪽"). For a margin line with several pieces at the same
# height, only the digits of this piece are ignored; the rest must match character for character to count as a repeat.
# Each optional part carries the white space after it, so no two runs of optional white space are neighbours: with
# `\s*(?:/…)?\s*(?:쪽|페이지)?\s*`, a digit followed by a long run of white space and then anything else was tried in every way
# of sharing the white space out (cubic). The texts matched are the same (Pinned by: test_audit_pdf_tail.py::TestPageNumberShape).
_PAGE_NUMBER = re.compile(r"^[-–(\[]?\s*(?:(?:page|p\.)\s*)?\d{1,4}\s*(?:/\s*\d{1,4}\s*)?(?:(?:쪽|페이지)\s*)?[-–)\]]?$", re.IGNORECASE)
# Token that can be a page number (_page_number_blind): without its digits only punctuation, a page word or a single Latin letter
# remains ('3', '3/10', '3쪽', 'p.3', '2024-01-05', 'A-1'). A value with a unit ('12건') is not one.
_PAGE_WORDS = frozenset({"page", "p", "pp", "of", "쪽", "페이지", "면", "頁"})
# Token that is a value, not a page number: a thousands comma ('3,450,000'), a percent or currency sign, a decimal joining two
# digit groups with one dot ('6.8'). A date written with dots ('2024.01.05') has three digit groups and is not a decimal.
_VALUE_TOKEN = re.compile(r"\d,\d{3}(?!\d)|[%‰₩$€£¥]|^\D*\d+\.\d+\D*$")
_NON_WORD = re.compile(r"[\W_]+")
_TOKEN = re.compile(r"\S+")


# When a row shaped like the margin row also appears this many times at the same position in the body of the same page, it is a
# column's values, not a header or footer.
_DATA_REPEATS = 2


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
