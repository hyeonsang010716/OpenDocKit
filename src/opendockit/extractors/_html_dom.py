"""The tree made ready for rendering: sidebars and hidden parts dropped, lists of Word and Google Docs nested, headings
and tables the parser left open or misplaced put right, form values, symbol fonts, scripts and struck text marked, and
pictures turned into placeholders.
"""

import base64
import binascii
import bisect
from collections.abc import Callable, Container
import itertools
import re

from bs4 import BeautifulSoup, NavigableString, Tag, XMLParsedAsHTMLWarning
from bs4.element import PageElement, PreformattedString

from opendockit.extractors._html_base import (
    _BLOCK_GAP,
    _BLOCK_TAGS,
    _CHECKED,
    _CODE_BLOCKS,
    _FOLDED_STYLE,
    _HEADER_PROBE_ROWS,
    _HEADINGS,
    _MAIN_CONTENT_TAGS,
    _PARSER_CALLERS,
    _UNCHECKED,
    _content_holders,
    _list_number,
    _Places,
    _replace,
    _solid_length,
)
from opendockit.extractors._html_tables import _header_rows_on_top, _names_values_in_pairs, _table_rows
from opendockit.extractors._inline import (
    SUBSCRIPT,
    SUPERSCRIPT,
    _escape_link_text,
    format_script,
)
from opendockit.extractors._package import decimal_int
from opendockit.extractors._symbols import SYMBOL_GLYPHS, symbol_glyph
from opendockit.models import ConvertOptions, Extraction, quiet_warnings


# Inputs whose value is not content: invisible values (hidden/password) and button labels.
_VALUELESS_INPUTS = {"hidden", "password", "submit", "button", "file", "image", "reset"}


# A checkbox/radio carrying any other role is not a form value but a device that opens and closes a menu (a checkbox holding
# state for CSS).
_CHOICE_ROLES = {"checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio"}


# Elements whose content the parser leaves as raw text (`_reopen_raw_text_elements`), and those among them that an end tag
# closes (plaintext is never closed).
_RAW_TEXT_TAGS = ["textarea", "xmp", "plaintext"]
_CLOSABLE_RAW_TEXT = re.compile(r"</(textarea|xmp)\s*>", re.IGNORECASE)
# The end tags at the end of the document (`_closed_at_the_end`). Only this much of the tail is examined — a few end tags are
# enough.
_TRAILING_END_TAGS = re.compile(r"(?:</[A-Za-z][^<>]*>\s*)+\Z")
_TRAILING_BYTES = 4096
_MAX_REOPENED = 16


_STRIKE_TAGS = ["del", "s", "strike"]
_STRIKE = "~~"
# Where a run of struck text is cut by a code block (`_struck_runs`): no cell is this object, so the next text opens a new run.
_RUN_BREAK = object()
# One run of struck text: its text nodes in document order, each with the table it is grouped with (`_struck_runs`).
_Run = list[tuple[NavigableString, Tag | None]]
# Elements that keep two struck runs apart (`_joined_runs`): what is written as a block, a line break or a cell of its own.
# Other elements only wrap their text.
_BETWEEN_RUNS = _BLOCK_TAGS | set(_HEADINGS) | {"br", "li", "ul", "ol", "pre", "img", "table", "tr", "td", "th", "thead", "tbody",
                                                "tfoot"}
# No emphasis is written inside a heading or inline code (`_html_render._render_nodes`, `_CODE_TAGS`).
_PLAIN_INSIDE = frozenset({*_HEADINGS, "code", "kbd", "samp", "tt"})
_LINE_PIECES = frozenset({"br", "img"})  # of `_BETWEEN_RUNS`, what does not end a block
_SPAN_BREAKERS = frozenset({"table", "pre", "ul", "ol"})  # `_html_render._BLOCK_LEAVES`: no span is written around one
_EMPHASIS_TAGS = {"b": "b", "strong": "b", "i": "i", "em": "i"}  # `_html_render._EMPHASIS_MARKS`
# CSS strikethrough. Both `text-decoration: underline line-through` and `text-decoration-line: line-through`.
# Each declaration (up to the next `;`) is read once, from its first `text-decoration…:` — a later one in the same declaration
# has less after it, so it cannot match when the first does not. Starting again at every occurrence took 15 seconds on a
# style of 20,000 `text-decoration:` (312KB)
# (Pinned by: test_audit_html_text.py::TestAttributeValuesThatRepeatTheStartOfAPattern).
_STRIKE_STYLE = re.compile(r"(?:^|;)(?>[^;]*?text-decoration(?:-line)?\s*:)[^;]*\bline-through\b", re.IGNORECASE)


# Only the `display` / `visibility` (and `content-visibility`) properties themselves: matched anywhere, the pattern removed a
# visible element styled `backface-visibility:hidden`, text and all
# (Pinned by: test_audit_html_text.py::TestStylePropertiesThatOnlyEndLikeAHidingOne).
_HIDDEN_STYLE = re.compile(r"(?<![\w-])(?:display\s*:\s*none|(?:content-)?visibility\s*:\s*hidden)", re.IGNORECASE)
# Hiding that a descendant cannot undo (`display:none`, `content-visibility:hidden`), and the inherited `visibility`, which a
# descendant can set back with `visibility:visible` (`_drop_hidden`).
_UNDOABLE_HIDING = re.compile(r"(?<![\w-])(?:display\s*:\s*none|content-visibility\s*:\s*hidden)", re.IGNORECASE)
_VISIBLE_STYLE = re.compile(r"(?<![\w-])visibility\s*:\s*visible", re.IGNORECASE)
# Elements whose `visibility:hidden` is not taken as hiding: the page itself, hidden by an anti-flicker snippet until a script
# shows it.
_PAGE_ROOTS = {"html", "body"}


# Hidden elements (blocks) that can be a folded answer, and the elements right before one that unfold it. Lists (ul/ol) are
# left out because they are dropdown menus.
_FOLDED_TAGS = {"div", "p", "dd", "section", "article", "blockquote"}
_OPENER_TAGS = {"a", "button", "dt", "summary", *_HEADINGS}
# A link that stays where it unfolds instead of going elsewhere: no href, or `#…`/`javascript:…`.
_STAYING_LINK = re.compile(r"\s*(?:#|javascript:|$)", re.IGNORECASE)
# Names (class/id) and roles of hidden elements that are not folded answers: menus, modals, tooltips, layer popups.
_OVERLAY_NAME = re.compile(r"modal|popup|pop-|tooltip|menu|layer|dropdown|dialog|gnb|lnb|nav|toast|banner|overlay", re.IGNORECASE)
_OVERLAY_ROLES = {"dialog", "alertdialog", "menu", "menubar", "tooltip", "navigation", "listbox"}


# Table structure elements. Text placed inside a table but outside these is shown by browsers moved in front of the table
# (`_foster_parent_table_content`).
_TABLE_PARTS = {"thead", "tbody", "tfoot", "tr"}
_TABLE_CELLS = {"td", "th", "caption", "colgroup", "col"}
_TABLE_TAG = re.compile(r"<table[\s>]", re.IGNORECASE)
_ROW_START = re.compile(r"<tr[\s>]", re.IGNORECASE)


# The font an element declares (`_declared_font`): font-family in style (first name), the font shorthand property.
_FONT_FAMILY = re.compile(r"(?:^|;)\s*font-family\s*:\s*([^;]+)", re.IGNORECASE)
_FONT_SHORTHAND = re.compile(r"(?:^|;)\s*font\s*:\s*([^;]+)", re.IGNORECASE)


# Class of a list exported by Google Docs (`lst-kix_<list id>-<level>`). It lays out a separate list per level side by side
# (`_nest_google_docs_lists`).
# Each class name is read once, from its first `lst-kix_` (`list` is the name up to the level) — same reason as
# `_STRIKE_STYLE`: a class of 20,000 `lst-kix_` (156KB) took 5 seconds.
_GOOGLE_DOCS_LIST = re.compile(r"(?<!\S)(?>\S*?(?=lst-kix_))(?P<list>lst-kix_\S+)-(?P<level>\d+)")
# Style of a list paragraph that Word wrote with 'Save as Web Page' (`mso-list:l0 level2 lfo1`) and its bullet slot
# (`mso-list:Ignore`, `_nest_word_lists`).
_WORD_LIST_LEVEL = re.compile(r"mso-list\s*:\s*l\d+\s+level(\d+)", re.IGNORECASE)
_WORD_LIST_MARKER = re.compile(r"mso-list\s*:\s*ignore", re.IGNORECASE)
# The deepest level of a Word list. Levels beyond it (level50000 in crafted HTML) count as this level — every item added to a
# `<ul>` nested tens of thousands deep walked to the last descendant, which took quadratic time
# (Pinned by: test_core_header_tables_cell_code_and_terminal_codes.py::TestDeepWordListLevels).
_MAX_WORD_LIST_LEVEL = 9
# Number of `<text>` elements of an inline SVG whose text is kept (`_keep_svg_labels`). Up to this many they are always kept;
# beyond it, only diagrams where word labels (two or more letters) are at least half are kept, up to `_MAX_SVG_LABELS` — a
# chart with many numeric labels is noise listing axis ticks and legends.
_FEW_SVG_LABELS = 10
_MAX_SVG_LABELS = 300


# Words in the names of data-* attributes that hold the real address of a lazy-loaded picture (`_is_lazy`), and the maximum
# side length taken for a spacer picture (pixels, `_is_spacer`).
_LAZY_SOURCE_WORDS = ("src", "original", "lazy")


_SPACER_SIDE = 2


_MARKDOWN_HEADING = re.compile(r"^#{1,6} ", re.MULTILINE)


def _drop_sidebars(soup: BeautifulSoup) -> None:
    """Removes `<aside>` (side menus, ads, related posts) outside the main content (`_MAIN_CONTENT_TAGS`). An aside inside the
    main content is kept.

    Removing every aside made the warning/note boxes (`<aside class="warning">`) in the body of technical documents and notices
    vanish without a warning (Pinned by: test_core_html_blocks.py::TestAsideInsideTheMainContent). Deliberate: an aside used as
    a side menu stays when it is inside the main content — removing it loses body text, keeping it only adds a short menu text.
    """
    # Decided for all of them in one walk and removed together (`_lying_inside`, `_decompose_all`)
    # (Pinned by: test_audit_html_text.py::TestManyElementsThatLookUpwardsUnderDeepNesting)
    asides = soup.find_all("aside")
    kept = _lying_inside(asides, _MAIN_CONTENT_TAGS)
    _decompose_all([aside for aside in asides if id(aside) not in kept])


def _keep_svg_labels(soup: BeautifulSoup, cut: frozenset[int] = frozenset()) -> None:
    """When the label text of an inline `<svg>` (`<text>`, HTML inside `<foreignObject>`) is worth keeping
    (`_is_labelled_svg`), replaces the svg with a `<span>` holding that text in document order. Other svgs are removed as
    before (`_DROP_TAGS`). An svg cut at the end of the front part (`cut`, `_open_at_the_end`) is removed too.

    Removing svgs whole made labels written as text ('매출 120억', "sales 12 billion") vanish
    (Pinned by: test_core_html_layout_cells.py::TestTextInsideInlineSvg). Counting only `<text>` made the text of diagrams
    written with foreignObject, as Mermaid (htmlLabels default) and draw.io do, vanish entirely (Pinned by:
    test_core_round48.py::TestForeignObjectLabelsInInlineSvg). A `<text>` inside a foreignObject is already part of the outer
    foreignObject's text.
    """
    places: _Places = {}
    # Inner svgs and labels inside a foreignObject are found by walking down each outermost svg once, not by walking up from
    # every svg and label to the root of the document
    # (Pinned by: test_audit_html_text.py::TestManyElementsThatLookUpwardsUnderDeepNesting)
    inner: set[int] = set()

    for svg in soup.find_all("svg"):
        if id(svg) in inner:
            continue  # replaced or removed together with the outer svg
        inner.update(id(nested) for nested in svg.find_all("svg"))
        if id(svg) in cut:
            continue  # the label count of a cut svg is unknown
        labels = [" ".join(label.get_text(" ").split()) for label in _svg_labels(svg)]
        labels = [label for label in labels if label]
        if _is_labelled_svg(labels):
            span = soup.new_tag("span")
            span.string = " ".join(labels)
            _replace(svg, span, places)


def _svg_labels(svg: Tag) -> list[Tag]:
    """The label elements of an svg in document order: every `<text>` and `<foreignObject>` that is not inside a foreignObject
    (with a stack, like `_nearest`; a foreignObject is not descended into)."""
    found: list[Tag] = []
    stack = list(reversed(svg.contents))
    while stack:
        child = stack.pop()
        if not isinstance(child, Tag):
            continue
        if child.name in ("text", "foreignobject"):
            found.append(child)
        if child.name != "foreignobject":
            stack.extend(reversed(child.contents))
    return found


def _is_labelled_svg(labels: list[str]) -> bool:
    """Whether to keep the svg labels `labels` as text. Up to `_FEW_SVG_LABELS` they are kept; beyond that, only when word
    labels with two or more letters are at least half and there are at most `_MAX_SVG_LABELS`.

    The text of flowcharts and architecture diagrams with more than ten labels ('고객 주문 접수', '재고 확인' …) vanished
    entirely (Pinned by: test_core_round46.py::TestDiagramWithManyLabels). Deliberate: charts with many numeric labels (axis
    ticks, legends, '1월') and text under two characters (the icon letter 'i') are removed as number-listing noise.
    """
    if not labels or sum(map(len, labels)) < 2:
        return False
    if len(labels) <= _FEW_SVG_LABELS:
        return True
    words = sum(1 for label in labels if sum(map(str.isalpha, label)) >= 2)
    return len(labels) <= _MAX_SVG_LABELS and words * 2 >= len(labels)


def _nest_google_docs_lists(soup: BeautifulSoup) -> None:
    """Turns the per-level lists of HTML exported from Google Docs (`<ul class="lst-kix_<id>-<level>">`) into nested lists by
    moving each into the last item of the shallower list right before it. Only neighbouring lists with nothing but whitespace
    between them are considered.

    Google Docs lays out a separate list per level side by side and indents with CSS only, so sub-items were read at the same
    level as their parents (Pinned by: test_core_html_layout_cells.py::TestGoogleDocsListLevels). Numbering follows the `start`
    written on each list as is.
    """
    levels = {id(node): level for node in soup.find_all(["ul", "ol"])
              if (match := _GOOGLE_DOCS_LIST.search(" ".join(node.get("class") or ())))
              and (level := decimal_int(match.group("level"), None)) is not None}
    if not levels:
        return
    runs: list[list[Tag]] = []
    previous: Tag | None = None

    for node in soup.find_all(["ul", "ol"]):
        if id(node) not in levels:
            continue
        before = node.previous_sibling
        while isinstance(before, NavigableString) and not before.strip():
            before = before.previous_sibling
        if before is None or before is not previous:
            runs.append([])
        runs[-1].append(node)
        previous = node

    # The item count and last item of each list are tracked separately. Re-counting the items of the preceding list, which
    # grows with every join, took 79 s for 40,000 pieces. Pieces to detach (emptied by joining, or to be moved into a parent
    # item) are collected and detached at once (same reason as `_decompose_all`, Pinned by:
    # test_core_th_rows_front_tables_and_linear_time.py::TestManySiblingsTakeLinearTime::test_many_google_docs_list_pieces).
    counts: dict[int, int] = {}
    last_items: dict[int, Tag] = {}
    leaving: list[Tag] = []
    emptied: list[Tag] = []
    moves: list[tuple[Tag, Tag]] = []

    for run in runs:
        stack: list[Tag] = []
        for node in run:
            while stack and levels[id(stack[-1])] > levels[id(node)]:
                stack.pop()
            items = node.find_all("li", recursive=False)
            if stack and levels[id(stack[-1])] == levels[id(node)]:
                if _continues_google_docs_list(stack[-1], node, counts[id(stack[-1])]):
                    for item in items:
                        stack[-1].append(item.extract())
                    counts[id(stack[-1])] += len(items)
                    if items:
                        last_items[id(stack[-1])] = items[-1]
                    leaving.append(node)
                    emptied.append(node)
                    continue
                stack.pop()
            if stack and id(stack[-1]) in last_items:
                leaving.append(node)
                moves.append((node, last_items[id(stack[-1])]))
            counts[id(node)] = len(items)
            if items:
                last_items[id(node)] = items[-1]
            stack.append(node)

    _decompose_all(leaving, detach_only=True)
    for node in emptied:
        node.decompose()
    for node, item in moves:
        item.append(node)


def _nest_word_lists(soup: BeautifulSoup) -> None:
    """Groups neighbouring list paragraphs (`<p style="mso-list:lN levelK">`) of HTML that Word wrote with 'Save as Web Page'
    (.htm, Outlook mail) and turns them into `<ul>` nested by `levelK`. A bullet slot (`mso-list:Ignore`) that is a single
    symbol character (including the `o` of Courier New) is removed; a number (`1.`, `가.`) is kept in front of the item text
    (`_word_list_marker`). Only neighbouring paragraphs with nothing but whitespace between them are considered.

    Word writes a list as paragraphs with indentation CSS, so sub-items were read as paragraphs at the same height as their
    parents (Pinned by: test_core_word_lists_cell_blocks_and_mail_lines.py::TestWordSavedHtmlListLevels). A skipped level
    (1 → 3) goes only one level deeper. Deliberate: neighbours are one list even when their list numbers (l0, l1) differ — the
    number remains in the item text. The paragraphs are detached and moved all at once (same reason as `_decompose_all`).
    """
    runs: list[list[tuple[Tag, int]]] = []
    previous: Tag | None = None

    paragraphs = soup.find_all("p", style=_WORD_LIST_LEVEL)
    in_items = _lying_inside(paragraphs, ("li",))
    for paragraph in paragraphs:
        level = decimal_int(_WORD_LIST_LEVEL.search(str(paragraph.get("style"))).group(1), None)
        if level is None or id(paragraph) in in_items:
            continue
        level = min(level, _MAX_WORD_LIST_LEVEL)
        before = paragraph.previous_sibling
        while isinstance(before, NavigableString) and not before.strip():
            before = before.previous_sibling
        if before is None or before is not previous:
            runs.append([])
        runs[-1].append((paragraph, level))
        previous = paragraph
    if not runs:
        return

    _decompose_all([paragraph for run in runs for paragraph, _ in run[1:]], detach_only=True)
    places: _Places = {}

    for run in runs:
        root = soup.new_tag("ul")
        _replace(run[0][0], root, places)
        stack: list[tuple[int, Tag]] = [(run[0][1], root)]
        last_items: dict[int, Tag] = {}
        for paragraph, level in run:
            while len(stack) > 1 and stack[-1][0] > level:
                stack.pop()
            if level > stack[-1][0] and id(stack[-1][1]) in last_items:
                nested = soup.new_tag("ul")
                last_items[id(stack[-1][1])].append(nested)
                stack.append((level, nested))
            marker = _word_list_marker(paragraph)
            if marker is not None:
                symbol = marker.get_text().strip()
                if len(symbol) <= 1 and (not symbol.isalnum() or symbol == "o"):
                    marker.decompose()
            item = soup.new_tag("li")
            item.append(paragraph)
            stack[-1][1].append(item)
            last_items[id(stack[-1][1])] = item


def _word_list_marker(paragraph: Tag) -> Tag | None:
    """The bullet slot of a Word list paragraph: the `mso-list:Ignore` span or, without one (old Word/Outlook — it is only
    wrapped in `<![if !supportLists]>` comments, and comments are already removed), the span at the very start of the
    paragraph."""
    marker = paragraph.find("span", style=_WORD_LIST_MARKER)
    if marker is not None:
        return marker
    first = next((child for child in paragraph.contents if not isinstance(child, NavigableString) or child.strip()), None)
    return first if isinstance(first, Tag) and first.name == "span" else None


def _continues_google_docs_list(previous: Tag, node: Tag, previous_items: int) -> bool:
    """Whether the neighbouring list piece `node` at the same level continues `previous` (`previous_items` items): the same
    list id and tag and, for a numbered list, a `start` that is the next number."""
    def list_id(tag: Tag) -> str | None:
        match = _GOOGLE_DOCS_LIST.search(" ".join(tag.get("class") or ()))
        return match.group("list") if match else None

    if node.name != previous.name or list_id(node) != list_id(previous):
        return False
    if node.name == "ul":
        return True
    following = _list_number(previous.get("start"), 1) + previous_items
    return _list_number(node.get("start"), following) == following


def _with_title_heading(text: str, title: str | None) -> str:
    """When the body has no heading (`#`) at all, puts the document title (`<title>`) at the very front as `# title`. Left as
    is when the body is empty or its first line already contains that title.

    With `<title>` only in the metadata the text had no heading
    (Pinned by: test_core_html_layout_cells.py::TestTitleWhenTheBodyHasNoHeading). Deliberately narrow: it is not inserted
    when the body has even one heading — a title carrying the site name ('사이트 | 공지', "site | notice") would stack on top
    of the body's heading. Not used for mail (which has the subject in its header). Known accepted gap: an HTML fragment in a
    DOCX (altChunk) goes through the same extractor, so the fragment's title becomes a heading.
    """
    body = text.strip()
    first_line = body.split("\n", 1)[0]
    # The first line may carry emphasis marks now (`**공지** 사항`); the title is looked for without them
    if (not title or not body or _MARKDOWN_HEADING.search(_outside_code_blocks(body)) or title in first_line
            or title in first_line.replace("*", "")):
        return text
    return f"# {title}\n\n{body}"


def _outside_code_blocks(text: str) -> str:
    """The lines of `text` outside its code blocks (``` fences). A `# comment` line of a shell or Python example is not a
    heading, and the title was left out of a page whose only `#` lines were code
    (Pinned by: test_audit_web_fixes.py::TestTitleOfAPageWhoseCodeHasCommentLines). Fences are paired as `clean_text` pairs
    them: with an odd number of fence lines the last one is not a fence (it is neutralized as `\\````), so the headings after a
    stray ``` paragraph still count (Pinned by: TestTitleOfAPageWhoseCodeHasCommentLines::test_stray_fence_line_does_not_hide_a_heading)."""
    lines = text.split("\n")
    fences = [index for index, line in enumerate(lines) if line.lstrip().startswith("```")]
    unpaired = fences[-1] if len(fences) % 2 else None
    kept: list[str] = []
    fenced = False
    for index, line in enumerate(lines):
        if index != unpaired and line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced:
            kept.append(line)
    return "\n".join(kept)


def _document_nodes(soup: BeautifulSoup) -> list:
    """The nodes to render, in document order: the children of body, content outside body (after `</body>`/`</html>`), and a
    concatenated following document (a second html root).

    lxml places text and tables after `</html>`, and a concatenated second HTML document, outside body (as sibling roots of
    html). Reading only body made that content vanish without a warning
    (Pinned by: test_core_html.py::TestContentOutsideTheBody). Everything is shown, as in a browser, with a block break
    between roots. DOCTYPEs and processing instructions outside the document are not text. Not `unwrap`ping html/body is
    deliberate — it searches the parent's list linearly for every child, which is quadratic time on a large document (same
    reason as `_put_form_values`).
    """
    nodes: list[object] = []
    for top in soup.contents:
        if isinstance(top, PreformattedString):
            continue
        if not isinstance(top, Tag) or top.name != "html":
            nodes.append(top)
            continue
        if nodes:
            nodes.append(_BLOCK_GAP)
        for child in top.contents:
            if isinstance(child, Tag) and child.name == "body":
                nodes.extend(child.contents)
            elif not isinstance(child, PreformattedString):
                nodes.append(child)
    return nodes


def _flatten_nested_headings(soup: BeautifulSoup) -> None:
    """Takes a heading inside a heading out to right after the outer heading (together with the siblings that follow it). A
    browser closes the previous heading when the next heading arrives while it is still open.

    lxml nests them as written, so a document repeating the typo `<h3>` for `</h3>` became headings hundreds of levels deep,
    the recursion of heading rendering exceeded the limit, and the whole document was rejected as a 'corrupted document'.
    Processing is in document order, so the outer heading has already been taken out and the ancestor search ends after one
    level (Pinned by: test_text_html.py::TestMalformedNesting).

    A heading that is a direct child of the outer heading (the common form of the typo) is not moved; the outer one is closed
    instead (`_close_heading_before`). Moving it makes bs4 walk the whole remaining chain below the moved heading, which took
    9.9 s (quadratic) on 156KB of repeated `<h3>제목`
    (Pinned by: test_text_html.py::TestMalformedNesting::test_many_heading_tags_left_open_take_linear_time).
    """
    headings = soup.find_all(list(_HEADINGS))
    # `_flatten_nested_headings` only moves content out of headings (and wraps what precedes an already handled heading), so a
    # heading that starts outside every heading never gets one around it and need not look for one
    nested = _lying_inside(headings, _HEADINGS)
    for heading in headings:
        if heading.parent is not None and heading.parent.name in _HEADINGS:
            _close_heading_before(soup, heading.parent, heading)
            continue
        if id(heading) not in nested:
            continue
        outer = heading.find_parent(list(_HEADINGS))
        if outer is not None:
            _move_after(outer, [heading, *heading.next_siblings])


def _lying_inside(nodes: list[PageElement], names: Container[str], inside: dict[int, bool] | None = None) -> set[int]:
    """`id`s of those of `nodes` that lie inside an element named in `names` (the same answer as `find_parent(names)` for each,
    as the tree is now). Each node walks upwards and stops at an element already visited, so it is linear time. Calls that ask
    about the same tree and `names` may share `inside` (what was learned on the way up).

    `find_parent` walks to the root for every node that has no such ancestor: 8,000 headings under 8,000 unclosed `<div>`
    (117KB) took 31 seconds (Pinned by: test_audit_html_text.py::TestManyHeadingsUnderDeepNesting).
    """
    found: set[int] = set()
    if inside is None:
        inside = {}  # element not named in `names` → whether it lies inside one that is

    for node in nodes:
        path: list[Tag] = []
        parent = node.parent
        while parent is not None and parent.name not in names and id(parent) not in inside:
            path.append(parent)
            parent = parent.parent
        lies_inside = parent is not None and (parent.name in names or inside[id(parent)])
        inside.update((id(passed), lies_inside) for passed in path)
        if lies_inside:
            found.add(id(node))
    return found


def _reopen_raw_text_elements(soup: BeautifulSoup, closed: frozenset[str]) -> None:
    """Repairs the elements whose content the parser leaves as raw text (textarea/xmp/plaintext). One left unclosed, which
    swallowed everything to the end of the document, keeps only what precedes its first tag; the rest is re-read as HTML and
    placed after it. A closed xmp (the old 'show markup as is') is a code block (pre).

    An unclosed `<textarea name=a/>` (HTML has no self-closing) turned the body after it into text with its tags,
    `<p>…</p></body></html>`, and the `<b>코드</b>` of an xmp came out as paragraph text
    (Pinned by: test_core_html_mail_hidden_and_frames.py::TestRawTextElementsLeftOpen). An unclosed one is an element that
    reaches the end of the document (`_reaches_the_end`) and whose name is not in `closed` (names of elements closed by an end
    tag at the end of the document, `_closed_at_the_end`/`_closed_after`). Previously this was decided by whether the raw text
    contained `</body>`/`</html>`, which read an HTML example held in a closed textarea (a CMS editing screen) as body, and in
    the front part of a large document (`_extract_front`, which has no such tag) did not re-read an unclosed one, so a
    document whose whole conversion was within the limit exceeded it with the tags as text
    (Pinned by: test_core_html_raw_text_and_removal.py). Deliberate: the text of a closed textarea is an entered value and is
    left as is (`_put_form_values`). Re-reading happens at most `_MAX_REOPENED` times — if the re-read rest again has an
    unclosed element, what follows it is read again, which is quadratic in their number
    (Pinned by: TestRawTextElementsLeftOpen::test_many_unclosed_elements_take_linear_time). Known accepted gap: what follows
    more unclosed elements in a row than that stays as text with its tags.
    """
    pending = soup.find_all(_RAW_TEXT_TAGS)
    reopened = 0

    while pending:
        tag = pending.pop(0)
        text = tag.get_text()
        cut = text.find("<")
        if reopened < _MAX_REOPENED and cut >= 0 and tag.name not in closed and _reaches_the_end(tag):
            reopened += 1
            with quiet_warnings((XMLParsedAsHTMLWarning, _PARSER_CALLERS)):
                rest = BeautifulSoup(text[cut:], "lxml")
            container = rest.body or rest
            nodes = list(container.contents)
            _move_after(tag, [node.extract() for node in nodes])
            pending[:0] = [found for node in nodes if isinstance(node, Tag)
                           for found in ([node] if node.name in _RAW_TEXT_TAGS else node.find_all(_RAW_TEXT_TAGS))]
            if not text[:cut].strip():
                tag.decompose()
                continue
            tag.string = text[:cut]
        if tag.name != "textarea":
            tag.name = "pre"


def _reaches_the_end(tag: Tag) -> bool:
    """Whether no other element or (non-whitespace) text of the document follows `tag` — an unclosed raw text element swallows
    everything to the end of the document."""
    node = tag
    while isinstance(node, Tag) and node.contents:
        node = node.contents[-1]

    node = node.next_element
    while node is not None:
        if not isinstance(node, NavigableString) or node.strip():
            return False
        node = node.next_element
    return True


def _open_at_the_end(soup: BeautifulSoup) -> frozenset[int]:
    """The `id`s of the elements holding the last piece of the document — in the front part (`_extract_front`) these are the
    elements that may have been cut at its end.

    Decisions that look at a whole element to decide whether to keep it (the svg label count, the link ratio of a folded
    answer) differ on a cut element from the whole conversion, and with text absent from the whole conversion a document
    within the limit was blocked as 'exceeded'
    (Pinned by: test_core_front_cut_and_tiff_pixels.py::TestElementCutAtTheEndOfTheFront). These elements are decided towards
    removal — all that matters is that the front part never has more text than the whole. An element closed inside the front
    part that sits at its end is included too, but that only reduces the text.
    """
    node: PageElement = soup
    while isinstance(node, Tag) and node.contents:
        node = node.contents[-1]
    return frozenset(id(parent) for parent in node.parents)


def _drop_unsettled_cut_table(soup: BeautifulSoup, cut: frozenset[int]) -> None:
    """Removes the outermost table cut at the end of the front part (`cut`) when its shape may change with the rows that
    follow.

    The number of header rows (`_header_row_count`), whether it is a layout table (`_is_layout_table`), a headerless
    name/value table (`_names_values_in_pairs`) and the table title row (which looks at a few rows below) are decided from the
    whole table. Deciding from the cut rows alone produced longer text than the whole conversion (an HTML table instead of a
    layout table, several rows instead of one header line) and blocked a document within the limit as 'exceeded' (Pinned by:
    test_core_th_rows_front_tables_and_linear_time.py::TestTableCutAtTheEndOfTheFront). The table counts as unsettled when it
    has an inner table, has few rows, has a header reaching the cut end, or is a name/value pair table. Removing it only
    reduces the text of the front part, so whether the whole exceeds the limit stays correct (`_open_at_the_end`). An
    ordinary data table with many rows is rendered as is, so even a large 'Excel download' table still stops at the front part.
    """
    if not cut:
        return
    table = next((table for table in soup.find_all("table") if id(table) in cut), None)
    if table is None:
        return
    rows = _table_rows(table)
    if (len(rows) <= _HEADER_PROBE_ROWS + 1 or table.find("table") is not None
            or _header_rows_on_top(table, rows) >= len(rows) - 1 or _names_values_in_pairs(rows)):
        table.decompose()


def _closed_at_the_end(text: str) -> frozenset[str]:
    """Those of the end tags at the end of the document that belong to raw text elements (textarea/xmp). Such an element
    reaching the end of the document was closed there (`_reaches_the_end`)."""
    trailing = _TRAILING_END_TAGS.search(text[-_TRAILING_BYTES:])
    if trailing is None:
        return frozenset()
    return frozenset(name.lower() for name in _CLOSABLE_RAW_TEXT.findall(trailing.group()))


def _closed_after(text: str, start: int) -> frozenset[str]:
    """Names of the raw text elements whose end tag appears after `start`. An element reaching the end of the front part
    (`_extract_front`) is closed if it is closed after that point."""
    names: set[str] = set()

    for match in _CLOSABLE_RAW_TEXT.finditer(text, start):
        names.add(match.group(1).lower())
        if len(names) == 2:
            break
    return frozenset(names)


def _close_heading_before(soup: BeautifulSoup, outer: Tag, heading: Tag) -> None:
    """Closes the outer heading before its child `heading`: the children before `heading` are wrapped in a new heading with
    the same name and attributes, and the outer heading becomes a div without attributes. Text order and rendering are the same
    as moving `heading` and its following siblings to after the outer heading. Only the preceding children are moved."""
    closed = soup.new_tag(outer.name, attrs=dict(outer.attrs))
    before = list(itertools.takewhile(lambda node: node is not heading, outer.contents))
    outer.name, outer.attrs = "div", {}
    outer.insert(0, closed)
    for node in before:
        closed.append(node)


def _foster_parent_table_content(soup: BeautifulSoup) -> None:
    """Moves what sits inside a table but outside its cells, as a browser does: text and inline elements to right before the
    table, tables to right after it.

    lxml leaves the text of `</tr>소계: 300<tr>` and `<table>안내</table>` inside the table as is, and puts the next table
    after a missing `</table>` inside the outer table (outside its cells). Rendering reads only cells, so these vanished
    without a warning. Elements wrapping rows (`<form>`) are descended into. Whitespace-only text is not moved. Processing is
    in document order, so a table taken out is processed again at its own turn (no recursion)
    (Pinned by: test_text_html.py::TestContentOutsideTableCells).
    """
    for table in soup.find_all("table"):
        before: list[object] = []
        after: list[Tag] = []
        stack = list(reversed(table.contents))

        while stack:
            child = stack.pop()
            if isinstance(child, NavigableString):
                if str(child).strip():
                    before.append(child)
            elif not isinstance(child, Tag) or child.name in _TABLE_CELLS:
                continue
            elif child.name == "table":
                after.append(child)
            elif child.name in _TABLE_PARTS or child.find(["tr", "td", "th"]) is not None:
                stack.extend(reversed(child.contents))
            else:
                before.append(child)

        if before or after:
            _move_beside(table, before, after)


def _move_beside(table: Tag, before: list, after: list) -> None:
    """Moves `before`, in order, to right before `table` and `after` to right after it. Both are detached in one go
    (`_decompose_all`) and put in at positions counted from the table's, which is looked up once.

    `insert_before(node.extract())` per node looks up the node's position and the table's among their siblings every time:
    40,000 texts between the rows of one table (781KB) took 29 seconds, 40,000 tables outside its cells 14 seconds
    (Pinned by: test_audit_html_text.py::TestMuchContentOutsideTheCellsOfATable).
    """
    _decompose_all(before, detach_only=True)
    _decompose_all(after, detach_only=True)
    parent = table.parent
    index = next(position for position, child in enumerate(parent.contents) if child is table)
    for offset, node in enumerate(before):
        parent.insert(index + offset, node)
    for offset, node in enumerate(after, start=1):
        parent.insert(index + len(before) + offset, node)


def _move_after(anchor: Tag, nodes: list) -> None:
    """Moves `nodes`, in order, to right after `anchor`."""
    for node in nodes:
        anchor.insert_after(node.extract())
        anchor = node


def _keep_expander_buttons(soup: BeautifulSoup) -> None:
    """Keeps the text of expander buttons: buttons inside a heading (h1–h6) and buttons with `aria-expanded`/`aria-controls`.
    The questions of an accordion are there. One inside a heading becomes heading text; any other becomes a line (block). The
    remaining buttons (copy, search, close) are removed by `_DROP_TAGS`
    (Pinned by: test_text_html.py::TestCollapsedQuestionsAndAnswers).
    """
    buttons = soup.find_all("button")
    in_headings = _lying_inside(buttons, _HEADINGS)
    for button in buttons:
        if id(button) not in in_headings and (button.has_attr("aria-expanded") or button.has_attr("aria-controls")):
            button.name = "div"
    _unwrap_all([button for button in buttons if id(button) in in_headings])


def _unwrap_all(tags: list[Tag]) -> None:
    """`unwrap()` for each of `tags`, collected in document order: the element is taken out and its children stay in its place.
    It goes from the back and measures the child positions once per parent, like `_decompose_all` — unwrapping a later sibling
    leaves the position of an earlier one unchanged, and the children of an element are done before the element itself.

    bs4's `unwrap` looks up the position of the element among its siblings from the start: 40,000 buttons in one heading
    (703KB) took 11 seconds (Pinned by: test_audit_html_text.py::TestManyButtonsInsideOneHeading).
    """
    places: dict[int, dict[int, int]] = {}  # parent id → (child id → position)

    for tag in reversed(tags):
        parent = tag.parent
        if id(parent) not in places:
            places[id(parent)] = {id(child): index for index, child in enumerate(parent.contents)}
        index = places[id(parent)][id(tag)]
        tag.extract(_self_index=index)
        for child in reversed(tag.contents[:]):
            parent.insert(index, child)


def _folded_answers(soup: BeautifulSoup, cut: frozenset[int] = frozenset()) -> set[int]:
    """The 'folded answers' (`id`s) among hidden elements that are not to be removed. A saved FAQ keeps its answers folded with
    inline `display:none` and unfolds one when its question is clicked.

    Deliberately narrow — most hidden elements are menus, modals and tooltips and keep being removed. One is kept only when
    all of the following hold: it is a block with inline `display:none` (div, p, dd …; lists are excluded as dropdown menus),
    its immediately preceding sibling is an element that unfolds it (a link that goes nowhere else, a button, a heading, a dt,
    or an element wrapping just one of those), its name/role is not a menu, modal or tooltip, and less than half of its text
    is links (a bundle of links is a menu). Anything inside a hidden element is removed together with the outer one. An
    element cut at the end of the front part (`cut`, `_open_at_the_end`) is not kept, since its link ratio is unknown.
    """
    folded: set[int] = set()
    # Blocks inside a hidden block that was measured and is not kept: they go with it (`_drop_hidden`) and are not measured.
    # Measuring each of 4,000 nested blocks that are mostly links took 9.5 seconds (the text below it, once per level)
    # (Pinned by: test_audit_html_text.py::TestHiddenBlocksNestedInsideHiddenBlocks). Known accepted gap: answers folded
    # inside answers, each of them kept, are still measured once per level.
    removed: set[int] = set()
    for tag in soup.find_all(_FOLDED_TAGS):
        if id(tag) in removed:
            continue
        if id(tag) in cut or not _FOLDED_STYLE.search(str(tag.get("style", ""))) or tag.has_attr("hidden") or tag.get("aria-hidden") == "true":
            continue
        opener = tag.find_previous_sibling(True)
        if opener is None or not _is_opener(opener) or _is_overlay(tag):
            continue
        length = _solid_length(tag)
        if length and 2 * sum(_solid_length(link) for link in tag.find_all("a")) < length:
            folded.add(id(tag))
        elif _hidden_by_style(tag):
            removed.update(id(inner) for inner in tag.find_all(_FOLDED_TAGS))
    return folded


def _is_opener(tag: Tag) -> bool:
    # A wrapper around a single element is peeled off: <p><a href="#">질문</a></p>, <div class="q"><button>…</button></div>
    for _ in range(3):
        children = [child for child in tag.contents if isinstance(child, Tag) or str(child).strip()]
        if tag.name in _OPENER_TAGS or len(children) != 1 or not isinstance(children[0], Tag):
            break
        tag = children[0]
    if tag.name == "a":
        return _STAYING_LINK.match(str(tag.get("href", ""))) is not None
    return tag.name in _OPENER_TAGS and bool(tag.get_text(strip=True))


def _is_overlay(tag: Tag) -> bool:
    classes = tag.get("class") or []
    name = " ".join([*(classes if isinstance(classes, list) else [str(classes)]), str(tag.get("id", ""))])
    return str(tag.get("role", "")).strip().lower() in _OVERLAY_ROLES or _OVERLAY_NAME.search(name) is not None


def _put_form_values(soup: BeautifulSoup) -> None:
    """Replaces form elements with the values that were shown on screen: the value of a text input, the text of a textarea,
    the chosen option of a select, ☒/☐ for checkboxes and radio buttons.

    Removing them whole made the values entered into saved e-approval documents and application forms (name, reason, chosen
    type) vanish. Fields without a value, invisible values (hidden/password) and buttons are not content and are left out. A
    placeholder is not an entered value. Hidden elements have already been removed
    (Pinned by: test_text_html.py::TestFormValues).

    The element is not detached; it is turned in place into a `<span>` without attributes and the text is put inside.
    `replace_with`/`decompose` search the parent's child list linearly for their own position, which was quadratic in the
    number of controls of one form (16,000 took 5.6 s). A span is an element rendering merely descends into, so the result is
    the same as placing the text pieces directly
    (Pinned by: TestFormValues::test_thousands_of_form_controls_are_replaced_in_linear_time).
    """
    for control in soup.find_all(["input", "select", "textarea"]):
        if control.name == "input":
            values = _input_values(control)
        elif control.name == "select":
            values = _selected_options(control)
        else:
            values = control.get_text().strip().split("\n")
        values = [" ".join(value.split()) for value in values]
        control.clear()
        control.attrs = {}
        control.name = "span"
        if not any(values):
            continue

        # Space on both sides: keeps the value from sticking to neighbouring fields and labels. Multiple lines (textarea) are
        # joined with line-break elements.
        control.append(" ")
        for index, value in enumerate(values):
            if index:
                control.append(soup.new_tag("br"))
            control.append(value)
        control.append(" ")


def _put_symbol_fonts(soup: BeautifulSoup) -> None:
    """Replace characters drawn with a symbol font (Wingdings, Wingdings 2, Symbol) using that font's table (`_symbols.SYMBOL_GLYPHS`, the
    same one DOCX and PPTX use). The font comes from font-family / font in style or from `<font face>`; when an inner element sets another
    font, the text inside it has that font.

    .htm files saved by Word and Outlook HTML mail write bullets and check boxes as numbered glyphs inside the font (`§`, `Ø`, `þ`, `¨`,
    Wingdings 2 `R`), so checked and unchecked items came out as `þ` and `¨` (Pinned by: test_core_html.py::TestSymbolFontsInHtml). Only
    Latin-1 and private-use (U+F0xx) characters are replaced; whitespace (the `&nbsp;` Word puts after a bullet — € in the Symbol table),
    characters missing from the table and Hangul are left as is.
    """
    if soup.find(lambda node: _declared_font(node) in SYMBOL_GLYPHS) is None:
        return

    # Walk once from top to bottom, handing the font down (linear even in deeply nested documents).
    stack: list[tuple[Tag, str | None]] = [(soup, None)]
    places: _Places = {}

    while stack:
        node, font = stack.pop()
        for child in list(node.children):
            if isinstance(child, Tag):
                declared = _declared_font(child)
                stack.append((child, font if declared is None else declared))
            elif type(child) is NavigableString and font in SYMBOL_GLYPHS:
                _replace(child, "".join(_symbol_character(font, character) for character in child), places)


def _declared_font(tag: Tag) -> str | None:
    """Font name the element declares (lowercase; the first name when there are several). None when it declares none; an empty string for
    a shorthand property that is not a symbol font (`font: 7pt "Times"`)."""
    if tag.name == "font" and tag.get("face"):
        return str(tag["face"]).split(",")[0].strip(" '\"").lower()
    style = tag.get("style")
    if not style:
        return None
    family = _FONT_FAMILY.search(str(style))
    if family:
        return family.group(1).split(",")[0].strip(" '\"").lower()
    shorthand = _FONT_SHORTHAND.search(str(style))
    if shorthand is None:
        return None
    value = shorthand.group(1).lower()
    return next((name for name in sorted(SYMBOL_GLYPHS, key=len, reverse=True)
                 if re.search(rf"(?<![\w-]){name}(?![\w-])", value)), "")


def _symbol_character(font: str, character: str) -> str:
    code = ord(character)
    if character.isspace() or not (code < 0x100 or 0xF000 <= code <= 0xF0FF):
        return character
    return symbol_glyph(font, code) or character


def _input_values(control: Tag) -> list[str]:
    kind = str(control.get("type") or "text").strip().lower()
    if kind in _VALUELESS_INPUTS:
        return []
    if kind in {"checkbox", "radio"}:
        if str(control.get("role") or kind).strip().lower() not in _CHOICE_ROLES:
            return []  # Wikipedia's per-article checkboxes that open and close dropdowns (role="button") came out as ☐
        return [_CHECKED if control.has_attr("checked") else _UNCHECKED]
    return [str(control.get("value") or "")]


def _selected_options(control: Tag) -> list[str]:
    """Chosen options. A single-choice list falls back to its first option when nothing is chosen (that is what browsers show). A
    multiple-choice list gives every chosen option."""
    options = control.find_all("option")
    chosen = [option for option in options if option.has_attr("selected")]
    if not control.has_attr("multiple"):
        chosen = chosen[-1:] or options[:1]
    return [", ".join(text for text in (" ".join(option.get_text().split()) for option in chosen) if text)]


def _mark_scripts(soup: BeautifulSoup) -> None:
    """Turn superscripts and subscripts into notation (`10^6`, `H_2O`). Scripts inside a script are converted first — collecting the outer
    one's text whole gives `e^{x2}`
    (Pinned by: test_text_html.py::TestNestedScriptsAndStrikethrough::test_nested_superscript_marks_the_inner_one_first)."""
    places: _Places = {}
    scripts = soup.find_all(["sup", "sub"])
    # Not inside a code block: `x<sup>2</sup>` there is code as written, and a mark would change it
    # (Pinned by: test_audit_web_fixes.py::TestNoMarksInsideCode)
    in_code = _lying_inside(scripts, _CODE_BLOCKS)

    for outer in scripts:
        if outer.parent is None or id(outer) in in_code:
            continue  # already replaced together with the outer script, or code
        for script in [*reversed(outer.find_all(["sup", "sub"])), outer]:
            before = script.previous_sibling
            marked = format_script(script.get_text(), SUPERSCRIPT if script.name == "sup" else SUBSCRIPT,
                                   before.get_text() if before is not None else "")
            _replace(script, marked, places)


def _mark_struck_text(soup: BeautifulSoup, *, links: bool = False, emphasis: bool = False) -> None:
    """Mark struck-through text (`<del>`, `<s>`, `<strike>`, CSS `text-decoration: line-through`) as `~~text~~`. As plain text,
    `정가 10,000원 8,000원` (list price 10,000 won 8,000 won) reads as two valid values.

    The marks go before the first character and after the last one (whitespace stays outside). A strikethrough inside a strikethrough and
    one with no text are not marked (Pinned by: test_text_html.py::TestNestedScriptsAndStrikethrough).
    A pair never crosses a table cell boundary (`_struck_runs`) — a strikethrough over a whole row or table became `| ~~가 | 100~~ |`, and
    when a table inside a cell was kept as an HTML table the closing mark landed in the inner table's last cell.

    The runs of the whole document are found first, runs with nothing between them are joined (`_joined_runs`), and the marks
    are written at the end, all together (`_write_marks`). `links` / `emphasis`: whether links and emphasis are written as
    Markdown — where they are, their marks can lie between two runs.
    """
    nested: set[int] = set()
    # Text inside a code block gets no marks — it is code as written, and the `<del>` lines of a diff view in `<pre>` came out as
    # `~~…~~` inside the fence. A strikethrough holding a code block is marked in pieces around it (`_struck_runs`)
    # (Pinned by: test_audit_web_fixes.py::TestNoMarksInsideCode).
    struck_elements = soup.find_all(_is_struck)
    if not struck_elements:
        return
    in_code = _lying_inside(struck_elements, _CODE_BLOCKS)
    holding_code = _holding(soup.find_all(_CODE_BLOCKS))
    runs: list[_Run] = []
    for struck in struck_elements:
        if id(struck) in nested or id(struck) in in_code or struck.name in _CODE_BLOCKS:
            continue
        nested.update(id(inner) for inner in struck.find_all(_is_struck))
        strings = {id(string) for string in struck.find_all(string=True) if string.strip()}
        runs.extend(_struck_runs(struck, strings, holding_code))
    spaces: list[tuple[Tag, bool]] = []
    if len(runs) > 1:
        runs, spaces = _joined_runs(soup, runs, links=links, emphasis=emphasis)
    _write_marks(runs, spaces)


def _holding(nodes: list[Tag]) -> set[int]:
    """`id`s of the ancestors of `nodes`. Each node climbs until it reaches an element already found, so it is linear time —
    asking `find("pre")` of every table nested in a struck cell walked the whole subtree each time: 8,000 nested tables took
    19 seconds (Pinned by: test_audit_web_fixes.py::TestNoMarksInsideCode::test_deeply_nested_tables_in_a_struck_cell_take_linear_time)."""
    found: set[int] = set()
    for node in nodes:
        parent = node.parent
        while parent is not None and id(parent) not in found:
            found.add(id(parent))
            parent = parent.parent
    return found


def _struck_runs(struck: Tag, strings: set[int], holding_code: set[int] = frozenset()) -> list[_Run]:
    """Split the text nodes inside a strikethrough into consecutive runs per table cell. Each run gets one pair of `~~`.

    A table held by a strikethrough inside a table cell is part of the cell text as a whole — its text nodes are grouped with the table
    (`(text, table)`) and the marks go outside the table, before and after. Flattened into cell text that is `~~… 20~~` as before; kept as
    an HTML table it is `~~… <table>…</table>~~`. A table held by a strikethrough outside any cell, or a strikethrough on a row or table
    itself, is paired per cell. The document tree is scanned once — no climbing up the ancestors for every text node in deep nesting.
    A code block (`<pre>`) is not descended into and ends the run: the text before it and the text after it are paired
    separately, and the fence gets no mark inside (Pinned by: test_audit_web_fixes.py::TestNoMarksInsideCode). A table inside a
    cell that holds a code block is not grouped as a whole, its cells are paired one by one — grouped, the marks around it
    spanned the fence of a code block drawn from a layout table.
    """
    base = struck if struck.name in ("td", "th") else struck.find_parent(["td", "th"])
    runs: list[_Run] = []
    last_cell: object = None
    stack: list[tuple[object, Tag | None, Tag | None]] = [(child, None, base) for child in reversed(struck.contents)]

    while stack:
        node, table, cell = stack.pop()
        if isinstance(node, Tag):
            if table is None and node.name in _CODE_BLOCKS:
                last_cell = _RUN_BREAK  # the next text starts a new run
                continue
            if table is None and node.name == "table" and cell is not None and id(node) not in holding_code:
                table = node
            elif table is None and node.name in ("td", "th"):
                cell = node
            stack.extend((child, table, cell) for child in reversed(node.contents))
            continue
        if id(node) not in strings:
            continue
        if not runs or cell is not last_cell:
            runs.append([])
            last_cell = cell
        runs[-1].append((node, table))
    return runs


class _Gap:
    """What lies between the end of one struck run and the start of the next one (`_joined_runs`)."""

    __slots__ = ("apart", "between", "closed", "entered", "left", "opened", "text")

    def __init__(self) -> None:
        self.apart = False  # whitespace, a line break, a block or a cell boundary: the two runs are not written side by side
        self.text = False  # text that is not whitespace
        self.left: Tag | None = None  # the outermost table that ended
        self.entered: list[Tag] = []  # tables that began and have not ended, outermost first
        self.between: Tag | None = None  # a table that began and ended
        # Link and emphasis spans that ended / that began and have not ended. A span is [texts before it, texts before its
        # end, elements before it, elements before its end, element name]
        self.closed: list[list] = []
        self.opened: list[list] = []


def _joined_runs(soup: BeautifulSoup, runs: list[_Run], *, links: bool, emphasis: bool) -> tuple[list[_Run], list[tuple[Tag, bool]]]:
    """Join struck runs (`_struck_runs`, in document order) that have nothing between them into one run, so that they get one
    pair of marks — `<del>가</del><del>나</del>` is `~~가나~~`, the same as consecutive struck runs of a DOCX
    (`_inline.resolve_strikes`). Marked one by one they gave `~~가~~~~나~~`, and four tildes in a row are not a strikethrough
    mark (Pinned by: test_audit_web_fixes.py::TestAdjacentStruckRuns).

    Two runs are joined only when no text (not even whitespace), line break, block or cell boundary lies between them: with
    one of those the marks are apart already and stay as they are. Nor are they joined when a link or emphasis span that is
    written begins or ends between the two and holds an end of the joined pair: its own mark lies between the two pairs
    (`~~가~~**~~나~~**`), and one pair would cross the span, which then loses its emphasis (`_inline._without_edge_strike`).
    A span that lies wholly inside the second run is no obstacle — its half of the pair is moved out in front of it, next
    to the first run's (`~~가~~~~**나** 다~~`) — and neither is a span that is not written (no emphasis in a heading or in
    inline code, a span holding a block, an `<a>` without an address).

    The second value is the tables that need a space next to them (the table, True for after it). A table inside a cell is
    flattened into the cell text with nothing around it (`_html_render._cell_text`), so a run ending in its last cell met the run
    that follows the table (`~~20~~~~끝~~`). Those two cannot be one pair — when the table is drawn as a table the pair
    would cross its boundary — so a space goes outside the table, where a browser breaks the line anyway.

    The document is walked once, element ends included, and nothing is looked up per run.
    """
    first_of = {id(run[0][0]): index for index, run in enumerate(runs)}
    last_of = {id(run[-1][0]): index for index, run in enumerate(runs)}
    gaps: list[_Gap | None] = [None] * len(runs)  # gaps[i]: between runs[i - 1] and runs[i]
    first_text = [0] * len(runs)  # how many texts come before the run's first one
    last_text = [0] * len(runs)
    gap = _Gap()
    texts = 0  # texts (not whitespace) seen so far
    plain = 0  # headings and inline code we are inside of: no emphasis is written there (`_html_render._render_nodes`)
    elements = 0
    # A span is written only when all its words are in one block (`_inline.resolve_inline`): where the blocks begin and end
    # (in texts seen), and where the tables, lists and code blocks begin (in elements seen)
    block_edges: list[int] = []
    leaf_blocks: list[int] = []
    stack: list[object] = [soup]

    while stack:
        node = stack.pop()
        if isinstance(node, NavigableString):
            if not node:
                continue
            if not node.strip():
                gap.apart = True
                continue
            index = first_of.get(id(node))
            if index is None:
                gap.apart = gap.text = True
            else:
                gaps[index] = gap
                gap = _Gap()  # what comes up to the end of the run is inside it
                first_text[index] = texts
            index = last_of.get(id(node))
            if index is not None:
                gap = _Gap()
                last_text[index] = texts
            texts += 1
        elif isinstance(node, tuple):  # the end of an element
            tag, span, apart = node
            gap.apart = gap.apart or apart
            if apart and tag.name not in _LINE_PIECES:
                block_edges.append(texts)
            if span is not None:
                span[1], span[3] = texts, elements + 1
                if gap.opened and gap.opened[-1] is span:
                    gap.opened.pop()  # began and ended between the two runs: it holds no text, so nothing is written for it
                else:
                    gap.closed.append(span)
            elif tag.name == "table":
                if gap.entered:
                    gap.between = gap.entered.pop()
                else:
                    gap.left = tag
            elif tag.name in _PLAIN_INSIDE:
                plain -= 1
        elif isinstance(node, Tag):
            name = node.name
            span = None
            apart = name in _BETWEEN_RUNS or "role" in node.attrs  # an ARIA role can make any element a table or a cell
            gap.apart = gap.apart or apart
            elements += 1
            if apart and name not in _LINE_PIECES:
                block_edges.append(texts)
            if name in _SPAN_BREAKERS:
                leaf_blocks.append(elements)
            if name == "table":
                gap.entered.append(node)
            elif name in _PLAIN_INSIDE:
                plain += 1
            elif not apart and ((name == "a" and links and node.get("href"))
                                or (name in _EMPHASIS_TAGS and emphasis and not plain)):
                span = [texts, texts, elements, elements, _EMPHASIS_TAGS.get(name, name)]
                gap.opened.append(span)
            stack.append((node, span, apart))
            stack.extend(reversed(node.contents))

    def marked(span: list) -> bool:
        """Whether the mark of a span that holds one end of the joined pair is written between the two runs. Not when the
        span is not written (it holds a table, list or code block, or a block begins or ends between two of its words). A
        picture in the span does not make it unwritten: a link around `[Image: …]` is written, and a pair joined over its
        bracket opened inside the link text and closed outside it
        (Pinned by: test_audit_web_fixes.py::TestAdjacentStruckRuns::test_runs_are_not_joined_over_the_edge_of_a_span_with_a_picture)."""
        leaf = bisect.bisect_right(leaf_blocks, span[2])
        edge = bisect.bisect_right(block_edges, span[0])
        return not ((leaf < len(leaf_blocks) and leaf_blocks[leaf] < span[3])
                    or (edge < len(block_edges) and block_edges[edge] < span[1]))

    joined: list[_Run] = [runs[0]]
    spaces: list[tuple[Tag, bool]] = []
    start = first_text[0]  # of the run that the next one may be joined to
    for index in range(1, len(runs)):
        gap, run = gaps[index], runs[index]
        if gap is None:
            joined.append(run)
            start = first_text[index]
            continue
        table, behind = (gap.left, True) if gap.left is not None else (gap.entered[0], False) if gap.entered else (gap.between, True)
        if table is not None and not gap.text:
            beside = table.next_sibling if behind else table.previous_sibling
            if not (isinstance(beside, NavigableString) and (beside[:1] if behind else beside[-1:]).isspace()):
                spaces.append((table, behind))
        before, after = str(joined[-1][-1][0]), str(run[0][0])
        # A bold span that ends and one that begins there are written as one span (`_inline.resolve_inline`): no mark between
        ended = sorted(span[4] for span in gap.closed if span[0] <= start and marked(span))
        begun = sorted(span[4] for span in gap.opened if span[1] > last_text[index] and marked(span))
        if (not gap.apart and not before[-1].isspace() and not after[0].isspace() and ended == begun and "a" not in ended):
            joined[-1].extend(run)
        else:
            joined.append(run)
            start = first_text[index]
    return joined, spaces


def _write_marks(runs: list[_Run], spaces: list[tuple[Tag, bool]]) -> None:
    """Put `~~` before the first character of each run and after its last (whitespace stays outside). When that text is in a
    table inside a cell, put it before/after the table. `spaces`: the tables that get a space before / after (True) them,
    outside the mark (`_joined_runs`).

    Everything is written at the end, one parent at a time: its child list is built anew in one pass and the new text
    nodes are linked into the tree here (`_link_between`). bs4's `replace_with` / `insert_before` look up their own
    position in the parent's child list from the start, and taking a child out or putting one in shifts the rest of the
    list, so one strikethrough holding many code blocks — one run per piece of text between them — took time quadratic in
    their number: 40,000 blocks 23 seconds, now 1. With the positions measured once per parent (`_decompose_all`) but each
    node still taken out and put in, 160,000 blocks took 19 seconds, now 7 (6 of them outside the marking)
    (Pinned by: test_audit_web_fixes.py::TestNoMarksInsideCode::test_many_code_blocks_in_one_strikethrough_take_linear_time).
    """
    texts: dict[int, tuple[NavigableString, str]] = {}
    # node id → (where, text) in document order. Where: -1 a space before the node and 0 a mark before it, 1 the text that takes
    # its place, 2 a mark after it and 3 a space after it
    edits: dict[int, list[tuple[int, str]]] = {}
    parents: dict[int, Tag] = {}

    def note(node: PageElement, where: int, text: str) -> None:
        if node.parent is not None:
            edits.setdefault(id(node), []).append((where, text))
            parents[id(node.parent)] = node.parent

    for table, behind in spaces:
        note(table, 3 if behind else -1, " ")
    for run in runs:
        (first, first_table), (last, last_table) = run[0], run[-1]
        if first_table is not None:
            note(first_table, 0, _STRIKE)
        else:
            text = str(first)
            texts[id(first)] = (first, f"{text[:len(text) - len(text.lstrip())]}{_STRIKE}{text.lstrip()}")
        if last_table is not None:
            note(last_table, 2, _STRIKE)
        else:
            text = texts.get(id(last), (last, str(last)))[1]
            texts[id(last)] = (last, f"{text.rstrip()}{_STRIKE}{text[len(text.rstrip()):]}")
    for node, text in texts.values():
        note(node, 1, text)

    for parent in parents.values():
        children: list[PageElement] = []
        for child in parent.contents:
            placed = False
            last = child  # what the nodes after the child are put behind
            for where, text in sorted(edits.get(id(child), ())):
                new = NavigableString(text)
                if where <= 1:
                    _link_between(new, parent, child.previous_sibling, child)
                    if where == 1:  # … and the text node it takes the place of goes: it has nothing below it
                        new.next_sibling, new.next_element = child.next_sibling, child.next_element
                        if new.next_sibling is not None:
                            new.next_sibling.previous_sibling = new
                        if new.next_element is not None:
                            new.next_element.previous_element = new
                        child.parent = child.previous_sibling = child.next_sibling = None
                        child.previous_element = child.next_element = None
                        placed, last = True, new
                else:
                    if not placed:
                        children.append(child)
                        placed = True
                    _link_between(new, parent, last, last.next_sibling)
                    last = new
                children.append(new)
            if not placed:
                children.append(child)
        parent.contents[:] = children


def _link_between(new: NavigableString, parent: Tag, previous: PageElement | None, following: PageElement | None) -> None:
    """Make `new` the child of `parent` between two children that are next to each other (None at either end), as bs4's
    `insert` does: siblings and the order of the elements in the document. The caller puts it into `parent.contents`."""
    if following is not None:
        before = following.previous_element
    else:
        before = parent if previous is None else previous
        while previous is not None and isinstance(before, Tag) and before.contents:
            before = before.contents[-1]
    after = before.next_element
    new.parent = parent
    new.previous_sibling, new.next_sibling = previous, following
    new.previous_element, new.next_element = before, after
    before.next_element = new
    if after is not None:
        after.previous_element = new
    if previous is not None:
        previous.next_sibling = new
    if following is not None:
        following.previous_sibling = new


def _is_struck(tag: Tag) -> bool:
    return tag.name in _STRIKE_TAGS or ("style" in tag.attrs and _STRIKE_STYLE.search(str(tag["style"])) is not None)


def _with_table_around_bare_rows(text: str) -> str:
    """An 'Excel download' that starts at `<tr>` with no `<table>` gets a table wrapped around everything from the first `<tr>` to the
    last `</tr>`.

    The HTML parser drops row and cell tags outside a table, so the cell text ran together on one line
    (Pinned by: test_text_html.py::test_bare_table_rows_without_a_table_element_are_rendered_as_a_table).
    """
    if _TABLE_TAG.search(text):
        return text
    first = _ROW_START.search(text)
    last = text.lower().rfind("</tr>")
    if first is None or last < first.start():
        return text
    end = last + len("</tr>")
    return f"{text[:first.start()]}<table>{text[first.start():end]}</table>{text[end:]}"


def _image_placeholder(image: Tag, extraction: Extraction, options: ConvertOptions,
                       saved_picture: Callable[[str], bytes | None] | None = None) -> str:
    """Take out only data: URI images embedded in the body and picture parts saved alongside in MHTML (`saved_picture`). External URLs are
    not fetched (Deliberate: SSRF risk — the server would be made to request arbitrary addresses). External images keep only their alt text.

    SVG data: URIs (`data:image/svg+xml`) are not taken out; like external pictures they keep only the alt text — they are the same icons
    as an inline `<svg>` (`_DROP_TAGS`) and cannot be decoded anyway. Every icon produced an `[Image]` and a 'WMF/EMF vector etc.' warning
    (Pinned by: test_core_mail_parts_and_icons.py::TestSvgDataUriIcons).
    Lazy-loading placeholder pictures (the real address is in `data-src` etc., or a GIF/PNG of 2 pixels or less) also keep only the alt
    text, like external pictures. Taken out, they were filtered as small pictures (`converter._prepare_images`) and even the alt text (the
    figures of a chart) was erased
    (Pinned by: test_core_html_mail_hidden_and_frames.py::TestLazyLoadedPicturePlaceholders).
    """
    alt = " ".join(str(image.get("alt", "")).split())
    # The alt text written inside `[Image: …]` has its brackets escaped like link text (`_inline._escape_link_text`): an alt such
    # as `a](https://evil) [b` closed the bracket and made a link (Pinned by: test_audit_web_fixes.py::TestAltTextCannotMakeALink)
    shown = f" [Image: {_escape_link_text(alt)}] " if alt else ""
    source = str(image.get("src", ""))
    # The scheme and the base64 mark are compared without case and leading whitespace, as the SVG check always was:
    # `src=" data:image/png…"` and `DATA:image/png…` gave no picture
    # (Pinned by: test_audit_html_text.py::TestDataUriWrittenWithCapitalsOrLeadingSpace)
    lowered = source.lstrip().lower()
    if lowered.startswith("data:image/svg") or (lowered.startswith("data:") and _is_lazy(image)):
        return shown
    if options.extract_images and lowered.startswith("data:image/") and ";base64," in lowered:
        try:
            data = base64.b64decode(source.split(",", 1)[1], validate=False)
        except (binascii.Error, ValueError):
            data = b""
        if _is_spacer(data):
            return shown
        if data:
            return f" {extraction.add_image(data, alt=alt)} "
    elif saved_picture is not None and source:
        data = saved_picture(source.strip())
        if data:
            return f" {extraction.add_image(data, alt=alt)} "
    return shown


def _is_lazy(image: Tag) -> bool:
    """Whether the picture is lazy-loaded: the real address sits in an attribute such as `data-src`, `data-original` or `data-lazy-src`
    (name contains src / original / lazy, value is an address that is not data: — text containing `.` or `/`). The data: URI in `src` is
    then a placeholder picture."""
    for name, value in image.attrs.items():
        text = str(value).strip()
        if (name.startswith("data-") and any(word in name for word in _LAZY_SOURCE_WORDS) and not text.startswith("data:")
                and ("." in text or "/" in text)):
            return True
    return False


def _is_spacer(data: bytes) -> bool:
    """Whether this is a GIF/PNG of at most 2 pixels in width and height (transparent placeholder or spacer picture). Only the size in the
    header is read."""
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        width, height = int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    elif data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    else:
        return False
    return width <= _SPACER_SIDE and height <= _SPACER_SIDE


def _hidden_by_style(tag: Tag) -> bool:
    return tag.attrs is not None and (tag.has_attr("hidden") or bool(_HIDDEN_STYLE.search(str(tag.get("style", "")))))


def _drop_hidden(soup: BeautifulSoup, kept: set[int] = frozenset(), whole: bool = True) -> None:
    """Remove hidden elements: first `hidden`, `display:none` and `visibility:hidden` (folded answers in `kept` stay), then those
    `aria-hidden="true"` elements that do not hold the main content.

    What stays despite aria-hidden is an app root hidden by a modal library: it is a content element (main / article), holds main, article
    or h1, or is a child of body holding at least half of the document's text (only when `whole` — see `_extract_front`). react-modal and
    MUI put aria-hidden on `#root` / `#__next` while a modal is open (it is still visible on screen). A page saved in that state lost its
    whole body and kept only the modal text (a cookie notice)
    (Pinned by: test_core_html_mail_hidden_and_frames.py::TestAriaHiddenAppRootUnderAModal). Small aria-hidden elements such as icon
    glyphs and cloned slides are removed. 'Half' is measured on the text after hidden elements are removed — counting the text of a hidden
    terms-of-service layer too got the app root removed. Elements holding the content are collected by climbing up just once from each
    content element, and the body text is measured only once — re-measuring for every aria-hidden element took 47 seconds on 468KB.
    """
    _decompose_all(_hidden_nodes(soup, [tag for tag in soup.find_all(_hidden_by_style) if id(tag) not in kept]))

    marked = soup.find_all(attrs={"aria-hidden": "true"})
    if not marked:
        return
    holders = _content_holders(soup)
    body = soup.body
    page = max(_solid_length(body), 1) if body is not None else 1
    _decompose_all([tag for tag in marked if id(tag) not in holders
                    and not (whole and body is not None and tag.parent is body and 2 * _solid_length(tag) >= page)])


def _hidden_nodes(soup: BeautifulSoup, hidden: list[Tag]) -> list[PageElement]:
    """What to remove for the hidden elements `hidden` (in document order), in document order. An element hidden only by
    `visibility:hidden` keeps the descendants that set `visibility:visible` again, as a browser shows them: only the other
    content of that element is removed, and the elements on the way down stay as shells. `visibility:hidden` on html or body is
    not hiding — an anti-flicker snippet hides the page until a script shows it, and the whole page came out as 'No text was
    extracted' (Pinned by: test_audit_web_fixes.py::TestVisibilityHiddenWithAVisibleDescendant). `hidden`, `display:none` and
    `content-visibility:hidden` remove the element whole: nothing inside can show again.
    Documents without such a case take the old path unchanged (the elements themselves)."""
    if not hidden:
        return []
    visibles = soup.find_all(style=_VISIBLE_STYLE)
    above: set[int] = set()  # elements with a `visibility:visible` descendant (each climbs once, stopping at one already seen)
    for visible in visibles:
        node = visible.parent
        while node is not None and id(node) not in above:
            above.add(id(node))
            node = node.parent
    special = [tag for tag in hidden if not tag.has_attr("hidden") and not _UNDOABLE_HIDING.search(str(tag.get("style", "")))
               and (tag.name in _PAGE_ROOTS or id(tag) in above)]
    if not special:
        return list(hidden)
    visible_ids = {id(visible) for visible in visibles}
    special_ids = {id(tag) for tag in special}
    removed = {id(tag) for tag in hidden} - special_ids
    for tag in special:
        if tag.name in _PAGE_ROOTS:
            continue
        # Below `tag`: an element with a visible descendant is a shell (descended into), a visible element stays, and another
        # element handled here on its own (`special`) does its own part; everything else goes
        stack = list(tag.contents)
        while stack:
            child = stack.pop()
            if id(child) in visible_ids or id(child) in special_ids:
                continue
            if id(child) in above:
                stack.extend(child.contents)
            else:
                removed.add(id(child))
    return [node for node in soup.descendants if id(node) in removed]


def _decompose_all(nodes: list[PageElement], *, detach_only: bool = False) -> None:
    """Remove `nodes`, collected in document order, from the tree (elements are `decompose`d, text nodes detached). Removes from the back
    and measures the child positions once per parent — an earlier sibling's position is unchanged when a later sibling is removed.

    bs4's `decompose` / `extract` look up their own position in the parent's child list from the start, so many removals under one parent
    took time quadratic in their number: a 4.5MB order table with a `display:none` detail row attached to every row took 17 seconds,
    64,000 paragraphs and buttons 30 seconds
    (Pinned by: test_core_html_raw_text_and_removal.py::TestManyRemovedSiblingsTakeLinearTime). Nodes inside an already removed ancestor
    are skipped. With `detach_only` nodes are only detached, not destroyed (they will be moved elsewhere).
    """
    places: dict[int, dict[int, int]] = {}  # parent id → (child id → position)

    for node in reversed(nodes):
        parent = node.parent
        # `Tag.decomposed` reads `_decomposed` with getattr, and on a tag that was never decomposed the attribute is missing, so
        # bs4 takes it for the name of a child tag and searches the whole subtree for one: asking 5,000 open tables nested in
        # each other took 10 seconds. The flag is an instance attribute once set, so it is read from the instance
        # (Pinned by: test_text_html.py::TestContentOutsideTableCells::test_deeply_chained_open_tables_are_read_quickly).
        if parent is None or (isinstance(node, Tag) and vars(node).get("_decomposed", False)):
            continue
        if id(parent) not in places:
            places[id(parent)] = {id(child): index for index, child in enumerate(parent.contents)}
        node.extract(_self_index=places[id(parent)][id(node)])
        if isinstance(node, Tag) and not detach_only:
            node.decompose()
