"""MIME mail and mailboxes, shared by the text and HTML extractors: finding where a mail starts, parsing it within the
nesting limits, its body parts in document order, forwarded mails, decoded headers and the attachment warning.
"""

import base64
import binascii
import codecs
import email
from email.errors import HeaderParseError
from email.header import Header, decode_header, make_header
from email.message import Message
import quopri
import re

from opendockit.exceptions import CorruptedDocumentError
from opendockit.extractors._encoding import _EVERYDAY_HANGUL, _KOREAN_LEGACY, _WESTERN, _declared_codec, _strict, decode_text


# Number of names written in the mail attachment warning (beyond it 'and N more'; the same number as the PDF attachment warning).
_ATTACHMENT_NAMES_SHOWN = 10
# Maximum characters of one name in that warning, and the control characters (C0, DEL, C1) taken out of it (`_one_line`).
_ATTACHMENT_NAME_CHARS = 100
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


# The header line names of the MIME header block (`mime_header_block`) and the headers used to identify mail. MHTML detection for
# HTML (`html.is_mhtml`) looks at the same block.
# The range searched for the header block. When it was 8KB, Office 365 mail whose headers exceed 8KB with Received, ARC, DKIM and
# X-MS-Exchange lines was not identified as mail and became text, base64 body and all
# (Pinned by: test_core_mail_and_detect.py::TestMailWithALongHeaderBlock). The block is walked only once, line by line, so
# widening the range stays linear.
MIME_HEAD_BYTES = 256 * 1024
_MIME_HEADER_NAME = re.compile(rb"[!-9;-~]+:")
# The mbox separator line (`From - Thu Mar 07 10:15:00 2024`, `From kim@example.com Mon Sep  1 10:00:00 2025`). It stays at the
# very top of an .eml saved by Thunderbird etc. Only lines with a time (`10:15`) after the sender position count — text that
# starts with 'From the start …' does not.
_MBOX_FROM_LINE = re.compile(rb"From \S+ [^\r\n]*\d:\d\d")
# `From ` at the start of a line — a candidate separator line for the next mail of an mbox archive (`mail_box`).
_LINE_START_FROM = re.compile(rb"^From ", re.MULTILINE)
# The `>` mbox put before a body line starting with `From ` (mboxrd adds one more to `>From` lines too). Only one is stripped
# (`_unescaped_from_lines`).
_ESCAPED_FROM_LINE = re.compile(rb"^>(>*From )", re.MULTILINE)
# Upper limit on the number of mails read from an mbox archive (`mail_box`) and the heading text before each mail.
MAX_BOX_MAILS = 100
_MAIL_BOX_TITLE = "Message"
MIME_VERSION = re.compile(rb"^MIME-Version:", re.IGNORECASE | re.MULTILINE)
# Mail that carries a body: multipart (related is MHTML — `html.is_mhtml`) or a single plain-text part.
# A mail whose top-level type is message/rfc822 (a forwarded mail saved on its own, or a mail program that wraps the mail it
# forwards) is mail too: it was text, headers and all (Pinned by: test_audit_web_fixes.py::TestTopLevelForwardedMail).
_MAIL_CONTENT_TYPE = re.compile(rb"^Content-Type:\s*(?:multipart/(?!related\b)[\w.+-]+|text/plain|message/rfc822)\b",
                                re.IGNORECASE | re.MULTILINE)
# Identifying mail without MIME-Version (`_mail_without_mime_version`): the sender, a recipient/subject/date, and a MIME body
# header (a mail Content-Type — related and HTML included — or Content-Transfer-Encoding).
_MAIL_SENDER = re.compile(rb"^From:", re.IGNORECASE | re.MULTILINE)
_MAIL_ADDRESSING = re.compile(rb"^(?:To|Cc|Subject|Date):", re.IGNORECASE | re.MULTILINE)
_MAIL_BODY_HEADER = re.compile(rb"^(?:Content-Type:\s*(?:multipart/[\w.+-]+|text/(?:plain|html))\b|Content-Transfer-Encoding:)",
                               re.IGNORECASE | re.MULTILINE)
# The top-level Content-Type of encrypted mail (`is_encrypted_mail`): S/MIME (application/pkcs7-mime, except the signature-only
# signed-data and certs-only) and PGP/MIME (multipart/encrypted).
_ENCRYPTED_CONTENT_TYPE = re.compile(
    rb"^Content-Type:\s*(?:multipart/encrypted\b|application/(?:x-)?pkcs7-mime\b(?![^\r\n]*smime-type\s*=\s*\"?(?:signed-data|certs-only)))",
    re.IGNORECASE | re.MULTILINE)
# RFC 2047 encoded words (`=?charset?b?…?=`). In a header mixed with 8-bit text only the words are decoded (`_decoded_header`).
_ENCODED_WORD = re.compile(r"(=\?[^?\s]+\?[bBqQ]\?[^?\s]*\?=)")
# The mail headers written out before the body.
_MAIL_HEADERS = ("Subject", "From", "To", "Cc", "Date")
# The sender position of a web page saved by a browser (MHTML): `<Saved by Windows Internet Explorer 8>`, `<Saved by Blink>`.
_SAVED_BY = re.compile(r"\s*<?\s*Saved by\b", re.IGNORECASE)


def mime_header_block(head: bytes) -> bytes | None:
    """The MIME header block at the very start of `head` (through the blank line that ends it), otherwise None. Each line is
    looked at only once (linear time).

    A header line is `name: value` (the name is ASCII without a colon or whitespace), a folded line starts with a space or tab,
    and the first line must be a header line. Lines end with `\\n` or `\\r\\n`, and the block ends with an empty line (a line of
    only whitespace is a folded line). None if any other line intervenes or it ends without a blank line.
    """
    end = _header_block_end(head, 0, len(head))
    return None if end is None else head[:end]


def _header_block_end(data: bytes, start: int, limit: int) -> int | None:
    """The position where the MIME header block (`mime_header_block`) at the very start of `data[start:limit]` ends (after the
    blank line), otherwise None. It looks in place without slicing a copy — an mbox archive calls this for every candidate
    separator line (`mail_box`)."""
    position = start
    while True:
        newline = data.find(b"\n", position, limit)
        if newline < 0:
            return None
        line = data[position:newline].removesuffix(b"\r")
        if not line:
            return newline + 1 if position > start else None
        folded = position > start and line[:1] in (b" ", b"\t")
        if b"\r" in line or not (folded or _MIME_HEADER_NAME.match(line)):
            return None
        position = newline + 1


def mime_start(data: bytes) -> int:
    """Where the MIME header block would start: after the BOM and leading whitespace, and, if what follows is an mbox separator
    line (`_MBOX_FROM_LINE`) with a header block from the next line on, after that line. Mail detection (`mime_mail`) and MHTML
    detection (`html.is_mhtml`) look for the block at the same position.

    The `From - Thu Mar 07 10:15:00 2024` line at the very top of an .eml saved by Thunderbird etc. is not shaped like a header
    line, so the mail became text, base64 body and all (Pinned by: test_core_mail_and_detect.py::TestMailWithAnMboxFromLine).
    It is not skipped unless a header block follows.
    """
    start = _content_start(data)
    after = _after_mbox_from_line(data, start)
    return start if after is None else after


def _content_start(data: bytes) -> int:
    """The position after the BOM and leading whitespace."""
    head = data[:MIME_HEAD_BYTES]
    start = len(codecs.BOM_UTF8) if head.startswith(codecs.BOM_UTF8) else 0
    return len(head) - len(head[start:].lstrip())


def _after_mbox_from_line(data: bytes, start: int) -> int | None:
    """If the line at `start` is an mbox separator line (`_MBOX_FROM_LINE`) and a MIME header block starts on the next line, the
    position of that next line, otherwise None."""
    newline = data.find(b"\n", start, start + MIME_HEAD_BYTES)
    if newline < 0 or not _MBOX_FROM_LINE.match(data, start, newline):
        return None
    after = newline + 1
    return after if _header_block_end(data, after, after + MIME_HEAD_BYTES) is not None else None


def mime_mail(data: bytes) -> Message | None:
    """The message if this is a MIME mail whose leading header block has MIME-Version and a mail Content-Type (a multipart other
    than multipart/related, or text/plain) (.eml, or .mht of the same structure), or a mail without MIME-Version
    (`_mail_without_mime_version`), otherwise None.

    Deliberate: it is read as mail whatever the transfer encoding (base64, quoted-printable, 7bit, 8bit). Previously only mail
    with at least one base64/quoted-printable part was unpacked and the rest was left as text, headers and all, as 'readable
    plain-text mail'; but in 7bit/8bit mail (the Thunderbird default, Gmail's ASCII mail) the MIME boundaries and part headers
    remained, RFC 2047 subjects (`=?utf-8?b?…?=`) were not decoded and the same body of multipart/alternative appeared twice
    (Pinned by: test_core_mail_and_detect.py::TestMailWithoutTransferEncoding). For transfer-encoded mail the base64 itself
    became a 'successful' result
    (Pinned by: test_text_html.py::TestEncodedMail, test_core_mail_and_detect.py::TestPlainMailWithAnEncodedAttachment).
    Mail that omits the MIME-Version line (old groupware, mail sent by scripts) is seen as mail only when the sender, a recipient
    (or subject/date) and a MIME body header are all present (`_mail_without_mime_version`). It used to be text, encoded subject
    and base64 body and all (Pinned by: test_core_mailboxes_and_charsets.py::TestMailWithoutMimeVersion). In that case MHTML
    detection (which needs MIME-Version) does not take it, so text/html and multipart/related are accepted too.
    """
    start = mime_start(data)
    headers = mime_header_block(data[start:start + MIME_HEAD_BYTES])
    if headers is None:
        return None
    if MIME_VERSION.search(headers) is not None:
        if _MAIL_CONTENT_TYPE.search(headers) is None:
            return None
    elif not _mail_without_mime_version(headers):
        return None
    return parse_mail(data[start:])


def parse_mail(data: bytes) -> Message:
    """Read MIME mail bytes into a message. If the multipart boundaries nest deeper than `_MAX_MIME_NESTING` levels it is
    rejected as a corrupted document before parsing (`_mime_nesting_exceeds`), and after reading, multiparts without parts and
    delivery failure reports are turned into text parts (`_readable_parts`).

    The standard library parser checks every open boundary on every line, so 900 levels + a body of 200,000 lines (438KB) took
    18 seconds (lines × depth; beyond 1000 levels it was 'corrupted' anyway through a recursion limit error.
    Pinned by: test_core_malformed_mail_and_html.py::TestDeeplyNestedMultipart). Even at the limit it costs lines × limit —
    32 levels + 2 million lines (4MB) take 5.8 seconds (1 level takes 1.2 seconds).
    Forwarded mails (message/rfc822) nested inside each other have their own bound (`_MAX_FORWARD_NESTING`): the parser reads
    them by recursion, and from about a thousand levels on the mail failed with a recursion error that depends on the stack in
    use (Pinned by: test_audit_web_fixes.py::TestDeeplyNestedForwardedMails).
    """
    exceeded = _mime_nesting_exceeds(data, _MAX_MIME_NESTING)
    if exceeded == "multipart":
        raise CorruptedDocumentError(f"Mail MIME parts nested deeper than {_MAX_MIME_NESTING} levels cannot be read. The file may be corrupted.")
    if exceeded == "forward":
        raise CorruptedDocumentError(f"Mail forwarded inside forwarded mails more than {_MAX_FORWARD_NESTING} levels deep cannot be read. "
                                     "The file may be corrupted.")
    message = email.message_from_bytes(data)
    _readable_parts(message)
    return message


def _mime_nesting_exceeds(data: bytes, limit: int, forward_limit: int = 0) -> str | None:
    """'multipart' if the nesting of multipart boundaries exceeds `limit` levels, 'forward' if forwarded mails (a Content-Type
    line of message/rfc822) nest deeper than `forward_limit` (default `_MAX_FORWARD_NESTING`), otherwise None.

    It scans once over only the line-start `--boundary` lines and the `boundary=` of Content-Type headers (continuation lines
    included): a declaration is pushed as an open boundary, a delimiter line of an open boundary closes the boundaries inside it
    (as the parser does), and a closing line (`--boundary--`) closes everything up to that boundary. A `boundary=` written in
    body text is not a Content-Type line and is not counted. Known accepted gap: a part that ends without a closing line stays
    open and is counted deeper than it really is (sibling parts are closed by the next delimiter line).
    A forwarded mail is a level of its own that only an enclosing boundary closes (like an RFC 2231 declaration below): sibling
    forwarded mails are separated by the delimiter lines of the part around them, a forward of a forward has none."""
    forward_limit = forward_limit or _MAX_FORWARD_NESTING
    stack: list[bytes | int | tuple[str, int]] = []
    depth: dict[bytes | int | tuple[str, int], int] = {}  # open boundary → its position in the stack
    forwards = 0  # forwarded-mail levels in the stack

    for match in _MIME_NESTING_LINE.finditer(data):
        if match.group("forward"):
            key = ("forward", match.start())
            depth[key] = len(stack)
            stack.append(key)
            forwards += 1
            if forwards > forward_limit:
                return "forward"
            continue
        boundary: bytes | int | None = match.group("declared_quoted") or match.group("declared")
        if boundary is not None:
            # An RFC 2231 parameter (`boundary*=us-ascii''b1`, `boundary*0=b1`), which the parser honours too, is counted as a
            # level of its own that no delimiter line of its own closes (its value is not decoded; the key is its position, which
            # no line can equal). Not seen at all, it went around the limit: 600 levels + 200,000 lines took 9 seconds
            # (Pinned by: test_audit_html_text.py::TestMultipartNestingDeclaredWithExtendedParameters). Known accepted gap: a
            # mail with more than `limit` such declarations outside any part closed by an ordinary boundary is rejected.
            if match.group("extended"):
                boundary = match.start()
            if boundary not in depth:
                depth[boundary] = len(stack)
                stack.append(boundary)
                if len(stack) - forwards > limit:
                    return "multipart"
            continue
        line = match.group("delimiter").rstrip()
        closing = line.endswith(b"--") and line[:-2] in depth
        boundary = line[:-2] if closing else line
        if boundary not in depth:
            continue
        for opened in stack[depth[boundary] + (0 if closing else 1):]:
            del depth[opened]
        del stack[len(depth):]
        forwards = sum(1 for opened in stack if isinstance(opened, tuple))
    return None


def _readable_parts(message: Message) -> None:
    """Turn parts that have text to read but are not seen as body parts into text/plain: a multipart that was not split into
    parts because it has no boundary or the boundary never occurs in the body (its body is the text), and the
    message/delivery-status of a delivery failure report (recipient, status code and diagnostics as `name: value` lines).

    Neither is caught on the path that finds body parts (text/plain, text/html), so the body or the failure cause vanished
    without a warning and only the headers remained
    (Pinned by: test_core_malformed_mail_and_html.py::TestMultipartWithoutUsableBoundary).
    """
    for part in _mail_parts(message):
        if part.get_content_type() == "message/delivery-status" and part.is_multipart():
            fields = ["\n".join(f"{name}: {' '.join(str(value).split())}" for name, value in block.items())
                      for block in part.get_payload() if isinstance(block, Message)]
            del part["Content-Transfer-Encoding"]
            part.set_payload("\n\n".join(field for field in fields if field).encode())
            part.set_type("text/plain")
            part.set_param("charset", "utf-8")
        elif part.get_content_maintype() == "multipart" and not part.is_multipart():
            part.set_type("text/plain")


def _mail_without_mime_version(headers: bytes) -> bool:
    """Is a header block without MIME-Version a mail: when the sender (From), one of recipient/subject/date, and a MIME body
    header (a mail Content-Type or Content-Transfer-Encoding) are all present. Conservative — a note that starts with `From:`,
    `To:`, `Subject:` has no body header and stays text."""
    return all(pattern.search(headers) is not None for pattern in (_MAIL_SENDER, _MAIL_ADDRESSING, _MAIL_BODY_HEADER))


def is_encrypted_mail(data: bytes) -> bool:
    """Is the leading header block an encrypted mail (S/MIME enveloped-data, PGP/MIME multipart/encrypted). Only blocks with
    MIME-Version or a sender are considered.

    S/MIME encrypted mail was not identified as mail and was a 'successful' result of headers and base64 ciphertext, and
    PGP/MIME kept only the headers (Pinned by: test_core_mailboxes_and_charsets.py::TestEncryptedMail). Signature-only mail
    (multipart/signed, smime-type=signed-data) does not count.
    """
    start = mime_start(data)
    headers = mime_header_block(data[start:start + MIME_HEAD_BYTES])
    if headers is None or (MIME_VERSION.search(headers) is None and _MAIL_SENDER.search(headers) is None):
        return False
    return _ENCRYPTED_CONTENT_TYPE.search(headers) is not None


def mail_box(data: bytes) -> tuple[list[Message], int]:
    """(the mails to read, the number of mails contained) if this is mail (`mime_mail`), otherwise ([], 0). A file that starts
    with an mbox separator line is split into mails at every line-start separator line (one followed by a header block,
    `_after_mbox_from_line`) and the first `MAX_BOX_MAILS` are read (mbox archive).

    Of an mbox holding several mails only the first was read: with multipart mail the later mails were discarded as the epilogue
    of the first, and with plain-text mail the separator lines and headers of the later mails became body
    (Pinned by: test_core_mail_parts_and_icons.py::TestMailboxWithSeveralMails). Deliberate: an .eml that does not start with a
    separator line is not split — mbox rewrites a 'From …hh:mm' line inside a body as `>From`, so it is a separator line only in
    an archive. When the file splits into two or more, it is split even if the first mail does not pass mail detection
    (`mime_mail`) (two or more of separator line + header block make an archive): when the first mail was a single HTML part,
    multipart/related or an old mail without MIME headers, the later mails were appended as raw text or vanished (Pinned by:
    test_core_mailboxes_and_charsets.py::TestMailboxThatStartsWithAnHtmlMail, TestMailWithoutMimeVersion). An archive is read
    only after it is split — read whole as one mail, the boundaries of mails without closing boundaries pile up between the
    mails, and from the 33rd mail on the whole archive was rejected as 'nested deeper than 32 levels'
    (Pinned by: test_core_mbox_markdown_and_api.py::TestMailboxOfMailsWithoutClosingBoundaries).
    """
    first = _after_mbox_from_line(data, _content_start(data))
    bounds = [] if first is None else [(match.start(), after) for match in _LINE_START_FROM.finditer(data, first)
                                       if (after := _after_mbox_from_line(data, match.start())) is not None]
    if not bounds:
        mail = mime_mail(data if first is None else _unescaped_from_lines(data))
        return ([mail], 1) if mail is not None else ([], 0)

    starts = [first, *(after for _, after in bounds)]
    ends = [line for line, _ in bounds] + [len(data)]
    mails = [parse_mail(_unescaped_from_lines(data[start:end])) for start, end in zip(starts[:MAX_BOX_MAILS], ends, strict=False)]
    return mails, len(starts)


def _unescaped_from_lines(data: bytes) -> bytes:
    """In the mails of a file that starts with an mbox separator line (an archive, or an .eml saved by Thunderbird), strip one
    `>` that mbox put before a body line starting with `From ` (the mboxrd rule). Called after the separator lines have been
    found — a stripped line must not look like the separator line of the next mail.

    `>From here` stayed in the body as is
    (Pinned by: test_core_word_lists_cell_blocks_and_mail_lines.py::TestMailboxFromLineEscapes).
    Known accepted gap: in an old mboxo archive, which does not rewrite `>From` lines, a quoted line that was `>From` to begin
    with loses one `>` as well — the two rules cannot be told apart from the file alone, and without stripping, the common case
    (`From ` in a body) would always be wrong. An .eml without a separator line is not changed.
    """
    return _ESCAPED_FROM_LINE.sub(rb"\1", data)


def mail_box_warning(read: int, total: int) -> str | None:
    """The warning if the mails of an mbox archive were not all read because of the limit (`MAX_BOX_MAILS`)."""
    if read >= total:
        return None
    return f"The mailbox (mbox) holds {total:,} messages, but only the first {read:,} were read. Split the rest off and convert it separately."


def mail_box_title(number: int) -> str:
    """The heading text placed before the `number`-th mail of an mbox archive (`## Message N`). Text and HTML (`<h2>`) mail use
    the same text."""
    return f"{_MAIL_BOX_TITLE} {number}"


def is_sent_mail(message: Message) -> bool:
    """Is this a message with mail headers: it has recipients (To, Cc), or its sender is not a browser's saved-by mark
    (`<Saved by …>`).

    MHTML detection (`html.is_mhtml`) also takes mail with only a single text/html part and HTML mail with embedded pictures
    (multipart/related). The subject, sender and date of such mail vanished
    (Pinned by: test_core_mail_and_detect.py::TestSinglePartHtmlMail). A web page saved by IE, Chrome or Word has no recipient,
    and its sender is absent or `<Saved by …>`.
    """
    if message.get("To") or message.get("Cc"):
        return True
    sender = message.get("From")
    return bool(sender) and _SAVED_BY.match(str(sender)) is None


# The heading text placed before a forwarded mail (message/rfc822 part). Text (`extract_text`) and HTML (`html.extract_html`)
# mail use the same text.
FORWARDED_MAIL_TITLE = "Forwarded message"
# Upper limit on the number of forwarded mails read. Those beyond it are only named in a warning, like attachments (not reached
# unless a mail has been forwarded over and over).
_MAX_FORWARDED_MAILS = 20


def mail_messages(message: Message) -> tuple[list[Message], list[str]]:
    """The mail and the mails forwarded 'as attachment' inside it (message/rfc822 parts) in document order (a forward inside a
    forwarded mail comes right after that mail), and the names of the forwarded mails not read because they are beyond the limit
    (`_MAX_FORWARDED_MAILS`) (forwards inside those are not counted).

    Deliberate: forwarded mails are read too and placed after the outer body. The forwarder's own text
    ('아래 메일 확인 바랍니다', "please see the mail below") does not give an LLM the context. Previously the walk that finds
    body parts (`Message.walk`) went down into forwarded mails, and the HTML body of a forwarded mail pushed out the outer
    plain-text body (Pinned by: test_core_text_and_mail.py::TestForwardedMail).
    """
    messages: list[Message] = []
    skipped: list[str] = []
    stack = [message]

    while stack:
        current = stack.pop()
        if len(messages) > _MAX_FORWARDED_MAILS:
            skipped.append(_mail_name(current))
            continue
        messages.append(current)
        # The mail itself is looked at too: a forwarded mail whose own type is message/rfc822 (a forward of a forward without a
        # multipart around it) holds its mail directly, and the body inside it vanished without a warning
        # (Pinned by: test_audit_web_fixes.py::TestForwardedMailThatIsItselfAForward).
        forwarded = [inner for part in _mail_parts(current) if (inner := _forwarded_message(part)) is not None]
        stack.extend(reversed(forwarded))
    return messages, skipped


def _mail_parts(message: Message) -> list[Message]:
    """The parts of a mail (itself included) in document order. It does not go down into forwarded mails (message/*) —
    `mail_messages` reads those separately."""
    parts: list[Message] = []
    stack = [message]

    while stack:
        part = stack.pop()
        parts.append(part)
        if part.is_multipart() and part.get_content_maintype() != "message":
            stack.extend(reversed([child for child in part.get_payload() if isinstance(child, Message)]))
    return parts


def _forwarded_message(part: Message) -> Message | None:
    """The mail inside if the part is message/rfc822, otherwise None. One wrapped in a transfer encoding (base64,
    quoted-printable) is decoded and parsed again.

    Exchange sometimes sends a forwarded mail wrapped in base64. The parser reads the wrapped text as a mail without headers, so
    the base64 appeared as is where the forwarded mail belongs
    (Pinned by: test_core_mailboxes_and_charsets.py::TestForwardedMailInATransferEncoding). The part is replaced with the decoded
    mail, so a later call returns the same message.
    """
    if part.get_content_type() != "message/rfc822":
        return None
    payload = part.get_payload()
    inner = payload[0] if isinstance(payload, list) and payload and isinstance(payload[0], Message) else None
    encoding = str(part.get("Content-Transfer-Encoding", "")).strip().lower()
    if inner is None or encoding not in ("base64", "quoted-printable"):
        return inner

    # A mail wrapped in base64 is read by the parser as text without headers. With quoted-printable the headers are already
    # parsed, so the header lines are restored and decoded together with the rest.
    wrapped = inner.get_payload()
    if not isinstance(wrapped, str) or (encoding == "base64" and inner.keys()):
        return inner
    if inner.keys():
        wrapped = "".join(f"{name}: {value}\n" for name, value in inner.items()) + "\n" + wrapped
    try:
        raw = (base64.b64decode("".join(wrapped.split()) + "==", validate=False) if encoding == "base64"
               else quopri.decodestring(wrapped.encode("ascii", errors="replace")))
    except (binascii.Error, ValueError):
        return inner
    inner = parse_mail(raw)
    part.set_payload([inner])
    del part["Content-Transfer-Encoding"]
    return inner


def _is_attachment(part: Message, message: Message) -> bool:
    """Is this an attachment of `message`: its Content-Disposition is attachment, or a leaf part that is not the top one has a
    name (`name=`, `filename=`) and no Content-ID or Content-Location for the body to point at — a part with no Disposition, or
    an inline part that is not text (text/*).

    Attachments that carry only a name (old Outlook, groupware) were read as body, so a text/html attachment pushed out the
    plain-text body, with no warning either (Pinned by: test_core_text_and_mail.py::TestNamedMailPartWithoutDisposition).
    Apple Mail and iOS Mail send a PDF placed in the middle of the body as `inline; filename=…`, which was neither read nor
    warned about (Pinned by: test_core_mail_parts_and_icons.py::TestHtmlBodySplitAroundAnInlineAttachment). A picture embedded
    in the body (Content-ID) and the name of a top-level single-part mail are not attachments.
    Known accepted gap: a text part (text/*) with `inline; filename=…` is read as body (text a mail program shows unfolded in
    the body).
    """
    if part is message or part.is_multipart():
        return False
    disposition = part.get_content_disposition()
    if disposition == "attachment":
        return True
    if disposition == "inline" and part.get_content_maintype() == "text":
        return False
    return (disposition in (None, "inline") and part.get_filename() is not None and part.get("Content-ID") is None
            and part.get("Content-Location") is None)


def _mail_name(message: Message) -> str:
    subject = message.get("Subject")
    return _decoded_header(subject) if subject is not None else ""  # counted as an unnamed attachment (`_attachment_list`)


def mail_body_parts(message: Message) -> list[Message]:
    """The body parts of a mail (text/plain, text/html; attachments and forwarded mails excluded)."""
    return [part for part in _mail_parts(message) if part.get_content_type() in ("text/plain", "text/html")
            and not _is_attachment(part, message)]


# Upper limit on the multipart nesting descended to find the body HTML parts (`mail_html_body_parts`). Real mail has three or
# four levels.
_MAX_MIME_DEPTH = 64
# The nesting of multipart boundaries allowed before parsing (`parse_mail`). Even mail forwarded over and over has about ten
# levels.
_MAX_MIME_NESTING = 32
# The nesting of forwarded mails (message/rfc822 inside message/rfc822) allowed before parsing (`parse_mail`). The parser reads
# each level by recursion (about one frame per level) and failed near a thousand levels.
_MAX_FORWARD_NESTING = 100
# The lines `_mime_nesting_exceeds` looks at: a line-start `--boundary`, and the boundary parameter of a Content-Type header (or
# of its continuation line).
_MIME_NESTING_LINE = re.compile(
    rb"^--(?P<delimiter>[^\r\n]*)"
    rb"|^(?:content-type:|[ \t])[^\r\n]*?\bboundary(?P<extended>\*[\d*]*)?\s*=\s*"
    rb"(?:\"(?P<declared_quoted>[^\"\r\n]*)\"|(?P<declared>[^\s;\"]+))"
    rb"|^content-type:\s*(?P<forward>message/rfc822)\b",
    re.IGNORECASE | re.MULTILINE)


def mail_html_body_parts(message: Message) -> list[Message]:
    """All the body HTML parts of a mail (attachments and forwarded mails excluded) in document order. In multipart/alternative
    only the last branch that has HTML (they are different renderings of the same body); in any other multipart (mixed, related)
    all the branches are visited in turn.

    Apple Mail sends an attachment placed in the middle of the body as `multipart/mixed > (text/html ①, attachment, text/html ②)`.
    Only the first text/html part was read, so body ② after the attachment vanished without a warning
    (Pinned by: test_core_mail_parts_and_icons.py::TestHtmlBodySplitAroundAnInlineAttachment).
    """
    return _html_body_parts(message, message, 0)


def _html_body_parts(part: Message, message: Message, depth: int) -> list[Message]:
    if depth > _MAX_MIME_DEPTH or _is_forward(part, message):
        return []
    if not part.is_multipart():
        return [part] if part.get_content_type() == "text/html" and not _is_attachment(part, message) else []
    children = [child for child in part.get_payload() if isinstance(child, Message)]
    if part.get_content_subtype() == "alternative":
        return next((found for child in reversed(children) if (found := _html_body_parts(child, message, depth + 1))), [])
    return [found for child in children for found in _html_body_parts(child, message, depth + 1)]


def _is_forward(part: Message, message: Message) -> bool:
    """Is `part` a forwarded mail (message/*) of `message`, or `message` itself when its type is message/rfc822? Neither has a
    body of `message` in it — `mail_messages` reads the mail inside separately."""
    return part.get_content_maintype() == "message" and (part is not message or part.get_content_type() == "message/rfc822")


def mail_body_sequence(message: Message) -> list[Message]:
    """The body parts of a mail (text/html, text/plain; attachments and forwarded mails excluded) in document order.
    multipart/alternative holds different renderings of the same body, so only one is chosen: the HTML parts of the last branch
    that has HTML (same as `mail_html_body_parts`), or, without HTML, the last branch that has a body. In any other multipart
    (mixed, related) all the branches are visited in turn.

    With a plain-text body + an HTML part (signature, mailing list footer) in a mixed, only the HTML was read and the plain-text
    body vanished without a warning
    (Pinned by: test_core_html_mail_hidden_and_frames.py::TestPlainAndHtmlBodiesSideBySide).
    """
    return _body_sequence(message, message, 0)


def mail_html_outside_alternative(message: Message) -> bool:
    """Is any of the body HTML parts of the mail (`mail_body_sequence`) outside multipart/alternative — then it is not another
    rendering of the plain-text body but a separate body (the HTML body or footer of a mixed) that vanishes if only the plain
    text is read (`html.mail_reads_as_html`)."""
    inside = {id(sub) for part in message.walk() if part.get_content_type() == "multipart/alternative" for sub in part.walk()}
    return any(part.get_content_type() == "text/html" and id(part) not in inside for part in mail_body_sequence(message))


def _body_sequence(part: Message, message: Message, depth: int) -> list[Message]:
    if depth > _MAX_MIME_DEPTH or _is_forward(part, message):
        return []
    if not part.is_multipart():
        return [part] if part.get_content_type() in ("text/plain", "text/html") and not _is_attachment(part, message) else []
    children = [child for child in part.get_payload() if isinstance(child, Message)]
    if part.get_content_subtype() == "alternative":
        return _html_body_parts(part, message, depth) or next(
            (found for child in reversed(children) if (found := _body_sequence(child, message, depth + 1))), [])
    return [found for child in children for found in _body_sequence(child, message, depth + 1)]


def mail_head_text(message: Message) -> str:
    """The mail headers to place before the body (subject, sender, recipients, cc, date). Decodes RFC 2047 encoding
    (`=?euc-kr?b?…?=`)."""
    lines = []

    for name in _MAIL_HEADERS:
        value = message.get(name)
        if value is not None:
            lines.append(f"{name}: {' '.join(_decoded_header(value).split())}")
    return "\n".join(lines)


def _decoded_header(value: object) -> str:
    """A header value as text: raw 8-bit bytes are detected and decoded like a body (`decode_text`), and RFC 2047 words
    (`=?euc-kr?b?…?=`) are decoded.

    A header that writes UTF-8/CP949 bytes as they are, without RFC 2047 encoding (groupware, old mail programs), is wrapped by
    the parser in an `unknown-8bit` `Header`, and `str()` emits a replacement character per byte, so the subject and sender
    became `������` (Pinned by: test_core_text_and_mail.py::TestRawEightBitMailHeaders). It is turned back into the original
    bytes and decoded, and then any RFC 2047 words that may be mixed in are decoded.
    """
    if isinstance(value, Header):
        value = "".join(_header_chunk_text(chunk, charset) for chunk, charset in decode_header(value))
    text = str(value)
    if not text.isascii():
        # `decode_header` returns non-ASCII text mixed with encoded words as bytes of the `\\uc804` form. Only the words are decoded,
        # one by one, and the rest of the text is left as is. Whitespace between two adjacent words is removed (RFC 2047).
        parts = _ENCODED_WORD.split(text)
        return "".join(_decoded_words(part) if index % 2 else
                       ("" if 0 < index < len(parts) - 1 and not part.strip() else part) for index, part in enumerate(parts))
    return _decoded_words(text)


def _decoded_words(text: str) -> str:
    """Decode RFC 2047 words. The charset of a word is read with the superset codec, as for a body
    (`_header_codec` — EUC-KR → CP949).

    When Outlook's `=?ks_c_5601-1987?B?…?=` (an EUC-KR label) contains extended Hangul (됬, 똠), the EUC-KR codec cannot decode
    it and the subject stayed as encoded words
    (Pinned by: test_core_mail_parts_and_icons.py::TestKoreanEncodedWordsWithExtendedHangul). Words that still do not decode are
    decoded piece by piece with `decode_text`.
    """
    # A word whose base64 is broken (`=?utf-8?b?A?=`) raises HeaderParseError, which is not a ValueError: one subject or
    # attachment name rejected the whole mail
    # (Pinned by: test_audit_html_text.py::TestEncodedWordThatCannotBeDecoded).
    try:
        pieces = decode_header(text)
    except (LookupError, UnicodeError, ValueError, HeaderParseError):
        return text
    # In a word labeled Windows-1252/ISO-8859-1 nearly every byte decodes strictly, so a CP949 subject was garbled into `[°øÁö]`.
    # It is judged by its content, like the same declaration on a body
    # (`_latin_labeled_text`, Pinned by: test_core_html_mail_hidden_and_frames.py::TestEncodedWordLabeledLatinButKorean).
    pieces = [(_latin_labeled_text(chunk, charset).encode(), "utf-8") if isinstance(chunk, bytes) and _header_codec(charset) == _WESTERN
              else (chunk, charset) for chunk, charset in pieces]
    try:
        return str(make_header([(chunk, _header_codec(charset)) for chunk, charset in pieces]))
    except (LookupError, UnicodeError, ValueError, HeaderParseError):
        return "".join(_header_chunk_text(chunk, charset) for chunk, charset in pieces)


def _latin_labeled_text(chunk: bytes, charset: str | None) -> str:
    """A header piece labeled Windows-1252/ISO-8859-1: strict CP949 decoding is tried first, as for a body (`decode_text`), but
    if more than half of the non-ASCII characters of that text are not everyday Hangul (KS X 1001 precomposed), it is read as
    declared.

    In a short header, two adjacent Western European letters (ção, öß, üß) decode strictly as a single CP949 hanja, and
    `Informações` became `Informa寤es`
    (Pinned by: test_core_malformed_mail_and_html.py::TestLatinSubjectWithAccentedLetterPairs).
    """
    text, encoding = decode_text(chunk, charset)
    if encoding != _KOREAN_LEGACY:
        return text
    non_ascii = [ch for ch in text if not ch.isascii()]
    if 2 * sum(ch in _EVERYDAY_HANGUL for ch in non_ascii) >= len(non_ascii):
        return text
    return _strict(chunk, _WESTERN) or chunk.decode("latin-1")


def _header_codec(charset: str | None) -> str | None:
    """charset of a header piece → the codec to decode with. The superset, as for a declared charset (`_declared_codec`); unknown
    names and the UTF family as they are."""
    if charset in (None, "unknown-8bit"):
        return charset
    return _declared_codec(charset) or charset


def _header_chunk_text(chunk: bytes | str, charset: str | None) -> str:
    if isinstance(chunk, str):
        return chunk
    codec = _header_codec(charset)
    if codec == _WESTERN:
        return _latin_labeled_text(chunk, charset)
    if codec in (None, "unknown-8bit"):
        return decode_text(chunk, charset)[0]
    try:
        return chunk.decode(codec)
    except (LookupError, UnicodeError):
        return decode_text(chunk, charset)[0]


# Signature parts that are not counted as attachments (S/MIME and PGP detached signatures). They are not content but the
# signature value of the body (`mail_attachment_warning`).
_SIGNATURE_TYPES = frozenset({"application/pkcs7-signature", "application/x-pkcs7-signature", "application/pgp-signature"})
# The Outlook-only formatting part (winmail.dat). This converter cannot convert it separately either.
_TNEF_TYPES = frozenset({"application/ms-tnef", "application/vnd.ms-tnef"})


def mail_attachment_warning(*mails: Message) -> str | None:
    """One warning that they were not read, if the mails (an mbox archive has several) and the mails forwarded inside them
    (`mail_messages`) have attachments (`_is_attachment`) or forwarded mails left unread beyond the limit. Forwarded mails that
    were read are not counted as attachments.

    Signature parts (`_SIGNATURE_TYPES`, smime.p7s) are not counted, and Outlook's winmail.dat (`_TNEF_TYPES`) is reported with a
    sentence saying it cannot be converted separately — for both, 'convert them separately' was wrong advice
    (Pinned by: test_core_mail_parts_and_icons.py::TestSignaturesAndTnefAttachments).
    Known accepted gap: S/MIME that wraps the body inside the signature (smime.p7m) is reported as an attachment as before — the
    body is inside it. Up to `_ATTACHMENT_NAMES_SHOWN` names are written — the one-line warning of a mail with 20,000
    attachments was 229KB
    (Pinned by: test_core_header_tables_cell_code_and_terminal_codes.py::TestManyMailAttachmentNames).
    """
    names, tnef = [], 0

    for mail in mails:
        messages, skipped = mail_messages(mail)
        for current in messages:
            for part in _mail_parts(current):
                if _forwarded_message(part) is not None or not _is_attachment(part, current):
                    continue
                kind = part.get_content_type()
                if kind in _TNEF_TYPES:
                    tnef += 1
                elif kind not in _SIGNATURE_TYPES:
                    names.append(_attachment_name(part))
        names += skipped

    warnings = []
    if names:
        warnings.append(f"{len(names)} mail attachment(s) ({_attachment_list(names)}) were not read. Convert attachments separately.")
    if tnef:
        warnings.append(f"{tnef} Outlook-only attachment(s) (winmail.dat) cannot be read. Attachments or formatted body text inside may be missing.")
    return " ".join(warnings) or None


def _attachment_list(names: list[str]) -> str:
    """The text inside the parentheses of the attachment warning: up to `_ATTACHMENT_NAMES_SHOWN` names are written and the rest
    become 'and N more'; unnamed attachments (empty text) are given only as a count. Unnamed attachments were repeated 10 times
    as 'unnamed, unnamed, …' (Pinned by: test_core_round44.py::TestUnnamedMailAttachments)."""
    named = [flat for name in names if (flat := _one_line(name))]
    parts = [*named[:_ATTACHMENT_NAMES_SHOWN]]

    if len(named) > _ATTACHMENT_NAMES_SHOWN:
        parts[-1] += f" and {len(named) - _ATTACHMENT_NAMES_SHOWN} more"
    if len(named) < len(names):
        parts.append(f"{len(names) - len(named)} unnamed attachment(s)")
    return ", ".join(parts)


def _one_line(name: str) -> str:
    """A name taken from the mail (attachment file name, subject of a forwarded mail) as it goes into the warning: on one line,
    without control characters, and at most `_ATTACHMENT_NAME_CHARS` characters (the same treatment as `mcp_server._one_line`).

    An RFC 2231 name (`filename*=utf-8''a%0Ab`) carries line breaks and has no length limit, so the one-line warning became
    several lines or thousands of characters (Pinned by: test_audit_html_text.py::TestAttachmentNameInTheWarning).
    """
    flat = " ".join(_CONTROL_CHARACTERS.sub(" ", name).split())
    return flat if len(flat) <= _ATTACHMENT_NAME_CHARS else flat[:_ATTACHMENT_NAME_CHARS - 1].rstrip() + "…"


def _attachment_name(part: Message) -> str:
    """The attachment file name, or empty text if there is none (`_attachment_list` counts those separately). A name written as
    RFC 2047 words (`filename="=?UTF-8?B?…?="`) instead of RFC 2231, as Gmail does, is decoded too — it appeared in the warning
    as encoded words (Pinned by: test_core_mail_parts_and_icons.py::TestEncodedAttachmentName)."""
    name = part.get_filename()
    return _decoded_header(name) if name else ""


def mail_plain_body(message: Message, parts: list[Message] | None = None) -> tuple[str, list[str]] | None:
    """The plain-text body parts of a mail (only those among `parts` if given), with the transfer encoding undone, decoded with
    each part's charset and joined: (text, the encoding of each part). None if there is none."""
    return _mail_body(message, "text/plain", parts)


def _mail_body(message: Message, content_type: str, parts: list[Message] | None = None) -> tuple[str, list[str]] | None:
    texts, encodings = [], []

    for part in mail_body_parts(message) if parts is None else parts:
        if part.get_content_type() != content_type:
            continue
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes) and payload.strip():
            text, encoding = decode_text(payload, part.get_content_charset())
            if content_type == "text/plain" and str(part.get_param("format", "")).lower() == "flowed":
                text = _unflowed(text, delete_space=str(part.get_param("delsp", "")).lower() == "yes")
            texts.append(text.strip())
            encodings.append(encoding)
    return ("\n\n".join(texts), encodings) if texts else None


def _unflowed(text: str, *, delete_space: bool) -> str:
    """`format=flowed` (RFC 3676) plain text back into its original paragraphs: a line ending with a space (except the signature
    separator `-- `) is joined with the next line of the same quote depth. With `delete_space` (DelSp=yes) that space is dropped
    when joining. The space added at the start of a line (space-stuffing) is stripped, and quoted lines keep the `> ` shape.

    Paragraphs written as running text were broken at every line (the Thunderbird and Apple Mail default,
    Pinned by: test_core_word_lists_cell_blocks_and_mail_lines.py::TestFormatFlowedPlainMail). Deliberate: lines are not joined
    where the quote depth changes (same as the specification).
    """
    lines: list[list[str]] = []  # the pieces to join, per line. Appending to one string is quadratic on a very long paragraph
    joining_depth: int | None = None  # the quote depth of the previous line, if that line continues

    for line in text.split("\n"):
        line = line.removesuffix("\r")
        depth = len(line) - len(line.lstrip(">"))
        content = line[depth:].removeprefix(" ")
        flowed = content.endswith(" ") and content != "-- "
        if flowed and delete_space:
            content = content[:-1]
        if joining_depth == depth and lines:
            lines[-1].append(content)
        else:
            lines.append([f"{'>' * depth} {content}" if depth else content])
        joining_depth = depth if flowed else None
    return "\n".join("".join(pieces) for pieces in lines)


def forwarded_mail_head(message: Message) -> str:
    """The heading line and headers to place before a forwarded mail."""
    return f"## {FORWARDED_MAIL_TITLE}\n\n{mail_head_text(message)}".strip()
