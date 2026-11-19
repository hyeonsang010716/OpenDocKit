"""MIME mail and mailboxes, shared by the text and HTML extractors: finding where a mail starts, parsing it within the
nesting limits, its body parts in document order, forwarded mails, decoded headers and the attachment warning.
"""

import base64
import codecs
import email
from email.errors import HeaderParseError, InvalidBase64LengthDefect
from email.header import Header, decode_header
from email.message import Message
from email.parser import BytesHeaderParser
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
# forwards) is mail too: it was text, headers and all (Pinned by: test_audit_web_fixes.py::TestTopLevelForwardedMail), and so
# is one of type message/global (`_FORWARD_TYPES`).
# So is a single meeting invitation part (text/calendar — calendar servers and ticketing tools send it alone) and a single
# part that is not text (a scanner's application/pdf, a phone's image/jpeg): both were text, raw headers and base64 and all,
# without a warning (Pinned by: test_core_mail_and_detect.py::TestSinglePartMailThatIsNotPlainText). The single part that is
# not text is the mail's attachment (`_is_attachment`). text/html stays MHTML; other text types are not taken.
_MAIL_CONTENT_TYPE = re.compile(rb"^Content-Type:\s*(?:multipart/(?!related\b)[\w.+-]+|text/(?:plain|calendar)|message/(?:rfc822|global)"
                                rb"|(?:application|image|audio|video|model|font)/[\w.+-]+)\b",
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
    than multipart/related, text/plain, text/calendar, message/rfc822, or a type that is not text — `_MAIL_CONTENT_TYPE`)
    (.eml, or .mht of the same structure), or a mail without MIME-Version
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


# What `_mime_nesting_exceeds` reads the way the parser (`email.feedparser`) does: a line ends at `\r\n`, `\r` or `\n`; a header
# line is `name:`, a folded line or a `From ` line; a delimiter line starts with `--`.
_LINE_END = re.compile(rb"\r\n|\r|\n")
_PARSER_HEADER_LINE = re.compile(rb"From |[!-9;-~]*:|[\t ]")
_DASH_LINE = re.compile(rb"(?<![^\r\n])--[^\r\n]*")
_CONTENT_TYPE_NAME = b"content-type:"
_MULTIPART_NAME = re.compile(rb"multipart/", re.IGNORECASE)
# Content-Type headers already resolved by the parser, by their bytes (a mail repeats a few of them). Longer ones are not kept.
_CACHED_HEADER_BYTES = 200


def _mime_nesting_exceeds(data: bytes, limit: int, forward_limit: int = 0) -> str | None:
    """'multipart' if the nesting of multipart parts exceeds `limit` levels, 'forward' if forwarded mails (message/* parts, each
    holding the next) nest deeper than `forward_limit` (default `_MAX_FORWARD_NESTING`), otherwise None.

    It follows the structure the parser follows, in one pass and without recursion: the header block of a part (walked line by
    line), its type and boundary as the parser itself resolves them from the first Content-Type header (`_part_structure` — RFC
    2231 forms included), and then, skipping the body, the next line that is a delimiter or closing line of an open multipart.
    Such a line ends every part inside that multipart, forwarded mails included; of two open multiparts with the same boundary
    the outer one takes the line, as in the parser. A level is counted only where the parser descends: a multipart with a
    boundary, and a message/* part other than a delivery report. `boundary=` or `Content-Type: message/rfc822` in a body, in a
    text attachment or under a `--` line that is not a delimiter of an open multipart is text.

    The earlier scan of single lines counted what the parser does not descend into: a plain mail quoting 150 MIME part headers
    was rejected as 'forwarded more than 100 levels deep', as were 150 forwarded mails side by side under an RFC 2231 boundary
    (Pinned by: test_audit_web_fixes.py::TestForwardLevelsFollowTheMultipartStructure), and looking for the end of each header
    block that way made a 1MB body of header-like lines take 12 seconds
    (Pinned by: test_audit_web_fixes.py::TestForwardScanOfHeaderLikeBodies). It also failed on `boundary=""`, which the parser
    takes as a boundary (Pinned by: test_audit_web_fixes.py::TestMultipartWithAnEmptyBoundary).
    """
    forward_limit = forward_limit or _MAX_FORWARD_NESTING
    size = len(data)
    # Open containers, outermost first: (separator, is a digest) for a multipart, None for a forwarded mail.
    stack: list[tuple[bytes, bool] | None] = []
    outermost: dict[bytes, int] = {}  # separator (`--boundary`) → the outermost open multipart that has it
    multiparts = forwards = 0
    resolved: dict[bytes, tuple[str, bytes | None]] = {}

    def owner(line: bytes) -> tuple[int, bool] | None:
        """(the open multipart that takes this `--` line, is it its closing line), or None when it is no delimiter."""
        line = line.rstrip(b" \t")
        opening = outermost.get(line, size)
        closing = outermost.get(line[:-2], size) if line.endswith(b"--") else size
        if opening == closing:
            return None
        return (opening, False) if opening < closing else (closing, True)

    def line_at(start: int) -> bytes:
        end = _LINE_END.search(data, start)
        return data[start:end.start() if end else size]

    def close_to(index: int) -> None:
        """Close the containers from `index` on."""
        nonlocal multiparts, forwards
        while len(stack) > index:
            closed = stack.pop()
            if closed is None:
                forwards -= 1
                continue
            multiparts -= 1
            if outermost.get(closed[0]) == len(stack):
                del outermost[closed[0]]

    position = 0
    unread_from_line = False  # the parser gave the last header line (`From …`) back to the body: it opens the next block
    in_digest = False  # a part of a multipart/digest without a Content-Type is a mail
    while True:
        # The header block of a part: up to a blank line (consumed), a line that is not a header line, or a delimiter.
        lines = 1 if unread_from_line else 0
        content_type = content_type_end = -1
        in_content_type = ends_with_from = False
        while position < size:
            if outermost and data.startswith(b"--", position) and owner(line_at(position)) is not None:
                break
            if not _PARSER_HEADER_LINE.match(data, position):
                blank = _LINE_END.match(data, position)
                if blank:
                    position = blank.end()
                break
            end = _LINE_END.search(data, position)
            following = end.end() if end else size
            if data[position:position + 1] in (b" ", b"\t"):
                if in_content_type:
                    content_type_end = following
            else:
                ends_with_from = data.startswith(b"From ", position)
                in_content_type = (content_type < 0 and not ends_with_from
                                   and data[position:position + len(_CONTENT_TYPE_NAME)].lower() == _CONTENT_TYPE_NAME)
                if in_content_type:
                    content_type, content_type_end = position, following
            lines += 1
            position = following
        unread_from_line = ends_with_from and lines > 1

        if content_type < 0:
            kind, separator = ("message" if in_digest else "", None)
        else:
            header = data[content_type:content_type_end]
            found = resolved.get(header)
            if found is None:
                found = _part_structure(header)
                if len(header) <= _CACHED_HEADER_BYTES:
                    resolved[header] = found
            kind, separator = found
        in_digest = False
        if kind == "message":
            stack.append(None)
            forwards += 1
            if forwards > forward_limit:
                return "forward"
            continue  # the mail inside starts right here
        if separator is not None:
            outermost.setdefault(separator, len(stack))
            stack.append((separator, kind == "digest"))
            multiparts += 1
            if multiparts > limit:
                return "multipart"

        # The body: nothing is looked at up to the next delimiter or closing line of an open multipart. After a closing line
        # comes the epilogue, which runs up to the next such line in the same way.
        report = kind == "report"
        while True:
            taken = None
            if outermost:
                for match in _DASH_LINE.finditer(data, position):
                    taken = owner(match.group())
                    if taken is not None:
                        break
            if report:
                # The parser reads the body of a delivery report as header blocks, each a part of its own, and a block that
                # declares a multipart is descended into: multiparts opened one inside the other in one block went around the
                # limit (900 levels and 200,000 lines, 1MB: 43 seconds). The body is not walked the parser's way; every
                # `multipart/` in it counts as a level that stays open to the end of the report
                # (Pinned by: test_audit_web_fixes.py::TestMultipartsInsideADeliveryReport).
                report = False
                end = match.start() if taken is not None else size
                declared = 0
                for _ in _MULTIPART_NAME.finditer(data, position, end):
                    declared += 1
                    if multiparts + declared > limit:
                        return "multipart"
            if taken is None:
                return None
            index, closing = taken
            end = _LINE_END.match(data, match.end())
            position = end.end() if end else size
            if closing:
                close_to(index)
                continue
            close_to(index + 1)
            # Delimiter lines one after another hold no part between them
            while data.startswith(b"--", position):
                line = line_at(position)
                again = owner(line)
                if again is None or again[0] != index:
                    break
                end = _LINE_END.match(data, position + len(line))
                position = end.end() if end else size
            opened = stack[index]
            in_digest = opened is not None and opened[1]
            unread_from_line = False
            break


def _part_structure(header: bytes) -> tuple[str, bytes | None]:
    """What the parser descends into for a part with this Content-Type header (its line and folded lines): ('message', None) for
    a message/* part other than a delivery report (the mail inside is read by recursion), ('report', None) for a delivery
    report (`_mime_nesting_exceeds` counts the multiparts its body declares), ('multipart' or 'digest', the
    separator `--boundary`) for a multipart whose boundary can occur in the data, otherwise ('', None) — a leaf, like a multipart
    without a boundary.

    The header is given to the standard library, so the type and the boundary (quoted, folded, RFC 2231 `boundary*=` and
    `boundary*0=`) are exactly the ones the parser will use; only headers that mention one of the two types get that far.
    """
    lowered = header.lower()
    if b"multipart/" not in lowered and b"message/" not in lowered:
        return "", None
    try:
        part = BytesHeaderParser().parsebytes(header)
        content_type = part.get_content_type()
        boundary = part.get_boundary() if content_type.startswith("multipart/") else None
        separator = None if boundary is None else b"--" + boundary.encode("ascii", errors="surrogateescape")
    except Exception:  # noqa: BLE001 — a header the parser cannot resolve is not descended into here; parsing decides the rest
        return "", None
    if content_type.startswith("message/"):
        return ("report", None) if content_type == "message/delivery-status" else ("message", None)
    if separator is None:
        return "", None
    return ("digest" if content_type == "multipart/digest" else "multipart"), separator


def _readable_parts(message: Message) -> None:
    """Turn parts that have text to read but are not seen as body parts into text/plain: a multipart that was not split into
    parts because it has no boundary or the boundary never occurs in the body (its body is the text), and the
    message/delivery-status of a delivery failure report (recipient, status code and diagnostics as `name: value` lines).

    Neither is caught on the path that finds body parts (text/plain, text/html), so the body or the failure cause vanished
    without a warning and only the headers remained
    (Pinned by: test_core_malformed_mail_and_html.py::TestMultipartWithoutUsableBoundary). The mails forwarded inside the mail
    are walked too: a bounce forwarded to the IT desk as an attachment lost its delivery report, and a forwarded mail without a
    usable boundary its body, while the same mails on their own read whole
    (Pinned by: test_core_malformed_mail_and_html.py::TestReadablePartsOfForwardedMails). The nesting is bounded before the mail
    is parsed (`_mime_nesting_exceeds`).
    """
    stack = [message]
    while stack:
        part = stack.pop()
        kind = part.get_content_type()
        if kind in _REPORT_TYPES and part.is_multipart():
            fields = ["\n".join(f"{name}: {' '.join(str(value).split())}" for name, value in block.items())
                      for block in part.get_payload() if isinstance(block, Message)]
            _as_text(part, "\n\n".join(field for field in fields if field))
        elif kind == "text/rfc822-headers" and not part.is_multipart():
            # The headers of the mail a bounce returns (Postfix, Exchange): which mail it was. Only the headers written before a
            # body are kept; the rest are routing and signatures. Unread, the part vanished without a warning
            # (Pinned by: test_core_malformed_mail_and_html.py::TestReadablePartsOfForwardedMails::test_parts_of_a_report_are_read).
            _as_text(part, mail_head_text(BytesHeaderParser().parsebytes(part_content(part) or b"")))
        elif part.get_content_maintype() == "multipart" and not part.is_multipart():
            part.set_type("text/plain")
        if part.is_multipart():
            stack.extend(reversed([child for child in part.get_payload() if isinstance(child, Message)]))


# The machine-readable part of a delivery report and of a read receipt: header blocks the parser reads as parts, written as
# `name: value` lines (`_readable_parts`). A read receipt (`Disposition: …; displayed`) vanished without a warning.
_REPORT_TYPES = frozenset({"message/delivery-status", "message/disposition-notification"})


def _as_text(part: Message, text: str) -> None:
    """Make `part` a UTF-8 text/plain part holding `text`."""
    del part["Content-Transfer-Encoding"]
    part.set_payload(text.encode())
    part.set_type("text/plain")
    part.set_param("charset", "utf-8")


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
# The types of a part that holds a forwarded mail: message/rfc822, and message/global (RFC 6532), the same for a mail whose
# headers may be UTF-8. The parser holds the mail inside a message/global part as it does for message/rfc822, but only
# message/rfc822 was read, and the part counted as neither body nor attachment: the forwarded mail vanished without a warning
# (Pinned by: test_core_text_and_mail.py::TestInternationalizedForwardedMail).
_FORWARD_TYPES = frozenset({"message/rfc822", "message/global"})
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
    """The mail inside if the part is a forwarded mail (`_FORWARD_TYPES`), otherwise None. One wrapped in a transfer encoding
    (base64, quoted-printable) is decoded and parsed again.

    Exchange sometimes sends a forwarded mail wrapped in base64. The parser reads the wrapped text as a mail without headers, so
    the base64 appeared as is where the forwarded mail belongs
    (Pinned by: test_core_mailboxes_and_charsets.py::TestForwardedMailInATransferEncoding). The part is replaced with the decoded
    mail, so a later call returns the same message.
    """
    if part.get_content_type() not in _FORWARD_TYPES:
        return None
    payload = part.get_payload()
    inner = payload[0] if isinstance(payload, list) and payload and isinstance(payload[0], Message) else None
    encoding = str(part.get("Content-Transfer-Encoding", "")).strip().lower()
    if inner is None or encoding not in ("base64", "quoted-printable"):
        return inner

    # A mail wrapped in base64 is read by the parser as text without headers. With quoted-printable the headers are already
    # parsed, so the header lines are restored and decoded together with the rest. Base64 cut off at any length is decoded up to
    # the cut (`base64_prefix`; Pinned by: test_core_mailboxes_and_charsets.py::TestBase64CutOffAtAnyLength).
    wrapped = inner.get_payload()
    if not isinstance(wrapped, str) or (encoding == "base64" and inner.keys()):
        return inner
    if inner.keys():
        wrapped = "".join(f"{name}: {value}\n" for name, value in inner.items()) + "\n" + wrapped
    try:
        raw = base64_prefix(wrapped) if encoding == "base64" else quopri.decodestring(wrapped.encode("ascii", errors="replace"))
    except ValueError:
        return inner
    inner = parse_mail(raw)
    part.set_payload([inner])
    del part["Content-Transfer-Encoding"]
    return inner


# The types of a single-part mail's own part that are its body: the plain-text and HTML bodies and a meeting invitation
# (`_mail_calendar.mail_calendar_events`). Any other is an attachment of the mail (`_is_attachment`).
_TOP_LEVEL_BODY_TYPES = ("text/plain", "text/html", "text/calendar")


def _is_attachment(part: Message, message: Message) -> bool:
    """Is this an attachment of `message`: its Content-Disposition is attachment, or a leaf part that is not the top one has a
    name (`name=`, `filename=`) and no Content-ID or Content-Location for the body to point at — a part with no Disposition, or
    an inline part that is not text (text/*).

    Attachments that carry only a name (old Outlook, groupware) were read as body, so a text/html attachment pushed out the
    plain-text body, with no warning either (Pinned by: test_core_text_and_mail.py::TestNamedMailPartWithoutDisposition).
    Apple Mail and iOS Mail send a PDF placed in the middle of the body as `inline; filename=…`, which was neither read nor
    warned about (Pinned by: test_core_mail_parts_and_icons.py::TestHtmlBodySplitAroundAnInlineAttachment). A picture embedded
    in the body (Content-ID) and the name of a top-level single-part mail are not attachments.
    The top-level part of a single-part mail is an attachment when it is not a type read as a body (`_TOP_LEVEL_BODY_TYPES`):
    the PDF of a scan-to-email mail gave only the headers, without a warning
    (Pinned by: test_core_mail_and_detect.py::TestSinglePartMailThatIsNotPlainText).
    Known accepted gap: a text/plain or text/html part with `inline; filename=…` is read as body (text a mail program shows
    unfolded in the body). Any other text part with a name is an attachment, inline or not: a `git format-patch --inline` patch
    or an inline CSV is neither body nor calendar, and it vanished without a warning
    (Pinned by: test_core_text_and_mail.py::TestNamedMailPartWithoutDisposition::test_named_inline_text_that_is_not_a_body_is_an_attachment).
    """
    if part.is_multipart():
        return False
    if part is message:
        return part.get_content_type() not in _TOP_LEVEL_BODY_TYPES
    disposition = part.get_content_disposition()
    if disposition == "attachment":
        return True
    if disposition == "inline" and part.get_content_type() in _INLINE_TEXT_TYPES:
        return False
    if part.get_content_type() == "text/calendar" and part.get_param("method") is not None:
        # The invitation itself (iTIP `method=REQUEST`, `CANCEL` …), the branch of an alternative beside its text, even with a
        # `name=`: taken for an attachment, its event was not read and the invitation was reported as an unread file
        # (Pinned by: test_core_mail_parts_and_icons.py::TestNamedInvitationInAnAlternative). A named .ics with
        # `Content-Disposition: attachment` stays an attachment.
        return False
    return (disposition in (None, "inline") and part.get_filename() is not None and part.get("Content-ID") is None
            and part.get("Content-Location") is None)


# Text parts read where they stand even when they carry a name: the body types, and a meeting invitation (`_mail_calendar`).
_INLINE_TEXT_TYPES = frozenset({"text/plain", "text/html", "text/calendar"})


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
    """Is `part` a forwarded mail (message/*) of `message`, or `message` itself when its type is a forwarded mail's
    (`_FORWARD_TYPES`)? Neither has a body of `message` in it — `mail_messages` reads the mail inside separately."""
    return part.get_content_maintype() == "message" and (part is not message or part.get_content_type() in _FORWARD_TYPES)


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
    # A pure-ASCII header is tried whole first, as the mail parser reads it: that takes a word the pattern below does not (one
    # without a charset, `=??q?Re:_plain?=`) and drops the whitespace between adjacent words
    # (Pinned by: test_core_mailboxes_and_charsets.py::TestAsciiHeaderIsDecodedWhole).
    if text.isascii():
        decoded = _decoded_words(text)
        if decoded != text:
            return decoded
    # Either the header mixes non-ASCII text with encoded words — `decode_header` returns that text as bytes of the `\\uc804`
    # form — or it did not decode as a whole because one of its words is broken. Only the words are decoded and the rest of
    # the text is left as is. A run of adjacent words (only whitespace between them, which is removed — RFC 2047) is decoded
    # together, so that a character split over two words is joined: decoded one by one, `제목
    # =?utf-8?b?7JU=?= =?utf-8?b?iOuFlQ==?=` came out with the words as written
    # (Pinned by: test_core_mailboxes_and_charsets.py::TestEightBitHeaderWithSplitEncodedWords).
    parts = _ENCODED_WORD.split(text)
    pieces: list[str] = []
    run: list[str] = []
    gaps: list[str] = []
    for index, part in enumerate(parts):
        if index % 2:
            run.append(part)
        elif run and not part.strip() and index < len(parts) - 1:
            gaps.append(part)  # whitespace between two adjacent words
        else:
            if run:
                pieces.append(_decoded_run(run, gaps))
                run, gaps = [], []
            pieces.append(part)
    return "".join(pieces)


def _decoded_run(words: list[str], gaps: list[str]) -> str:
    """A run of adjacent RFC 2047 words (`gaps` holds the whitespace written between them) decoded together. If the run does
    not decode as a whole because one word is broken (`=?utf-8?b?A?=`), the broken words stay as written and the good words
    between them are decoded, each stretch of adjacent good words together — so that one broken word does not leave the good
    ones encoded, in a header of raw 8-bit text and in a plain ASCII one alike, and a character split over two good words is
    still joined; the whitespace next to a word that stays as written is kept
    (Pinned by: test_core_mailboxes_and_charsets.py::TestEightBitHeaderWithSplitEncodedWords, TestBrokenEncodedWordInAHeader).
    """
    joined = " ".join(words)
    decoded = _decoded_words(joined)
    if decoded != joined or len(words) == 1:
        return decoded
    good = [_decoded_words(word) != word for word in words]
    pieces: list[str] = []
    start = 0
    while start < len(words):
        end = start + 1
        if good[start]:
            while end < len(words) and good[end]:
                end += 1
            pieces.append(_decoded_words(" ".join(words[start:end])))
        else:
            pieces.append(words[start])
        if end < len(words):
            pieces.append(gaps[end - 1])
        start = end
    return "".join(pieces)


# A line break that folds a header: one followed by a space or tab (RFC 5322 §2.2.3)
_FOLD = re.compile(r"\r?\n(?=[ \t])")
# The start of an RFC 2047 word as `decode_header` reads one (its pattern up to the encoded text), and the characters
# `str.splitlines` breaks a line at, as `decode_header` splits the header with it.
_WORD_START = re.compile(r"=\?[^?]*\?[qQbB]\?")
_LINE_BREAK = re.compile("[\n\r\x0b\x0c\x1c-\x1e\x85\u2028\u2029]")


def _decoded_words(text: str) -> str:
    """Decode RFC 2047 words. The charset of a word is read with the superset codec, as for a body
    (`_header_codec` — EUC-KR → CP949).

    When Outlook's `=?ks_c_5601-1987?B?…?=` (an EUC-KR label) contains extended Hangul (됬, 똠), the EUC-KR codec cannot decode
    it and the subject stayed as encoded words
    (Pinned by: test_core_mail_parts_and_icons.py::TestKoreanEncodedWordsWithExtendedHangul). Words that still do not decode are
    decoded piece by piece with `decode_text`.
    """
    unfolded, rest = _FOLD.sub("", text), ""
    # `decode_header` looks for the `?=` closing a word from every word start to the end of the line, so a header of word starts
    # that are never closed took the square of its length (`=?a?q?x` repeated: 137 KB in 9 s; a forwarded mail's subject or an
    # attachment name is not bounded by the 256 KB header range). Only a start after the last `?=` can fail that way, and no word
    # is complete from there on: that rest is left as written, which is what `decode_header` makes of it, and only the text
    # before it is decoded. The rest starts after the last `?=`, which may close a word even when a start begins at its `=`
    # (`…?=?a?q?x`). A header of several lines is split into lines by `decode_header`, so that one is left to the word by word
    # decoding of `_decoded_header`
    # (Pinned by: test_core_mailboxes_and_charsets.py::TestUnclosedEncodedWords).
    last_end = unfolded.rfind("?=")
    unclosed = _WORD_START.search(unfolded, last_end + 2 if last_end >= 0 else 0)
    if unclosed is not None:
        if _LINE_BREAK.search(unfolded):
            return text
        unfolded, rest = unfolded[:unclosed.start()], unfolded[unclosed.start():]
    # A word whose base64 is broken (`=?utf-8?b?A?=`) raises HeaderParseError, which is not a ValueError: one subject or
    # attachment name rejected the whole mail
    # (Pinned by: test_audit_html_text.py::TestEncodedWordThatCannotBeDecoded).
    try:
        pieces = decode_header(unfolded)
    except (LookupError, UnicodeError, ValueError, HeaderParseError):
        return text
    # In a word labeled Windows-1252/ISO-8859-1 nearly every byte decodes strictly, so a CP949 subject was garbled into `[°øÁö]`.
    # It is judged by its content, like the same declaration on a body (`_header_chunk_text` → `_latin_labeled_text`,
    # Pinned by: test_core_html_mail_hidden_and_frames.py::TestEncodedWordLabeledLatinButKorean).
    # The pieces are joined as they are: `make_header` puts a space between an encoded piece and the text next to it unless
    # that text has one, so the quoted name `"=?euc-kr?B?…?=" <a@b>` became `" 홍구동 " <a@b>` and the attachment name
    # `=?utf-8?b?…?=.xlsx` `견적서 .xlsx`.
    # The whitespace between two adjacent encoded words is already dropped by `decode_header` (RFC 2047), and a word written
    # against the text before it (`Re:=?utf-8?q?x?=`) is joined to it, as the `email.policy.default` parser does. A folded
    # header is unfolded first (RFC 5322: the line break goes, the space after it stays, `_FOLD`): `decode_header` strips the
    # space starting each line, so `=?…?=\r\n <a@b>` became `홍길동<a@b>` and `Hello world\r\n =?utf-8?q?x?=` `Hello worldx`
    # (Pinned by: test_core_mailboxes_and_charsets.py::TestEncodedWordNextToText).
    return "".join(_header_chunk_text(chunk, charset) for chunk, charset in pieces) + rest


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


def part_content(part: Message) -> bytes | None:
    """The content of a leaf part, its transfer encoding (base64, quoted-printable) decoded: `get_payload(decode=True)`, except
    that base64 whose letters come to one more than a multiple of four is decoded up to the last whole byte (`base64_prefix`).

    For that length the standard library gives back the base64 itself, undecoded, and only records a defect on the part, so a
    body cut off in transit at one length in four came out as a page of base64 letters, while one cut at any other length
    gave the text up to the cut. The one letter left over carries six bits, less than a byte, so nothing is lost by dropping
    it, and the part is read like one cut at any other length, without a warning
    (Pinned by: test_core_mailboxes_and_charsets.py::TestBase64CutOffAtAnyLength).
    """
    seen = len(part.defects)
    payload = part.get_payload(decode=True)
    if not any(isinstance(defect, InvalidBase64LengthDefect) for defect in part.defects[seen:]):
        return payload
    encoded = part.get_payload()
    return base64_prefix(encoded) if isinstance(encoded, str) else payload


# What base64 decoding leaves out: anything that is not one of its 64 letters (line breaks, stray characters, padding).
_NOT_BASE64 = re.compile(r"[^A-Za-z0-9+/]+")


def base64_prefix(encoded: str) -> bytes:
    """The bytes of the base64 `encoded`, up to the last whole byte. Characters that are not base64 letters are left out, and
    a letter left over after the last whole byte (one more than a multiple of four) is dropped (`part_content`)."""
    letters = _NOT_BASE64.sub("", encoded)
    if len(letters) % 4 == 1:
        letters = letters[:-1]
    return base64.b64decode(letters + "=" * (-len(letters) % 4))


def _mail_body(message: Message, content_type: str, parts: list[Message] | None = None) -> tuple[str, list[str]] | None:
    texts, encodings = [], []

    for part in mail_body_parts(message) if parts is None else parts:
        if part.get_content_type() != content_type:
            continue
        payload = part_content(part)
        if isinstance(payload, bytes) and payload.strip():
            text, encoding = plain_part_text(part, payload) if content_type == "text/plain" else decode_text(payload, part.get_content_charset())
            texts.append(text.strip())
            encodings.append(encoding)
    return ("\n\n".join(texts), encodings) if texts else None


def plain_part_text(part: Message, payload: bytes) -> tuple[str, str]:
    """A plain-text body part as text (its decoded `payload`, read with the part's charset) and the encoding used, with
    `format=flowed` lines joined back (`_unflowed`, DelSp included). The text extractor (`_mail_body`) and the HTML extractor
    (`_html_mail._mail_body_html`) read plain parts through it.

    The HTML extractor decoded the plain parts placed beside an HTML part directly, so a flowed paragraph of a mail that also
    had an HTML footer was broken at every line again
    (Pinned by: test_core_word_lists_cell_blocks_and_mail_lines.py::TestFormatFlowedPlainMail::test_flowed_part_beside_an_html_part_is_joined_too).
    """
    text, encoding = decode_text(payload, part.get_content_charset())
    if str(part.get_param("format", "")).lower() == "flowed":
        text = _unflowed(text, delete_space=str(part.get_param("delsp", "")).lower() == "yes")
    return text, encoding


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
