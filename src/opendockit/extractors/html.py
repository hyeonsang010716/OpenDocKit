"""HTML → Markdown.

Walks the tree itself instead of using a general-purpose converter such as markdownify: HTML tables exported by Korean
systems (including ones disguised as ".xls") are full of rowspan/colspan, so merged cells must be unfolded onto a grid
(`TableGrid`). Links and emphasis are kept the quiet way (`ConvertOptions.keep_links`, `keep_emphasis`): only `http`, `https`
and `mailto` addresses, only `<b>` / `<strong>` / `<i>` / `<em>`, and nothing for bold that covers a whole block
(docs/decisions/inline-formatting.md).
"""

from collections.abc import Callable
from email.message import Message
import re

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from bs4.element import PreformattedString
from lxml import etree

from opendockit.extractors._encoding import decode_text, decoding_warning, decoding_warnings
from opendockit.extractors._html_base import _DROP_TAGS, _PARSER_CALLERS, _Inline, _inline, _omitted_rows, _Places, _replace
from opendockit.extractors._html_dom import (
    _closed_after,
    _closed_at_the_end,
    _decompose_all,
    _document_nodes,
    _drop_hidden,
    _drop_sidebars,
    _drop_unsettled_cut_table,
    _flatten_nested_headings,
    _folded_answers,
    _foster_parent_table_content,
    _image_placeholder,
    _keep_expander_buttons,
    _keep_svg_labels,
    _mark_scripts,
    _mark_struck_text,
    _nest_google_docs_lists,
    _nest_word_lists,
    _open_at_the_end,
    _put_form_values,
    _put_symbol_fonts,
    _reopen_raw_text_elements,
    _with_table_around_bare_rows,
    _with_title_heading,
)
from opendockit.extractors._html_mail import (
    _calendar_html,
    _declared_charset,
    _first_picture,
    _head_html,
    _html_part,
    _mail_body_html,
    _mhtml_message,
    _mhtml_pictures,
    _unread_frame_warning,
    is_mhtml,
    mail_reads_as_html,
)
from opendockit.extractors._html_math import _put_math
from opendockit.extractors._html_render import _render_nodes
from opendockit.extractors._mail import (
    FORWARDED_MAIL_TITLE,
    is_sent_mail,
    mail_attachment_warning,
    mail_box,
    mail_box_title,
    mail_box_warning,
    mail_head_text,
    mail_messages,
)
from opendockit.extractors._notation import omitted_rows_warning
from opendockit.markdown import solid_length
from opendockit.models import ConvertOptions, Extraction, Section, SectionKind, quiet_warnings


# An unclosed select, which the front-part check (`_extract_front`) cannot trust.
_SELECT_OPENING = re.compile(r"<select\b", re.IGNORECASE)
_SELECT_CLOSING = re.compile(r"</select\s*>", re.IGNORECASE)


# Length of the front part read first in a large document = character limit × this multiple (`_extract_front`). A text-dense
# document exceeds the limit with the first front part; if it does not, the front part grows by the text density. If by the
# density the whole document may also be within the limit, it looks as far as the last front part
# (× `_LAST_FRONT_CHARS_PER_LIMIT`), which holds the usual markup ratio of HTML (3–10 times the text).
_FIRST_FRONT_CHARS_PER_LIMIT = 2
_LAST_FRONT_CHARS_PER_LIMIT = 8


# Markdown decoration characters left out when counting the characters of the front part (`_extract_front`). An element cut at
# the end of the front part (an unclosed pre, math, heading …) is interpreted differently in the whole document, so its fence,
# `$` or `#` may be absent from the whole conversion — leaving them out never counts more than there really is.
_MARKDOWN_DECORATION = re.compile(r"[#`$>*~^_{}\\]")
# With links kept, the address part of `[text](address)` and the brackets are left out of that count as well. A link inside an
# element cut at the end of the front part can be plain text in the whole conversion (a heading that turns out to hold a table
# is read as text only), so the front part must not count what the link added. The emphasis marks are already left out (`*`).
# Addresses are percent-encoded (`_inline.link_target`), so they hold no whitespace or parentheses
# (Pinned by: test_inline_formatting.py::TestCharacterLimit::test_front_of_a_large_page_does_not_count_link_addresses).
_LINK_ADDRESS = re.compile(r"\]\((?:https?://|mailto:)[^()\s]*\)", re.IGNORECASE)
_LINK_BRACKETS = re.compile(r"[\[\]]")


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
    # Mail (.eml, `_mail.mime_mail`) has its HTML body part read like MHTML, with the header (subject, sender …) placed before
    # the body (Pinned by: test_text_html.py::TestEncodedMail, test_core_mail_and_detect.py::TestMailWithoutTransferEncoding).
    # Mail read as MHTML (a single text/html part, or multipart/related with embedded photos) also gets its mail header in
    # front when it has one (`_mail.is_sent_mail`).
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


def _extract_mail(mails: list[Message], total: int, options: ConvertOptions) -> Extraction:
    """Mails read as HTML (`_mail.mail_box` — an mbox mailbox holds several): for each mail, the bodies of that mail and of its
    forwarded mails (`_mail.mail_messages`) are joined in order and rendered as one HTML.

    Each body uses its HTML parts or, if there are none, its plain-text part turned into paragraphs. A forwarded mail is
    preceded by the 'Forwarded message' heading and that mail's header (the same shape as the text extractor's
    `_mail.forwarded_mail_head`). When the mailbox holds several mails, each is preceded by `## Message N` and its header
    (`_mail.mail_box_title`, the same shape as the text extractor). Each body is decoded separately with its part's charset and
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
    with quiet_warnings((XMLParsedAsHTMLWarning, _PARSER_CALLERS)):
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
