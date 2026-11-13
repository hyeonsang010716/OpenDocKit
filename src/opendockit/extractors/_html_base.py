"""What the HTML modules share: the tag sets and limits used by more than one of them, the state of the rendering in
progress (nesting depths, whether a cell or a heading is being written), and the placeholders that keep a piece of
finished text in the tree.
"""

from contextvars import ContextVar
from dataclasses import dataclass
import re

from bs4 import BeautifulSoup, Tag
from bs4.element import PageElement


# The modules that hand markup to the HTML parser, as the regex `models.quiet_warnings` matches the module a warning comes
# from. bs4 warns about a page that starts with an XML declaration at the place that called it, and the filter that keeps
# that warning from the caller of `convert` names these modules only
# (Pinned by: test_core_html.py::TestPagesWithAnXmlDeclaration).
_PARSER_CALLERS = r"opendockit\.extractors\._?html"


# Elements that are not content. Removed whole.
# nav is the site menu. footer is kept because it may hold information such as contact details. aside is removed only outside
# the main content (main/article), where it is taken for a side menu or an ad (`_drop_sidebars`).
# input/select/textarea are not removed — the values entered into a saved form are there (`_put_form_values`).
# button: only expander buttons (`_keep_expander_buttons`) keep their text; the rest are removed.
# The parser leaves the content of noframes/noembed as raw text, so it came out tags and all
# (Pinned by: test_core_html_mail_hidden_and_frames.py::TestNoFrames).
_DROP_TAGS = ["script", "style", "noscript", "noframes", "noembed", "template", "svg", "canvas", "iframe", "object", "embed",
              "button", "head", "nav"]
# An aside inside these elements is a warning/note box of the main content (`_drop_sidebars`).
_MAIN_CONTENT_TAGS = ["main", "article"]


# Marks for checkboxes and radio buttons. The same characters as DOCX legacy form fields (docx.py).
_CHECKED, _UNCHECKED = "☒", "☐"


_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}


# Block elements. legend/hgroup/search were missing, so text ran into what followed, as in '신청인 정보이름: 홍길동'
# (legend "신청인 정보" glued to "이름: 홍길동") (Pinned by: test_core_html.py::TestBlockElements).
_BLOCK_TAGS = {"address", "article", "aside", "blockquote", "dd", "details", "dialog", "div", "dl", "dt",
               "fieldset", "figcaption", "figure", "footer", "form", "header", "hgroup", "hr", "legend", "main", "nav", "p",
               "search", "section", "summary", "caption", "center"}


_FOLDED_STYLE = re.compile(r"display\s*:\s*none", re.IGNORECASE)


_WHITESPACE = re.compile(r"\s+")


# Number of bottom rows dropped by the table grid cell limit while the document is rendered (`_render_table` adds to it and
# `_extract` reports it as a warning).
_omitted_rows: ContextVar[list[int] | None] = ContextVar("omitted_rows", default=None)


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
    """`id`s of the content elements (main / article) and their ancestors, and of the ancestors of h1 (not h1 itself)."""
    holders: set[int] = set()
    for tag in soup.find_all([*_MAIN_CONTENT_TAGS, "h1"]):
        node = tag if tag.name in _MAIN_CONTENT_TAGS else tag.parent
        while node is not None and id(node) not in holders:
            holders.add(id(node))
            node = node.parent
    return holders


def _list_number(value: object, default: int | None = None) -> int | None:
    text = str(value).strip() if value is not None else ""
    return int(text) if re.fullmatch(r"[+-]?\d{1,9}", text) else default


def _solid_length(node: Tag) -> int:
    return sum(len(text) - text.count(" ") for text in node.get_text(" ").split())
