"""HTML that arrives as mail: MHTML pages and their pictures, the HTML body parts of a mail and when a mail reads as HTML,
frames that were not saved, and the charset a page declares.
"""

from collections.abc import Callable
from email.message import Message
from html import escape
import itertools
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from bs4.element import PreformattedString

from opendockit.extractors._encoding import decode_text
from opendockit.extractors._html_base import _DROP_TAGS, _PARSER_CALLERS, _WHITESPACE
from opendockit.extractors._html_dom import _closed_at_the_end, _decompose_all, _drop_hidden, _reopen_raw_text_elements, _with_table_around_bare_rows
from opendockit.extractors._mail import (
    MIME_HEAD_BYTES,
    MIME_VERSION,
    _unescaped_from_lines,
    is_sent_mail,
    mail_body_parts,
    mail_body_sequence,
    mail_head_text,
    mail_html_body_parts,
    mail_html_outside_alternative,
    mail_messages,
    mail_plain_body,
    mime_start,
    parse_mail,
)
from opendockit.extractors._mail import mime_header_block as _mime_header_block
from opendockit.extractors._mail_calendar import mail_calendar_events
from opendockit.models import quiet_warnings


# Characters that draw nothing on screen: zero-width spaces and joiners (ZWSP/ZWNJ/ZWJ/WJ/BOM), the combining grapheme joiner
# (U+034F), the soft hyphen. Mails use them as blank filler (`&zwnj;&nbsp;`) (`_has_text`).
_INVISIBLE_TEXT = re.compile("[\u200b-\u200d\u2060\ufeff\u034f\u00ad]")


# Upper bound on the MHTML text/html parts examined when checking whether the body is in a frame (`_unread_frame_warning`).
_MAX_FRAME_PARTS = 50
# The start of an `<iframe>` or `<frame>` tag (also of `<frameset>`, which holds them).
_FRAME_TAG = re.compile(r"<i?frame", re.IGNORECASE)


# The charset the document declares itself: `<meta charset=…>`, `<meta http-equiv="Content-Type" content="…; charset=…">`,
# the encoding of the XML declaration.
_META_CHARSET = re.compile(rb"<meta\b[^>]*?\bcharset\s*=\s*[\"']?\s*([\w.:+-]+)", re.IGNORECASE)
_XML_ENCODING = re.compile(rb"^\s*<\?xml\b[^>]*?\bencoding\s*=\s*[\"']([\w.:+-]+)", re.IGNORECASE)
# Length of the front part searched for a charset declaration. The declaration goes at the very start of <head> (browsers
# look at only 1,024 bytes).
_CHARSET_PRESCAN_BYTES = 4096
# Header of an MHTML (single-file web page): from the very start to the blank line there are only `name: value` lines (and
# continuation lines folded with spaces/tabs), and among them MIME-Version and a multipart/related (or text/html) Content-Type.
# The block is walked line by line by `_mime_header_block` (`_mail.mime_header_block`, shared with `_mail.mime_mail`, which
# detects transfer-encoded mail) — matching the whole block with one regex `(?:name:value\n(?:[ \t]+…\n)*)+\n` backtracks
# once for every way the two quantifiers can split the leading whitespace of the folded lines, and on text whose first line is
# `word:` followed only by indented lines with no blank line (XMP metadata) it did not finish even with 12 lines (a text file
# named .pdf hung for over 180 s)
# (Pinned by: test_detect.py::TestMhtml::test_prose_with_many_indented_lines_is_decided_in_linear_time).
_MHTML_CONTENT_TYPE = re.compile(rb"^Content-Type:\s*(?:multipart/related|text/html)\b", re.IGNORECASE | re.MULTILINE)


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
    """The text/html body parts of a mail (`_mail.mime_mail`) (`_mail.mail_html_body_parts` — in document order, including
    pieces separated by attachments) as (bytes, charset the part declares). An empty list if there are none (a mail with only
    plain-text parts is read as text).

    HTML that is an attachment (Content-Disposition: attachment, or a part that merely has a name — `_mail._is_attachment`) and
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
    """Whether a mail (`_mail.mime_mail`) is read by the HTML extractor: the outer mail has an HTML body, or among the forwarded
    mails (`_mail.mail_messages`) there is one with only an HTML body and no plain-text body, or one with a separate HTML body
    that is not an alternative rendering of the plain text (`_mail.mail_html_outside_alternative`). Everything else is read as
    plain text by the text extractor.

    When a forwarded mail's mixed part held both plain-text and HTML bodies, the text extractor read only the plain text and
    the HTML body vanished without a warning
    (Pinned by: test_core_text_and_mail.py::TestForwardedMail::test_forwarded_mixed_mail_keeps_both_its_plain_and_html_bodies,
    test_forwarded_alternative_with_an_html_footer_keeps_the_footer)."""
    messages, _ = mail_messages(message)
    return bool(mail_html_parts(message)) or any(
        mail_html_parts(forwarded) and (mail_plain_body(forwarded) is None or mail_html_outside_alternative(forwarded))
        for forwarded in messages[1:])


def _mail_body_html(message: Message) -> tuple[str, list[str]]:
    """The body HTML of one mail and the encodings decoded: the body parts (`_mail.mail_body_sequence` — the HTML branch of an
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
    # A page without the text of a frame tag has no frame element: it is not parsed again to look for one. A large page that
    # had been stopped at its front part (`_extract_front`) was parsed whole here whenever the file held a second HTML part
    # (Pinned by: test_audit_html_text.py::TestFrameCheckOfASavedPageWithoutFrames)
    if len(pages) < 2 or _FRAME_TAG.search(page) is None:
        return None
    base = str(pages[0].get("Content-Location") or "").strip()
    frames: dict[str, Message] = {}

    for part in pages[1:]:
        content_id = str(part.get("Content-ID") or "").strip().strip("<>")
        location = str(part.get("Content-Location") or "").strip()
        for key in (f"cid:{content_id}" if content_id else "", location):
            if key:
                frames.setdefault(key, part)

    with quiet_warnings((XMLParsedAsHTMLWarning, _PARSER_CALLERS)):
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
    with quiet_warnings((XMLParsedAsHTMLWarning, _PARSER_CALLERS)):
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
    """HTML for the events of a meeting invitation (`_mail_calendar.mail_calendar_events`), placed after the body: an `Event:` paragraph
    + a list. It renders in the same shape as for plain-text mail (`_mail_calendar.mail_calendar_text`)."""
    return "".join(f"<p>{escape(title)}:</p><ul>{''.join(f'<li>{escape(label)}: {escape(value)}</li>' for label, value in fields)}</ul>"
                   for title, fields in mail_calendar_events(message))


def _head_html(message: Message) -> str:
    """The mail header (`_mail.mail_head_text`) as HTML, its lines separated by `<br>`."""
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
    # the mail path (`_mail.mail_box`) — a mail with a single text/html part kept `>From here`
    # (Pinned by: test_audit_html_text.py::TestFromLineEscapeInASingleHtmlPartMail)
    start = mime_start(data)
    return parse_mail(_unescaped_from_lines(data[start:]) if b"From " in data[:start] else data[start:])


def _html_part(message: Message) -> tuple[bytes, str | None] | None:
    """The first text/html part with content (`mhtml_html_part`). In a sent mail (`_mail.is_sent_mail`) the first body HTML part is
    taken — HTML attachments and the HTML of forwarded mails are not its body (`_mail.mail_html_body_parts`); only when it has none
    is the first text/html part taken as before. An attached `x.html` placed before the body was read instead of the body, with a
    warning saying it had not been read
    (Pinned by: test_audit_web_fixes.py::TestHtmlAttachmentBeforeTheBodyOfAnMhtmlShapedMail)."""
    if is_sent_mail(message):
        for part in mail_html_body_parts(message):
            payload = part.get_payload(decode=True)
            if payload:
                return payload, part.get_content_charset()
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
