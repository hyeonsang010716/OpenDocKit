"""What the HTML modules share: the tag sets and limits used by more than one of them, the state of the rendering in
progress (nesting depths, whether a cell or a heading is being written), and the placeholders that keep a piece of
finished text in the tree.
"""

from contextvars import ContextVar
from dataclasses import dataclass
from functools import lru_cache
import re

from bs4 import BeautifulSoup, Tag, XMLParsedAsHTMLWarning
from bs4.element import NavigableString, PageElement, Script, Stylesheet
from lxml import etree

from opendockit.models import quiet_warnings


# The modules that hand markup to the HTML parser, as the regex `models.quiet_warnings` matches the module a warning comes
# from. bs4 warns about a page that starts with an XML declaration at the place that called it, and the filter that keeps
# that warning from the caller of `convert` names these modules only
# (Pinned by: test_core_html.py::TestPagesWithAnXmlDeclaration).
_PARSER_CALLERS = r"opendockit\.extractors\._?html"

# Which tree builder reads a page like a browser.
#
# Malformed and multi-part pages (content after `</html>`, a second concatenated document, raw-text elements such as
# <textarea>/<xmp>, the later parts of a mail) are only read whole by a parser that follows HTML5. libxml2 has the HTML5
# tokenizer from 2.14, which lxml's Linux and macOS wheels bundle from lxml 6.0 — but its Windows wheels still bundle 2.11,
# and no lxml release changes that (7.0.0b1 was measured to bundle 2.11.9 as well). There, html5lib builds the tree instead:
# it is a full HTML5 parser, and bs4 can use it to build the same kind of tree. It costs 2 to 4 times the parse of an
# ordinary page, and far more on the two shapes it is quadratic on (`_HTML5LIB_MAX_CHARS`, `_HTML5LIB_MAX_DEPTH`), which is
# why it is not used where libxml2 is new enough.
# Where neither is available the old reading stays, and `html._OLD_PARSER_WARNING` says what may be missing
# (Pinned by: test_core_html.py::TestOlderHtmlParser).
HTML5_TOKENIZER = etree.LIBXML_VERSION >= (2, 14)
try:
    import html5lib as _html5lib
except ImportError:   # not installed: only the default dependency on Windows brings it
    _html5lib = None
TREE_BUILDER = "lxml" if HTML5_TOKENIZER or _html5lib is None else "html5lib"
# Whether the pages above are read whole, whichever builder does it.
READS_LIKE_A_BROWSER = HTML5_TOKENIZER or _html5lib is not None


# How large a page html5lib may read. Its tokenizer is Python, and the time it spends on the text an unclosed raw-text
# element (<textarea>, <xmp>, <plaintext>) swallows grows with the square of that text: measured on Windows 11, a page of
# 272 KB took 0.29 s, one of 547 KB 4.1 s and one of 1.12 MB 36.8 s, where libxml2 took 0.05 s, 0.05 s and 0.14 s. An
# ordinary page, attributes and all, costs only 2 to 4 times as much, but the shape cannot be known before parsing, so the
# limit is on size. Past it the page is read by the older parser and reported, which is what an install without html5lib
# does: a large page is never slower than it was
# (Pinned by: test_core_html.py::TestLargePagesKeepTheOlderParser).
_HTML5LIB_MAX_CHARS = 256 * 1024

# The builder for the document being read — the page, or one body of a mail — set by `html._read_one_document` from that
# document's own text, never from the file it came in (a mail's attachments are not HTML). One document is read by one
# builder from beginning to end: the front check of a large page parses a slice of it (`_extract_front`), and a slice small enough for
# html5lib while the whole page was not gave the two readings different text, so a limit met by the whole conversion was
# reported as exceeded (Pinned by:
# test_core_html_raw_text_and_removal.py::TestUnclosedRawTextElementInALargePage::test_textarea_closed_after_the_front_is_not_read_as_html).
builder_for_document: ContextVar[str | None] = ContextVar("builder_for_document", default=None)


# How deeply a page html5lib reads may nest. Its tree construction walks the open elements for every tag, so the time grows
# with the square of the nesting: measured on Windows 11, 2,000 nested `<div>` took 0.28 s, 4,000 took 1.05 s and 8,000 took
# 6.18 s, where libxml2 took 0.01 s, 0.02 s and 0.05 s — and closing every one of them makes no difference (8,000 closed:
# 5.86 s). Size does not catch this: the 8,000-deep page is 39 KB. Real pages nest a few tens deep
# (Pinned by: test_core_html.py::TestDeeplyNestedPagesKeepTheOlderParser).
_HTML5LIB_MAX_DEPTH = 1_000

# Elements left out of the depth estimate below, because markup leaves them open as a matter of course and HTML closes them
# on its own: counting them would read an ordinary page of unclosed `<p>` as thousands deep and send it to the older parser,
# which is the very markup html5lib is here for. Void elements never nest at all.
_SELF_CLOSING = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source",
                           "track", "wbr"})
_AUTO_CLOSING = frozenset({"p", "li", "dt", "dd", "option", "optgroup", "tr", "td", "th", "thead", "tbody", "tfoot",
                           "caption", "colgroup", "rt", "rp", "html", "head", "body"})
_TAG = re.compile(r"<(/?)([a-zA-Z][^\s/>]*)")

# How many raw-text elements (<textarea>, <xmp>, <plaintext>) may be left open before the page goes to the older parser.
# Each one swallows the rest of the document, and html5lib spends time on that text in proportion to its square, so a page
# that leaves thousands of them open is quadratic in their number as well: 2,000 took 0.6 s and 8,000 took 9.1 s. The number
# is `_html_dom._MAX_REOPENED`, the point at which the repair pass stops re-reading what was swallowed anyway, so nothing is
# given up that was being repaired
# (Pinned by: test_core_html_mail_hidden_and_frames.py::TestRawTextElementsLeftOpen::test_many_unclosed_elements_take_linear_time).
_HTML5LIB_MAX_RAW_TEXT_LEFT_OPEN = 16
_RAW_TEXT = frozenset({"textarea", "xmp", "plaintext"})


def _too_slow_a_shape(markup: str, depth_limit: int) -> bool:
    """Whether the markup is one of the shapes html5lib is quadratic on: nested deeper than `depth_limit`, or leaving more
    than `_HTML5LIB_MAX_RAW_TEXT_LEFT_OPEN` raw-text elements open.

    An estimate, not the tree: elements that close themselves or that HTML closes for you are left out (`_SELF_CLOSING`,
    `_AUTO_CLOSING`), and a stray end tag only ever lifts the depth back up. It is read once and stops at the first limit
    reached, so it costs a fraction of either parse.
    """
    depth = 0
    left_open = 0
    for match in _TAG.finditer(markup):
        name = match.group(2).lower()
        closing = bool(match.group(1))
        if name in _RAW_TEXT:
            left_open += -1 if closing else 1
            if left_open > _HTML5LIB_MAX_RAW_TEXT_LEFT_OPEN:
                return True
        if name in _SELF_CLOSING or name in _AUTO_CLOSING:
            continue
        if closing:
            depth = max(0, depth - 1)
        else:
            depth += 1
            if depth > depth_limit:
                return True
    return False


def builder_for(markup: "str | bytes") -> str:
    """The builder to read one document with: html5lib unless the markup is one of the shapes it is slow on.

    Takes the file as it arrived or the text it decoded to; the size is checked first, so the bytes are never copied for a
    document large enough for that to matter. Only tag names are read from them, and those are ASCII.
    """
    if TREE_BUILDER != "html5lib":
        return TREE_BUILDER
    if len(markup) > _HTML5LIB_MAX_CHARS:
        return "lxml"
    if isinstance(markup, bytes):
        markup = markup.decode("latin-1", "replace")
    return "lxml" if _too_slow_a_shape(markup, _HTML5LIB_MAX_DEPTH) else TREE_BUILDER


def parse_html(markup: str, *, lenient: bool = False) -> BeautifulSoup:
    """The tree of a page, built by whichever parser reads it like a browser here.

    Every HTML tree of this package is built through this function, so that the choice is made in one place. The warning
    filter is part of it: bs4 warns about a page that starts with an XML declaration, at the place that called it.

    `lenient` asks for the forgiving parser whatever this platform would otherwise use. It is for a question about what the
    markup holds rather than about what a browser would show: an HTML5 parser drops a `<frameset>` that comes after body
    content, which is right for rendering and wrong for noticing that a saved page keeps its body in a frame (Pinned by:
    test_audit_html_text.py::TestFrameCheckOfASavedPageWithoutFrames::test_body_inside_a_frame_is_still_reported).
    """
    if lenient:
        builder = "lxml"
    else:   # the document's builder; outside a document (a lone call) decided from this markup alone
        builder = builder_for_document.get() or builder_for(markup)
    with quiet_warnings((XMLParsedAsHTMLWarning, _PARSER_CALLERS)):
        soup = BeautifulSoup(markup, builder)
    if builder == "html5lib":
        _type_the_text_of_scripts_and_styles(soup)
    return soup


# The text of these elements is not content, and bs4 gives it a string class of its own so that `get_text()` can leave it
# out. The class is only applied where libxml2 builds the tree: html5lib leaves the text an ordinary string, while the tag
# still says it keeps `Script` / `Stylesheet` — so `get_text()` on it matched nothing and came back empty. The TeX of a
# `<script type="math/tex">`, which is the only thing the formula is written in on a page MathJax has not rendered, was
# read back that way and vanished. Given its class here, so that nothing downstream has to know which parser built the tree
# (Pinned by: test_core_html.py::TestMathMl::test_tex_in_a_mathjax_script_is_the_formula).
_TEXT_CLASSES = (("script", Script), ("style", Stylesheet))


def _type_the_text_of_scripts_and_styles(soup: BeautifulSoup) -> None:
    classes = dict(_TEXT_CLASSES)
    for element in soup.find_all(list(classes)):
        for child in list(element.contents):   # replaced as we go
            if type(child) is NavigableString:
                child.replace_with(classes[element.name](str(child)))


# Elements that are not content. Removed whole.
# nav is the site menu. footer is kept because it may hold information such as contact details. aside is removed only outside
# the main content (main/article), where it is taken for a side menu or an ad (`_drop_sidebars`).
# input/select/textarea are not removed — the values entered into a saved form are there (`_put_form_values`).
# button: only expander buttons (`_keep_expander_buttons`) keep their text; the rest are removed.
# The parser leaves the content of noframes/noembed as raw text, so it came out tags and all
# (Pinned by: test_core_html_mail_hidden_and_frames.py::TestNoFrames).
# `<object>` and `<canvas>` are not removed: what they hold is the fallback content a page shows where the object or the drawing
# cannot be shown, which is the case here (`<object data="x.pdf"><p>…</p></object>` gave 'No text was extracted')
# (Pinned by: test_core_html.py::TestFallbackContentOfObjectAndCanvas).
# `<datalist>` is the list of suggestions an input offers while one types, which a browser never draws: its options came out
# glued into one word after the field (`City AfghanistanAlbania…`), and so did the fallback `<select>` some pages put in it
# (Pinned by: test_text_html.py::TestFormValues::test_suggestions_of_a_datalist_are_left_out).
# What `<video>` and `<audio>` hold is for browsers that cannot play them, as `<noembed>` is, and a browser that can never shows
# it: "Your browser does not support the video tag." came out as a paragraph of the page, glued to the one of the audio after it
# (Pinned by: test_text_html.py::TestMediaFallbackAndQuotes). Their text alternative is a caption file (`<track>`).
_DROP_TAGS = ["script", "style", "noscript", "noframes", "noembed", "template", "svg", "iframe", "embed", "button", "head", "nav",
              "datalist", "video", "audio"]
# An aside inside these elements is a warning/note box of the main content (`_drop_sidebars`). An element with one of these ARIA
# roles is the same landmark: Sphinx themes, docs.python.org and Read the Docs mark the body `<div role="main">`, and their
# footnotes, topics and sidebars (`<aside>` since docutils 0.18) were removed as side menus
# (Pinned by: test_core_html_blocks.py::TestAsideInsideTheMainContent::test_aside_inside_a_role_main_element_is_kept).
_MAIN_CONTENT_TAGS = ["main", "article"]
_MAIN_CONTENT_ROLES = frozenset(_MAIN_CONTENT_TAGS)


def _is_main_content(tag: Tag) -> bool:
    """Whether `tag` is the main content: a `<main>` / `<article>`, or an element whose ARIA role is main or article."""
    return tag.name in _MAIN_CONTENT_TAGS or not _MAIN_CONTENT_ROLES.isdisjoint(str(tag.get("role") or "").lower().split())


# Marks for checkboxes and radio buttons. The same characters as DOCX legacy form fields (docx.py).
_CHECKED, _UNCHECKED = "☒", "☐"


# A code block. Its text is the code as written, so no script or strikethrough marks are put inside (`_html_dom._mark_scripts`,
# `_mark_struck_text`). Inline code is not protected: it is written without backticks, and `<del><code>foo()</code></del>`
# would lose that it is struck.
_CODE_BLOCKS = frozenset({"pre"})


_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


# Block elements. legend/hgroup/search were missing, so text ran into what followed, as in '신청인 정보이름: 홍길동'
# (legend "신청인 정보" glued to "이름: 홍길동") (Pinned by: test_core_html.py::TestBlockElements).
_BLOCK_TAGS = {"address", "article", "aside", "blockquote", "dd", "details", "dialog", "div", "dl", "dt",
               "fieldset", "figcaption", "figure", "footer", "form", "header", "hgroup", "hr", "legend", "main", "nav", "p",
               "search", "section", "summary", "caption", "center"}


# The answer of a folded question (`<dd>`, `_html_dom._folded_answers`): hidden by its own style until a script opens it. Decided
# by the declaration that wins (`style_sets`), like hiding: matched on the raw text, `/* display:none */` or a later
# `display:block` made a shown answer a folded one (Pinned by:
# test_core_html_mail_hidden_and_frames.py::TestHidingDecidedByTheWinningDeclaration::test_shown_answer_of_a_list_is_not_folded).
FOLDED_VALUES = {"display": "none"}

# A declaration of an inline style: `name: value`, with `!important` at its end.
_STYLE_DECLARATION = re.compile(r"\s*([\w-]+)\s*:(.*)", re.DOTALL)
_IMPORTANT = re.compile(r"!\s*important\s*$", re.IGNORECASE)
_STYLE_COMMENT = re.compile(r"/\*.*?(?:\*/|$)", re.DOTALL)
_GLOBAL_VALUES = frozenset({"inherit", "initial", "unset", "revert", "revert-layer"})
# The values a browser accepts for the properties that hide (the first word of the value): a declaration with any other value is
# invalid and dropped, so an earlier one stays in force — `display:none; display:bogus` is hidden (Pinned by:
# test_core_html_mail_hidden_and_frames.py::TestHidingDecidedByTheWinningDeclaration::test_invalid_later_declaration_does_not_show_it).
_KNOWN_VALUES = {
    "display": _GLOBAL_VALUES | {
        "none", "contents", "block", "inline", "inline-block", "flow", "flow-root", "flex", "inline-flex", "grid",
        "inline-grid", "table", "inline-table", "table-row-group", "table-header-group", "table-footer-group", "table-row",
        "table-cell", "table-column-group", "table-column", "table-caption", "list-item", "run-in", "ruby", "ruby-base",
        "ruby-text", "ruby-base-container", "ruby-text-container", "math", "-webkit-box", "-webkit-inline-box", "-webkit-flex",
        "-webkit-inline-flex", "-moz-box", "-moz-inline-box", "-ms-flexbox", "-ms-inline-flexbox", "-ms-grid",
        "-ms-inline-grid", "grid-lanes", "inline-grid-lanes"},
    "visibility": _GLOBAL_VALUES | {"visible", "hidden", "collapse"},
    "content-visibility": _GLOBAL_VALUES | {"visible", "hidden", "auto"},
}
# A value computed from a variable or a function is valid when the style is read; what it becomes is not known here.
_COMPUTED_VALUE = re.compile(r"(?:var|env|attr)\(", re.IGNORECASE)


# The longest inline style whose properties are kept for the next time it is asked about (`_style_properties`): the cache lives
# as long as the process, and kept whole, a thousand styles of a few MB held a server's memory long after their pages.
_CACHED_STYLE_CHARS = 4_096


def _style_properties(style: str) -> dict[str, str]:
    """The properties of an inline style and the first word of the value that wins for each (lowercase), as a browser applies
    them (`_read_style_properties`). A page checks one style several times — hidden, then under a hidden ancestor, then for
    a visible descendant — so the result for a short style is kept (the returned dict is shared: not to be changed)
    (Pinned by: test_core_html_mail_hidden_and_frames.py::TestHidingDecidedByTheWinningDeclaration::
    test_long_styles_are_not_kept_after_their_page)."""
    return _cached_style_properties(style) if len(style) <= _CACHED_STYLE_CHARS else _read_style_properties(style)


@lru_cache(maxsize=1024)
def _cached_style_properties(style: str) -> dict[str, str]:
    return _read_style_properties(style)


def _read_style_properties(style: str) -> dict[str, str]:
    """Comments left out, the declarations split at `;`, the last valid one written wins unless an earlier one is `!important`
    and it is not. One pass over the style (linear time)."""
    if "/*" in style:
        style = _STYLE_COMMENT.sub("", style)
    properties: dict[str, str] = {}
    important: set[str] = set()
    for declaration in style.split(";"):
        match = _STYLE_DECLARATION.match(declaration)
        if match is None:
            continue
        name = match.group(1).lower()
        value = match.group(2)
        marked = _IMPORTANT.search(value)
        if marked is not None:
            value = value[:marked.start()]
        elif name in important:
            continue
        words = value.split(None, 1)
        word = words[0].lower() if words else ""
        known = _KNOWN_VALUES.get(name)
        if known is not None and word not in known and not _COMPUTED_VALUE.match(word):
            continue   # invalid: a browser drops the declaration
        if marked is not None:
            important.add(name)
        properties[name] = word
    return properties


def style_sets(style: str, values: dict[str, str]) -> bool:
    """Whether the winning declaration (`_style_properties`) of one of the properties of `values` in an inline style has that
    property's value."""
    if ":" not in style:
        return False
    properties = _style_properties(style)
    return any(properties.get(name) == value for name, value in values.items())


_WHITESPACE = re.compile(r"\s+")


# Number of bottom rows dropped by the table grid cell limit while the document is rendered (`_render_table` adds to it and
# `_extract` reports it as a warning).
_omitted_rows: ContextVar[list[int] | None] = ContextVar("omitted_rows", default=None)
# The character limit while the document is rendered (`_extract`), for drawing its tables (`markdown.TableGrid.render(room=...)`): a
# vertically merged value is repeated on every row it covers, and a 970 KB page with one 400,000-character cell merged over
# 30,000 rows was drawn to 80 million characters (6 s) before the page was counted
# (Pinned by: test_office_table_char_limit.py::TestMergedCopiesPastTheLimit::test_html_rowspan_copies_stop_at_the_limit). Only
# the table's own text is charged against it, so it holds wherever the table is written.
_table_room: ContextVar[int | None] = ContextVar("table_room", default=None)


@dataclass(frozen=True, slots=True)
class _Inline:
    """Links and emphasis to keep while the document is rendered (`ConvertOptions.keep_links`, `keep_emphasis`). `cut` holds the
    `id`s of the elements that may be cut at the end of the front part of a large document (`_open_at_the_end`): a link among
    them stays text, because its text may be different in the whole conversion."""

    links: bool
    emphasis: bool
    cut: frozenset[int]


# None when both options are off — rendering is then exactly what it was before the options existed.
_inline: ContextVar[_Inline | None] = ContextVar("inline", default=None)


# How many lower rows of a two-column table to examine when deciding the title row of a multi-row header (the same range in
# which `markdown.table_title` looks for numeric cells).
_HEADER_PROBE_ROWS = 5


_BLOCK_GAP = "\n\n"


# parent id → (parent, child id → position). `_replace` measures the child positions once per parent and keeps them here.
_Places = dict[int, tuple[Tag, dict[int, int]]]


def _replace(node: PageElement, new: PageElement | str, places: _Places) -> None:
    """Same as `node.replace_with(new)`. The parent's child positions are measured once per parent and kept in `places`; since one node is
    swapped for one, the other siblings' positions are unchanged.

    bs4's `replace_with` looks up its own position in the parent's child list from the start, so many replacements under one parent took
    time quadratic in their number: 40,000 scripts 27.6 seconds, 40,000 pictures 14.9 seconds, 40,000 label svgs 15.1 seconds
    (Pinned by: test_core_th_rows_front_tables_and_linear_time.py::TestManySiblingsTakeLinearTime). Within one call, replace only through
    this function, or the positions no longer match.
    """
    parent = node.parent
    entry = places.get(id(parent))
    if entry is None or entry[0] is not parent:
        entry = places[id(parent)] = (parent, {id(child): index for index, child in enumerate(parent.contents)})
    where = entry[1]
    index = where.pop(id(node))
    node.extract(_self_index=index)
    parent.insert(index, new)
    where[id(parent.contents[index])] = index


def _content_holders(soup: BeautifulSoup) -> set[int]:
    """`id`s of the content elements (main / article, `_is_main_content`) and their ancestors, and of the ancestors of h1 (not h1
    itself)."""
    holders: set[int] = set()
    for tag in soup.find_all(lambda tag: tag.name == "h1" or _is_main_content(tag)):
        node = tag if _is_main_content(tag) else tag.parent
        while node is not None and id(node) not in holders:
            holders.add(id(node))
            node = node.parent
    return holders


def _list_number(value: object, default: int | None = None) -> int | None:
    text = str(value).strip() if value is not None else ""
    return int(text) if re.fullmatch(r"[+-]?\d{1,9}", text) else default


def _solid_length(node: Tag) -> int:
    return sum(len(text) - text.count(" ") for text in node.get_text(" ").split())
