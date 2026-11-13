"""HTML → Markdown.

Walks the tree itself instead of using a general-purpose converter such as markdownify: HTML tables exported by Korean
systems (including ones disguised as ".xls") are full of rowspan/colspan, so merged cells must be unfolded onto a grid
(`TableGrid`). Links and emphasis are kept the quiet way (`ConvertOptions.keep_links`, `keep_emphasis`): only `http`, `https`
and `mailto` addresses, only `<b>` / `<strong>` / `<i>` / `<em>`, and nothing for bold that covers a whole block
(docs/decisions/inline-formatting.md).
"""

import base64
import binascii
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from email.message import Message
from html import escape
import itertools
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, NavigableString, Tag, XMLParsedAsHTMLWarning
from bs4.element import PageElement, PreformattedString
from lxml import etree

from opendockit.extractors._shared import (
    BLOCK_INSIDE,
    BOLD_OFF,
    BOLD_ON,
    ITALIC_OFF,
    ITALIC_ON,
    LINK_ON,
    SUBSCRIPT,
    SUPERSCRIPT,
    SYMBOL_GLYPHS,
    InlineMark,
    cell_level_mark,
    decimal_int,
    format_number,
    format_script,
    link_off,
    link_target,
    omitted_rows_warning,
    resolve_inline,
    symbol_glyph,
)
from opendockit.extractors.text import (
    FORWARDED_MAIL_TITLE,
    MIME_HEAD_BYTES,
    MIME_VERSION,
    _unescaped_from_lines,
    decode_text,
    decoding_warning,
    decoding_warnings,
    is_sent_mail,
    mail_attachment_warning,
    mail_body_parts,
    mail_body_sequence,
    mail_box,
    mail_box_title,
    mail_box_warning,
    mail_calendar_events,
    mail_head_text,
    mail_html_body_parts,
    mail_html_outside_alternative,
    mail_messages,
    mail_plain_body,
    mime_start,
    parse_mail,
)
from opendockit.extractors.text import mime_header_block as _mime_header_block
from opendockit.markdown import TableGrid, render_table, solid_length, table_title
from opendockit.models import ConvertOptions, Extraction, Section, SectionKind, quiet_warnings


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
# Inputs whose value is not content: invisible values (hidden/password) and button labels.
_VALUELESS_INPUTS = {"hidden", "password", "submit", "button", "file", "image", "reset"}
# Marks for checkboxes and radio buttons. The same characters as DOCX legacy form fields (docx.py).
_CHECKED, _UNCHECKED = "☒", "☐"
# A checkbox/radio carrying any other role is not a form value but a device that opens and closes a menu (a checkbox holding
# state for CSS).
_CHOICE_ROLES = {"checkbox", "radio", "switch", "menuitemcheckbox", "menuitemradio"}
# ARIA tables (`_render_aria_grid`): table/cell roles, real table elements (handled as tables even when a role is attached), and
# the maximum number of columns `aria-colindex` may skip.
_GRID_ROLES = {"grid", "table", "treegrid"}
_ARIA_CELL_ROLES = {"cell", "gridcell", "columnheader", "rowheader"}
_ARIA_SKIPPED_TAGS = {"table", "thead", "tbody", "tfoot", "tr", "td", "th", "ul", "ol", "dl", "pre"}
_MAX_ARIA_COLUMN_SKIP = 200
# Maximum nesting depth at which an ARIA table inside the cell of another one is drawn as a table. Deeper ones are descended
# as blocks (recursion limit; Pinned by: test_audit_html_text.py::TestDeeplyNestedAriaGrids).
_MAX_ARIA_GRID_DEPTH = 20
_aria_grid_depth: ContextVar[int] = ContextVar("aria_grid_depth", default=0)
# Elements whose content the parser leaves as raw text (`_reopen_raw_text_elements`), and those among them that an end tag
# closes (plaintext is never closed).
_RAW_TEXT_TAGS = ["textarea", "xmp", "plaintext"]
_CLOSABLE_RAW_TEXT = re.compile(r"</(textarea|xmp)\s*>", re.IGNORECASE)
# The end tags at the end of the document (`_closed_at_the_end`). Only this much of the tail is examined — a few end tags are
# enough.
_TRAILING_END_TAGS = re.compile(r"(?:</[A-Za-z][^<>]*>\s*)+\Z")
_TRAILING_BYTES = 4096
_MAX_REOPENED = 16
# An unclosed select, which the front-part check (`_extract_front`) cannot trust.
_SELECT_OPENING = re.compile(r"<select\b", re.IGNORECASE)
_SELECT_CLOSING = re.compile(r"</select\s*>", re.IGNORECASE)
_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_STRIKE_TAGS = ["del", "s", "strike"]
_STRIKE = "~~"
# CSS strikethrough. Both `text-decoration: underline line-through` and `text-decoration-line: line-through`.
_STRIKE_STYLE = re.compile(r"text-decoration(?:-line)?\s*:[^;]*\bline-through\b", re.IGNORECASE)
# Block elements. legend/hgroup/search were missing, so text ran into what followed, as in '신청인 정보이름: 홍길동'
# (legend "신청인 정보" glued to "이름: 홍길동") (Pinned by: test_core_html.py::TestBlockElements).
_BLOCK_TAGS = {"address", "article", "aside", "blockquote", "dd", "details", "dialog", "div", "dl", "dt",
               "fieldset", "figcaption", "figure", "footer", "form", "header", "hgroup", "hr", "legend", "main", "nav", "p",
               "search", "section", "summary", "caption", "center"}
# Only the `display` / `visibility` (and `content-visibility`) properties themselves: matched anywhere, the pattern removed a
# visible element styled `backface-visibility:hidden`, text and all
# (Pinned by: test_audit_html_text.py::TestStylePropertiesThatOnlyEndLikeAHidingOne).
_HIDDEN_STYLE = re.compile(r"(?<![\w-])(?:display\s*:\s*none|(?:content-)?visibility\s*:\s*hidden)", re.IGNORECASE)
_FOLDED_STYLE = re.compile(r"display\s*:\s*none", re.IGNORECASE)
# Hidden elements (blocks) that can be a folded answer, and the elements right before one that unfold it. Lists (ul/ol) are
# left out because they are dropdown menus.
_FOLDED_TAGS = {"div", "p", "dd", "section", "article", "blockquote"}
_OPENER_TAGS = {"a", "button", "dt", "summary", *_HEADINGS}
# A link that stays where it unfolds instead of going elsewhere: no href, or `#…`/`javascript:…`.
_STAYING_LINK = re.compile(r"\s*(?:#|javascript:|$)", re.IGNORECASE)
# Names (class/id) and roles of hidden elements that are not folded answers: menus, modals, tooltips, layer popups.
_OVERLAY_NAME = re.compile(r"modal|popup|pop-|tooltip|menu|layer|dropdown|dialog|gnb|lnb|nav|toast|banner|overlay", re.IGNORECASE)
_OVERLAY_ROLES = {"dialog", "alertdialog", "menu", "menubar", "tooltip", "navigation", "listbox"}
# `type` of a numbered list → number shape (`_shared.format_number`). Case-sensitive.
_LIST_NUMBER_SHAPES = {"a": "LATIN_SMALL", "A": "LATIN_CAPITAL", "i": "ROMAN_SMALL", "I": "ROMAN_CAPITAL"}
_WHITESPACE = re.compile(r"\s+")
# Characters that draw nothing on screen: zero-width spaces and joiners (ZWSP/ZWNJ/ZWJ/WJ/BOM), the combining grapheme joiner
# (U+034F), the soft hyphen. Mails use them as blank filler (`&zwnj;&nbsp;`) (`_has_text`).
_INVISIBLE_TEXT = re.compile("[\u200b-\u200d\u2060\ufeff\u034f\u00ad]")
_FENCED = re.compile(r"(```\n.*?\n```)", re.DOTALL)
_BLANK_LINES = re.compile(r"\n{2,}")
# Placeholder for a nested list detached while its list item is rendered (private-use characters — clean_text removes them
# if they are left in the body).
_LIST_SLOT_OPEN, _LIST_SLOT_CLOSE = "\ue024", "\ue025"
_LIST_SLOT = re.compile(f"{_LIST_SLOT_OPEN}(\\d+){_LIST_SLOT_CLOSE}")
# Maximum list indentation depth. Deeper lists are folded onto one line (a list 14,000 levels deep was rejected with a
# recursion-limit error) (Pinned by: test_text_html.py::TestDeepNesting).
_MAX_LIST_DEPTH = 20
# Maximum depth of stacked quote marks (`> `). Deeper quote blocks are only separated as blocks. The parser nests an unclosed
# `<blockquote>` one level per occurrence, and prefixing `> ` to the whole inner text again at every level is quadratic in the
# depth (4,500 levels, 58KB took 48 s and 2.9GB) (Pinned by: test_text_html.py::TestMalformedNesting).
_MAX_QUOTE_DEPTH = 20
# Table structure elements. Text placed inside a table but outside these is shown by browsers moved in front of the table
# (`_foster_parent_table_content`).
_TABLE_PARTS = {"thead", "tbody", "tfoot", "tr"}
_TABLE_CELLS = {"td", "th", "caption", "colgroup", "col"}
_TABLE_TAG = re.compile(r"<table[\s>]", re.IGNORECASE)
_ROW_START = re.compile(r"<tr[\s>]", re.IGNORECASE)
# Maximum nesting depth at which a layout table is unfolded into blocks. Deeper layout tables are folded onto one line inside
# the cell like data tables (recursion limit).
_MAX_LAYOUT_DEPTH = 20
_layout_depth: ContextVar[int] = ContextVar("layout_depth", default=0)
# Maximum nesting depth at which a table inside a cell is handed over as a one-line HTML table. Deeper tables are flattened
# to their text on one line (recursion limit).
_MAX_EMBED_DEPTH = 20
_embed_depth: ContextVar[int] = ContextVar("embed_depth", default=0)
# Number of bottom rows dropped by the table grid cell limit while the document is rendered (`_render_table` adds to it and
# `_extract` reports it as a warning).
_omitted_rows: ContextVar[list[int] | None] = ContextVar("omitted_rows", default=None)
# Whether the text of a table cell is being rendered (`_cell_text`). Headings and code blocks inside a cell stay as lines,
# without the `#`/``` marks.
_in_cell: ContextVar[bool] = ContextVar("in_cell", default=False)


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
# Whether the text of a heading or table caption is being rendered: a title keeps its links but is not emphasized (`_title_text`).
_in_heading: ContextVar[bool] = ContextVar("in_heading", default=False)
# Maximum characters (excluding whitespace) of the name cell in a name | value table. Field names such as '교육명' (course
# name), '대상자' (attendees), '문서번호' (document number) (`_pairs_names_with_values`).
_NAME_CHARS = 20
# The font an element declares (`_declared_font`): font-family in style (first name), the font shorthand property.
_FONT_FAMILY = re.compile(r"(?:^|;)\s*font-family\s*:\s*([^;]+)", re.IGNORECASE)
_FONT_SHORTHAND = re.compile(r"(?:^|;)\s*font\s*:\s*([^;]+)", re.IGNORECASE)
# Indentation of a table's first column (`_indent_marks`): inline CSS indentation properties, lengths, px per unit, and the
# maximum depth rank to mark. Cells that look like numbers are left out.
_CSS_INDENT = re.compile(r"(?:^|;)\s*(?P<name>padding-left|padding|text-indent|margin-left)\s*:\s*(?P<value>[^;!]+)", re.IGNORECASE)
_CSS_LENGTH = re.compile(r"(\d+(?:\.\d+)?)(px|pt|em|rem|)", re.IGNORECASE)
_CSS_UNITS = {"px": 1.0, "pt": 4 / 3, "em": 16.0, "rem": 16.0, "": 0.0}
# Upper bound on one CSS length (px, `_css_length`). No real indentation comes near it.
_MAX_CSS_LENGTH = 10000.0
_MAX_INDENT_RANK = 5
_NUMBER_TEXT = re.compile(r"[-+−▲▼△(]?\s*[₩$€¥£]?\s*\d[\d,]*(?:\.\d+)?\s*%?\)?")
# MathML (`_put_math`): annotation encodings of the LaTeX the document wrote, the wrapper of Wikipedia's alttext, script
# elements → marks, elements that are not text, and the maximum depth unfolded LaTeX-style (recursion).
_TEX_ENCODINGS = {"application/x-tex", "tex", "latex", "application/x-latex", "text/x-tex"}
_DISPLAY_STYLE = re.compile(r"\{\\displaystyle\s*(.*)\}", re.DOTALL)
_MATH_SCRIPTS = {"msup": "^", "msub": "_", "msubsup": "_", "munder": "_", "mover": "^", "munderover": "_"}
_MATH_IGNORED = {"annotation", "annotation-xml", "none", "mprescripts", "mphantom"}
_MAX_MATH_DEPTH = 50
# Page blocks that cannot appear inside an equation (`_maths_holding_page_blocks`).
_PAGE_BLOCKS_IN_MATH = [*_BLOCK_TAGS, *_HEADINGS, "table", "ul", "ol", "li", "pre"]
# Definition lists (`_render_definitions`): maximum characters of a description kept on one `term: description` line, and the
# maximum nesting depth rendered as `term: description`.
_DEFINITION_CHARS = 200
_MAX_DEFINITION_DEPTH = 20
_definition_depth: ContextVar[int] = ContextVar("definition_depth", default=0)
# How many lower rows of a two-column table to examine when deciding the title row of a multi-row header (the same range in
# which `markdown.table_title` looks for numeric cells).
_HEADER_PROBE_ROWS = 5
# Upper bound on the rows of a table taken for a layout table holding heading elements, and lower bound on the blocks in one
# cell for a table taken for a one-row layout table (`_holds_page_blocks`).
_MAX_HEADED_LAYOUT_ROWS = 3
_MIN_LAYOUT_CELL_BLOCKS = 3
_LAYOUT_BLOCKS = {"p", "ul", "ol", "blockquote", "pre", "dl", *_HEADINGS}
# Class of a list exported by Google Docs (`lst-kix_<list id>-<level>`). It lays out a separate list per level side by side
# (`_nest_google_docs_lists`).
_GOOGLE_DOCS_LIST = re.compile(r"lst-kix_\S+-(\d+)")
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
_BLOCK_GAP = "\n\n"
# Words in the names of data-* attributes that hold the real address of a lazy-loaded picture (`_is_lazy`), and the maximum
# side length taken for a spacer picture (pixels, `_is_spacer`).
_LAZY_SOURCE_WORDS = ("src", "original", "lazy")
# Upper bound on the MHTML text/html parts examined when checking whether the body is in a frame (`_unread_frame_warning`).
_MAX_FRAME_PARTS = 50
_SPACER_SIDE = 2
_QUOTE_END = object()  # where a quote block ends on the stack of `_render_nodes`
_CODE_END = object()  # where an inline code element ends on the stack of `_render_nodes`
# Bold and italic elements (`ConvertOptions.keep_emphasis`). Deliberately only these four: `<cite>`, `<var>`, `<dfn>` and CSS
# `font-weight` / `font-style` are how a page is styled, and following them would mark navigation, captions and whole pages.
_EMPHASIS_MARKS = {"b": (BOLD_ON, BOLD_OFF), "strong": (BOLD_ON, BOLD_OFF), "i": (ITALIC_ON, ITALIC_OFF), "em": (ITALIC_ON, ITALIC_OFF)}
# Inline code. The text inside is not emphasized — `*` is a character of the code there.
_CODE_TAGS = {"code", "kbd", "samp", "tt"}
# Leaf blocks no link or emphasis span can hold (`_shared.BLOCK_INSIDE`). A heading is a single line and may sit inside a link.
_BLOCK_LEAVES = {"table", "pre", "ul", "ol"}
# Length of the front part read first in a large document = character limit × this multiple (`_extract_front`). A text-dense
# document exceeds the limit with the first front part; if it does not, the front part grows by the text density. If by the
# density the whole document may also be within the limit, it looks as far as the last front part
# (× `_LAST_FRONT_CHARS_PER_LIMIT`), which holds the usual markup ratio of HTML (3–10 times the text).
_FIRST_FRONT_CHARS_PER_LIMIT = 2
_LAST_FRONT_CHARS_PER_LIMIT = 8
_MARKDOWN_HEADING = re.compile(r"^#{1,6} ", re.MULTILINE)
# Markdown decoration characters left out when counting the characters of the front part (`_extract_front`). An element cut at
# the end of the front part (an unclosed pre, math, heading …) is interpreted differently in the whole document, so its fence,
# `$` or `#` may be absent from the whole conversion — leaving them out never counts more than there really is.
_MARKDOWN_DECORATION = re.compile(r"[#`$>*~^_{}\\]")
# With links kept, the address part of `[text](address)` and the brackets are left out of that count as well. A link inside an
# element cut at the end of the front part can be plain text in the whole conversion (a heading that turns out to hold a table
# is read as text only), so the front part must not count what the link added. The emphasis marks are already left out (`*`).
# Addresses are percent-encoded (`_shared.link_target`), so they hold no whitespace or parentheses
# (Pinned by: test_inline_formatting.py::TestCharacterLimit::test_front_of_a_large_page_does_not_count_link_addresses).
_LINK_ADDRESS = re.compile(r"\]\((?:https?://|mailto:)[^()\s]*\)", re.IGNORECASE)
_LINK_BRACKETS = re.compile(r"[\[\]]")
# The charset the document declares itself: `<meta charset=…>`, `<meta http-equiv="Content-Type" content="…; charset=…">`,
# the encoding of the XML declaration.
_META_CHARSET = re.compile(rb"<meta\b[^>]*?\bcharset\s*=\s*[\"']?\s*([\w.:+-]+)", re.IGNORECASE)
_XML_ENCODING = re.compile(rb"^\s*<\?xml\b[^>]*?\bencoding\s*=\s*[\"']([\w.:+-]+)", re.IGNORECASE)
# Length of the front part searched for a charset declaration. The declaration goes at the very start of <head> (browsers
# look at only 1,024 bytes).
_CHARSET_PRESCAN_BYTES = 4096
# Header of an MHTML (single-file web page): from the very start to the blank line there are only `name: value` lines (and
# continuation lines folded with spaces/tabs), and among them MIME-Version and a multipart/related (or text/html) Content-Type.
# The block is walked line by line by `_mime_header_block` (`text.mime_header_block`, shared with `text.mime_mail`, which
# detects transfer-encoded mail) — matching the whole block with one regex `(?:name:value\n(?:[ \t]+…\n)*)+\n` backtracks
# once for every way the two quantifiers can split the leading whitespace of the folded lines, and on text whose first line is
# `word:` followed only by indented lines with no blank line (XMP metadata) it did not finish even with 12 lines (a text file
# named .pdf hung for over 180 s)
# (Pinned by: test_detect.py::TestMhtml::test_prose_with_many_indented_lines_is_decided_in_linear_time).
_MHTML_CONTENT_TYPE = re.compile(rb"^Content-Type:\s*(?:multipart/related|text/html)\b", re.IGNORECASE | re.MULTILINE)


# The HTML5 tokenizer arrived in libxml2 2.14. Reading malformed pages "like a browser" below (content after `</html>`, a
# second concatenated document, raw-text elements such as <textarea>/<xmp>, mail whose HTML parts are joined) depends on it.
# lxml's wheels for Linux and macOS bundle 2.14 from lxml 6.0 on, but its Windows wheels still bundle 2.11, where those
# inputs silently lose content. The result cannot be repaired here, so it is reported
# (Pinned by: test_core_html.py::TestOlderHtmlParser; tests that need the tokenizer are skipped via `tests.needs_html5_tokenizer`).
HTML5_TOKENIZER = etree.LIBXML_VERSION >= (2, 14)
_OLD_PARSER_WARNING = ("The installed lxml bundles an HTML parser older than libxml2 2.14 (as its Windows builds do), so content of "
                       "malformed or multi-part HTML and mail (text after the end of the page, further mail parts) may be missing.")


def extract_html(data: bytes, options: ConvertOptions) -> Extraction:
    extraction = _extract_html(data, options)
    if not HTML5_TOKENIZER and _OLD_PARSER_WARNING not in extraction.warnings:
        extraction.warnings.append(_OLD_PARSER_WARNING)
    return extraction


def _extract_html(data: bytes, options: ConvertOptions) -> Extraction:
    # A declared charset is used only when strict decoding succeeds (decode_text holds the order of decisions). When the
    # declaration was ignored, documents declaring `shift_jis`/`windows-1252` were garbled as CP949
    # (Pinned by: test_text_html.py::TestHtmlDeclaredCharset).
    # Mail (.eml, `text.mime_mail`) has its HTML body part read like MHTML, with the header (subject, sender …) placed before
    # the body (Pinned by: test_text_html.py::TestEncodedMail, test_core_mail_and_detect.py::TestMailWithoutTransferEncoding).
    # Mail read as MHTML (a single text/html part, or multipart/related with embedded photos) also gets its mail header in
    # front when it has one (`text.is_sent_mail`).
    # An mbox mailbox (two or more mails) is a mailbox even when its first mail is MHTML-shaped (same order as `detect`).
    mails, total = mail_box(data)
    if len(mails) < 2 and is_mhtml(data):
        message = _mhtml_message(data)
        mail = message if is_sent_mail(message) else None
        part = _html_part(message)
    else:
        if any(mail_reads_as_html(mail) for mail in mails):
            return _extract_mail(mails, total, options)
        message = mail = part = None
    saved_picture = None
    if part is None:
        declared = _declared_charset(data)
    else:
        data, declared = part[0], part[1] or _declared_charset(part[0])
        saved_picture = _mhtml_pictures(message) if options.extract_images else None

    text, encoding = decode_text(data, declared, markup=True)
    text = _with_table_around_bare_rows(text)
    extraction = _extract_front(text, encoding, options, saved_picture)
    if extraction is None:
        extraction = _extract(text, encoding, options, saved_picture)

    if mail is None:
        section = extraction.sections[0]
        section.text = _with_title_heading(section.text, extraction.metadata.get("title"))
        warning = _unread_frame_warning(message, text) if part is not None else None
        if warning:
            extraction.warnings.append(warning)
    elif part is not None:
        section = extraction.sections[0]
        section.text = f"{mail_head_text(mail)}\n\n{section.text}".lstrip()
        warning = mail_attachment_warning(mail)
        if warning:
            extraction.warnings.append(warning)
    return extraction


def is_mhtml(data: bytes) -> bool:
    """Whether this is MHTML (single-file web page, .mht). Groupware and ERP systems serve it as .xls/.doc downloads.

    Looks at the header structure: a note that merely mentions 'MIME-Version' in its body, or plain-text mail
    (text/plain, multipart/mixed), is not MHTML (Pinned by: test_detect.py::TestMhtml).
    """
    start = mime_start(data)
    headers = _mime_header_block(data[start:start + MIME_HEAD_BYTES])
    if headers is None:
        return False
    return MIME_VERSION.search(headers) is not None and _MHTML_CONTENT_TYPE.search(headers) is not None


def mhtml_html_part(data: bytes) -> tuple[bytes, str | None] | None:
    """The first text/html part of an MHTML, transfer encoding (base64/quoted-printable) decoded, as (bytes, charset the part
    declares). None if there is none.

    Deliberate: only the first part is read. Later text/html parts are frames the page embeds (ads and the like).
    """
    return _html_part(_mhtml_message(data))


def mail_html_parts(message: Message) -> list[tuple[bytes, str | None]]:
    """The text/html body parts of a mail (`text.mime_mail`) (`text.mail_html_body_parts` — in document order, including
    pieces separated by attachments) as (bytes, charset the part declares). An empty list if there are none (a mail with only
    plain-text parts is read as text).

    HTML that is an attachment (Content-Disposition: attachment, or a part that merely has a name — `text._is_attachment`) and
    HTML inside a forwarded mail (message/rfc822) are not this mail's body — the attached HTML was read instead of the
    plain-text body
    (Pinned by: test_core_mail_and_detect.py::TestMailWithoutTransferEncoding::test_html_attachment_of_a_plain_mail_is_not_read_as_its_body,
    test_core_text_and_mail.py::TestNamedMailPartWithoutDisposition, TestForwardedMail).
    """
    parts = []

    for part in mail_html_body_parts(message):
        payload = part.get_payload(decode=True)
        if payload:
            parts.append((payload, part.get_content_charset()))
    return parts


def mail_reads_as_html(message: Message) -> bool:
    """Whether a mail (`text.mime_mail`) is read by the HTML extractor: the outer mail has an HTML body, or among the forwarded
    mails (`text.mail_messages`) there is one with only an HTML body and no plain-text body, or one with a separate HTML body
    that is not an alternative rendering of the plain text (`text.mail_html_outside_alternative`). Everything else is read as
    plain text by the text extractor.

    When a forwarded mail's mixed part held both plain-text and HTML bodies, the text extractor read only the plain text and
    the HTML body vanished without a warning
    (Pinned by: test_core_text_and_mail.py::TestForwardedMail::test_forwarded_mixed_mail_keeps_both_its_plain_and_html_bodies,
    test_forwarded_alternative_with_an_html_footer_keeps_the_footer)."""
    messages, _ = mail_messages(message)
    return bool(mail_html_parts(message)) or any(
        mail_html_parts(forwarded) and (mail_plain_body(forwarded) is None or mail_html_outside_alternative(forwarded))
        for forwarded in messages[1:])


def _extract_mail(mails: list[Message], total: int, options: ConvertOptions) -> Extraction:
    """Mails read as HTML (`text.mail_box` — an mbox mailbox holds several): for each mail, the bodies of that mail and of its
    forwarded mails (`text.mail_messages`) are joined in order and rendered as one HTML.

    Each body uses its HTML parts or, if there are none, its plain-text part turned into paragraphs. A forwarded mail is
    preceded by the 'Forwarded message' heading and that mail's header (the same shape as the text extractor's
    `text.forwarded_mail_head`). When the mailbox holds several mails, each is preceded by `## Message N` and its header
    (`text.mail_box_title`, the same shape as the text extractor). Each body is decoded separately with its part's charset and
    then joined — the joined HTML documents are rendered in order, as a browser would (`_document_nodes`). The HTML body of a
    forwarded mail used to push out the outer plain-text body
    (Pinned by: test_core_text_and_mail.py::TestForwardedMail, test_core_mail_parts_and_icons.py::TestMailboxWithSeveralMails).
    """
    documents: list[str] = []
    encodings: list[str] = []

    for number, mail in enumerate(mails, start=1):
        messages, _ = mail_messages(mail)
        for index, message in enumerate(messages):
            body, found = _mail_body_html(message)
            body += _calendar_html(message)
            if index:
                body = f"<h2>{FORWARDED_MAIL_TITLE}</h2><p>{_head_html(message)}</p>{body}"
            elif number > 1:
                body = f"<h2>{mail_box_title(number)}</h2><p>{_head_html(message)}</p>{body}"
            documents.append(body)
            encodings.extend(found)

    encoding = encodings[0] if encodings else "utf-8"
    saved_picture = _first_picture([_mhtml_pictures(mail) for mail in mails]) if options.extract_images else None
    text = "\n".join(documents)
    extraction = _extract_front(text, encoding, options, saved_picture) or _extract(text, encoding, options, saved_picture)

    section = extraction.sections[0]
    title = f"## {mail_box_title(1)}\n\n" if len(mails) > 1 else ""
    section.text = f"{title}{mail_head_text(mails[0])}\n\n{section.text}".lstrip()

    extraction.warnings.extend(warning for warning in decoding_warnings(encodings) if warning not in extraction.warnings)
    extraction.warnings.extend(warning for warning in (mail_attachment_warning(*mails), mail_box_warning(len(mails), total))
                               if warning)
    return extraction


def _mail_body_html(message: Message) -> tuple[str, list[str]]:
    """The body HTML of one mail and the encodings decoded: the body parts (`text.mail_body_sequence` — the HTML branch of an
    alternative, and all plain-text and HTML parts of a mixed) joined in order or, when there is no HTML part, the plain-text
    body as paragraphs (Pinned by: test_core_html_mail_hidden_and_frames.py::TestPlainAndHtmlBodiesSideBySide).

    When the HTML parts hold no visible text at all (an alternative branch with nothing but a logo picture), the plain-text
    branch is placed after the HTML as paragraphs (the picture stays). With the plain text in front, the `<head>` of the HTML
    document that follows landed inside the body and `<title>` came out as text. Reading only the HTML made the body of the
    plain-text branch vanish without a warning, leaving only the header
    (Pinned by: test_core_mail_bodies_and_events.py::TestHtmlAlternativeWithoutText).
    """
    if not mail_html_parts(message):
        plain = mail_plain_body(message)
        return (_plain_paragraphs(plain[0]), plain[1]) if plain else ("", [])
    documents: list[str] = []
    encodings: list[str] = []
    read: set[int] = set()
    has_text = False

    # Plain-text parts outside an alternative (the plain-text body or footer of a mixed) are placed in order with the HTML
    # parts. Plain text is wrapped in a document of its own — so that the `<head>` of the HTML document that follows does not
    # land inside a plain-text paragraph.
    for part in mail_body_sequence(message):
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes) or not payload.strip():
            continue
        if part.get_content_type() == "text/html":
            text, encoding = decode_text(payload, part.get_content_charset() or _declared_charset(payload), markup=True)
            documents.append(_with_table_around_bare_rows(text))
            has_text = has_text or _has_text(text)
        else:
            text, encoding = decode_text(payload, part.get_content_charset())
            documents.append(f"<html><body>{_plain_paragraphs(text)}</body></html>")
            read.add(id(part))
        encodings.append(encoding)

    # When the HTML has no visible text, the plain-text branch of the alternative goes after it
    leftover = [part for part in mail_body_parts(message) if part.get_content_type() == "text/plain" and id(part) not in read]
    plain = mail_plain_body(message, leftover) if leftover and not has_text else None
    html = "\n".join(documents)
    if plain:
        return html + _plain_paragraphs(plain[0]), [*encodings, *plain[1]]
    return html, encodings


def _unread_frame_warning(message: Message, page: str) -> str | None:
    """A warning when the body of an MHTML seems to be in a frame part saved along with it: among the text/html parts that the
    iframes/frames of the page read point to (`cid:` or Content-Location), one has more visible text than the page.

    In an MHTML saved from a Naver blog or cafe the outer page is only a shell (title and iframe) and the body is in a frame
    part (`mainFrame`, `cafe_main`). Since only the first HTML part is read, deliberately (`mhtml_html_part`), the 'successful'
    result was a single title line with no warning
    (Pinned by: test_core_html_mail_hidden_and_frames.py::TestMhtmlWhoseBodyIsInAFrame). The frame is not read instead — there
    is no basis for telling which frame is the body (ad, comment and menu frames). A frame with less text than the page (an
    ad) is not reported.
    """
    pages = list(itertools.islice((part for part in message.walk() if part.get_content_type() == "text/html"), _MAX_FRAME_PARTS))
    if len(pages) < 2:
        return None
    base = str(pages[0].get("Content-Location") or "").strip()
    frames: dict[str, Message] = {}

    for part in pages[1:]:
        content_id = str(part.get("Content-ID") or "").strip().strip("<>")
        location = str(part.get("Content-Location") or "").strip()
        for key in (f"cid:{content_id}" if content_id else "", location):
            if key:
                frames.setdefault(key, part)

    with quiet_warnings((XMLParsedAsHTMLWarning, r"opendockit\.extractors\.html")):
        soup = BeautifulSoup(page, "lxml")
    sources = [str(frame.get("src") or "").strip() for frame in soup.find_all(["iframe", "frame"])]
    found = {id(part): part for source in sources if source
             and (part := frames.get(source) or frames.get(_resolved(base, source))) is not None}
    if not found:
        return None
    own = _visible_length(page)
    for part in found.values():
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes) and _visible_length(decode_text(payload, part.get_content_charset(), markup=True)[0]) > own:
            return ("The body of the web page seems to be inside a frame (iframe). The frame content was not read — open the "
                    "body page directly, save it, and convert that file.")
    return None


def _visible_length(html: str) -> int:
    """Number of visible characters in the HTML (`_visible_strings`, excluding whitespace)."""
    return sum(len(_WHITESPACE.sub("", text)) for text in _visible_strings(html))


def _visible_strings(html: str) -> list[str]:
    """Text pieces outside elements that are not rendered (`_DROP_TAGS`) and hidden elements (`_drop_hidden`). Invisible
    characters (`_INVISIBLE_TEXT`) are removed."""
    with quiet_warnings((XMLParsedAsHTMLWarning, r"opendockit\.extractors\.html")):
        soup = BeautifulSoup(html, "lxml")
    _reopen_raw_text_elements(soup, _closed_at_the_end(html))
    _decompose_all(soup.find_all([*_DROP_TAGS, "title"]))
    _drop_hidden(soup)
    return [_INVISIBLE_TEXT.sub("", text) for text in soup.find_all(string=True) if not isinstance(text, PreformattedString)]


def _has_text(html: str) -> bool:
    """Whether the HTML holds text to render: outside elements that are not rendered (`_DROP_TAGS`) and hidden elements
    (`_drop_hidden`) there is a character that is neither whitespace nor invisible (alt text of pictures is not counted).

    Promotional mail hides the inbox preview text (preheader) with `display:none` and fills blanks with `&zwnj;&nbsp;`. Taking
    that for text made the body of the plain-text branch vanish even though the HTML branch was pictures only (Pinned by:
    test_core_html_mail_hidden_and_frames.py::TestHtmlAlternativeWithOnlyHiddenText). Deliberate: folded answers
    (`_folded_answers`) are not considered — an HTML branch with nothing else merely gets the plain text added.
    """
    return any(text.strip() for text in _visible_strings(html))


def _calendar_html(message: Message) -> str:
    """HTML for the events of a meeting invitation (`text.mail_calendar_events`), placed after the body: an `Event:` paragraph
    + a list. It renders in the same shape as for plain-text mail (`text.mail_calendar_text`)."""
    return "".join(f"<p>{escape(title)}:</p><ul>{''.join(f'<li>{escape(label)}: {escape(value)}</li>' for label, value in fields)}</ul>"
                   for title, fields in mail_calendar_events(message))


def _head_html(message: Message) -> str:
    """The mail header (`text.mail_head_text`) as HTML, its lines separated by `<br>`."""
    return "<br>".join(escape(line) for line in mail_head_text(message).split("\n"))


def _first_picture(finders: list[Callable[[str], bytes | None]]) -> Callable[[str], bytes | None]:
    """A function that asks the picture finders of several mails (`_mhtml_pictures`) in turn and gives the first picture
    found."""
    if len(finders) == 1:
        return finders[0]
    return lambda source: next((data for find in finders if (data := find(source))), None)


def _plain_paragraphs(text: str) -> str:
    """A plain-text mail body as HTML paragraphs: chunks separated by blank lines become `<p>`, line breaks `<br>`. Runs of
    spaces within a line collapse to one, as in HTML."""
    paragraphs = re.split(r"\n[ \t]*\n", text.strip())
    return "".join(f"<p>{'<br>'.join(escape(line) for line in paragraph.split(chr(10)))}</p>" for paragraph in paragraphs)


def _mhtml_message(data: bytes) -> Message:
    # A file that starts with an mbox separator line (an .eml saved by Thunderbird) has its `>From ` body lines unescaped, as on
    # the mail path (`text.mail_box`) — a mail with a single text/html part kept `>From here`
    # (Pinned by: test_audit_html_text.py::TestFromLineEscapeInASingleHtmlPartMail)
    start = mime_start(data)
    return parse_mail(_unescaped_from_lines(data[start:]) if b"From " in data[:start] else data[start:])


def _html_part(message: Message) -> tuple[bytes, str | None] | None:
    for part in message.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            if payload:
                return payload, part.get_content_charset()
    return None


def _mhtml_pictures(message: Message) -> Callable[[str], bytes | None]:
    """Returns a function that finds the picture parts saved in an MHTML by `<img src>`. They are indexed by
    `cid:<Content-ID>` and Content-Location, and relative addresses are resolved against the location of the first HTML part.
    Picture bytes are decoded only for the ones referred to. Only parts inside the file are considered — external addresses
    are still never fetched (Pinned by: test_text_html.py::TestMhtml::test_picture_saved_with_the_page_is_extracted).
    """
    parts: dict[str, Message] = {}
    base = ""
    for part in message.walk():
        location = str(part.get("Content-Location") or "").strip()
        if part.get_content_type() == "text/html":
            base = base or location
        if part.get_content_maintype() != "image":
            continue
        content_id = str(part.get("Content-ID") or "").strip().strip("<>")
        if content_id:
            parts.setdefault(f"cid:{content_id}", part)
        if location:
            parts.setdefault(location, part)

    def find(source: str) -> bytes | None:
        part = parts.get(source) or (parts.get(_resolved(base, source)) if base else None)
        payload = part.get_payload(decode=True) if part is not None else None
        return payload if isinstance(payload, bytes) and payload else None

    return find


def _resolved(base: str, source: str) -> str:
    """`source` resolved against `base` (`urljoin`), or `source` as written when either cannot be parsed as an address.
    `urljoin` raises ValueError on an unclosed `[` (`//[bad`, 'Invalid IPv6 URL'), and one `src` of a saved page rejected the
    whole document (Pinned by: test_audit_html_text.py::TestAddressThatCannotBeResolvedInMhtml)."""
    try:
        return urljoin(base, source)
    except ValueError:
        return source


def _declared_charset(data: bytes) -> str | None:
    head = data[:_CHARSET_PRESCAN_BYTES]
    match = _META_CHARSET.search(head) or _XML_ENCODING.search(head)
    return match.group(1).decode("ascii", errors="replace") if match else None


def _extract_front(text: str, encoding: str, options: ConvertOptions,
                   saved_picture: Callable[[str], bytes | None] | None) -> Extraction | None:
    """For a large document, read only the front part first. If that alone exceeds the character limit, return that
    extraction and do not parse the rest. None when it cannot be told whether the limit will be exceeded (the whole document
    is read).

    Parsing a 40MB document whole, only for it to be rejected anyway, took 34 s and 2.5GB. The text of the front part is
    contained as is in the text of the whole, so exceeding the limit here means the whole exceeds it too
    (Pinned by: test_text_html.py::TestLargeHtmlStopsEarly). When the first front part (limit × 8 characters) is within the
    limit, the whole is estimated from its text density, and if the whole looks likely to exceed the limit the front part is
    grown enough to exceed it (1.5 times margin, at least double) and examined again — an 'Excel download' (47MB) with
    formatting attributes on every cell had its first front part within the limit, so the whole was parsed again, taking 22 s
    and 2.6GB (Pinned by: test_markup_heavy_document_over_the_limit_is_not_parsed_whole). The first front part starts small, at
    limit × 2 characters — starting at limit × 8, a text-dense 100,000-row 'Excel download' (9MB) took 7.6 s parsing and
    rendering four times what was needed to exceed the limit
    (Pinned by: test_text_html.py::TestLargeHtmlStopsEarly::test_dense_table_over_the_limit_parses_little_more_than_it_needs).
    The front part goes up to half of the document only — beyond that the whole is read.
    The front part does not keep an aria-hidden app root for being 'half of the document text' (`whole=False`) — in the front
    part it was half and stayed, while in the whole it was removed, so text absent from the whole conversion (916 characters)
    exceeded the limit of 966 characters (Pinned by: test_core_html_mail_hidden_and_frames.py::
    TestAriaHiddenAppRootUnderAModal::test_early_stop_on_a_large_page_does_not_count_text_the_whole_page_hides). For a raw
    text element (textarea etc.) reaching the end of the front part, whether to re-read it is decided by whether it is closed
    in the whole document (`_closed_after`) — the interpretation must match the whole conversion
    (Pinned by: test_core_html_raw_text_and_removal.py::TestUnclosedRawTextElementInALargePage). If the front part has an
    unclosed select, the whole is read — lxml discards elements inside a select differently depending on later markup (the
    next select etc.), so the front part exceeded the limit with text absent from the whole conversion
    (Pinned by: TestUnclosedSelectInALargePage). The characters of the front part are counted without Markdown decoration
    characters (`_MARKDOWN_DECORATION`) — in broken markup a `<pre>`/`<math>` caught at the end of the front part exceeded the
    limit with a fence or `$` absent from the whole conversion (Pinned by: test_core_round45.py::TestLimitOnBrokenHtml).
    """
    if options.max_chars is None:
        return None
    size = options.max_chars * _FIRST_FRONT_CHARS_PER_LIMIT
    last = options.max_chars * _LAST_FRONT_CHARS_PER_LIMIT

    while len(text) > size * 2:
        end = text.rfind(">", 0, size)
        if end <= 0:
            return None
        front = text[:end + 1]
        if len(_SELECT_OPENING.findall(front)) > len(_SELECT_CLOSING.findall(front)):
            return None  # an unclosed select is interpreted differently depending on later markup
        closed = _closed_at_the_end(front) | _closed_after(text, end + 1)
        extraction = _extract(front, encoding, options, saved_picture, whole=False, closed=closed)
        front_text = extraction.sections[0].text
        if options.keep_links:
            front_text = _LINK_BRACKETS.sub("", _LINK_ADDRESS.sub("", front_text))
        counted = solid_length(_MARKDOWN_DECORATION.sub("", front_text))
        if extraction.count(counted, options):
            return extraction
        if counted * len(text) <= options.max_chars * len(front):
            if size >= last:
                return None  # at this density the whole may also be within the limit
            size = last
            continue
        size = max(size * 3 // 2, len(front) * options.max_chars * 5 // (counted * 4))  # enough to exceed the limit + 1.25 times margin
    return None


def _extract(text: str, encoding: str, options: ConvertOptions,
             saved_picture: Callable[[str], bytes | None] | None = None, whole: bool = True,
             closed: frozenset[str] | None = None) -> Extraction:
    # For an HTML fragment starting with an XML declaration (`<?xml …?><TABLE>`), bs4 emitted XMLParsedAsHTMLWarning to the
    # caller. This is a document already decided to be read with this parser — XML data goes to text at detection
    # (Pinned by: test_core_html.py::TestXmlDataWithHtmlElementNames).
    with quiet_warnings((XMLParsedAsHTMLWarning, r"opendockit\.extractors\.html")):
        soup = BeautifulSoup(text, "lxml")

    metadata = {"encoding": encoding}
    if soup.title and soup.title.string and soup.title.string.strip():
        metadata["title"] = soup.title.string.strip()

    _reopen_raw_text_elements(soup, _closed_at_the_end(text) if closed is None else closed)
    cut = frozenset() if whole else _open_at_the_end(soup)
    folded = _folded_answers(soup, cut)
    _keep_expander_buttons(soup)
    _keep_svg_labels(soup, cut)
    _decompose_all(soup.find_all(_DROP_TAGS))
    _drop_sidebars(soup)
    # Not only comments: a DOCTYPE in the middle of the body is a node of its own too, and its name came out as the paragraph
    # `html` (Pinned by: test_audit_html_text.py::TestDoctypeInsideTheBody)
    _decompose_all(soup.find_all(string=lambda node: isinstance(node, PreformattedString)))
    _drop_hidden(soup, kept=folded, whole=whole)

    extraction = Extraction(metadata=metadata)
    warning = decoding_warning(encoding)
    if warning:
        extraction.warnings.append(warning)
    places: _Places = {}

    for image in soup.find_all("img"):
        _replace(image, _image_placeholder(image, extraction, options, saved_picture), places)

    # Equations, form values, super/subscripts and strikethrough are turned into text before rendering, so they come out the
    # same on paths that only collect text, such as headings, table cells and folded lists.
    _put_math(soup)
    _put_symbol_fonts(soup)
    _put_form_values(soup)
    _mark_scripts(soup)
    _mark_struck_text(soup)

    _flatten_nested_headings(soup)
    _foster_parent_table_content(soup)
    _nest_google_docs_lists(soup)
    _nest_word_lists(soup)
    _drop_unsettled_cut_table(soup, cut)
    omitted: list[int] = []
    token = _omitted_rows.set(omitted)
    inline = _Inline(options.keep_links, options.keep_emphasis, cut) if options.keep_links or options.keep_emphasis else None
    inline_token = _inline.set(inline)
    try:
        extraction.sections.append(Section(SectionKind.DOCUMENT, 1, _render_nodes(_document_nodes(soup), list_depth=0)))
    finally:
        _inline.reset(inline_token)
        _omitted_rows.reset(token)
    if sum(omitted):
        extraction.warnings.append(omitted_rows_warning(sum(omitted)))
    return extraction


def _drop_sidebars(soup: BeautifulSoup) -> None:
    """Removes `<aside>` (side menus, ads, related posts) outside the main content (`_MAIN_CONTENT_TAGS`). An aside inside the
    main content is kept.

    Removing every aside made the warning/note boxes (`<aside class="warning">`) in the body of technical documents and notices
    vanish without a warning (Pinned by: test_core_html_blocks.py::TestAsideInsideTheMainContent). Deliberate: an aside used as
    a side menu stays when it is inside the main content — removing it loses body text, keeping it only adds a short menu text.
    """
    for aside in soup.find_all("aside"):
        if aside.find_parent(_MAIN_CONTENT_TAGS) is None:
            aside.decompose()


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

    for svg in soup.find_all("svg"):
        if svg.find_parent("svg") is not None or id(svg) in cut:
            continue  # replaced or removed together with the outer svg. The label count of a cut svg is unknown
        labels = [" ".join(label.get_text(" ").split()) for label in svg.find_all(["text", "foreignobject"])
                  if label.find_parent("foreignobject") is None]
        labels = [label for label in labels if label]
        if _is_labelled_svg(labels):
            span = soup.new_tag("span")
            span.string = " ".join(labels)
            _replace(svg, span, places)


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
              and (level := decimal_int(match.group(1), None)) is not None}
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

    for paragraph in soup.find_all("p", style=_WORD_LIST_LEVEL):
        level = decimal_int(_WORD_LIST_LEVEL.search(str(paragraph.get("style"))).group(1), None)
        if level is None or paragraph.find_parent("li") is not None:
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
        return match.group(0).rsplit("-", 1)[0] if match else None

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
    if not title or not body or _MARKDOWN_HEADING.search(body) or title in first_line or title in first_line.replace("*", ""):
        return text
    return f"# {title}\n\n{body}"


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
    for heading in soup.find_all(list(_HEADINGS)):
        if heading.parent is not None and heading.parent.name in _HEADINGS:
            _close_heading_before(soup, heading.parent, heading)
            continue
        outer = heading.find_parent(list(_HEADINGS))
        if outer is not None:
            _move_after(outer, [heading, *heading.next_siblings])


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
            with quiet_warnings((XMLParsedAsHTMLWarning, r"opendockit\.extractors\.html")):
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

        for node in before:
            table.insert_before(node.extract())
        _move_after(table, after)


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
    for button in soup.find_all("button"):
        if button.find_parent(list(_HEADINGS)) is not None:
            button.unwrap()
        elif button.has_attr("aria-expanded") or button.has_attr("aria-controls"):
            button.name = "div"


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
    for tag in soup.find_all(_FOLDED_TAGS):
        if id(tag) in cut or not _FOLDED_STYLE.search(str(tag.get("style", ""))) or tag.has_attr("hidden") or tag.get("aria-hidden") == "true":
            continue
        opener = tag.find_previous_sibling(True)
        if opener is None or not _is_opener(opener) or _is_overlay(tag):
            continue
        length = _solid_length(tag)
        if length and 2 * sum(_solid_length(link) for link in tag.find_all("a")) < length:
            folded.add(id(tag))
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


def _put_math(soup: BeautifulSoup) -> None:
    """Replaces MathML (`<math>`) with the same `$LaTeX$` notation as Office and Hangul (HWP) equations
    (`_shared.render_omml`). Joining only the characters read `r²π` as `r2π` and the fraction 3/4 as `34`
    (Pinned by: test_core_html.py::TestMathMl).

    The LaTeX the document wrote is used first: `<annotation encoding="application/x-tex">` (LaTeXML, MathJax) or, without it,
    `alttext` (Wikipedia's `{\\displaystyle …}` is stripped). The whole element is replaced so that presentation MathML and the
    annotation text do not run together and give the same expression twice. With neither, the presentation MathML is unfolded
    LaTeX-style (`_mathml_latex`). The replacement method is the same as `_put_form_values` (in-place span, linear time).
    """
    for math in _maths_holding_page_blocks(soup):
        math.unwrap()
    for math in soup.find_all("math"):
        if math.parent is None:
            continue  # already replaced together with the outer equation
        latex = _math_source(math)
        if latex is None:
            latex = " ".join(_mathml_latex(math, 0).split())

        math.clear()
        math.attrs = {}
        math.name = "span"
        if latex:
            math.append(f"${latex}$")  # surrounding spacing stays as in the source (so no space appears before a comma)


def _maths_holding_page_blocks(soup: BeautifulSoup) -> list[Tag]:
    """`<math>` elements holding page blocks such as paragraphs, tables and lists (except inside annotation-xml). That is the
    body after an unclosed `<math>` having landed inside it, not an equation — it is unwrapped and read as ordinary text.
    Turning it into an equation collected only the text of MathML elements, so `<math>abc<p>after</p>` became `$after$` (abc
    lost, the whole body after it a one-line equation) (Pinned by: test_core_round45.py::TestLimitOnBrokenHtml). Each block
    walks upwards and stops at an element already visited, so it is linear time."""
    holding: list[Tag] = []
    seen: set[int] = set()

    for block in soup.find_all(_PAGE_BLOCKS_IN_MATH):
        node = block.parent
        while isinstance(node, Tag) and id(node) not in seen and node.name != "annotation-xml":
            seen.add(id(node))
            if node.name == "math":
                holding.append(node)
            node = node.parent
    return holding


def _math_source(math: Tag) -> str | None:
    """The LaTeX of the equation as the document wrote it. None if there is none."""
    for annotation in math.find_all("annotation"):
        if str(annotation.get("encoding", "")).strip().lower() in _TEX_ENCODINGS:
            text = " ".join(annotation.get_text().split())
            if text:
                return text

    text = " ".join(str(math.get("alttext", "")).split())
    if not text:
        return None
    wrapped = _DISPLAY_STYLE.fullmatch(text)
    return wrapped.group(1).strip() if wrapped else text


def _mathml_latex(node: object, depth: int) -> str:
    """Presentation MathML as one LaTeX-style line. The aim is not complete LaTeX but making the structure (scripts,
    fractions, radicals, upper/lower limits) readable (the same as `_shared.omml_to_latex`). Unknown elements have their
    children joined in order. Deeper than `_MAX_MATH_DEPTH`, only the characters are joined."""
    if isinstance(node, NavigableString):
        return "" if isinstance(node, PreformattedString) else str(node).strip()
    if not isinstance(node, Tag):
        return ""
    name = node.name
    if name in _MATH_IGNORED:
        return ""
    if depth >= _MAX_MATH_DEPTH:
        return "".join(node.get_text().split())

    children = [child for child in node.children if isinstance(child, Tag)]
    parts = [_mathml_latex(child, depth + 1) for child in children]

    if name == "semantics":
        return parts[0] if parts else ""  # the first child is presentation, the rest are annotations
    if name == "mspace":
        return " "
    if name in _MATH_SCRIPTS and len(parts) >= 2:
        base, scripts = parts[0], parts[1:]
        if name in ("msup", "msub"):
            return f"{base}{_MATH_SCRIPTS[name]}{_math_group(scripts[0])}"
        if name == "msubsup" and len(scripts) >= 2:
            return f"{base}_{_math_group(scripts[0])}^{_math_group(scripts[1])}"
        # Upper/lower limits (∑, lim, underbars etc.) are always wrapped in braces — so they do not read as glued to the
        # following character, as in `∑_{i=1}^ni`.
        if name == "munderover" and len(scripts) >= 2:
            return f"{base}_{{{scripts[0]}}}^{{{scripts[1]}}}"
        return f"{base}{_MATH_SCRIPTS[name]}{{{scripts[0]}}}"
    if name == "mfrac" and len(parts) >= 2:
        return f"\\frac{{{parts[0]}}}{{{parts[1]}}}"
    if name == "msqrt":
        return f"\\sqrt{{{''.join(parts)}}}"
    if name == "mroot" and len(parts) >= 2:
        return f"\\sqrt[{parts[1]}]{{{parts[0]}}}"
    if name == "mfenced":
        separator = str(node.get("separators", ",")).strip()[:1]
        return f"{node.get('open', '(')}{separator.join(parts)}{node.get('close', ')')}"
    if name == "mtable":
        rows = " \\\\ ".join(parts)
        return f"\\begin{{matrix}}{rows}\\end{{matrix}}"
    if name in ("mtr", "mlabeledtr"):
        return " & ".join(parts)
    if not children:
        return " ".join(node.get_text().split())  # mi·mn·mo·mtext·ms
    # Unknown elements have their child elements and the text between them joined in order. Joining only the elements lost the
    # `-` of `<math>- <span>…</span></math>`
    # (Pinned by: test_core_round45.py::TestLimitOnBrokenHtml::test_text_beside_elements_inside_math_is_kept).
    tag_parts = iter(parts)
    return "".join(next(tag_parts) if isinstance(child, Tag) else _mathml_latex(child, depth + 1) for child in node.children)


def _math_group(text: str) -> str:
    return text if len(text) == 1 else f"{{{text}}}"


def _put_symbol_fonts(soup: BeautifulSoup) -> None:
    """Replace characters drawn with a symbol font (Wingdings, Wingdings 2, Symbol) using that font's table (`_shared.SYMBOL_GLYPHS`, the
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

    for outer in soup.find_all(["sup", "sub"]):
        if outer.parent is None:
            continue  # already replaced together with the outer script
        for script in [*reversed(outer.find_all(["sup", "sub"])), outer]:
            before = script.previous_sibling
            marked = format_script(script.get_text(), SUPERSCRIPT if script.name == "sup" else SUBSCRIPT,
                                   before.get_text() if before is not None else "")
            _replace(script, marked, places)


def _mark_struck_text(soup: BeautifulSoup) -> None:
    """Mark struck-through text (`<del>`, `<s>`, `<strike>`, CSS `text-decoration: line-through`) as `~~text~~`. As plain text,
    `정가 10,000원 8,000원` (list price 10,000 won 8,000 won) reads as two valid values.

    The marks go before the first character and after the last one (whitespace stays outside). A strikethrough inside a strikethrough and
    one with no text are not marked (Pinned by: test_text_html.py::TestNestedScriptsAndStrikethrough).
    A pair never crosses a table cell boundary (`_struck_runs`) — a strikethrough over a whole row or table became `| ~~가 | 100~~ |`, and
    when a table inside a cell was kept as an HTML table the closing mark landed in the inner table's last cell.
    """
    nested: set[int] = set()
    for struck in soup.find_all(_is_struck):
        if id(struck) in nested:
            continue
        nested.update(id(inner) for inner in struck.find_all(_is_struck))
        strings = {id(string) for string in struck.find_all(string=True) if string.strip()}
        for run in _struck_runs(struck, strings):
            _mark_run(run)


def _struck_runs(struck: Tag, strings: set[int]) -> list[list[tuple[NavigableString, Tag | None]]]:
    """Split the text nodes inside a strikethrough into consecutive runs per table cell. Each run gets one pair of `~~`.

    A table held by a strikethrough inside a table cell is part of the cell text as a whole — its text nodes are grouped with the table
    (`(text, table)`) and the marks go outside the table, before and after. Flattened into cell text that is `~~… 20~~` as before; kept as
    an HTML table it is `~~… <table>…</table>~~`. A table held by a strikethrough outside any cell, or a strikethrough on a row or table
    itself, is paired per cell. The document tree is scanned once — no climbing up the ancestors for every text node in deep nesting.
    """
    base = struck if struck.name in ("td", "th") else struck.find_parent(["td", "th"])
    runs: list[list[tuple[NavigableString, Tag | None]]] = []
    last_cell: Tag | None = None
    stack: list[tuple[object, Tag | None, Tag | None]] = [(child, None, base) for child in reversed(struck.contents)]

    while stack:
        node, table, cell = stack.pop()
        if isinstance(node, Tag):
            if table is None and node.name == "table" and cell is not None:
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


def _mark_run(run: list[tuple[NavigableString, Tag | None]]) -> None:
    """Put `~~` before the run's first character and after its last (whitespace stays outside). When that text is in a table inside a
    cell, put it before/after the table."""
    (first, first_table), (last, last_table) = run[0], run[-1]
    texts: dict[int, tuple[NavigableString, str]] = {}

    if first_table is not None:
        first_table.insert_before(NavigableString(_STRIKE))
    else:
        text = str(first)
        texts[id(first)] = (first, f"{text[:len(text) - len(text.lstrip())]}{_STRIKE}{text.lstrip()}")
    if last_table is not None:
        last_table.insert_after(NavigableString(_STRIKE))
    else:
        text = texts.get(id(last), (last, str(last)))[1]
        texts[id(last)] = (last, f"{text.rstrip()}{_STRIKE}{text[len(text.rstrip()):]}")

    for node, text in texts.values():
        node.replace_with(text)


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
    source = str(image.get("src", ""))
    if source.lstrip()[:18].lower().startswith("data:image/svg") or (source.startswith("data:") and _is_lazy(image)):
        return f" [Image: {alt}] " if alt else ""
    if options.extract_images and source.startswith("data:image/") and ";base64," in source:
        try:
            data = base64.b64decode(source.split(",", 1)[1], validate=False)
        except (binascii.Error, ValueError):
            data = b""
        if _is_spacer(data):
            return f" [Image: {alt}] " if alt else ""
        if data:
            return f" {extraction.add_image(data, alt=alt)} "
    elif saved_picture is not None and source:
        data = saved_picture(source.strip())
        if data:
            return f" {extraction.add_image(data, alt=alt)} "
    return f" [Image: {alt}] " if alt else ""


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
    _decompose_all([tag for tag in soup.find_all(_hidden_by_style) if id(tag) not in kept])

    marked = soup.find_all(attrs={"aria-hidden": "true"})
    if not marked:
        return
    holders = _content_holders(soup)
    body = soup.body
    page = max(_solid_length(body), 1) if body is not None else 1
    _decompose_all([tag for tag in marked if id(tag) not in holders
                    and not (whole and body is not None and tag.parent is body and 2 * _solid_length(tag) >= page)])


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
        if parent is None or (isinstance(node, Tag) and node.decomposed):
            continue
        if id(parent) not in places:
            places[id(parent)] = {id(child): index for index, child in enumerate(parent.contents)}
        node.extract(_self_index=places[id(parent)][id(node)])
        if isinstance(node, Tag) and not detach_only:
            node.decompose()


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


def _render_children(node: Tag, list_depth: int) -> str:
    return _render_nodes(node.contents, list_depth)


def _render(node: object, list_depth: int) -> str:
    return _render_nodes([node], list_depth)


def _render_nodes(nodes: list, list_depth: int) -> str:
    """Render the nodes in document order. Elements that only wrap (div, span, font, b …) and quote blocks are descended with a stack
    instead of recursion.

    Unclosed inline tags (a repeated `<font>line<br>`) are nested one level deeper per line by the parser. Recursing per element hit the
    recursion limit at around 330 levels and the document was rejected as a 'corrupted document'. Besides nodes, the stack holds the blank
    line for the spot where a block ends (str) and the spot where a quote block ends (`_QUOTE_END`)
    (Pinned by: test_text_html.py::TestDeepGenericNesting).

    Links and emphasis (`_inline`): `<a href>` and `<b>` / `<strong>` / `<i>` / `<em>` put an `InlineMark` before and after
    their content, and each frame is written out by `_shared.resolve_inline`, which decides what becomes `[text](url)` and
    `**bold**` (one block only, whitespace outside, a block that is bold throughout left unmarked …). The document tree is not
    touched, so everything that measures the text of an element (layout tables, name cells, folded answers) sees what it saw
    before. A span never reaches into a table, list, code block or quote block: those are rendered on their own, and
    `BLOCK_INSIDE` tells the resolver that a span around one is not written
    (Pinned by: test_inline_formatting.py::TestHtmlLinks, TestHtmlEmphasis).
    """
    inline = _inline.get()
    emphasis = inline is not None and inline.emphasis and not _in_heading.get()
    code_depth = 0  # inline code elements we are inside of: their text is not emphasized
    frames: list[list[str | InlineMark]] = [[]]  # the last one is where we are writing now. One is pushed per quote block
    stack: list[object] = list(reversed(nodes))

    while stack:
        node = stack.pop()
        if isinstance(node, NavigableString):
            frames[-1].append(_WHITESPACE.sub(" ", str(node)))
        elif node is _QUOTE_END:
            inner = _single_blank_lines(_frame_text(frames.pop(), inline).strip())
            quoted = "\n".join(f"> {line}" if line.strip() else ">" for line in inner.split("\n"))
            if inline is not None:
                frames[-1].append(BLOCK_INSIDE)
            frames[-1].append(f"{_BLOCK_GAP}{quoted}{_BLOCK_GAP}")
        elif node is _CODE_END:
            code_depth -= 1
        elif isinstance(node, (str, InlineMark)):
            frames[-1].append(node)
        elif isinstance(node, Tag):
            name = node.name
            definitions = _render_definitions(node) if name == "dl" else None
            if definitions is not None:
                if inline is not None:
                    frames[-1].append(BLOCK_INSIDE)
                frames[-1].append(definitions)
                continue
            if name in _LEAF_RENDERERS:
                if inline is not None and name in _BLOCK_LEAVES:
                    frames[-1].append(BLOCK_INSIDE)
                frames[-1].append(_LEAF_RENDERERS[name](node, list_depth))
                continue
            grid = _render_aria_grid(node) if _roles(node) & _GRID_ROLES and name not in _ARIA_SKIPPED_TAGS else None
            if grid is not None:
                if inline is not None:
                    frames[-1].append(BLOCK_INSIDE)
                frames[-1].append(f"\n\n{grid}\n\n")
                continue
            if name == "blockquote" and len(frames) <= _MAX_QUOTE_DEPTH:
                frames.append([])
                stack.append(_QUOTE_END)
            elif name in _BLOCK_TAGS or name == "li":  # a quote block past the limit is treated as a block too
                frames[-1].append(_BLOCK_GAP)
                stack.append(_BLOCK_GAP)
            elif inline is not None:
                if name == "a":
                    url = _link_address(node, inline)
                    if url is not None:
                        frames[-1].append(LINK_ON)
                        stack.append(link_off(url))
                elif name in _CODE_TAGS:
                    code_depth += 1
                    stack.append(_CODE_END)
                elif emphasis and not code_depth and name in _EMPHASIS_MARKS:
                    opening, closing = _EMPHASIS_MARKS[name]
                    frames[-1].append(opening)
                    stack.append(closing)
            stack.extend(reversed(node.contents))
    return _frame_text(frames[0], inline)


def _frame_text(frame: list[str | InlineMark], inline: _Inline | None) -> str:
    """The pieces of one frame of `_render_nodes` as text, with its link and emphasis spans written as Markdown."""
    return "".join(frame) if inline is None else resolve_inline(frame)


def _link_address(anchor: Tag, inline: _Inline) -> str | None:
    """The address `<a href>` is written with, or None when it stays text: links are not kept, the address is not an `http`,
    `https` or `mailto` one (relative addresses, `#fragment`, `javascript:` and empty ones — nothing is resolved against the page
    address, which a saved file does not have), or the element may be cut at the end of the front part of a large document."""
    if not inline.links or id(anchor) in inline.cut:
        return None
    address = anchor.get("href")
    return link_target(address) if isinstance(address, str) else None


def _single_blank_lines(text: str) -> str:
    """Collapse consecutive blank lines into one. The inside of code blocks (``` fences) is left alone.

    A quote block prefixes every line of its inner text with `>`, so several blank lines at block boundaries (between `<div><br></div>`
    paragraphs) became 3–8 empty `>` lines that `clean_text` cannot collapse (1.6x the character count of a quoted Gmail reply;
    Pinned by: test_core_html_blocks.py::TestBlankLinesInsideAQuote).
    """
    lines: list[str] = []
    fenced = False

    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        elif not fenced and not line.strip() and lines and not lines[-1].strip():
            continue
        lines.append(line)
    return "\n".join(lines)


def _roles(node: Tag) -> set[str]:
    return set(str(node.get("role", "")).lower().split())


def _render_aria_grid(node: Tag) -> str | None:
    """Draw a table built from ARIA roles (`role=grid|table|treegrid` > `row` > cells) as a grid, like a real table. Only when there are at
    least two rows and some row has at least two cells; otherwise None — it is descended as blocks as before.

    div tables with no `<table>` (AG Grid, list screens of in-house systems) were scattered into one paragraph per cell, and the columns
    shifted when a cell was empty (Pinned by: test_core_html.py::TestAriaGridTables). Rows are the nearest `row` descendants, cells the
    nearest cell descendants of that row, and an empty cell still counts as a cell. With `aria-colindex` (1-based) the cell goes in that
    column (cells left out by virtual scrolling). Spans (`aria-colspan`) are not considered.
    """
    if _aria_grid_depth.get() >= _MAX_ARIA_GRID_DEPTH:
        return None
    rows = _role_descendants(node, wanted={"row"})
    cells = [_role_descendants(row, wanted=_ARIA_CELL_ROLES) for row in rows]
    if len(rows) < 2 or max(map(len, cells)) < 2:
        return None

    grid = TableGrid()
    token = _aria_grid_depth.set(_aria_grid_depth.get() + 1)
    try:
        for row_index, row_cells in enumerate(cells):
            col = 0
            for cell in row_cells:
                index = decimal_int(str(cell.get("aria-colindex", "")).strip(), 0)
                if col < index <= col + _MAX_ARIA_COLUMN_SKIP:
                    col = index - 1
                grid.place(row_index, col, _cell_text(cell))
                col += 1
    finally:
        _aria_grid_depth.reset(token)

    omitted = _omitted_rows.get()
    rendered = grid.render()
    if omitted is not None:
        omitted.append(grid.omitted_rows())
    return rendered


def _role_descendants(node: Tag, *, wanted: set[str]) -> list[Tag]:
    """The nearest elements under `node` whose role is in `wanted`, in document order. Does not descend into a found element or into inner
    tables (`<table>`, ARIA tables) (with a stack, like `_nearest`)."""
    found: list[Tag] = []
    stack = list(reversed(node.contents))
    while stack:
        child = stack.pop()
        if not isinstance(child, Tag):
            continue
        roles = _roles(child)
        if roles & wanted:
            found.append(child)
        elif child.name != "table" and not roles & _GRID_ROLES:
            stack.extend(reversed(child.contents))
    return found


def _render_definitions(node: Tag) -> str | None:
    """Render a definition list (`<dl>`) as `term: description` lines. Several descriptions are joined with `; `. When a description is a
    block (multiple lines) or long (`_DEFINITION_CHARS`), the description blocks follow the term line. Terms and descriptions used to
    become separate paragraphs, blurring the pairing of '시행일' (effective date) and '2024-07-01'
    (Pinned by: test_text_html.py::TestDefinitionLists).

    None when there is text or an element outside dt / dd (and the divs wrapping them), when there is a folded answer (`display:none` dd —
    `_folded_answers`), or when the definition list is nested deeper than `_MAX_DEFINITION_DEPTH` levels — it is descended as blocks as
    before (question / answer paragraphs of a folded FAQ, the recursion limit in deep nesting).
    """
    entries = _definition_entries(node)
    if entries is None or _definition_depth.get() >= _MAX_DEFINITION_DEPTH:
        return None

    groups: list[tuple[list[str], list[str]]] = []  # (terms, descriptions). A term after a description opens a new group

    token = _definition_depth.set(_definition_depth.get() + 1)
    try:
        for entry in entries:
            text = _render_children(entry, list_depth=0).strip()
            if not groups or (entry.name == "dt" and groups[-1][1]):
                groups.append(([], []))
            if text:
                groups[-1][0 if entry.name == "dt" else 1].append(text)
    finally:
        _definition_depth.reset(token)

    blocks = []
    for terms, details in groups:
        inline = (len(terms) == 1 and details and "\n" not in terms[0] and not any("\n" in detail for detail in details)
                  and sum(map(len, details)) <= _DEFINITION_CHARS)
        if inline:
            separator = " " if terms[0].endswith((":", "：")) else ": "
            blocks.append(f"{terms[0]}{separator}{'; '.join(details)}")
        else:
            blocks.extend([*terms, *details])
    return f"{_BLOCK_GAP}{_BLOCK_GAP.join(blocks)}{_BLOCK_GAP}"


def _definition_entries(node: Tag) -> list[Tag] | None:
    """The dt / dd of a definition list in document order (including pairs wrapped in a div). None when there is other text or another
    element, a folded dd, or no dt."""
    entries: list[Tag] = []
    for child in node.contents:
        for item in (child.contents if isinstance(child, Tag) and child.name == "div" else [child]):
            if isinstance(item, NavigableString):
                if str(item).strip():
                    return None
            elif item.name not in ("dt", "dd") or (item.name == "dd" and _FOLDED_STYLE.search(str(item.get("style", "")))):
                return None
            else:
                entries.append(item)
    return entries if any(entry.name == "dt" for entry in entries) else None


def _title_text(node: Tag) -> str:
    """One-line text of a heading or a table caption (`_inline_text`). A title keeps its links but is not emphasized:
    `<h2><b>제목</b></h2>` is still just a heading, and headings and captions are often bold or italic as a whole
    (Pinned by: test_inline_formatting.py::TestHtmlEmphasis::test_heading_and_caption_are_not_emphasized)."""
    token = _in_heading.set(True)
    try:
        return _inline_text(node)
    finally:
        _in_heading.reset(token)


def _render_heading(node: Tag, list_depth: int) -> str:
    title = _title_text(node)
    if not title:
        return ""
    if _in_cell.get():
        return f"\n{title}\n"  # `## ` was left in the cell text (`_cell_text`)
    return f"\n\n{'#' * _HEADINGS[node.name]} {title}\n\n"


# A line inside code that starts with ```. The indentation is kept and a `\` is inserted before the first backtick.
_CODE_FENCE_LINE = re.compile(r"^([ \t]*)(?=```)", re.MULTILINE)
# Leading indentation of a code line inside a table cell (`_indent_entities`) and the number of columns one tab takes up.
_CODE_INDENT = re.compile(r"^[ \t]+(?=\S)", re.MULTILINE)
_TAB_WIDTH = 4


def _indent_entities(match: re.Match) -> str:
    """Turn the leading spaces / tabs of a code line inside a table cell into that many `&nbsp;`. Whitespace in cell text is trimmed and
    collapsed per line (`render_table`, `clean_text`), which destroyed the block structure of Python code (`def f():<br>if x:<br>return 1`).
    Deliberate: whitespace-like characters (NBSP, full-width space) are collapsed as well, so it is kept in HTML notation like the cell
    line break `<br>` (Pinned by: test_core_header_tables_cell_code_and_terminal_codes.py::TestIndentedCodeInsideATableCell)."""
    return "&nbsp;" * len(match.group().expandtabs(_TAB_WIDTH))


def _render_code_block(node: Tag, list_depth: int) -> str:
    """Render `<pre>` as a ``` code block. A ``` line inside the code is neutralized by inserting a backslash before the backticks.

    Deliberate: it is not wrapped in a longer fence (````). `clean_text` pairs fences by counting the lines that start with ``` regardless
    of fence length, so an inner ``` line looked like it closed the block, the whitespace of the code after it was collapsed, and the
    neutralizing of the unpaired line broke the outer closing fence
    (Pinned by: test_text_html.py::TestCodeBlockWithFenceInside). The neutralized notation is the same as `clean_text`'s unpaired fence.
    """
    places: _Places = {}
    for line_break in node.find_all("br"):
        _replace(line_break, "\n", places)  # it has no text, so it vanished in get_text and the lines ran together
    code = _CODE_FENCE_LINE.sub(r"\1\\", node.get_text().strip(chr(10)))
    if _in_cell.get():
        return f"\n{_CODE_INDENT.sub(_indent_entities, code)}\n"  # the fence was left in the cell text, mixed with `<br>` (`_cell_text`)
    return f"\n\n```\n{code}\n```\n\n"


# Elements that render themselves without being descended into. For elements not listed here `_render_nodes` descends into the children.
_LEAF_RENDERERS = {
    "br": lambda node, list_depth: "\n",
    "img": lambda node, list_depth: "",
    "table": lambda node, list_depth: f"\n\n{_render_table(node)}\n\n",
    "pre": _render_code_block,
    "ul": lambda node, list_depth: f"\n\n{_render_list(node, list_depth)}\n\n",
    "ol": lambda node, list_depth: f"\n\n{_render_list(node, list_depth)}\n\n",
    **dict.fromkeys(_HEADINGS, _render_heading),
}


def _render_list(node: Tag, list_depth: int) -> str:
    if list_depth >= _MAX_LIST_DEPTH:
        return f"{'  ' * list_depth}- {' '.join(node.get_text(' ').split())}"

    lines = []
    ordered = node.name == "ol"
    entries = _list_entries(node)
    # Numbers follow the source: `start`, the counting-down `reversed` (starting at the item count), an item's `value`, and the letter /
    # Roman numeral `type` (Pinned by: test_text_html.py::TestOrderedListNumbers).
    step = -1 if ordered and node.has_attr("reversed") else 1
    number = _list_number(node.get("start"))
    if number is None:
        number = sum(1 for item in entries if isinstance(item, Tag) and item.name == "li") if step < 0 else 1
    shape = _LIST_NUMBER_SHAPES.get(str(node.get("type", "")).strip())
    # Items of a Confluence checklist (li class="checked") and a Notion to-do list (div.checkbox-on / checkbox-off in the li) carry their
    # done state as the same symbols as form checkboxes — the done state used to be lost
    # (Pinned by: test_core_html.py::TestConfluenceTaskList, test_core_html_blocks.py::TestNotionToDoList)
    classes = node.get_attribute_list("class")
    task_list = "inline-task-list" in classes
    to_do_list = "to-do-list" in classes
    for item in entries:
        if isinstance(item, str):
            lines.append(f"{'  ' * list_depth}- {item}")  # text outside the items. No number is written
            continue
        if item.name in {"ul", "ol"}:
            lines.append(_render_list(item, list_depth + 1))
            continue
        if item.name != "li":
            lines.append(f"\n{_render(item, list_depth).strip()}\n")  # a table or code block outside the items
            continue

        # Nested lists belonging to this item: not only direct children but also ones wrapped in div / p. Missing a wrapped one merges it
        # into the item text as the single line "- 하위 1 - 하위 2" (sub 1, sub 2). Lists of deeper items and lists inside table cells are
        # handled when those are rendered.
        # Each nested list is detached leaving a placeholder in its place, the item text is drawn, and the text is then split at the
        # placeholders to put the lists back in source order.
        # The detached lists are restored after drawing — a table with tables inside its cells draws the same cell twice
        # (`_render_table`), and the second drawing saw only the placeholder text and rejected the whole document with an IndexError
        # (Pinned by: test_core_html.py::TestNestedListInsideATableCell).
        nested = _nearest(item, wanted={"ul", "ol"}, stop={"li", "table"})
        slots = [NavigableString(f"{_LIST_SLOT_OPEN}{index}{_LIST_SLOT_CLOSE}") for index in range(len(nested))]
        for child, slot in zip(nested, slots, strict=True):
            child.replace_with(slot)
        try:
            pieces = _item_pieces(_item_blocks(_render_children(item, list_depth)), len(nested))
        finally:
            for child, slot in zip(nested, slots, strict=True):
                slot.replace_with(child)
        first = pieces.pop(0) if pieces and isinstance(pieces[0], str) and not pieces[0].startswith(("```", "|")) else ""

        # Markdown numbered lists use digits only. A letter number is written after a bullet — it has to stay a list line for the nested
        # indentation to be kept.
        marker = "-"
        if ordered:
            number = _list_number(item.get("value"), number)
            marker = f"- {format_number(number, shape)}." if shape else f"{number}."
        if task_list:
            marker += f" {_CHECKED if 'checked' in item.get_attribute_list('class') else _UNCHECKED}"
        elif to_do_list and (box := item.find(class_=["checkbox-on", "checkbox-off"], recursive=False)) is not None:
            marker += f" {_CHECKED if 'checkbox-on' in box.get_attribute_list('class') else _UNCHECKED}"
        lines.append(_item_text(f"{'  ' * list_depth}{marker} {first}".rstrip(), pieces, nested, list_depth))
        number += step
    return "\n".join(line for line in lines if line.strip()).rstrip("\n")  # leading whitespace is the indentation of nested lists


def _item_pieces(blocks: list[str], nested_count: int) -> list[str | int]:
    """Split the item blocks at the nested-list placeholders into text blocks (str) and nested-list indices (int), in source order. A
    placeholder inside a code block or table does not split the block; it goes after that block. Lists whose placeholder vanished while
    drawing go last (the old order)."""
    pieces: list[str | int] = []

    for block in blocks:
        if block.startswith(("```", "|")):
            pieces.append(_LIST_SLOT.sub("", block))
            pieces.extend(int(found) for found in _LIST_SLOT.findall(block))
            continue
        for position, part in enumerate(_LIST_SLOT.split(block)):
            if position % 2:
                pieces.append(int(part))
            elif part.strip():
                pieces.append(part.strip())

    seen = {piece for piece in pieces if isinstance(piece, int)}
    pieces.extend(index for index in range(nested_count) if index not in seen)
    return pieces


def _item_text(line: str, pieces: list[str | int], nested: list[Tag], list_depth: int) -> str:
    """Append the text blocks and nested lists after the item line, in source order.

    Previously, after the nested lists were detached, all the remaining text was drawn as the item line and blocks and the nested lists
    were appended last, so text after a nested list ('작성 후 서명합니다.' — "fill in, then sign.") moved ahead of the sub-items
    (Pinned by: test_core_text_and_mail.py::TestTextAfterANestedList). A second paragraph, code block or table inside an item is placed
    under the item as a block set off by a blank line — merged into one line, a multi-line command gets joined with spaces. A nested list
    goes on the line right below an item line or a list, and after a blank line when it follows a block. An item that ends with a block is
    set off from the next item by a blank line.
    """
    text, previous = line, "line"

    for piece in pieces:
        if isinstance(piece, int):
            rendered = _render_list(nested[piece], list_depth + 1)
            if not rendered.strip():
                continue
            text += ("\n" if previous in ("line", "list") else "\n\n") + rendered
            previous = "list"
        else:
            text += "\n\n" + piece
            previous = "block"
    return text + "\n" if previous == "block" else text


def _list_number(value: object, default: int | None = None) -> int | None:
    text = str(value).strip() if value is not None else ""
    return int(text) if re.fullmatch(r"[+-]?\d{1,9}", text) else default


def _list_entries(node: Tag) -> list[Tag | str]:
    """The contents of a list in document order: items (li), nested lists / tables / code blocks placed directly outside the items, and
    text outside the items (consecutive text is one chunk).

    Reading only the direct `<li>` children silently lost the nested list in `<ul><li>상위</li><ul>…</ul></ul>` (상위 = parent), items
    wrapped in `<div>`, and text outside the items (common in old editors and hand-written HTML). Wrapping elements are unwrapped. A stack
    is used instead of recursion (same reason as `_nearest`)
    (Pinned by: test_text_html.py::TestListContentOutsideItems).
    """
    entries: list[Tag | str] = []
    loose: list[str] = []

    def flush() -> None:
        text = " ".join("".join(loose).split())
        loose.clear()
        if text:
            entries.append(text)

    stack: list[object] = list(reversed(node.contents))
    while stack:
        child = stack.pop()
        if child is None:  # the spot where a block ended. Break here so it does not join the text that follows
            flush()
        elif isinstance(child, NavigableString):
            loose.append(str(child))
        elif isinstance(child, Tag):
            if child.name in {"li", "ul", "ol", "table", "pre"}:
                flush()
                entries.append(child)
            else:
                if child.name in _BLOCK_TAGS or child.name == "br":
                    flush()
                    stack.append(None)
                stack.extend(reversed(child.contents))
    flush()
    return entries


def _item_blocks(text: str) -> list[str]:
    """Split a list item's content into blocks. Code blocks and tables keep their line structure; in other paragraphs whitespace is
    collapsed to single spaces."""
    blocks = []
    for part in _FENCED.split(text):
        if part.startswith("```"):
            blocks.append(part)
            continue
        for paragraph in _BLANK_LINES.split(part):
            if paragraph.strip():
                is_table = paragraph.lstrip().startswith("|")
                blocks.append(paragraph.strip() if is_table else " ".join(paragraph.split()))
    return blocks


def _own_caption(table: Tag) -> Tag | None:
    """The caption of this table: the first one outside its cells and inner tables (the parser leaves a caption written inside
    tbody or a wrapping form where it is). `table.find("caption")` also found the caption of a table nested in a cell, so the
    outer table took it and it came out twice (Pinned by: test_audit_html_text.py::TestCaptionOfATableInsideACell)."""
    found = _nearest(table, wanted={"caption"}, stop={"table", "td", "th"})
    return found[0] if found else None


def _render_table(table: Tag) -> str:
    caption = _own_caption(table)
    caption_text = _title_text(caption) if caption else ""
    rows = _table_rows(table)
    if (_is_one_column_box(rows) or _is_layout_table(rows)) and _layout_depth.get() < _MAX_LAYOUT_DEPTH:
        rendered = _render_layout_table(rows)
        return f"{caption_text}\n\n{rendered}" if caption_text else rendered
    header_count = _header_row_count(table, rows)
    marks = _indent_marks(rows, header_count)

    grid, header, placed_rows = _table_grid(rows, header_count, embedding=False, marks=marks)
    titles = _header_titles(grid.rows(), rows, header_count, placed_rows) if header_count > 1 else []
    if header_count - len(titles) <= 1 < header_count:
        # When the header rows are just a title row + one row, there is nothing to merge. The grid drawing (`render_table`, `TableGrid`)
        # moves the title out as a paragraph above the table.
        header_count = 1
    if header_count > 1:
        # Join a multi-row header ("요금" (rate) above "정가" / "할인가" (list price / discount price)) top to bottom per column into one
        # row, so the LLM reads each column's meaning in one go.
        # In this case it is a Markdown table (horizontal spans in the body go in the first cell only).
        # The table title rows at the very top (`_header_titles`) are not merged but moved out as paragraphs above the table — the title
        # used to be attached to every column name
        # (Pinned by: test_text_html.py::TestTitleRowInsideMultiRowHeader).
        columns = zip(*header.rows()[len(titles):], strict=False)
        merged = [" ".join(dict.fromkeys(value for value in column if value)) for column in columns]
        # The grid is built without the rows that have no cells at all (an empty tr in thead). The header rows must be counted on the grid
        # as well, or the first data row gets cut off with them
        # (Pinned by: test_quality.py::test_html_empty_header_row_does_not_swallow_the_first_data_row).
        header_rows = sum(1 for index in range(header_count) if index in placed_rows)
        rendered = "\n\n".join([*titles, render_table([merged, *grid.rows()[header_rows:]])])
        drawn = grid
    else:
        headless = header_count == 0 and _names_values_in_pairs(rows)
        rendered, drawn = grid.render(headless=headless), grid  # an HTML table if horizontal spans remain (markdown.TableGrid)
        if any(_inner_tables(cell) for row in rows for cell in _row_cells(row)) and (
                _is_html_table(rendered) or _mostly_nested_tables(rows)):
            # If it is already an HTML table, tables inside cells are kept as one-line HTML tables (`TableGrid.embed`, same shape as
            # HWP / HWPX / DOCX). A Markdown table cannot hold a table in a cell, so the whole outer table would have to become HTML;
            # it is flattened into cell text as before instead — the shape of infoboxes and wiki tables is not changed. But in a
            # name | value table whose text is at least half inner tables (`_is_layout_table`) the inner table is the main content and
            # flattening it into one line loses most of the rows and columns, so it is switched to an HTML table
            # (Pinned by: test_text_html.py::TestLayoutTables::
            # test_tables_inside_the_cells_of_an_html_form_keep_their_rows_and_columns,
            # test_data_table_with_a_small_nested_table_in_one_cell_stays_a_table,
            # test_name_value_table_whose_text_is_mostly_a_nested_table_keeps_both_structures).
            drawn = _table_grid(rows, header_count, embedding=True, marks=marks)[0]
            rendered = drawn.render(headless=headless)

    # Rows dropped by the grid cell limit are warned about once at the end of the document, with the same wording as the other formats
    # (`_shared.omitted_rows_warning`) — the lower rows of an 'Excel download' HTML with very many rows vanished without a warning
    # (Pinned by: test_core_html.py::TestRowsLeftOutByTheGridLimit).
    omitted = _omitted_rows.get()
    if omitted is not None:
        omitted.append(drawn.omitted_rows())
    return f"{caption_text}\n\n{rendered}" if caption_text else rendered


def _header_titles(grid_rows: list[list[str]], rows: list[Tag], header_count: int, placed_rows: set[int]) -> list[str]:
    """Text of the table title rows at the top of a multi-row header. Only rows without vertical spans are considered, and the decision is
    `markdown.table_title` (same rules as grid drawing: a row with a single filled cell, a unit / basis note row, and in a two-column
    table only when what is below is a header or numbers). The last header row is never a title.
    Deliberate: in a two-column table only multi-word text is a title — a single word ('요금', rate) is merged as the parent header of
    '정가 | 할인가' (list price | discount price)
    (Pinned by: test_text_html.py::TestLayoutTables::test_name_value_table_whose_nested_table_has_a_two_row_header_stays_a_layout,
    TestTitleRowInsideMultiRowHeader::test_two_column_table_with_a_spanning_title_row)."""
    placed = [index for index in range(len(rows)) if index in placed_rows]  # grid row position → table row index
    keep = [col for col, column in enumerate(zip(*grid_rows, strict=False)) if any(cell.strip() for cell in column)]
    titles: list[str] = []

    for position, index in enumerate(placed[:min(header_count - 1, len(grid_rows) - 1)]):
        if index >= header_count - 1 or any(_span(cell, "rowspan") > 1 for cell in _row_cells(rows[index])):
            break
        below = [[row[col] for col in keep] for row in grid_rows[position + 1:position + 2 + _HEADER_PROBE_ROWS]]
        title = table_title([grid_rows[position][col] for col in keep], below)
        if title is None or (len(keep) == 2 and " " not in title.strip()):
            break  # a one-word cell ('요금') in a two-column table is the parent header of the two cells below ('정가', '할인가'), so it is merged
        titles.append(title)
    return titles


def _indent_marks(rows: list[Tag], header_count: int) -> dict[int, str]:
    """First-column cell of a body row (`id`) → indent level mark (`└ `, `└─ `, `_shared.cell_level_mark` — the same marks as list levels
    inside a table cell).

    Financial statements and budgets show sub-accounts by indenting with `&nbsp;` or padding, but leading whitespace in cell text is
    stripped when the table is built, so '매출원가' (cost of sales) and its sub-account '재료비' (materials) read as the same level. The
    leading `&nbsp;` / full-width spaces and the inline CSS indentation (padding-left, text-indent, margin-left) of the first-column cells
    are measured, and only cells deeper than the shallowest cell in that column are marked, by depth rank — a table that gives every cell
    the same padding is unchanged. Cells that look like numbers are not touched
    (Pinned by: test_text_html.py::TestIndentedLabelsInTables).

    A one-column table, a cell spanning all columns and a cell holding blocks (a heading, a list, two or more paragraphs) are layout
    boxes, not label cells, so they are not measured — a mail body cell that differed only in padding became '└ ## 결재 요청 알림'
    (Pinned by: test_core_html_layout_cells.py::TestIndentMarksOnlyForLabelCells).
    """
    width = max((sum(_span(cell, "colspan") for cell in _row_cells(row)) for row in rows), default=0)
    if width < 2:
        return {}
    depths: dict[int, float] = {}
    covered_until = 0  # last row whose first column is covered by a rowspan from a row above, + 1

    for index, row in enumerate(rows):
        cells = _row_cells(row)
        if not cells or index < covered_until:
            continue
        first = cells[0]
        covered_until = index + _span(first, "rowspan")
        if index < max(header_count, 1) or _span(first, "colspan") >= width or _holds_blocks(first):
            continue  # with no header row the first row is the header (`render_table`)
        text = " ".join(first.get_text(" ").split())
        if text and not _NUMBER_TEXT.fullmatch(text):
            depths[id(first)] = _indent_depth(first)
    if not depths:
        return {}

    base = min(depths.values())
    deeper = sorted({round(depth - base) for depth in depths.values() if depth - base >= 2})
    ranks = {depth: rank for rank, depth in enumerate(deeper, start=1)}
    return {key: cell_level_mark(min(ranks[round(depth - base)], _MAX_INDENT_RANK))
            for key, depth in depths.items() if round(depth - base) in ranks}


def _holds_blocks(cell: Tag) -> bool:
    """Whether the cell holds block content: a heading, list, table, quote or code block, or two or more paragraphs (p / div). A label
    cell wrapped in a single paragraph (`<p style="margin-left:…">` in HTML saved by Word) does not count."""
    if cell.find([*_HEADINGS, "ul", "ol", "table", "blockquote", "pre", "dl"]) is not None:
        return True
    return len(cell.find_all(["p", "div"], limit=2)) >= 2


def _indent_depth(cell: Tag) -> float:
    """Indent depth of the cell text (approximate px). Each leading `&nbsp;` is 4px, a full-width space 16px, plus the CSS indentation of
    the cell and of its first child element."""
    depth = 0.0
    for node in [cell, *([cell.find(True)] if cell.find(True) is not None else [])]:
        for match in _CSS_INDENT.finditer(str(node.get("style", ""))):
            depth += _css_length(match.group("value"), last=match.group("name") == "padding")
    for string in cell.strings:
        for char in string:
            if char == "\xa0":
                depth += 4
            elif char == "\u3000":
                depth += 16
            elif not char.isspace():
                return depth
    return depth


def _css_length(value: str, *, last: bool) -> float:
    """CSS length (px, pt, em, rem, unitless 0) in px. For the `padding` shorthand the left value is used (the fourth; the second when
    there are two or three)."""
    parts = value.split()
    if last and parts:
        parts = [parts[{1: 0, 2: 1, 3: 1}.get(len(parts), 3)]]
    match = _CSS_LENGTH.fullmatch(parts[0]) if parts else None
    if match is None:
        return 0.0
    # A length of hundreds of digits is `inf`, and rounding the depth raised OverflowError — one style attribute rejected the
    # whole document (Pinned by: test_audit_html_text.py::TestHugeCssLengthInATableCell)
    return min(float(match.group(1)) * _CSS_UNITS[match.group(2).lower()], _MAX_CSS_LENGTH)


def _table_grid(rows: list[Tag], header_count: int, *, embedding: bool,
                marks: dict[int, str] | None = None) -> tuple[TableGrid, TableGrid, set[int]]:
    """The table's grid, a grid of the header rows only (horizontally spanned cells filled in as well), and the indices of the rows that
    have cells placed in them. With `embedding`, tables inside cells are handed to the grid.
    `marks` maps a cell (`id`) → the indent level mark to put before the cell text (`_indent_marks`)."""
    grid = TableGrid()
    header = TableGrid()
    placed_rows: set[int] = set()
    for row_index, row in enumerate(rows):
        for cell in _row_cells(row):
            # A rowspan longer than the table's remaining rows is cut at the end of the table, as browsers do. `rowspan=99` produced 97
            # empty rows (Pinned by: test_text_html.py::test_html_rowspan_longer_than_the_table_does_not_add_empty_rows).
            text, col_span = _cell_text(cell, embed_into=grid if embedding else None), _span(cell, "colspan")
            if marks and id(cell) in marks and text:
                text = marks[id(cell)] + text
            row_span = min(_span(cell, "rowspan"), len(rows) - row_index)
            grid.place_next(row_index, text, row_span=row_span, col_span=col_span)
            placed_rows.update(range(row_index, row_index + row_span))
            if row_index < header_count:
                header.place_next(row_index, text, row_span=min(row_span, header_count - row_index), col_span=col_span,
                                  fill_horizontal=True)
    return grid, header, placed_rows


def _row_cells(row: Tag) -> list[Tag]:
    """Cells (td / th) directly under the row. Same as `find_all(recursive=False)` but without building bs4's filter, which is several
    times faster on large tables."""
    return [child for child in row.contents if isinstance(child, Tag) and child.name in ("td", "th")]


def _inner_tables(cell: Tag) -> list[Tag]:
    """Tables with two or more rows inside the cell (outermost only). A one-row table is kept on one line like cell text."""
    return [table for table in _nearest(cell, wanted={"table"}, stop=set()) if len(_table_rows(table)) > 1]


def _is_html_table(rendered: str) -> bool:
    """Whether a `TableGrid.render` result is an HTML table. Table tags in cell text are neutralized (`neutralize_table_tags`), so a
    `<table>` at the start of a line can only be the table itself."""
    return rendered.startswith("<table>") or "\n<table>" in rendered


def _embedded_grid(table: Tag) -> TableGrid | None:
    """Grid of a table to be handed to a cell. None for what cannot be drawn as a one-line HTML table inside the outer cell (caption,
    multi-row header, layout table, deep nesting) — only the text is flattened into one line as before."""
    rows = _table_rows(table)
    if _embed_depth.get() >= _MAX_EMBED_DEPTH or not _has_one_row_header(table, rows) or _is_layout_table(rows):
        return None
    header_count = _header_row_count(table, rows)

    token = _embed_depth.set(_embed_depth.get() + 1)
    try:
        return _table_grid(rows, header_count, embedding=True)[0]
    finally:
        _embed_depth.reset(token)


def _has_one_row_header(table: Tag, rows: list[Tag]) -> bool:
    """Whether it has no caption or multi-row header and so can sit as a one-line HTML table inside an outer HTML cell (`_embedded_grid`)."""
    return _own_caption(table) is None and _header_row_count(table, rows) <= 1


def _is_layout_table(rows: list[Tag]) -> bool:
    """A table holding a table of two or more rows in a cell is treated as screen layout when one of these holds: the outer table has at
    most two rows; it has one column; at least half of the text is in inner tables (name | value tables excepted —
    `_pairs_names_with_values`); one cell holding an inner table has at least half of the text and that cell's text is mostly paragraphs
    outside the inner table, or the outer table has only one row with two or more cells (the body cell of a notice or HTML mail laid out
    as logo / body / footer).

    A Markdown table cell cannot hold a table, so a data table inside a layout table was merged into the single line
    "이름 점수 홍 90 김 80" (name score Hong 90 Kim 80). When a data table with many rows, like an infobox, holds a small table in one
    cell, the row structure of the outer table matters more and it is left alone — that cell is just part of the table text. A large data
    table with a cell holding one line of description + a table of choices ('parameter | default | description' in the Apache manual) is
    also left alone, because that cell's text is mostly the inner table. One-row tables or paragraphs inside cells are common in data
    tables too, so they are not used as evidence.
    A mail with one head row (logo | notice number) above rows of a single cell is layout even when the table in the body cell is longer
    than that cell's paragraphs — columns only arise when two or more rows put cells side by side. An approval form with several
    name | value rows stays a data table
    (Pinned by: test_text_html.py::TestLayoutTables).
    """
    cells = [cell for row in rows for cell in row.find_all(["td", "th"], recursive=False)]
    holders = [(cell, tables) for cell in cells
               if (tables := [table for table in _nearest(cell, wanted={"table"}, stop=set()) if len(_table_rows(table)) > 1])]
    if not holders:
        return _holds_page_blocks(rows, cells)
    filled = [len(row.find_all(["td", "th"], recursive=False)) for row in rows]
    if sum(1 for count in filled if count) <= 2 or max(filled) == 1:
        return True
    total = sum(_solid_length(cell) for cell in cells)
    inner = {id(cell): sum(_solid_length(table) for table in tables) for cell, tables in holders}
    if 2 * sum(inner.values()) >= total and not (
            _pairs_names_with_values(rows)
            and all(_has_one_row_header(table, _table_rows(table)) for _, tables in holders for table in tables)):
        return True
    one_row_of_columns = sum(1 for count in filled if count > 1) <= 1
    return any(2 * _solid_length(cell) >= total and (one_row_of_columns or _solid_length(cell) >= 2 * inner[id(cell)])
               for cell, _ in holders)


def _holds_page_blocks(rows: list[Tag], cells: list[Tag]) -> bool:
    """A table that is layout even without an inner table: it has at most `_MAX_HEADED_LAYOUT_ROWS` rows and one cell with at least half
    of the table text holds a heading (h1–h6) together with other text, or it is a one-row table of two or more cells in which one cell
    holds at least `_MIN_LAYOUT_CELL_BLOCKS` blocks (paragraphs, lists, headings …) (`_is_layout_table`). Headings and blocks within a
    one-row table inside a cell are not counted. A table whose first row is a header row made entirely of th is a data table.

    A page laid out as left menu | body was drawn as a data table: the whole body ran on inside one header cell joined by `<br>` and the
    headings stayed in the cell complete with '## ' (Pinned by: test_core_html_layout_cells.py::TestLayoutTableWithoutANestedTable).
    Deliberately narrow: a list or a paragraph or two inside a cell is common in data tables too (an '항목 | 내용' (item | content) table
    with a separate header row), and a cell holding only a heading (`<h3>기본</h3>` in the header of a price table) is a column name. A
    table with `<h4>새 기능</h4>` (new features) and a long description in a cell under a '버전 | 변경 사항' (version | changes) header
    was scattered into paragraphs and lost the pairing of version and change — header cells are not used for layout
    (Pinned by: test_core_header_tables_cell_code_and_terminal_codes.py::TestSmallTableWithAHeaderRowIsNotLayout).
    """
    placed = [row for row in rows if _row_cells(row)]
    if len(placed) > _MAX_HEADED_LAYOUT_ROWS:
        return False  # do not measure the text of every cell in a large table
    if len(placed) > 1 and all(cell.name == "th" for cell in _row_cells(placed[0])):
        return False
    total = sum(map(_solid_length, cells))
    if any(
            (headings := _nearest(cell, wanted=set(_HEADINGS), stop={"table"}))
            and 2 * _solid_length(cell) >= total and _solid_length(cell) > sum(map(_solid_length, headings))
            for cell in cells):
        return True
    return len(placed) == 1 and len(cells) >= 2 and any(
        len(_nearest(cell, wanted=_LAYOUT_BLOCKS, stop={"table"})) >= _MIN_LAYOUT_CELL_BLOCKS for cell in cells)


def _pairs_names_with_values(rows: list[Tag]) -> bool:
    """A name | value table: at least three rows have two or more cells and the first cell of every such row is a short name (no inner
    table).

    A data table holding a small table in every row ('구분 | 값' (category | value) + a 2×2 table per row), or an '항목 | 내용'
    (item | content) table holding a long roster table in one cell, had more than half of its text in inner tables, so it was flattened
    as layout and lost its header and name-value pairs. Layouts such as logo | title / menu | body / footer have fewer than three rows
    with two or more cells. When the inner table has a multi-row header or a caption it cannot sit inside an outer HTML cell and is
    flattened into one line, so it stays layout as before (Pinned by: test_text_html.py::TestLayoutTables::
    test_page_layout_whose_body_cell_is_a_table_beside_a_menu_stays_a_layout,
    test_name_value_table_whose_nested_table_has_a_two_row_header_stays_a_layout).
    """
    named = 0
    for row in rows:
        cells = _row_cells(row)
        if len(cells) < 2:
            continue
        if cells[0].find("table") is not None or not 0 < _solid_length(cells[0]) <= _NAME_CHARS:
            return False
        named += 1
    return named >= 3


def _names_values_in_pairs(rows: list[Tag]) -> bool:
    """Whether this is an item / value table without a header: leaving out single-cell rows (full-width body cells) there are two or more
    rows, and the cells of every such row are repetitions of the pair `th, td` (`th, td, th, td`, `th, td(colspan)`).

    In approval forms and contract summary tables that write the name cells as th, the first row is not a column header, yet it was drawn
    as the header and '계약명 | 2024 서버 유지보수' (contract name | 2024 server maintenance) became column names
    (Pinned by: test_core_html.py::TestNameValueTablesWithRowHeaders). A full-width body cell row (`td colspan=4`) under an approval form
    broke the pair check, with the same result (Pinned by: test_core_html_blocks.py::TestNameValueTableWithAFullWidthRow). Deliberately
    narrow: a table where only the first cell is th ('역할(th) | 이름 | 연락처' — role | name | contact) may have a column header as its
    first row and is left alone. A table whose first row is all th is already counted as having a header by `_header_row_count`.
    """
    pairs = 0
    for row in rows:
        names = [cell.name for cell in _row_cells(row)]
        if len(names) == 1:
            continue
        if not names or len(names) % 2 or names != ["th", "td"] * (len(names) // 2):
            return False
        pairs += 1
    return pairs >= 2


def _mostly_nested_tables(rows: list[Tag]) -> bool:
    cells = [cell for row in rows for cell in _row_cells(row)]
    nested = sum(_solid_length(table) for cell in cells for table in _inner_tables(cell))
    return 2 * nested >= sum(_solid_length(cell) for cell in cells)


def _solid_length(node: Tag) -> int:
    return sum(len(text) - text.count(" ") for text in node.get_text(" ").split())


def _is_one_column_box(rows: list[Tag]) -> bool:
    """A table with at most one cell per row — a notice / information box. Drawn as a table it is a single column and gets unwrapped into
    paragraphs anyway (`render_table`), so from the start the blocks inside the cells are drawn as they are, as in a layout table. Drawn
    as cell text (`_cell_text`), the `##` of headings inside the cell and the blank lines between paragraphs vanished, and code blocks
    came out in the body without a fence, `&nbsp;` indentation and all. The same holds with a one-cell th header ('공지사항', notices) —
    a header row is evidence of a data table only when there are two or more columns
    (`_holds_page_blocks`, Pinned by: test_core_round44.py::TestOneColumnBoxTable)."""
    counts = [len(_row_cells(row)) for row in rows]
    return any(counts) and max(counts) == 1


def _render_layout_table(rows: list[Tag]) -> str:
    """Layout table: flatten the cells into blocks in top-to-bottom, left-to-right order; inner tables are each rendered as tables."""
    token = _layout_depth.set(_layout_depth.get() + 1)
    try:
        cells = [_render_children(cell, list_depth=0).strip()
                 for row in rows for cell in row.find_all(["td", "th"], recursive=False)]
    finally:
        _layout_depth.reset(token)
    return "\n\n".join(cell for cell in cells if cell)


def _header_row_count(table: Tag, rows: list[Tag]) -> int:
    """Number of header rows: the number of thead rows when there is a thead, otherwise the number of rows at the top made only of th.

    In a table where every row is made only of th (a name-value table written with th only) just the first row is the header. Counting
    them all as header rows merges the table into a single header row
    (Pinned by: test_text_html.py::TestHeaderRowsOfThOnlyTables). From the second row on, a row is a header only when the header row above
    ties it in with a span — a rowspan from a row above reaches down, or the row directly above has a colspan ('정가' / '할인가' under
    '요금'). When data rows written with th had one td row (a total) below them, all the rows above were merged into a single header row
    and overlapping values in the same column were erased
    (Pinned by: test_core_th_rows_front_tables_and_linear_time.py::TestThRowsAboveADataRow).
    """
    count = _header_rows_on_top(table, rows)
    return 1 if count == len(rows) > 1 else count


def _header_rows_on_top(table: Tag, rows: list[Tag]) -> int:
    """The header row count of `_header_row_count` before 'every row is th' is reduced to the first row."""
    head = table.find("thead")
    if head is not None and head.find_parent("table") is table:
        return sum(1 for row in rows if row.find_parent("thead") is head)
    count = 0
    spanned_until = 0  # last row covered by a header row's rowspan, + 1
    above_spans_columns = False

    for index, row in enumerate(rows):
        cells = row.find_all(["td", "th"], recursive=False)
        if not cells or any(cell.name != "th" for cell in cells):
            break
        if index and index >= spanned_until and not above_spans_columns:
            break
        spanned_until = max(spanned_until, *(index + _span(cell, "rowspan") for cell in cells))
        above_spans_columns = any(_span(cell, "colspan") > 1 for cell in cells)
        count += 1
    return count


def _table_rows(table: Tag) -> list[Tag]:
    """Collect only this table's rows, leaving out the rows of nested tables (rows inside thead / tbody / tfoot included). tfoot rows go
    last.

    HTML4 has `<tfoot>` written before `<tbody>`, so reading in document order put the total row ahead of the data
    (Pinned by: test_text_html.py::TestHtmlInlineRendering::test_table_footer_written_before_the_body_goes_last).
    """
    rows = _nearest(table, wanted={"tr"}, stop={"table"})
    return sorted(rows, key=lambda row: row.parent is not None and row.parent.name == "tfoot")  # stable sort: the rest keeps its order


def _nearest(node: Tag, *, wanted: set[str], stop: set[str]) -> list[Tag]:
    """Collect the `wanted` tags under `node` in document order. Does not descend into a found tag or into `stop` tags.

    Finding all descendants and then climbing the ancestors of each one gets slower with the square of the nesting depth; 20,000 levels
    of tables took 59 seconds. Scan only once, with a stack instead of recursion (Pinned by: test_text_html.py::TestDeepNesting).
    """
    found: list[Tag] = []
    stack = list(reversed(node.contents))
    while stack:
        child = stack.pop()
        if not isinstance(child, Tag):
            continue
        if child.name in wanted:
            found.append(child)
        elif child.name not in stop:
            stack.extend(reversed(child.contents))
    return found


def _cell_text(cell: Tag, *, embed_into: TableGrid | None = None) -> str:
    """Text of a cell. A line break inside a cell (`<br>`) is emitted by `_render` as a newline and written as `<br>` by `render_table`.
    Turning it into a newline character here beforehand lets the whitespace cleanup of text nodes collapse it into a space
    (`A-100<br>B-200` → `A-100 B-200`)
    (Pinned by: test_text_html.py::TestHtmlInlineRendering::test_line_break_inside_a_table_cell_is_kept).

    Headings and code blocks inside a cell (`<h2>`, `<pre>`) are kept as text and line breaks, without the `#` / ``` marks (`_in_cell`).
    Left with their marks they became `| ## 칸제목 |` (칸제목 = cell heading) and ```` ```<br>x=1 ````
    (Pinned by: test_core_word_lists_cell_blocks_and_mail_lines.py::TestHeadingAndCodeInsideATableCell).

    A table inside the cell becomes a placeholder handed to that grid when `embed_into` is given; when it is not given, or the table
    cannot be handed over, only its text is kept on one line. The document tree is restored after drawing — it must be possible to draw
    the same cell twice (flattened, and embedded) (`_render_table`).
    """
    stand_ins: list[tuple[NavigableString, Tag]] = []
    for nested in _nearest(cell, wanted={"table"}, stop=set()):
        grid = _embedded_grid(nested) if embed_into is not None and len(_table_rows(nested)) > 1 else None
        placeholder = embed_into.embed(grid) if embed_into is not None and grid is not None else ""
        stand_in = NavigableString(placeholder or _flat_table_text(nested))
        nested.replace_with(stand_in)
        stand_ins.append((stand_in, nested))
    token = _in_cell.set(True)
    try:
        return _render_children(cell, list_depth=0).strip()
    finally:
        _in_cell.reset(token)
        for stand_in, nested in stand_ins:
            stand_in.replace_with(nested)


def _flat_table_text(table: Tag) -> str:
    """Turn an inner table that cannot be handed to the cell into one line of text: the caption and every cell, rendered inline the same
    way as a heading (`_inline_text`), joined with spaces.

    `get_text(" ")` inserts a space at every text node, so `매출 1<b>2</b>,345원` became `매출 1 2 ,345원` and `CO<sub>2</sub>` became
    `CO _2` (Pinned by: test_core_html_layout_cells.py::TestNestedTableFlattenedIntoACell).
    """
    caption = table.find("caption", recursive=False)
    parts = [_inline_text(caption)] if caption is not None else []
    parts.extend(_inline_text(cell) if cell.find([*_HEADINGS, "blockquote"]) is None else " ".join(cell.get_text(" ").split())
                 for row in _table_rows(table) for cell in _row_cells(row))  # keeps heading / quote marks (`#`, `>`) out of the one line
    return " ".join(part for part in parts if part)


def _span(cell: Tag, attribute: str) -> int:
    """Span count. 1 when it is not a decimal number — `²` and `⑤` pass `isdigit()` but make `int()` fail, which rejected the whole
    document (the `isdecimal()` rule of the module guide;
    Pinned by: test_text_html.py::test_span_attribute_that_is_not_a_plain_number_does_not_reject_the_document).
    The same goes for numbers longer than 4,300 digits (`decimal_int`;
    Pinned by: test_core_malformed_mail_and_html.py::TestHugeNumbersInHtmlAndMail).
    Large values are capped at 200."""
    value = decimal_int(str(cell.get(attribute, "1")).strip(), 1)
    return min(value, 200) if value > 0 else 1


def _inline_text(node: Tag) -> str:
    """Text of a heading or table caption on one line. Uses the same inline rendering as body paragraphs — inserting a space at every text
    node split words and scripts at inline tags (`H<sub>2</sub>O` → `H _2 O`, `인사<em>발령</em>` → `인사 발령`). For a heading that
    holds a table, list or code block only the text is collected, so that Markdown syntax does not get mixed into the one line
    (Pinned by: test_text_html.py::TestHtmlInlineRendering::test_heading_and_caption_use_the_same_inline_rendering_as_paragraphs)."""
    if node.find(["table", "ul", "ol", "pre"]) is not None:
        return " ".join(node.get_text(" ").split())
    return " ".join(_render_children(node, list_depth=0).split())
