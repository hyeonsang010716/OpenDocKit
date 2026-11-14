"""Markdown rendering and normalization tools shared by the extractors.

Table rendering and text normalization are done only here, in one place, so that every format produces output of the same shape.
"""

from collections.abc import Iterable
import re
import unicodedata

from opendockit.models import IMAGE_MARKER


# Zero-width characters, direction controls, BOM, soft hyphen. Invisible on screen, but they ruin tokens and search matching.
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁤﻿­]")
# Remove the C0 control characters and DEL, keeping only tab (\t) and newline (\n). Form feed (\f) and vertical tab (\v) are
# text boundaries, so they are turned into line breaks instead of being removed (`_LINE_BREAK_CONTROLS`) — removed, the numbers on
# either side in `합계 100\f200` (합계 = total) ran together and the CSV cell `2\f3` became `23` (Pinned by: test_markdown.py::
# TestCleanText::test_page_break_and_vertical_tab_become_line_breaks, TestRenderTable::test_page_break_and_vertical_tab_inside_a_cell_become_br).
_CONTROL = re.compile(r"[\x00-\x08\x0e-\x1f\x7f]")
_LINE_BREAK_CONTROLS = str.maketrans({"\x0b": "\n", "\x0c": "\n"})
# The private use areas (BMP and supplementary planes 15 and 16). Symbols of fonts specific to Hangul (HWP) and Office are mapped
# here and become meaningless characters in any other environment. Hancom fonts use the supplementary areas too (U+F02B1 etc.)
# (Pinned by: test_markdown.py::TestCleanText::test_removes_supplementary_private_use_characters).
_PRIVATE_USE = re.compile("[\ue000-\uf8ff\U000f0000-\U000ffffd\U00100000-\U0010fffd]")
# However, the bullets Word and LibreOffice export to PDF are private-use characters of the Symbol and Wingdings fonts (U+F000 +
# font code). Removing them loses the list bullets, so only the common ones are turned into Unicode symbols
# (Pinned by: test_quality.py::test_symbol_font_bullets_become_unicode_bullets).
# The check boxes ✗ ✓ ☒ ☑ (Wingdings FB–FE) are included too — with ☒ removed and only □ left, the selected item read as the
# opposite. Codes whose meaning differs by font (Wingdings ☒ 78 and □ 6F are ξ and ο in Symbol) are not included. DOCX and PPTX,
# which know the font, convert them first with `extractors._symbols.SYMBOL_GLYPHS`
# (Pinned by: test_markdown.py::TestCleanText::test_check_box_glyphs_of_symbol_fonts_survive_without_a_font).
_SYMBOL_FONT_GLYPHS = str.maketrans({
    "\uf0b7": "•", "\uf06c": "●", "\uf0a1": "○", "\uf0a7": "▪", "\uf06e": "■", "\uf071": "❑", "\uf075": "◆",
    "\uf076": "❖", "\uf0a8": "□", "\uf0d8": "➢", "\uf0e0": "→", "\uf0e8": "➔", "\uf0f0": "⇨", "\uf0fb": "✗",
    "\uf0fc": "✓", "\uf0fd": "☒", "\uf0fe": "☑", "\uf02d": "-", "\uf0a0": " ",
    # Latin ligatures. A typeset PDF stores fi and fl as one character, which made 'ﬁrst' a different word from 'first'. NFKC
    # would also change ①, ㈜ and ², so only the ligatures are expanded
    # (Pinned by: test_markdown.py::TestCleanText::test_expands_latin_ligatures_but_keeps_other_compatibility_characters).
    "\ufb00": "ff", "\ufb01": "fi", "\ufb02": "fl", "\ufb03": "ffi", "\ufb04": "ffl", "\ufb05": "st", "\ufb06": "st",
})
_SPACE_LIKE = re.compile("[  -   　]")
_INNER_SPACES = re.compile(r"[ \t]+")
_BLANK_RUN = re.compile(r"\n{3,}")
# Only nested list lines made by the extractors keep their indentation. Any other leading whitespace reads as a code block in
# Markdown, so it is removed. An empty item (a bare `-` or `N.`) keeps its indentation only when the next line is a list line
# indented deeper — the empty item that only holds a nested list, whose nesting was lost without it
# (Pinned by: test_audit_web_fixes.py::TestEmptyListItemHoldingANestedList). Any other paragraph that is only `-` or `2025.`
# is stripped as before: with its spaces kept, five of them made it an indented code block.
_LIST_LINE = re.compile(r"^( +)(?:[-*]|\d+\.) ")
_BARE_LIST_MARKER = re.compile(r"^( +)(?:[-*]|\d+\.)[ \t]*$")
_LIST_ITEM = re.compile(r"^ *(?:[-*]|\d+\.) ")
# The span counts and cell addresses of a table are numbers written in the file, so they are not trusted as is. A few KB of input
# used seconds and gigabytes (DOCX gridSpan of 3 million, HWPX cell address 3000×3000, 150 HTML cells each merged 200×200).
# Pinned by: test_markdown.py::TestTableGridLimits.
# Upper bound on the cells one cell's span fills. Over it, the vertical span (the value repeated on each row) is kept first and
# the horizontal one is reduced.
_MAX_SPAN_AREA = 10_000
# Total cells that spans may fill in one table. Once used up, later spans are ignored and the value goes only in the first cell.
_SPAN_BUDGET = 500_000
# Upper bound on the grid cells rows() builds. Over it, the bottom rows are dropped and their number is reported through
# `TableGrid.omitted_rows` (the extractor warns).
_MAX_GRID_CELLS = 5_000_000
# The structural tags of an HTML table. Those inside cell text are neutralized by neutralize_table_tags.
# The placeholder of a table inside a table (`TableGrid.embed`). Private-use characters, so clean_text removes any left unresolved.
_EMBED = re.compile("\ue020(\\d+)\ue021")
_TABLE_TAG = re.compile(r"</?(?:table|thead|tbody|tfoot|tr|td|th|br)\b[^>]*>", re.IGNORECASE)


def clean_text(text: str, *, collapse_spaces: bool = True) -> str:
    """Unicode normalization (NFC) and whitespace cleanup.

    NFC is required: files made on macOS and HWP files sometimes store Hangul in decomposed jamo (NFD), and left as is,
    the same characters become a different string for search and embedding.
    `collapse_spaces=False` is for TXT and MD, where the original whitespace carries meaning (code indentation is preserved).
    """
    # Remove first, then normalize. In the other order, the characters on either side of a removed one become newly combinable
    # and the result is no longer NFC.
    text = text.replace("\r\n", "\n").replace("\r", "\n").translate(_LINE_BREAK_CONTROLS)
    text = _INVISIBLE.sub("", text)
    text = _CONTROL.sub("", text)
    text = _PRIVATE_USE.sub("", text.translate(_SYMBOL_FONT_GLYPHS))
    text = _SPACE_LIKE.sub(" ", text)
    text = unicodedata.normalize("NFC", text)

    # An unpaired last fence line (```) is neutralized as `\````. When a paragraph of a non-Markdown document (DOCX etc.) had a
    # single piece of text starting with ```, the whole section after it became a code block and whitespace cleanup stopped too
    # (Pinned by: test_markdown.py::TestUnclosedCodeFence). TXT and MD (collapse_spaces=False) are left as the author wrote them.
    source = text.split("\n")
    fences = [index for index, line in enumerate(source) if line.lstrip().startswith("```")]
    unpaired = fences[-1] if collapse_spaces and len(fences) % 2 else None
    # Blank lines inside paired fences are not reduced — consecutive blank lines in a code block (two lines between functions)
    # were reduced to one and the shape of the code changed (Pinned by: test_markdown.py::TestBlankLinesInsideCodeFences).
    # What follows an unpaired last fence is not a code block, so it is reduced.
    last_opening = fences[-1] if len(fences) % 2 else None
    lines = []
    in_fence = False
    for index, line in enumerate(source):
        if index == unpaired:
            line = "\\" + line.lstrip()
        elif line.lstrip().startswith("```"):
            in_fence = not in_fence and index != last_opening
            lines.append(line.rstrip())
            continue
        if in_fence or not collapse_spaces:
            line = line.rstrip()
        else:
            list_match = _LIST_LINE.match(line) or _empty_item_holding_a_list(source, index)
            line = (list_match.group(1) if list_match else "") + _INNER_SPACES.sub(" ", line.strip())
        if line or in_fence or (lines and lines[-1]):
            lines.append(line)

    return "\n".join(lines).strip("\n")


def _empty_item_holding_a_list(source: list[str], index: int) -> re.Match[str] | None:
    """The match of an indented bare list marker (`  -`) at `source[index]` when the next line is a list line (or another bare
    marker) indented deeper, otherwise None."""
    bare = _BARE_LIST_MARKER.match(source[index])
    if bare is None or index + 1 >= len(source):
        return None
    following = _LIST_LINE.match(source[index + 1]) or _BARE_LIST_MARKER.match(source[index + 1])
    return bare if following is not None and len(following.group(1)) > len(bare.group(1)) else None


def solid_length(text: str) -> int:
    """The number of characters certain to remain after cleaning (`clean_text`) and rendering. Used to decide the character limit midway.

    Counted in NFC without whitespace, image placeholders, and invisible, control and private-use characters. Hangul in
    decomposed jamo shrinks to 1/3 when normalized, and a placeholder disappears entirely if its picture is filtered out, so
    counting the raw length comes out larger than the real one.
    """
    # A nested-table placeholder goes too, with its number: the table it stands for is counted when it is drawn, and text of
    # the document that only has the shape of one is dropped by the table that holds it. Counted, 80 digits between the two
    # private-use characters stopped a table that was within the limit, and the document came back cut without `exceeded`
    # (Pinned by: test_markdown.py::TestSolidLength::test_nested_table_placeholder_is_not_counted).
    text = _EMBED.sub("", IMAGE_MARKER.sub("", text))
    text = _PRIVATE_USE.sub("", _CONTROL.sub("", _INVISIBLE.sub("", text)).translate(_SYMBOL_FONT_GLYPHS))
    return len("".join(unicodedata.normalize("NFC", text).split()))


def join_blocks(blocks: Iterable[str]) -> str:
    """Join paragraph and table blocks with one blank line. Empty blocks are dropped; consecutive list items are joined with one line break."""
    parts: list[str] = []
    previous_is_item = False
    for block in blocks:
        if not block or not block.strip():
            continue
        is_item = bool(_LIST_ITEM.match(block))
        if parts:
            parts.append("\n" if previous_is_item and is_item else "\n\n")
        parts.append(block.rstrip())
        previous_is_item = is_item
    return "".join(parts).strip()


def _cell_lines(value: str) -> list[str]:
    value = unicodedata.normalize("NFC", _SPACE_LIKE.sub(" ", _CONTROL.sub("", _INVISIBLE.sub("", value.translate(_LINE_BREAK_CONTROLS)))))
    lines = [_INNER_SPACES.sub(" ", line).strip() for line in value.replace("\r", "\n").split("\n")]
    return [line for line in lines if line]


def _clean_cell(value: str) -> str:
    return "<br>".join(_cell_lines(value)).replace("|", "\\|")


def neutralize_table_tags(text: str) -> str:
    """Neutralize only the table tags (`<td>`, `</tr>`, `<br>`, etc.) in HTML table cell text, with `&lt;`. Deliberate: the result
    is fed to an LLM, not drawn on screen, so the rest of the text (`x<5`, `A & B`, `|`) stays as in the original — escaping
    everything is noise. Table tags left as they are would be confused with cell and row boundaries
    (Pinned by: test_markdown.py::TestMergedTableAsHtml::test_only_table_tags_in_cell_text_are_neutralized).
    Only the text up to its last `>` is searched — a tag ends with one, so nothing after it can match. Searched whole, every tag
    name with no `>` after it was scanned to the end of the text (80,000 `<td` in one cell took 8 seconds)
    (Pinned by: test_audit_core.py::TestUnclosedTableTagsInCellText)."""
    end = text.rfind(">") + 1
    return _TABLE_TAG.sub(lambda match: "&lt;" + match.group()[1:], text[:end]) + text[end:]


def _html_cell(value: str) -> str:
    return "<br>".join(neutralize_table_tags(line) for line in _cell_lines(value))


def render_table(rows: list[list[str | None]], *, merged_titles_only: bool = False, headless: bool = False) -> str:
    """Turn a two-dimensional list of cells into a GFM Markdown table.

    - A line break inside a cell becomes `<br>` (a Markdown table row has to be one line).
    - Rows and columns that are entirely empty are removed. Hangul (HWP) and Word form documents have many empty cells used for spacing.
    - A table with only one column is a layout box, so it is unfolded into paragraphs instead of a table.
    - The first row is used as the header row. In most document tables the first row is the header, and when it is not, the LLM
      still reads it without trouble. However, a table title row at the very top (`_title_of`) is moved into a paragraph above the table.

    With `merged_titles_only`, a top row with only one filled cell is a title only when all the other cells (empty columns aside)
    are cells covered by a horizontal span (None). Used when a PDF ruled table, which passes merged cells as None, continues
    from the previous page — a fragment of a row carried over to the next page was one cell among empty cells ("") and was
    taken out as a title and became a paragraph (Pinned by: test_markdown.py::TestTitleRowOnlyFromMergedCells). Other formats
    pass empty cells and merged cells without distinguishing them, so they use the default (the old rule).

    With `headless`, the table has no header row: every row goes under an empty header row as data, and no title row is taken
    out either. Used only for HTML name–value tables that have a name cell on every row (`th, td, th, td`) — the values of the
    first row were read as column names (Pinned by: test_core_html.py::TestNameValueTablesWithRowHeaders).
    """
    kept_rows = [row for row in rows if any(_clean_cell(cell or "") for cell in row)]
    cleaned = [[_clean_cell(cell or "") for cell in row] for row in kept_rows]
    if not cleaned:
        return ""

    width = max(len(row) for row in cleaned)
    padded = [row + [""] * (width - len(row)) for row in cleaned]
    keep = [col for col in range(width) if any(row[col] for row in padded)]
    grid = [[row[col] for col in keep] for row in padded]

    if len(keep) == 1:
        return "\n\n".join(_uncell(row[0]) for row in grid)

    # A title row at the top of a table (a horizontally merged title, or title + unit) used as the header row would lose the column
    # names, so it is moved into a paragraph above the table.
    titles = []
    while len(keep) >= 2 and len(grid) > 1 and not headless:
        title = _title_of([cell for cell in grid[0] if cell], len(keep), grid[0], grid[1:2 + _HEADER_PROBE_ROWS])
        if title is None or (merged_titles_only and sum(1 for cell in grid[0] if cell) == 1
                             and _has_open_cell(kept_rows[len(titles)], keep)):
            break
        titles.append(_uncell(title))
        grid = grid[1:]
    if titles:
        keep = [col for col in range(len(grid[0])) if any(row[col] for row in grid)]
        grid = [[row[col] for col in keep] for row in grid]
        if len(keep) == 1:
            return "\n\n".join([*titles, *(_uncell(row[0]) for row in grid)])

    if headless:
        grid = [["" for _ in keep], *grid]
    lines = [
        "| " + " | ".join(grid[0]) + " |",
        "| " + " | ".join("---" for _ in keep) + " |",
    ]
    lines.extend("| " + " | ".join(row) + " |" for row in grid[1:])
    return "\n\n".join([*titles, "\n".join(lines)])


def _has_open_cell(row: list[str | None], columns: list[int]) -> bool:
    """Whether, among the `columns` of the table row (`row`), there is an empty cell not covered by a span (not None). A column the
    row is too short to have counts as a covered cell."""
    return any(col < len(row) and row[col] is not None and not _clean_cell(row[col]) for col in columns)


# A unit or as-of note in the second cell of a title row (`(단위: 천원)` (unit: thousand won), `[기준: 2024.12.31]` (as of),
# `기준일: …` (as-of date)).
_UNIT_NOTE = re.compile(r"[(\[<]\s*(?:단위|기준)|(?:단위|기준(?:일자?|일시|시점)?)\s*[:：]")


# Cell text counted as a number when deciding the title row of a two-column table (sign, currency, thousands separators, decimals,
# percent, negatives in parentheses), and how many rows below a header candidate to look in for numbers.
_NUMBER_CELL = re.compile(r"[-+−▲▼△(]?\s*[₩$€¥£]?\s*\d[\d,]*(?:\.\d+)?\s*%?\)?")
_HEADER_PROBE_ROWS = 5


def _title_of(cells: list[str], width: int, row: list[str] | None = None,
              below: list[list[str]] | None = None) -> str | None:
    """If the filled cells of the top row (`cells`) are a table title, the text to put in a paragraph above the table; otherwise None.
    A title is a row with only one filled cell (a horizontally merged title), or, in a table of four or more columns, a row with
    only two filled cells whose second cell is a unit or as-of note ('2024년 매출 현황 | … | (단위: 천원)', i.e. "2024 sales |
    … | (unit: thousand won)"). With the latter used as the header row, the real header row dropped down into the data rows
    (Pinned by: test_markdown.py::TestRenderTable::test_lifts_title_row_with_a_unit_note_above_table,
    TestMergedTableAsHtml::test_title_row_with_a_unit_note_is_a_paragraph_above_an_html_table). Deliberate: a two-cell row
    counts only when its second cell is a unit or as-of note — a group header ('상반기 | | 하반기 |', first half / second half)
    and the header row of a narrow table stay the header row
    (Pinned by: test_markdown.py::TestRenderTable::test_two_cell_top_row_that_is_not_a_title_stays_the_header).

    In a two-column table (`width` 2) even a one-cell row can be a header ('구분 | ', category) or data, so the rule is narrower:
    it is a title only when just the left cell is filled (`row`), the row right below has non-numeric text in both cells, and
    there is a number cell within the `_HEADER_PROBE_ROWS` rows below that (`below`, the same idea as the sheet's
    `_looks_like_header`). '부서별 인원 현황 | ' (headcount by department) became the header and '부서 | 인원' (department |
    headcount) dropped down into the data (XLSX merged title, CSV, HTML colspan=2). Known accepted gap: the title row of a
    two-column table with no number cells stays the header row
    (Pinned by: test_markdown.py::TestTwoColumnTitleRow)."""
    if width == 2:
        return cells[0] if len(cells) == 1 and row and row[0] and _heads_numbers(below or []) else None
    if len(cells) == 1:
        return cells[0]
    if len(cells) == 2 and width >= 4 and _UNIT_NOTE.match(cells[1]):
        return f"{cells[0]} {cells[1]}"
    return None


def table_title(row: list[str], below: list[list[str]]) -> str | None:
    """If the top row of a table (`row`, the cells without the empty columns) is a table title, the text to put in a paragraph above
    the table; otherwise None. `below` is the rows under it (same columns). The rule `render_table` and `TableGrid` use to take
    out a title row (`_title_of`) is shared with the extractor that merges multi-row headers itself (HTML thead)."""
    cells = [_clean_cell(cell or "") for cell in row]
    if len(cells) < 2 or not any(cells):
        return None
    title = _title_of([cell for cell in cells if cell], len(cells), cells,
                      [[_clean_cell(cell or "") for cell in other] for other in below])
    return _uncell(title) if title is not None else None


def _heads_numbers(below: list[list[str]]) -> bool:
    """Whether `below[0]` has non-numeric text in both cells and the rows under it have a number cell (the title-row decision for a
    two-column table)."""
    if not below or not all(cell and not _NUMBER_CELL.fullmatch(cell) for cell in below[0]):
        return False
    return any(_NUMBER_CELL.fullmatch(cell) for row in below[1:_HEADER_PROBE_ROWS + 1] for cell in row)


def _uncell(cell: str) -> str:
    return cell.replace("<br>", "\n").replace("\\|", "|")


def merged_copy(text: str) -> str:
    """The text to repeat in the cells below for a vertical span. Picture placeholders and nested-table placeholders stay only in
    the top cell — repeated, the picture description or table went in once per row
    (Pinned by: test_markdown.py::TestTableGrid::test_vertical_merge_repeats_text_but_not_image_placeholder,
    TestMergedTableAsHtml::test_nested_table_in_a_vertically_merged_cell_appears_once)."""
    return _EMBED.sub("", IMAGE_MARKER.sub("", text)).strip()


class TableGrid:
    """Unfold a table with merged cells (rowspan, colspan) into a row × column grid.

    Deliberate: in a Markdown table, a vertically merged value is repeated on each row below, and a horizontally merged one
    goes only in the first cell. Repeated, each row carries by itself which category its values belong to (a vertical span is
    usually a category column). However, picture placeholders are left out of the repeated cells. The picture description went
    in once per row, inflating the character count and delaying the limit decision during description
    (Pinned by: test_markdown.py::TestTableGrid::test_vertical_merge_repeats_text_but_not_image_placeholder).
    A horizontal span cannot be drawn in Markdown, so a table that still has horizontally spanning cells after the empty rows
    and columns are removed is output by `render` as an HTML table (Pinned by: test_markdown.py::TestMergedTableAsHtml). A
    table containing a table (`embed`) is HTML as well.
    """

    def __init__(self) -> None:
        self._cells: dict[tuple[int, int], str] = {}
        # The placed cells (row, column, row span, column span, text) — in placement order. A cell placed on a spot an earlier cell
        # occupies is not added (the cell placed first wins, as in Markdown).
        self._origins: list[tuple[int, int, int, int, str]] = []
        # Spot → ordinal of the placed cell that filled it (-1 for a spot filled by the overflow of a cell that lost its start spot).
        # An HTML span is built only from the spots a cell filled itself.
        self._owners: dict[tuple[int, int], int] = {}
        self._embedded: list[TableGrid] = []  # the tables inside cells (in the order of embed's placeholder numbers)
        # The nested tables already drawn in the drawing under way. A table inside a table is drawn once: document text can hold
        # the private-use characters a placeholder is made of, and when every forged copy drew the nested table again, copies at
        # each level of nesting multiplied — a 736-byte ODT took 26 s and 1.4 GB
        # (Pinned by: test_markdown.py::TestMergedTableAsHtml::test_text_shaped_like_a_nested_table_placeholder_draws_nothing).
        self._drawn: set[int] = set()
        self._span_budget = _SPAN_BUDGET
        # The real extent of the table: the lowest row and rightmost column where a cell was placed (`place`) or a covered cell
        # was reported (`reach`). Spans are cut to this extent when drawing — extended by the span counts written in the file,
        # rowSpan=5000 in a 2-row table became 4,999 fake rows and gridSpan=10000 in a top cell became 10,000 empty columns
        # (Pinned by: test_markdown.py::TestTableGridLimits::test_row_span_past_the_last_row_does_not_add_rows,
        # test_column_span_past_the_last_column_does_not_cut_rows).
        self._last_row = self._last_col = -1
        self._omitted = 0  # number of bottom rows dropped by the grid cell limit (`omitted_rows`)
        # Row → the column before which every column is filled (where `place_next` starts looking for an empty spot). Cells are
        # never removed, so the earlier columns stay filled.
        self._next_col: dict[int, int] = {}

    def place(self, row: int, col: int, text: str, *, row_span: int = 1, col_span: int = 1,
              fill_horizontal: bool = False) -> None:
        """`fill_horizontal` is for merging header rows — it fills the value into the horizontally merged cells too, so it can be
        joined with the header below. A filled cell is a value, so the table extent reaches the spanned columns (`_last_col`)."""
        row_span, col_span = max(row_span, 1), max(col_span, 1)
        self.reach(row, col + col_span - 1 if fill_horizontal else col)
        if row_span * col_span > _MAX_SPAN_AREA:
            row_span = min(row_span, _MAX_SPAN_AREA)
            col_span = max(_MAX_SPAN_AREA // row_span, 1)
        if row_span * col_span > 1:
            if row_span * col_span > self._span_budget:
                row_span = col_span = 1
            else:
                self._span_budget -= row_span * col_span

        owner = -1
        if (row, col) not in self._cells:
            owner = len(self._origins)
            self._origins.append((row, col, row_span, col_span, text))
        copy = merged_copy(text) if row_span * col_span > 1 else text
        for dr in range(row_span):
            for dc in range(col_span):
                if (row + dr, col + dc) not in self._cells:
                    self._cells[row + dr, col + dc] = text if dr == dc == 0 else (copy if dc == 0 or fill_horizontal else "")
                    self._owners[row + dr, col + dc] = owner

    def place_next(self, row: int, text: str, *, row_span: int = 1, col_span: int = 1,
                   fill_horizontal: bool = False) -> None:
        """Place a cell that comes in order without an address (HTML), skipping the merged cells coming down from above.

        The empty spot is looked for from the column last placed on that row. Looking from column 0 was quadratic in the number
        of cells of a row (56 seconds for one row of 50,000 cells, Pinned by:
        test_markdown.py::TestTableGridLimits::test_cells_placed_in_order_on_a_long_row_take_linear_time).
        """
        col = self._next_col.get(row, 0)
        while (row, col) in self._cells:
            col += 1
        self._next_col[row] = col
        self.place(row, col, text, row_span=row_span, col_span=col_span, fill_horizontal=fill_horizontal)

    def place_or_next(self, row: int, col: int, text: str, *, row_span: int = 1, col_span: int = 1) -> None:
        """A cell with a written address (HWP, HWPX). If an earlier cell already occupies that spot (two cells with the same address,
        an earlier cell's span larger than the table), `place` would drop the text, so a cell with text is put in the next empty
        spot of that row like a cell without an address (`place_next`). In corrupted files cell text vanished without a warning
        (Pinned by: test_hwp_robustness.py::TestCellsOnAnOccupiedAddress)."""
        if (row, col) in self._cells and text.strip():
            self.place_next(row, text, row_span=row_span, col_span=col_span)
        else:
            self.place(row, col, text, row_span=row_span, col_span=col_span)

    def reach(self, row: int, col: int) -> None:
        """Report, without text, that the table extends to this spot — a cell covered by a span (a DOCX/PPTX vMerge cell, etc.). A
        table whose last row is entirely covered by vertical spans from the cells above does not lose that row either
        (Pinned by: test_markdown.py::TestTableGridLimits::test_covered_cell_reported_by_the_extractor_keeps_its_row)."""
        self._last_row, self._last_col = max(self._last_row, row), max(self._last_col, col)

    def omitted_rows(self) -> int:
        """Number of bottom rows dropped by the grid cell limit (`_MAX_GRID_CELLS`) in the last drawing (nested tables included).
        The extractor reports it as a warning."""
        return self._omitted + sum(table.omitted_rows() for table in self._embedded)

    def rows(self) -> list[list[str]]:
        """Build the grid from only the rows and columns where cells were placed. Rows and columns with no cell are removed by
        render_table anyway, so they are never built in the first place, keeping one far-away cell address from inflating an
        empty grid."""
        row_keys, col_keys = self._axes()
        return [[self._cells.get((row, col), "") for col in col_keys] for row in row_keys]

    def embed(self, table: "TableGrid") -> str:
        """Hand a table inside a cell over to this table and return the placeholder to put in the cell text (empty text for an empty
        table). When this table is drawn, the nested table is drawn in place — as a one-line HTML table inside the HTML cell if
        this table has several columns, or as a separate table between paragraphs if it is a one-column box. It used to be
        flattened into 'cell / cell' lines and the row and column structure was lost (Pinned by: test_markdown.py::
        TestMergedTableAsHtml::test_nested_table_keeps_its_structure_inside_an_html_cell,
        test_table_inside_a_one_column_box_becomes_its_own_block)."""
        if not table._embedded and not any(_cell_lines(text) for text in table._cells.values()):
            return ""
        self._embedded.append(table)
        return f"\ue020{len(self._embedded) - 1}\ue021"

    def render(self, *, headless: bool = False) -> str:
        """`headless` is a table with no header row (the argument of the same name in `render_table`; in HTML every cell is a td)."""
        rows = self.rows()
        if not self._embedded:
            return self._html(rows, headless=headless) or render_table(rows, headless=headless)
        self._drawn = set()

        # A table inside a table cannot go in a Markdown cell, so it is HTML. Only a one-column box is unfolded into paragraphs as
        # before, with the nested table placed separately.
        html = self._html(rows, force=True, headless=headless)
        if html is not None:
            return html
        self._drawn = set()
        text = _EMBED.sub(lambda match: f"\n\n{self._nested(match, inline=False)}\n\n", render_table(rows, headless=headless))
        return _BLANK_RUN.sub("\n\n", text).strip()

    def flatten(self) -> str:
        """'cell / cell' lines for places that cannot hold a table (inside a footnote or header). Nested tables are flattened the same way."""
        text = "\n".join(" / ".join(cell for cell in row if cell) for row in self.rows())
        self._drawn = set()
        return _EMBED.sub(lambda match: self._embedded[index].flatten() if (index := self._first_use(match)) is not None else "", text)

    def _first_use(self, match: re.Match[str]) -> int | None:
        """The number of the nested table a placeholder stands for, the first time it is met in a drawing; None for a number
        that does not exist (the document text happened to contain the same shape) and for every later copy."""
        digits = match.group(1)
        index = int(digits) if len(digits) <= 9 else -1
        if not 0 <= index < len(self._embedded) or index in self._drawn:
            return None
        self._drawn.add(index)
        return index

    def _nested(self, match: re.Match[str], *, inline: bool) -> str:
        index = self._first_use(match)
        if index is None:
            return ""
        table = self._embedded[index]
        if not inline:
            return table.render()
        table._drawn = set()
        return table._html(table.rows(), force=True, inline=True) or ""

    def _cell_html(self, text: str) -> str:
        html = _html_cell(text)
        return _EMBED.sub(lambda match: self._nested(match, inline=True), html) if self._embedded else html

    def _axes(self) -> tuple[list[int], list[int]]:
        if not self._cells:
            return [], []
        rows = sorted({row for row, _ in self._cells if row <= self._last_row})
        cols = sorted({col for _, col in self._cells if col <= self._last_col})
        kept = max(_MAX_GRID_CELLS // len(cols), 1)
        self._omitted = max(len(rows) - kept, 0)
        return rows[:kept], cols

    def _html(self, rows: list[list[str]], *, force: bool = False, inline: bool = False, headless: bool = False) -> str | None:
        """The HTML (rowspan, colspan) of a table that still has horizontally spanning cells. None if no spanning cell remains — it
        is drawn as Markdown.

        `force` makes it HTML even without a spanning cell (when there is a table inside the table). A one-column table is still
        None — it is treated as a box and unfolded into paragraphs.
        `inline` is one-line HTML to put inside an outer cell: no title row is taken out, and a one-column table joins its cell
        texts with `<br>`.

        As in `render_table`, empty rows and columns are removed (the span counts are reduced accordingly), a title row at the
        top (`_title_of`) goes into a paragraph above the table and the columns are picked again, and the first row is used as
        the header row (th). A title row spanning all columns alone does not make it HTML — the output is as before. Text
        visible in the Markdown grid is not lost in HTML either (a spot not covered by a start cell keeps the grid's text).
        """
        row_keys, col_keys = self._axes()
        filled = [[bool(_cell_lines(cell)) for cell in row] for row in rows]
        kept_rows = [index for index, row in enumerate(filled) if any(row)]
        if not kept_rows:
            return None
        kept_cols = _filled_columns(filled, kept_rows)
        if inline and len(kept_cols) == 1:
            return "<br>".join(self._cell_html(rows[row][kept_cols[0]]) for row in kept_rows)
        if force and len(kept_cols) == 1:
            return None
        starts, taken = self._starts(row_keys, col_keys, kept_rows, kept_cols)

        # A row where a vertical span starts is not a title. Collected in advance so the start cells are not rescanned for every
        # row (that was quadratic in the number of rows).
        spanning = {row for (row, _), (_, row_span, _) in starts.items() if row_span > 1}
        titles: list[str] = []
        while (not inline and not headless and len(kept_cols) >= 2 and len(titles) < len(kept_rows) - 1 and len(titles) not in spanning
               and not any(_EMBED.search(rows[kept_rows[len(titles)]][col]) for col in kept_cols)):
            row = kept_rows[len(titles)]
            title = _title_of([_clean_cell(rows[row][col]) for col in kept_cols if filled[row][col]], len(kept_cols),
                              [_clean_cell(rows[row][col]) for col in kept_cols] if len(kept_cols) == 2 else [],
                              [[_clean_cell(rows[below][col]) for col in kept_cols]
                               for below in kept_rows[len(titles) + 1:len(titles) + 2 + _HEADER_PROBE_ROWS]]
                              if len(kept_cols) == 2 else [])
            if title is None:
                break
            titles.append(_uncell(title))
        if titles:
            kept_rows = kept_rows[len(titles):]
            kept_cols = _filled_columns(filled, kept_rows)
            starts, taken = self._starts(row_keys, col_keys, kept_rows, kept_cols)
        if not force and not any(col_span > 1 for _, _, col_span in starts.values()):
            return None

        lines = ["<table>"]
        for row in range(len(kept_rows)):
            tag = "th" if row == 0 and not headless else "td"
            cells = []
            for col in range(len(kept_cols)):
                if (row, col) in starts:
                    text, row_span, col_span = starts[row, col]
                    spans = (f' rowspan="{row_span}"' if row_span > 1 else "") + (f' colspan="{col_span}"' if col_span > 1 else "")
                    cells.append(f"<{tag}{spans}>{self._cell_html(text)}</{tag}>")
                elif (row, col) not in taken:
                    cells.append(f"<{tag}>{self._cell_html(rows[kept_rows[row]][kept_cols[col]])}</{tag}>")
            lines.append("<tr>" + "".join(cells) + "</tr>")
        lines.append("</table>")
        return "\n\n".join([*titles, ("" if inline else "\n").join(lines)])

    def _starts(self, row_keys: list[int], col_keys: list[int], kept_rows: list[int],
                kept_cols: list[int]) -> tuple[dict[tuple[int, int], tuple[str, int, int]], set[tuple[int, int]]]:
        """HTML start cells on the kept rows and columns → (text, row span, column span), and the spots the start cells occupy. The
        span rectangle is used as is only when it consists of spots that cell really filled in the grid; if a spot another cell
        filled first is in it, as in a broken file, it is reduced to one cell — the same 'the cell placed first wins' rule as
        the Markdown grid, so text visible in the grid is not covered in HTML and the cell count matches on every row."""
        row_at = {row_keys[index]: position for position, index in enumerate(kept_rows)}
        col_at = {col_keys[index]: position for position, index in enumerate(kept_cols)}
        starts: dict[tuple[int, int], tuple[str, int, int]] = {}
        taken: set[tuple[int, int]] = set()
        for owner, (row, col, row_span, col_span, text) in enumerate(self._origins):
            if row not in row_at or col not in col_at:
                continue  # the start spot is in a removed empty row or column
            spanned_rows = [key for key in range(row, row + row_span) if key in row_at]
            spanned_cols = [key for key in range(col, col + col_span) if key in col_at]
            if any(self._owners.get((r, c)) != owner for r in spanned_rows for c in spanned_cols):
                spanned_rows, spanned_cols = spanned_rows[:1], spanned_cols[:1]
            area = {(row_at[r], col_at[c]) for r in spanned_rows for c in spanned_cols}
            taken |= area
            starts[row_at[row], col_at[col]] = (text, len(spanned_rows), len(spanned_cols))
        return starts, taken


def _filled_columns(filled: list[list[bool]], kept_rows: list[int]) -> list[int]:
    return [col for col in range(len(filled[0])) if any(filled[row][col] for row in kept_rows)]
