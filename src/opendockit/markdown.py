"""Markdown rendering and normalization tools shared by the extractors.

Table rendering and text normalization are done only here, in one place, so that every format produces output of the same shape.
"""

from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from itertools import chain
import re
import unicodedata

from opendockit.models import IMAGE_MARKER


# Zero-width characters, direction controls (embeddings, overrides, isolates, the Arabic letter mark U+061C), the Mongolian
# vowel separator U+180E, BOM, soft hyphen. Invisible on screen, but they ruin tokens and search matching. The isolates
# (U+2066–2069) are what current editors write around a right-to-left name; only the older embeddings were removed and the
# isolates stayed between the letters
# (Pinned by: test_markdown.py::TestInvisibleAndControlCharacters::test_removes_bidi_isolates_and_other_invisible_marks).
_INVISIBLE = re.compile("[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\u061c\u180e\ufeff\u00ad]")
# Remove the C0 and C1 control characters and DEL, keeping only tab (\t) and newline (\n). Form feed (\f), vertical tab (\v) and
# next line (U+0085, the C1 line break) are text boundaries, so they are turned into line breaks instead of being removed
# (`_LINE_BREAK_CONTROLS`) — removed, the numbers on either side in `합계 100\f200` (합계 = total) ran together and the CSV cell
# `2\f3` became `23` (Pinned by: test_markdown.py::TestCleanText::test_page_break_and_vertical_tab_become_line_breaks,
# TestRenderTable::test_page_break_and_vertical_tab_inside_a_cell_become_br). C1 means the code points U+0080–009F after
# decoding: Windows-1252 bytes 0x80–0x9F (€, curly quotes) decode to other characters and are not touched; a C1 code point is
# left only by a latin-1 decode or a numeric reference, and is never text (Pinned by:
# test_markdown.py::TestInvisibleAndControlCharacters::test_removes_c1_controls_and_breaks_lines_at_next_line).
_CONTROL = re.compile(r"[\x00-\x08\x0e-\x1f\x7f-\x84\x86-\x9f]")
# The Unicode line and paragraph separators (U+2028, U+2029) are line breaks too: the macOS text system writes U+2028 for a line
# break (TextEdit, Pages, Mail RTF), and kept, it stayed inside a table cell, split `str.splitlines` in the middle of a table row,
# and let a quoted fence escape the first cleaning, so cleaning twice gave another text
# (Pinned by: test_markdown.py::TestCleanText::test_unicode_line_and_paragraph_separators_are_line_breaks). In a cell and a
# one-line value they are made line breaks with the others (`_LINE_BREAK_CONTROLS`); in a text they are left to `clean_text`,
# which breaks the line there only outside a code block, with the quote marks of the line (`_SEPARATORS`).
_LINE_BREAK_CONTROLS = str.maketrans({"\x0b": "\n", "\x0c": "\n", "\x85": "\n", "\u2028": "\n", "\u2029": "\n"})
_TEXT_LINE_BREAKS = str.maketrans({"\x0b": "\n", "\x0c": "\n", "\x85": "\n"})
_SEPARATORS = re.compile("[\u2028\u2029]")
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
_SPACE_LIKE = re.compile("[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]")  # the Unicode spaces, the Ogham space mark too
# The characters cleaning can leave a backtick for: the backtick, and the Greek varia U+1FEF, which NFC turns into one.
_BACKTICK_SOURCES = ("`", "\u1fef")
_INNER_SPACES = re.compile(r"[ \t]+")
_BLANK_RUN = re.compile(r"\n{3,}")
# Only nested list lines made by the extractors keep their indentation. Any other leading whitespace reads as a code block in
# Markdown, so it is removed. An empty item (a bare `-` or `N.`) keeps its indentation only when it is nested in another empty
# item and holds a deeper list (`_empty_item_holding_a_list`) — its nesting was lost without it
# (Pinned by: test_audit_web_fixes.py::TestEmptyListItemHoldingANestedList). Any other paragraph that is only `-` or `2025.`
# is stripped as before: with its spaces kept, five of them made it an indented code block. A marker followed only by
# whitespace (`  - `) is a bare marker too: it was taken for a list line, kept its indentation, lost the trailing space, and
# the next cleaning then stripped it as a bare marker (Pinned by: test_markdown.py::TestCleaningTwice). A list marker is ASCII
# digits: with `\d`, '１.' and '١.' lines kept their indentation (four spaces made a code block) and were joined to the item
# above them (Pinned by: test_markdown.py::TestCleanText::test_number_in_other_digits_is_not_a_list_line).
_LIST_LINE = re.compile(r"^( +)(?:[-*]|[0-9]+\.) (?=[ \t]*\S)")
_BARE_LIST_MARKER = re.compile(r"^( +)(?:[-*]|[0-9]+\.)[ \t]*$")
_EMPTY_ITEM = re.compile(r"^( *)([-*]|[0-9]+\.)$")
_LIST_ITEM = re.compile(r"^ *(?:[-*]|[0-9]+\.) ")
# The span counts and cell addresses of a table are numbers written in the file, so they are not trusted as is. A few KB of input
# used seconds and gigabytes (DOCX gridSpan of 3 million, HWPX cell address 3000×3000, 150 HTML cells each merged 200×200).
# Pinned by: test_markdown.py::TestTableGridLimits.
# Upper bound on the cells one cell's span fills. Over it, the vertical span (the value repeated on each row) is kept first and
# the horizontal one is reduced.
_MAX_SPAN_AREA = 10_000
# Total cells that spans may fill in one table, and in one conversion (`span_budget`). Once used up, later spans are ignored and
# the value goes only in the first cell. Per table only, many small tables of empty cells each spanning 100×100 filled 500,000
# cells apiece and wrote nothing: a 5.6 KB HWPX took 13 s with no output and no warning
# (Pinned by: test_markdown.py::TestTableGridLimits::test_spans_of_one_conversion_share_one_budget,
# test_hwp_robustness.py::TestSpansPastTheTable::test_many_tables_of_huge_spans_share_one_budget). A span is charged the area it
# claims, which it fills when placed, not the one the table's real extent clips it to at render time (Known accepted gap: a page
# of many `colspan="100"` section rows uses the budget up sooner. Pinned by: test_markdown.py::TestTableGridLimits::
# test_a_span_is_charged_the_area_the_file_claims_past_the_table).
_SPAN_BUDGET = 500_000
_CONVERSION_SPAN_BUDGET = 1_000_000
# What is left of the conversion's span budget and grid budget (`_CONVERSION_GRID_BUDGET`), shared by every table of the
# conversion ([spans, grid]; None outside `span_budget`).
_spans_left: ContextVar[list[int] | None] = ContextVar("spans_left", default=None)
# Upper bound on the grid cells rows() builds. Over it, the bottom rows are dropped and their number is reported through
# `TableGrid.omitted_rows` (the extractor warns).
_MAX_GRID_CELLS = 5_000_000
# Empty grid cells the tables of one conversion may build beyond as many as hold text (`span_budget`). The grid is every row
# that holds a cell by every column that holds one, so a few cells far apart make a dense grid of empty cells: in a 1.9 KB
# HWPX, one cell spanning 10,000 rows in one column and one spanning 1,000 columns in another row (each within the span
# limits) made 10,000 × 1,001 cells, of which 11,000 were filled, cut to 5 million and walked for 6 s per table; a 200 KB
# HTML table of 10,000 one-cell rows and one row of 1,000 cells did the same with no span. A table drawn (`render`,
# `flatten`) is built only from the rows and columns that hold text, which empties out the spans, and the empty cells left
# past as many as are filled are charged here. Once it is used up, the bottom rows of a table are dropped and reported like
# those over `_MAX_GRID_CELLS` (Pinned by: test_markdown.py::TestTableGridLimits::test_far_apart_spans_do_not_build_a_dense_grid,
# test_empty_cells_of_one_conversion_share_one_budget, test_hwp_robustness.py::TestSpansPastTheTable::
# test_far_apart_spans_of_many_tables_take_little_time). A table holds at least as many empty cells as filled ones for free,
# so a real table, whose cells are mostly filled, does not use it.
_CONVERSION_GRID_BUDGET = 1_000_000
# The structural tags of an HTML table. Those inside cell text are neutralized by neutralize_table_tags.
# The placeholder of a table inside a table (`TableGrid.embed`). Private-use characters, so clean_text removes any left unresolved.
_EMBED = re.compile("\ue020(\\d+)\ue021")
_TABLE_TAG = re.compile(r"</?(?:table|thead|tbody|tfoot|tr|td|th|br)\b[^>]*>", re.IGNORECASE)
# A line of quote marks and nothing else (once placeholders and removed characters are gone), left out of `solid_length`.
_BARE_QUOTE_LINE = re.compile(r"^[^\S\n]*>(?:[^\S\n]*>)*[^\S\n]*$", re.MULTILINE)


@contextmanager
def span_budget() -> Iterator[None]:
    """Give the tables drawn inside this block (one conversion, `converter.convert`) one budget of cells their spans may fill
    together (`_CONVERSION_SPAN_BUDGET`), on top of each table's own, and one of the empty grid cells they may build
    (`_CONVERSION_GRID_BUDGET`)."""
    token = _spans_left.set([_CONVERSION_SPAN_BUDGET, _CONVERSION_GRID_BUDGET])
    try:
        yield
    finally:
        _spans_left.reset(token)


def spans_left() -> tuple[int, ...] | None:
    """What is left of the conversion's span and grid budgets (None outside `span_budget`), to put back with `restore_spans`
    when the same content is drawn again from the start (the growing front parts of a large HTML page, `html._extract_front`)."""
    left = _spans_left.get()
    return None if left is None else tuple(left)


def restore_spans(value: tuple[int, ...] | None) -> None:
    """Put the conversion's span and grid budgets back to `value` (from `spans_left`)."""
    left = _spans_left.get()
    if left is not None and value is not None:
        left[:] = value


def clean_text(text: str, *, collapse_spaces: bool = True, line_separators: bool = True) -> str:
    """Unicode normalization (NFC) and whitespace cleanup.

    NFC is required: files made on macOS and HWP files sometimes store Hangul in decomposed jamo (NFD), and left as is,
    the same characters become a different string for search and embedding.
    `collapse_spaces=False` is for TXT and MD, where the original whitespace carries meaning (code indentation is preserved).
    `line_separators=False` leaves U+2028 and U+2029 as they are (code and data files, where they are characters of a string);
    otherwise they are line breaks outside a code block (`_break_at_separators`).
    """
    text = clean_characters(text)
    if line_separators and _SEPARATORS.search(text):
        text = "\n".join(_break_at_separators(text.split("\n"), collapse_spaces))

    # In documents that are not Markdown the only real code blocks are the ones the extractors write, with backtick fences. A
    # backtick line that opens a fence which is never closed is neutralized with a backslash: when a paragraph of a DOCX had a
    # single piece of text starting with ```, the whole section after it became a code block and whitespace cleanup stopped too
    # (Pinned by: test_markdown.py::TestUnclosedCodeFence). A line of tildes is text there and never opens or closes a code
    # block; so that a Markdown reader agrees, every line that would open a tilde fence is neutralized too, paired or not
    # (Pinned by: test_markdown.py::TestFencesThatAreText). TXT and MD (collapse_spaces=False) are the author's Markdown: both
    # kinds of fence are paired as CommonMark pairs them and nothing is neutralized.
    source = text.split("\n")
    # What each line is to the code blocks of the text (`code_fences`). Most texts hold no fence at all, and then nothing is
    # looked up per line: ordinary prose costs what it cost before fences were paired
    # (Pinned by: test_markdown.py::TestFencesThatAreText::test_text_without_a_fence_is_not_searched_for_fences).
    kinds: bytearray | None = None
    if "```" in text or (not collapse_spaces and "~~~" in text):
        fences = code_fences(source, tildes=not collapse_spaces)
        if fences.blocks or (collapse_spaces and fences.unpaired):
            kinds = bytearray(len(source))
            for opening, closing in fences.blocks:
                kinds[opening + 1:closing] = bytes([_INSIDE]) * (closing - opening - 1)
                kinds[opening] = kinds[closing] = _FENCE
            if collapse_spaces:
                for index in fences.unpaired:
                    kinds[index] = _UNPAIRED
    tildes = collapse_spaces and "~~~" in text
    quoted = collapse_spaces and ">" in text

    lines: list[str] = []
    for index, line in enumerate(source):
        kind = kinds[index] if kinds is not None else 0
        if kind == _FENCE:
            lines.append(line.rstrip())
            continue
        # Blank lines inside paired fences are not reduced — consecutive blank lines in a code block (two lines between functions)
        # were reduced to one and the shape of the code changed (Pinned by: test_markdown.py::TestBlankLinesInsideCodeFences).
        # What follows an unpaired fence is not a code block, so it is reduced.
        in_fence = kind == _INSIDE
        if in_fence or not collapse_spaces:
            line = line.rstrip()
        else:
            content = line.strip()
            if quoted and content.startswith(">"):
                line = _clean_quoted_line(line, kind == _UNPAIRED, tildes)
            else:
                if kind == _UNPAIRED:
                    content = "\\" + content
                elif tildes and "~~~" in content:
                    content = "\\" + content if content.startswith("~~~") else _TILDE_OPENING.sub(r"\1\\", content, count=1)
                list_match = _LIST_LINE.match(line) or _empty_item_holding_a_list(source, index, lines[-1] if lines else "")
                line = (list_match.group(1) if list_match else "") + _INNER_SPACES.sub(" ", content)
        if line or in_fence or (lines and lines[-1]):
            lines.append(line)

    return "\n".join(lines).strip("\n")


def clean_characters(text: str) -> str:
    """The part of `clean_text` that works on characters, before any line is looked at: line breaks made `\\n` (form feed,
    vertical tab and next line too), invisible, control and private-use characters removed, the glyphs of symbol fonts and
    the ligatures replaced, space-like characters made spaces, NFC. Cleaning the result again changes nothing. The line and
    paragraph separators are left to `clean_text`, so the lines are those of the text for every caller that counts them
    (`text._fenced_spans`)."""
    # Remove first, then normalize. In the other order, the characters on either side of a removed one become newly combinable
    # and the result is no longer NFC.
    text = text.replace("\r\n", "\n").replace("\r", "\n").translate(_TEXT_LINE_BREAKS)
    text = _INVISIBLE.sub("", text)
    text = _CONTROL.sub("", text)
    text = _PRIVATE_USE.sub("", text.translate(_SYMBOL_FONT_GLYPHS))
    text = _SPACE_LIKE.sub(" ", text)
    return unicodedata.normalize("NFC", text)


def _break_at_separators(lines: list[str], collapse_spaces: bool) -> list[str]:
    """`lines` with each line broken at its U+2028 and U+2029 (`clean_text`), outside the code blocks of the text.

    Made line breaks before the lines were looked at, a separator inside a quoted code block began a line without the quote
    marks, which closed the quote and escaped both fences of the block (`> \\````), a separator in a Markdown table row split
    the row, and one inside a fenced example changed the code (Pinned by: test_markdown.py::TestCleanText::
    test_line_separators_stay_inside_code_and_carry_the_quote_marks). So a line inside a code block keeps them, as does an
    indented code line of TXT and MD (`_indented_code`; indented prose, a list item's paragraph or a lazy continuation, is
    no code and breaks); in a table row (`| … |`) each is a
    space; any other line breaks there, each new line starting with the quote marks of the line (a list item goes on as its
    lazy continuation). The code blocks are those of the lines as they are, then as they are once broken: a new line may open
    or close a fence, and a line kept for a block that is then no block is broken too, so that the result is cleaned again
    to itself; past that, every separator left breaks its line. Linear: at most three pairings of the fences.
    """
    tildes = not collapse_spaces
    fenced = any("```" in line or (tildes and "~~~" in line) for line in lines)
    for attempt in range(3):
        blocks = code_fences(lines, tildes=tildes).blocks if fenced else []
        inside = {index for opening, closing in blocks for index in range(opening + 1, closing)}
        if not collapse_spaces:
            inside |= _indented_code(lines, {index for opening, closing in blocks for index in range(opening, closing + 1)})
        broken: list[str] = []
        changed = False
        for index, line in enumerate(lines):
            if not _SEPARATORS.search(line):
                broken.append(line)
                continue
            if attempt < 2 and index in inside:
                broken.append(line)
                continue
            changed = True
            match = _QUOTE_PREFIX.match(line)
            content = line[match.end():] if match else line
            if content.strip().startswith("|") and content.strip().endswith("|"):
                broken.append(_SEPARATORS.sub(" ", line))
                continue
            prefix = line[:match.end()] if match else ""
            first, *rest = _SEPARATORS.split(line)
            while rest and not rest[-1].strip():
                rest.pop()  # a separator at the end of a line ends nothing: the line ends there anyway
            broken.append(first)
            broken.extend(prefix + piece for piece in rest)
        lines = broken
        if not changed:
            break
    return lines


# The start of a list item (CommonMark): at most three spaces, a bullet or a number, then white space or the end of the line.
_LIST_ITEM_START = re.compile(r" {0,3}(?:[-*+]|[0-9]{1,9}[.)])(?:[ \t]|$)")
# An ATX heading (CommonMark): at most three spaces, one to six `#`, then white space or the end of the line.
_ATX_HEADING = re.compile(r" {0,3}#{1,6}(?:[ \t]|$)")


def _indented_code(lines: list[str], fenced: set[int]) -> set[int]:
    """The lines of TXT and MD that are indented code, as CommonMark reads it: indented by four spaces or a tab (after any
    quote marks), opened only after a blank line, a fenced block or a heading and outside a list (an indented line there is
    a paragraph of the item, one right after a line of text its lazy continuation), and going on through the indented and
    blank lines after it. The lines of fenced code blocks (`fenced`) are no part of one. The first line of text opens none,
    blank lines before it or not, and is read without its indentation: the converter strips the indentation at the start of
    the text, so taken for code (or for no list item, `    1. x`) it was read otherwise the next time and the result was not
    converted again to itself (Pinned by: test_markdown.py::TestCleanText::test_line_separators_of_indented_prose_are_line_breaks).
    """
    code: set[int] = set()
    in_code = may_open = in_list = begun = False
    for index, line in enumerate(lines):
        if index in fenced:
            # A fenced block ends at its closing line, so an indented line right after it opens code
            in_code = in_list = False
            may_open = begun = True
            continue
        match = _QUOTE_PREFIX.match(line)
        content = line[match.end():] if match else line
        if not content.strip():
            may_open = begun  # the blank lines before the first line of text are stripped with its indentation
            continue
        if not begun and match is None:
            content = content.lstrip()  # read as it is read again: the converter strips the indentation of the first line
        begun = True
        indented = content.startswith(("    ", "\t"))
        if indented and (in_code or (may_open and not in_list)):
            code.add(index)
            in_code, may_open = True, False
            continue
        in_code = False
        if _ATX_HEADING.match(content):
            in_list, may_open = False, True  # a heading is one line: an indented line right after it opens code
            continue
        if _LIST_ITEM_START.match(content):
            in_list = True
        elif not indented and may_open:
            in_list = False
        may_open = False
    return code


# What a line is to the code blocks of its text (`clean_text`): a fence line of a code block, a line inside one, or an opening
# fence that is never closed.
_FENCE, _INSIDE, _UNPAIRED = 1, 2, 3
# The quote marks at the start of a line, after any leading whitespace: `>` marks with any whitespace between them, and at most
# one space or tab after the last (CommonMark counts that one as part of the mark; what follows is the indentation of the
# quoted line). Whitespace between marks is part of the marks: `>  > x` is a quote in a quote, and read as a quote holding the
# text ` > x` it was written out as `> > x`, which the next cleaning read differently
# (Pinned by: test_markdown.py::TestCleaningTwice).
_QUOTE_PREFIX = re.compile(r"[ \t]*(>(?:[ \t]*>)*[ \t]?)")
# The opening of a code fence: three or more backticks or tildes, after any indentation, and the rest of the line (the info
# string). A backtick fence whose info string holds a backtick is not a fence (CommonMark): it is inline code.
_FENCE_OPENING = re.compile(r"\s*(`{3,}|~{3,})(.*)", re.DOTALL)
# Where a line of a document that is not Markdown would open a tilde fence: at its start, or after the list markers and
# quote marks it starts with, in any order (`- ~~~` opens a fence inside the item, `- > ~~~` inside the quote in the item).
# With list markers only, `- > ~~~`, `1. > ~~~` and `> - > ~~~` were left as fences
# (Pinned by: test_markdown.py::TestFencesThatAreText::test_tilde_line_behind_quote_marks_and_list_markers_is_neutralized).
# The line is already stripped.
_TILDE_OPENING = re.compile(r"^((?:>[ \t]*|(?:[-*+]|\d{1,9}[.)])[ \t]+)*)(?=~~~)")


def _clean_quoted_line(line: str, unpaired: bool, tildes: bool) -> str:
    """A quoted line (`> `, `> > `) of a document that is not Markdown, cleaned as its quote marks and a line of its own: the
    indentation of a nested list inside a quote is kept as at the top level. Taken whole, `>   - b` lost its two spaces and
    the nested item came out flat (Pinned by: test_markdown.py::TestQuotedBlocks)."""
    prefix, _, content = _split_quote(line)
    list_match = _LIST_LINE.match(content)
    content = content.strip()
    if unpaired:
        content = "\\" + content
    elif tildes and "~~~" in content:
        content = _TILDE_OPENING.sub(r"\1\\", content, count=1)
    content = (list_match.group(1) if list_match else "") + _INNER_SPACES.sub(" ", content)
    return prefix + content if content else prefix.rstrip()


class CodeFences:
    """Where the code fences of a text are (`code_fences`): the (opening line, closing line) of each code block in order, and
    the lines of the opening fences that are never closed."""

    __slots__ = ("blocks", "unpaired")

    def __init__(self) -> None:
        self.blocks: list[tuple[int, int]] = []
        self.unpaired: list[int] = []

    @property
    def inside(self) -> set[int]:
        """The line numbers between the fences of the code blocks."""
        return {index for opening, closing in self.blocks for index in range(opening + 1, closing)}


def _split_quote(line: str) -> tuple[str, int, str]:
    """(the quote marks — `> `, `> > `, `>>` — with each run of whitespace in them made one space, how many marks there are,
    the rest of the line). No quote marks: ('', 0, line). Marks written without whitespace stay so: the `>>> f()` prompt line
    of a Python session stays as it was. A tab after a mark is a space, as it was when the whole line was collapsed
    (Pinned by: test_markdown.py::TestQuotedBlocks::test_tab_after_a_quote_mark_becomes_a_space)."""
    match = _QUOTE_PREFIX.match(line)
    if match is None:
        return "", 0, line
    marks = match.group(1)
    return _INNER_SPACES.sub(" ", marks), marks.count(">"), line[match.end():]


def code_fences(lines: list[str], *, tildes: bool = True) -> CodeFences:
    """The code fences of `lines`, paired as CommonMark pairs them: a fence of three or more backticks or tildes is closed by the
    next fence line of the same character, at least as long, with nothing after it, at the same quote depth. A fence inside a
    quote (`> ```) is closed only inside that quote. An opening fence that is never closed is unpaired — not a code block, so
    what follows it is ordinary text — and the search goes on from the line after it.

    Without `tildes` only backtick fences are fences. That is how every format but TXT and Markdown is read: there the only
    real code blocks are the ones the extractors write, with backticks, and a line of tildes is text — a separator, an
    underline, `~~~10시~~~12시` (`clean_text` neutralizes it). Read as fences, two such lines made everything between them a
    code block (Pinned by: test_markdown.py::TestFencesThatAreText).

    Shared by `clean_text` and the picture rendering of the converter, so both see the same code blocks. Only ``` used to be a
    fence and fences were paired by counting: an unpaired `~~~` made the rest of the section code, and an inner ```` ```js ````
    line closed a ```` ```` ```` block (Pinned by: test_markdown.py::TestTildeFences). Linear in the text: the closing fences
    of a character and quote depth are walked once, and an opening fence longer than the closing fence it meets skips to the
    next longer one, which takes fewer steps than it has characters
    (Pinned by: test_markdown.py::TestTildeFences::test_many_unclosed_fences_take_linear_time,
    test_fence_lines_alone_are_cleaned_quickly).
    """
    fences = CodeFences()
    openings: list[tuple[int, str, int, int]] = []  # (line, fence character, fence length, quote depth)
    closings: dict[tuple[str, int], _Closings] = {}
    for index, line in enumerate(lines):
        if "```" not in line and not (tildes and "~~~" in line):
            continue
        depth, content = 0, line
        if ">" in line:
            _, depth, content = _split_quote(line)
        match = _FENCE_OPENING.match(content)
        if match is None:
            continue
        mark, info = match.groups()
        if (mark[0] == "`" and "`" in info) or (mark[0] == "~" and not tildes):
            continue
        openings.append((index, mark[0], len(mark), depth))
        if not info.strip():
            closing = closings.get((mark[0], depth))
            if closing is None:
                closing = closings[mark[0], depth] = _Closings()
            closing.lines.append(index)
            closing.lengths.append(len(mark))
    if not closings:
        fences.unpaired = [index for index, _, _, _ in openings]
        return fences

    ends: list[int] | None = None  # where the quote of each line ends; worked out for the first fence inside a quote
    closed = -1  # the closing line of the last code block
    for index, character, length, depth in openings:
        if index <= closed:
            continue
        closing = closings.get((character, depth))
        found = closing.first_after(index, length) if closing is not None else None
        if found is not None and depth:
            if ends is None:
                ends = _quote_ends([_split_quote(line)[1] if ">" in line else 0 for line in lines])
            if found >= ends[index]:
                found = None
        if found is None:
            fences.unpaired.append(index)
        else:
            fences.blocks.append((index, found))
            closed = found
    return fences


def _quote_ends(depths: list[int]) -> list[int]:
    """For each line, the number of the first line after it with fewer quote marks (where its quote ends), or the line count."""
    ends = [len(depths)] * len(depths)
    waiting: list[int] = []
    for index, depth in enumerate(depths):
        while waiting and depths[waiting[-1]] > depth:
            ends[waiting.pop()] = index
        waiting.append(index)
    return ends


class _Closings:
    """The lines that can close a fence of one character at one quote depth, in order, with the lengths of their fences."""

    __slots__ = ("_longer", "_next", "lengths", "lines")

    def __init__(self) -> None:
        self.lines: list[int] = []
        self.lengths: list[int] = []
        self._next = 0  # the first closing line after the line last asked about (the lines asked about only go forward)
        self._longer: list[int] | None = None  # for each closing line, the next one with a longer fence

    def first_after(self, line: int, length: int) -> int | None:
        """The first closing line after `line` whose fence is at least `length` long, or None. `line` must not go back between
        calls."""
        lines, lengths = self.lines, self.lengths
        position = self._next
        while position < len(lines) and lines[position] <= line:
            position += 1
        self._next = position
        while position < len(lines) and lengths[position] < length:
            if self._longer is None:
                self._longer = _next_longer(lengths)
            position = self._longer[position]
        return lines[position] if position < len(lines) else None


def _next_longer(lengths: list[int]) -> list[int]:
    """For each position, the next position holding a larger number, or the count."""
    longer = [len(lengths)] * len(lengths)
    waiting: list[int] = []
    for position, length in enumerate(lengths):
        while waiting and lengths[waiting[-1]] < length:
            longer[waiting.pop()] = position
        waiting.append(position)
    return longer


def escape_unpaired_fences(lines: list[str]) -> list[str]:
    """`lines` with the backtick lines that open a fence which is never closed neutralized the way `clean_text` does (a
    backslash before the fence, after the quote marks). For an extractor that writes real code blocks: run on the text
    between them, the lines of text that only look like a fence are settled before `clean_text` sees them next to the real
    ones (`_html_render._settle_fences`).

    The lines are looked at as `clean_text` will see them (`clean_characters`; the lines come back so, and one may have
    become several): a line that is "```" behind a zero-width space, a soft hyphen, a control or a private-use character,
    or whose info string is only such a character, was no fence here and one to `clean_text`, which then paired it with the
    fence of the code block next to it (Pinned by: test_text_html.py::TestTextThatLooksLikeAFence::
    test_backtick_paragraph_behind_a_character_that_cleaning_removes). Text that can hold no backtick is returned as it is.
    """
    text = "\n".join(lines)
    if not any(character in text for character in _BACKTICK_SOURCES):
        return lines
    lines = clean_characters(text).split("\n")
    if any("```" in line for line in lines):
        for index in code_fences(lines, tildes=False).unpaired:
            prefix, _, content = _split_quote(lines[index])
            lines[index] = prefix + "\\" + content.lstrip()
    return lines


def _empty_item_holding_a_list(source: list[str], index: int, previous: str) -> re.Match[str] | None:
    """The match of an indented bare list marker (`  -`) at `source[index]` that is an empty item nested in another empty item,
    otherwise None: the line above as already cleaned (`previous`) is itself a bare marker whose content column (its
    indentation + marker + one space) is exactly this indentation, and the next line is a list line (or another bare marker)
    indented exactly to this marker's content column — so both links of the chain nest in CommonMark. Right under an item
    line with text `  -` is a setext underline that turns the text into a heading; under `1.` (content column 3) a two-space
    `  1.` does not nest; and a `    - x` under `  1.` (content column 5) would leave it — all of these keep the old stripped
    form (Pinned by: test_audit_web_fixes.py::TestEmptyListItemHoldingANestedList)."""
    bare = _BARE_LIST_MARKER.match(source[index])
    parent = _EMPTY_ITEM.match(previous)
    if bare is None or parent is None or index + 1 >= len(source):
        return None
    if len(bare.group(1)) != len(parent.group(1)) + len(parent.group(2)) + 1:
        return None
    following = _LIST_LINE.match(source[index + 1]) or _BARE_LIST_MARKER.match(source[index + 1])
    column = len(bare.group(1)) + len(source[index].strip()) + 1
    return bare if following is not None and len(following.group(1)) == column else None


def clean_line(text: str) -> str:
    """`text` as one clean line: the characters `clean_text` removes are removed, NFC, and every run of whitespace (line breaks
    included) is one space, stripped. Without the Markdown handling of `clean_text` (fences, list indentation): for values such
    as document metadata that are not body text."""
    text = _INVISIBLE.sub("", text.translate(_LINE_BREAK_CONTROLS))
    text = _PRIVATE_USE.sub("", _CONTROL.sub("", text).translate(_SYMBOL_FONT_GLYPHS))
    return " ".join(unicodedata.normalize("NFC", text).split())


# Every character `solid_length` removes or changes besides white space: invisible, control and private-use characters (the
# private use areas hold the nested-table placeholders), the characters `_SYMBOL_FONT_GLYPHS` replaces (the Latin ligatures
# among them) and the first character of an image placeholder.
_SOLID_AS_WRITTEN = re.compile("[" + "".join(pattern.pattern[1:-1] for pattern in (_INVISIBLE, _CONTROL, _PRIVATE_USE))
                               + "".join(re.escape(chr(code)) for code in _SYMBOL_FONT_GLYPHS) + "⟦]")


def solid_length(text: str, *, quote_lines: bool = True) -> int:
    """The number of characters certain to remain after cleaning (`clean_text`) and rendering. Used to decide the character limit midway.

    Counted in NFC without whitespace, image placeholders, and invisible, control and private-use characters. Hangul in
    decomposed jamo shrinks to 1/3 when normalized, and a placeholder disappears entirely if its picture is filtered out, so
    counting the raw length comes out larger than the real one. With `quote_lines` False the marks of a line that holds only
    quote marks count too: text that is known to hold no picture (a sheet cell, `_sheet_layout._cell_length`).
    """
    if not text:
        return 0
    # Text with none of the characters the steps below remove or change (and already in NFC) is its length without white
    # space. Most cells and table rows are such text: with the steps, a 1 MB CSV at the default limit took 0.29 s, without 0.22 s
    # (Pinned by: test_markdown.py::TestSolidLength::test_plain_text_is_counted_as_by_the_full_steps).
    if (_SOLID_AS_WRITTEN.search(text) is None and (not quote_lines or ">" not in text)
            and unicodedata.is_normalized("NFC", text)):
        return len("".join(text.split()))
    # A nested-table placeholder goes too, with its number: the table it stands for is counted when it is drawn, and text of
    # the document that only has the shape of one is dropped by the table that holds it. Counted, 80 digits between the two
    # private-use characters stopped a table that was within the limit, and the document came back cut without `exceeded`
    # (Pinned by: test_markdown.py::TestSolidLength::test_nested_table_placeholder_is_not_counted).
    text = _EMBED.sub("", IMAGE_MARKER.sub("", text))
    text = _PRIVATE_USE.sub("", _CONTROL.sub("", _INVISIBLE.sub("", text)).translate(_SYMBOL_FONT_GLYPHS))
    # The quote marks of a line that holds nothing else go too: rendering drops a quoted line left with only its marks when
    # the picture on it is erased (too small, a repeated logo), and the empty quote lines around it
    # (`converter._settle_quote_gaps`). Counted, a 12-character quote with five erased pictures was reported over a limit of
    # 12 to 15 and skipped decoding the real picture beside it
    # (Pinned by: test_converter.py::TestPicturesInsideAQuote::test_erased_pictures_in_a_quote_do_not_count_toward_the_limit).
    if quote_lines and ">" in text:
        text = _BARE_QUOTE_LINE.sub("", text)
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


# Every character `_cell_lines` removes or changes besides the outer white space: invisible, control and space-like characters,
# the line-break controls, line breaks, tabs and a run of spaces.
_CELL_AS_WRITTEN = re.compile("[" + "".join(pattern.pattern[1:-1] for pattern in (_INVISIBLE, _CONTROL, _SPACE_LIKE))
                              + "\x0b\x0c\x85\u2028\u2029\r\n\t]|  ")


def _cell_lines(value: str) -> list[str]:
    # A cell with none of those characters (and already in NFC) is one line, its text stripped. Most cells are such text: the
    # full steps took a fifth of the time a sheet took to draw (Pinned by: test_markdown.py::TestCellLines).
    if _CELL_AS_WRITTEN.search(value) is None and (value.isascii() or unicodedata.is_normalized("NFC", value)):
        line = value.strip()
        return [line] if line else []
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


def render_table(rows: list[list[str | None]], *, merged_titles_only: bool = False, headless: bool = False,
                 room: int | None = None) -> str:
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

    `room` stops writing rows once the output is certain to be over the character limit (as in `TableGrid.render`).
    """
    return _render_rows(rows, merged_titles_only=merged_titles_only, headless=headless,
                        room=None if room is None else _Room(room))


class _Room:
    """The characters a drawing of a table may still write before the output is certain to be over the character limit
    (`TableGrid.render(room=...)`), shared by the tables nested in it. What is written is charged with its `solid_length` in the
    order it appears in the output; once it is used up, the rest of the drawing is not written."""

    __slots__ = ("left",)

    def __init__(self, left: int) -> None:
        self.left = left

    def spend(self, text: str) -> bool:
        """Charge `text`, written at this point of the drawing. True once the room is used up — nothing after it is written."""
        self.left -= solid_length(text)
        return self.left < 0


def _within(parts: Iterable[str], room: _Room | None) -> list[str]:
    """`parts` (in output order) up to and including the one with which the room is used up (all of them without a room). The
    parts are produced only as they are taken, so the rows past the room are never built."""
    if room is None:
        return list(parts)
    kept = []
    for part in parts:
        kept.append(part)
        if room.spend(part):
            break
    return kept


# Cell text at least this long is cleaned once per drawing (`_once_per_text`): the copies of a vertically merged cell are one text
# repeated on every covered row.
_LONG_CELL = 1_000


def _once_per_text[T](clean: Callable[[str], T]) -> Callable[[str], T]:
    """`clean` that works on each long text once and looks it up afterwards. Every copy of a 100,000-character cell merged over
    5,000 rows was cleaned again, twice per drawing."""
    done: dict[str, T] = {}

    def cached(value: str) -> T:
        if len(value) < _LONG_CELL:
            return clean(value)
        if value not in done:
            done[value] = clean(value)
        return done[value]
    return cached


def _render_rows(rows: list[list[str | None]], *, merged_titles_only: bool = False, headless: bool = False,
                 room: _Room | None = None) -> str:
    """`render_table` that stops writing rows once `room` is used up (`TableGrid.render`). Which rows, columns and title the table
    has is decided on all the rows first, so the rows written are exactly the front of the table drawn to the end."""
    # Only the filled cells of a row are kept ({column: cleaned text}), and a row of the grid is built from them when it is
    # written: padded to the widest row up front, a sheet of 200,000 rows with one cell in column CV cost rows × width before a
    # single row was written, and every row was written before the character limit cut the text
    # (Pinned by: test_audit_spreadsheet.py::TestSheetDrawingStopsAtTheLimit).
    clean = _once_per_text(_clean_cell)
    kept_rows: list[list[str | None]] = []
    filled_rows: list[dict[int, str]] = []
    used: dict[int, int] = {}  # column → number of kept rows with text in it
    for row in rows:
        filled = {col: text for col, cell in enumerate(row) if cell and (text := clean(cell))}
        if filled:
            kept_rows.append(row)
            filled_rows.append(filled)
            for col in filled:
                used[col] = used.get(col, 0) + 1
    if not filled_rows:
        return ""
    keep = sorted(used)

    def grid(index: int) -> list[str]:
        filled = filled_rows[index]
        return [filled.get(col, "") for col in keep]

    if len(keep) == 1:
        return "\n\n".join(_within((_uncell(grid(index)[0]) for index in range(len(filled_rows))), room))

    # A title row at the top of a table (a horizontally merged title, or title + unit) used as the header row would lose the column
    # names, so it is moved into a paragraph above the table. The rows are walked by index and the grid cut once: cut after every
    # title row, a list of 200,000 one-cell rows above a full row (an 800 KB CSV) took 59 s
    # (Pinned by: test_markdown.py::TestRenderTable::test_many_title_rows_take_linear_time).
    titles: list[str] = []
    probed: dict[int, list[str]] = {}  # the rows below the current top row, built once each
    while len(keep) >= 2 and len(filled_rows) - len(titles) > 1 and not headless:
        top = probed.pop(len(titles), None) or grid(len(titles))
        below = [probed.setdefault(index, grid(index))
                 for index in range(len(titles) + 1, min(len(titles) + 2 + _HEADER_PROBE_ROWS, len(filled_rows)))]
        title = _title_of([cell for cell in top if cell], len(keep), top, below)
        if title is None or (merged_titles_only and sum(1 for cell in top if cell) == 1
                             and _has_open_cell(kept_rows[len(titles)], keep)):
            break
        titles.append(_uncell(title))
    if titles:
        for filled in filled_rows[:len(titles)]:
            for col in filled:
                used[col] -= 1
        keep = [col for col in keep if used[col]]
        if len(keep) == 1:
            return "\n\n".join(_within(chain(titles, (_uncell(grid(index)[0])
                                                      for index in range(len(titles), len(filled_rows)))), room))

    body = range(len(titles), len(filled_rows))
    header = ["" for _ in keep] if headless else grid(body[0])
    lines = [
        "| " + " | ".join(header) + " |",
        "| " + " | ".join("---" for _ in keep) + " |",
    ]
    parts = _within(chain(titles, lines, ("| " + " | ".join(grid(index)) + " |" for index in body[0 if headless else 1:])), room)
    return "\n\n".join(part for part in [*parts[:len(titles)], "\n".join(parts[len(titles):])] if part)


def _has_open_cell(row: list[str | None], columns: list[int]) -> bool:
    """Whether, among the `columns` of the table row (`row`), there is an empty cell not covered by a span (not None). A column the
    row is too short to have counts as a covered cell."""
    return any(col < len(row) and row[col] is not None and not _clean_cell(row[col]) for col in columns)


# A unit or as-of note in the second cell of a title row (`(단위: 천원)` (unit: thousand won), `[기준: 2024.12.31]` (as of),
# `기준일: …` (as-of date)), and the English ones (`(Unit: USD)`, `Units: thousands`, `(in millions)`, `As of 2024-12-31`).
# Deliberately narrow: a bare `Unit` or `in thousands` without the colon or bracket can be a column name
# (Pinned by: test_audit_spreadsheet.py::TestEnglishUnitNotesInTitleRows).
_UNIT_NOTE = re.compile(r"[(\[<]\s*(?:단위|기준)|(?:단위|기준(?:일자?|일시|시점)?)\s*[:：]"
                        r"|(?i:[(\[<]?\s*units?\s*[:：]|[(\[<]\s*in\s+(?:thousands|millions|billions)\b|[(\[<]?\s*as\s+of\s+\S)")


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


class CellHeading(str):
    """A paragraph of a table cell that is a heading where a heading can stand. Its value is the paragraph as the cell writes
    it, so wherever the paragraph is taken for text it is the text it was before; `level` and `title` are the heading it is
    when its table is a layout box (`TableGrid.unfolded`). (A `str` subclass cannot have `__slots__` that hold values.)"""

    level: int
    title: str

    @classmethod
    def of(cls, written: str, level: int, title: str) -> "CellHeading":
        heading = cls(written)
        heading.level, heading.title = level, title
        return heading


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
        # What the drawing under way may still write (`render(room=...)`; None: no limit), shared with the nested tables it draws.
        self._room: _Room | None = None
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
        # The axes built since the last cell was placed (`_axes`, keyed by `filled_only`), so a drawing that builds the grid
        # more than once (`_draw`, then `_html`) charges the conversion's grid budget once.
        self._axes_built: dict[bool, tuple[list[int], list[int], int]] = {}

    def place(self, row: int, col: int, text: str, *, row_span: int = 1, col_span: int = 1,
              fill_horizontal: bool = False) -> tuple[int, int]:
        """`fill_horizontal` is for merging header rows — it fills the value into the horizontally merged cells too, so it can be
        joined with the header below. A filled cell is a value, so the table extent reaches the spanned columns (`_last_col`).
        Returns the spans (rows, columns) the cell was placed with, after the limits (`TableBudget.add` marks what it covers)."""
        row_span, col_span = max(row_span, 1), max(col_span, 1)
        self._axes_built.clear()
        self.reach(row, col + col_span - 1 if fill_horizontal else col)
        if row_span * col_span > _MAX_SPAN_AREA:
            row_span = min(row_span, _MAX_SPAN_AREA)
            col_span = max(_MAX_SPAN_AREA // row_span, 1)
        if row_span * col_span > 1:
            shared = _spans_left.get()
            if row_span * col_span > self._span_budget or (shared is not None and row_span * col_span > shared[0]):
                row_span = col_span = 1
            else:
                self._span_budget -= row_span * col_span
                if shared is not None:
                    shared[0] -= row_span * col_span

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
        return row_span, col_span

    def place_next(self, row: int, text: str, *, row_span: int = 1, col_span: int = 1,
                   fill_horizontal: bool = False) -> tuple[int, int]:
        """Place a cell that comes in order without an address (HTML), skipping the merged cells coming down from above.

        The empty spot is looked for from the column last placed on that row. Looking from column 0 was quadratic in the number
        of cells of a row (56 seconds for one row of 50,000 cells, Pinned by:
        test_markdown.py::TestTableGridLimits::test_cells_placed_in_order_on_a_long_row_take_linear_time).
        """
        col = self._next_col.get(row, 0)
        while (row, col) in self._cells:
            col += 1
        self._next_col[row] = col
        return self.place(row, col, text, row_span=row_span, col_span=col_span, fill_horizontal=fill_horizontal)

    def place_or_next(self, row: int, col: int, text: str, *, row_span: int = 1, col_span: int = 1) -> tuple[int, int]:
        """A cell with a written address (HWP, HWPX). If an earlier cell already occupies that spot (two cells with the same address,
        an earlier cell's span larger than the table), `place` would drop the text, so a cell with text is put in the next empty
        spot of that row like a cell without an address (`place_next`). In corrupted files cell text vanished without a warning
        (Pinned by: test_hwp_robustness.py::TestCellsOnAnOccupiedAddress)."""
        if (row, col) in self._cells and text.strip():
            return self.place_next(row, text, row_span=row_span, col_span=col_span)
        return self.place(row, col, text, row_span=row_span, col_span=col_span)

    def covers(self, row: int, col: int) -> bool:
        """Whether a placed cell, or the span of one, already fills this spot."""
        return (row, col) in self._cells

    def reach(self, row: int, col: int) -> None:
        """Report, without text, that the table extends to this spot — a cell covered by a span (a DOCX/PPTX vMerge cell, etc.). A
        table whose last row is entirely covered by vertical spans from the cells above does not lose that row either
        (Pinned by: test_markdown.py::TestTableGridLimits::test_covered_cell_reported_by_the_extractor_keeps_its_row)."""
        self._last_row, self._last_col = max(self._last_row, row), max(self._last_col, col)
        self._axes_built.clear()

    def omitted_rows(self) -> int:
        """Number of bottom rows dropped by the grid cell limit (`_MAX_GRID_CELLS`) or the conversion's grid budget
        (`_CONVERSION_GRID_BUDGET`) in the last drawing (nested tables included).
        The extractor reports it as a warning."""
        return self._omitted + sum(table.omitted_rows() for table in self._embedded)

    def rows(self) -> list[list[str]]:
        """Build the grid from only the rows and columns where cells were placed. Rows and columns with no cell are removed by
        render_table anyway, so they are never built in the first place, keeping one far-away cell address from inflating an
        empty grid."""
        return self._grid(filled_only=False)

    def _grid(self, *, filled_only: bool) -> list[list[str]]:
        row_keys, col_keys = self._axes(filled_only=filled_only)
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

    def render(self, *, headless: bool = False, room: int | None = None) -> str:
        """`headless` is a table with no header row (the argument of the same name in `render_table`; in HTML every cell is a td).

        `room` is the characters the output can still take before it is over the character limit (`TableBudget.room`; None: no
        limit). Once what the drawing wrote (`solid_length`, in output order, nested tables included) passes it, the remaining rows
        are not written: the output is over the limit with what comes before, so the text cut at the limit is the same. Which rows,
        columns, title and HTML-or-not the table has is still decided on the whole table. A vertically merged value is repeated
        on every row it covers, but the extractors count the cell text once (`TableBudget`): a 38 KB DOCX with one
        100,000-character cell merged over 5,000 rows was drawn to 500 million characters (65 s, 8.7 GB) before the per-block
        count saw it (Pinned by: test_office_table_char_limit.py::TestMergedCopiesPastTheLimit)."""
        self._room = None if room is None else _Room(room)
        try:
            return self._draw(headless=headless)
        finally:
            self._room = None

    def _draw(self, *, headless: bool = False) -> str:
        rows = self._grid(filled_only=True)
        if not self._embedded:
            return self._html(rows, headless=headless) or _render_rows(rows, headless=headless, room=self._room)
        self._drawn = set()

        # A table inside a table cannot go in a Markdown cell, so it is HTML. Only a one-column box is unfolded into paragraphs as
        # before, with the nested table placed separately.
        html = self._html(rows, force=True, headless=headless)
        if html is not None:
            return html
        self._drawn = set()
        if self._room is None:
            text = _EMBED.sub(lambda match: f"\n\n{self._nested(match, inline=False)}\n\n", render_table(rows, headless=headless))
            return _BLANK_RUN.sub("\n\n", text).strip()
        # With a room the paragraphs are charged in order with the nested tables drawn between them; the box's own text is cut
        # by its own length first (a copy of the room — nested tables only add to what comes before a row).
        box = _render_rows(rows, headless=headless, room=_Room(self._room.left))
        pieces: list[str] = []
        end: int | None = 0
        for match in _EMBED.finditer(box):
            pieces.append(box[end:match.start()])
            if self._room.spend(pieces[-1]):
                end = None
                break
            pieces.append(f"\n\n{self._nested(match, inline=False)}\n\n")
            end = match.end()
            if self._room.left < 0:
                end = None
                break
        if end is not None:
            pieces.append(box[end:])
            self._room.spend(pieces[-1])
        return _BLANK_RUN.sub("\n\n", "".join(pieces)).strip()

    def unfold(self, cells: Iterable[list[str]], heading_of: Callable[[str], tuple[int, str] | None],
               continues: Callable[[str, str], bool] | None = None) -> list[str]:
        """The blocks of this table as a layout box — one column, written as paragraphs — when paragraphs of its cells are
        headings: each heading a block of its own, the text around it written as the box writes a cell (`_draw`), a table
        nested in it as a block of its own.

        `cells` are the paragraphs of each cell in order, as the extractor read them; `heading_of` gives the level and the
        text of the ones that are headings. A table draws its cells from their text, where a paragraph is a line among
        lines, so a heading in a box was written as a line of text: the title of a Korean press release, set in a
        one-column table, and a paragraph in a heading style in a box of a Word document. Headings following each other for
        which `continues` holds are one title set on several lines and are written as one, their texts joined by a space.

        The extractor calls this instead of using what `render` wrote only when a cell holds a heading, so a box without one
        is written as before (Pinned by: test_markdown.py::TestUnfoldedBox)."""
        self._drawn = set()
        blocks: list[str] = []

        def written(plain: list[str]) -> None:
            text = render_table([["\n".join(plain)]]) if plain else ""
            if text and self._embedded:
                text = _BLANK_RUN.sub("\n\n", _EMBED.sub(lambda match: f"\n\n{self._nested(match, inline=False)}\n\n", text)).strip()
            if text:
                blocks.append(text)
            plain.clear()

        for cell in cells:
            plain: list[str] = []
            title: list[str] = []
            for block, following in zip(cell, [*cell[1:], None], strict=True):
                heading = heading_of(block)
                if heading is None:
                    plain.append(str(block))
                    continue
                written(plain)
                title.append(heading[1])
                if following is None or heading_of(following) is None or continues is None or not continues(block, following):
                    blocks.append(f"{'#' * heading[0]} {' '.join(title)}")
                    title = []
            written(plain)
        return blocks

    def unfolded(self, written: str, cells: list[list[str]]) -> list[str]:
        """The blocks of this one-column table of the top level: `written`, what `render` wrote, when none of the paragraphs
        of its `cells` is a `CellHeading`, and otherwise the box unfolded around its headings (`unfold`). For the formats that
        know a heading when they read it; one that settles its headings later keeps the cells until then (`_hangul._Box`)."""
        if not any(type(block) is CellHeading for cell in cells for block in cell):
            return [written]
        return self.unfold(cells, lambda block: (block.level, block.title) if type(block) is CellHeading else None)

    def flatten(self, room: int | None = None) -> str:
        """'cell / cell' lines for places that cannot hold a table (inside a footnote or header). Nested tables are flattened the same
        way. `room` stops the lines once their own text is past it (the character limit; a vertically merged value is on every
        line it covers, as in `render`)."""
        lines = (" / ".join(cell for cell in row if cell) for row in self._grid(filled_only=True))
        text = "\n".join(_within(lines, None if room is None else _Room(room)))
        self._drawn = set()
        return _EMBED.sub(lambda match: self._embedded[index].flatten(room) if (index := self._first_use(match)) is not None else "",
                          text)

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
        table._room = self._room
        try:
            if not inline:
                return table._draw()
            table._drawn = set()
            return table._html(table._grid(filled_only=True), force=True, inline=True) or ""
        finally:
            table._room = None

    def _cell_html(self, text: str) -> str:
        html = _html_cell(text)
        return _EMBED.sub(lambda match: self._nested(match, inline=True), html) if self._embedded else html

    def _axes(self, *, filled_only: bool) -> tuple[list[int], list[int]]:
        """The rows and columns of the grid: those where a cell was placed, or with `filled_only` (a drawing) those where one
        holds text — `render` removes the rows and columns without any anyway, and `flatten` writes no blank line for them.
        The bottom rows over the grid cell limit, or over what is left of the conversion's grid budget
        (`_CONVERSION_GRID_BUDGET`), are dropped."""
        if filled_only in self._axes_built:
            rows, cols, self._omitted = self._axes_built[filled_only]
            return rows, cols
        has_text = _once_per_text(lambda text: bool(text.strip()))
        spots = [(row, col) for (row, col), text in self._cells.items() if row <= self._last_row and col <= self._last_col
                 and (not filled_only or has_text(text))]
        if not spots:
            self._omitted = 0
            return [], []
        rows = sorted({row for row, _ in spots})
        cols = sorted({col for _, col in spots})
        kept = max(_MAX_GRID_CELLS // len(cols), 1)
        shared = _spans_left.get()
        if shared is not None:
            filled: dict[int, int] = {}
            for row, col in spots:
                if filled_only or has_text(self._cells[row, col]):
                    filled[row] = filled.get(row, 0) + 1
            # The empty cells past as many as are filled, for the first `count` rows; the most rows within what is left.
            charge = written = 0
            for count, row in enumerate(rows[:kept], start=1):
                written += filled.get(row, 0)
                if count == 1 or count * len(cols) - 2 * written <= shared[1]:
                    kept, charge = count, max(count * len(cols) - 2 * written, 0)
            shared[1] = max(shared[1] - charge, 0)
        self._omitted = max(len(rows) - kept, 0)
        self._axes_built[filled_only] = rows[:kept], cols, self._omitted
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
        row_keys, col_keys = self._axes(filled_only=True)
        has_lines = _once_per_text(lambda cell: bool(_cell_lines(cell)))
        filled = [[has_lines(cell) for cell in row] for row in rows]
        kept_rows = [index for index, row in enumerate(filled) if any(row)]
        if not kept_rows:
            return None
        kept_cols = _filled_columns(filled, kept_rows)
        if inline and len(kept_cols) == 1:
            lines = []
            for row in kept_rows:
                lines.append(self._cell_html(rows[row][kept_cols[0]]))
                if self._room is not None and self._room.spend(rows[row][kept_cols[0]]):
                    break
            return "<br>".join(lines)
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
            cells, written = [], 0
            for col in range(len(kept_cols)):
                if (row, col) in starts:
                    text, row_span, col_span = starts[row, col]
                    spans = (f' rowspan="{row_span}"' if row_span > 1 else "") + (f' colspan="{col_span}"' if col_span > 1 else "")
                    cells.append(f"<{tag}{spans}>{self._cell_html(text)}</{tag}>")
                elif (row, col) not in taken:
                    text = rows[kept_rows[row]][kept_cols[col]]
                    cells.append(f"<{tag}>{self._cell_html(text)}</{tag}>")
                else:
                    continue
                # The cell text is charged once the row is written; the nested tables in it charged themselves as they were drawn
                # (`solid_length` leaves their placeholders out).
                written += solid_length(text) if self._room is not None else 0
            lines.append("<tr>" + "".join(cells) + "</tr>")
            if self._room is not None:
                self._room.left -= written
                if self._room.left < 0:
                    break
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
