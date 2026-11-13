"""TXT and Markdown extraction. A text file that is a mail or a mailbox is read as its headers and bodies (`_mail`)."""

import base64
import binascii
import bisect
from email.message import Message
import html
import itertools
import re

from opendockit.extractors._encoding import decode_text, decoding_warnings
from opendockit.extractors._mail import (
    _mail_body,
    forwarded_mail_head,
    mail_attachment_warning,
    mail_body_parts,
    mail_box,
    mail_box_title,
    mail_box_warning,
    mail_head_text,
    mail_messages,
    mail_plain_body,
)
from opendockit.extractors._mail_calendar import mail_calendar_text
from opendockit.models import ConvertOptions, Extraction, Section, SectionKind


# Two or more NUL bytes in a row. Where a fixed-width export padded its columns.
_NUL_PADDING = re.compile("\x00{2,}")
# Terminal control sequences. CSI (ESC [ parameters intermediates final — color `ESC[31m`, `ESC[38:2:1:2:3m`, erase line
# `ESC[2K`, cursor `ESC[?25l`), OSC such as the window title (`ESC]0;title` + BEL or ESC \; not removed when unterminated — so it
# does not swallow text across lines), DCS/PM/APC strings (ended by ESC \), character set designation (the `ESC(B` of
# `tput sgr0`), two-byte commands (reset `ESC c`, cursor save/restore `ESC 7`/`ESC 8`, keypad `ESC =`/`ESC >`,
# `ESC D`, `ESC E`, `ESC H`, `ESC M`, string terminator `ESC \`). When only CSI was removed, `(B`, `]0;title` and `c` stayed in
# the text (Pinned by: test_core_round44.py::TestTerminalEscapesOtherThanCsi).
_TERMINAL_SEQUENCE = re.compile(
    r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b\n]*(?:\x07|\x1b\\)|[P^_][^\x1b\n]*\x1b\\|[()*+][0-9A-Za-z]|[78=>cDEHM\\])")


def _mail_text(mail: Message) -> tuple[str, list[str]] | None:
    """For a mail read as plain text: (headers + body + forwarded mails, the encodings decoded). None if the outer body is HTML
    only (it is read as text in its original form).

    A forwarded mail gives its plain-text body or, without one, its HTML body in original form (HTML under a text extension is
    text — only mail named .txt or .md takes this path. `detect` sends .eml to the HTML extractor).
    """
    messages, _ = mail_messages(mail)
    outer = mail_plain_body(mail)
    if outer is None and mail_body_parts(mail):
        return None
    # A mail with attachments only and no body keeps just its headers. The events of a meeting invitation go after the body
    # (`mail_calendar_text`)
    texts = [mail_head_text(mail), outer[0] if outer else "", mail_calendar_text(mail)]
    encodings = outer[1] if outer else ["utf-8"]

    for forwarded in messages[1:]:
        body = mail_plain_body(forwarded) or _mail_body(forwarded, "text/html") or ("", [])
        texts.append(f"{forwarded_mail_head(forwarded)}\n\n{body[0]}\n\n{mail_calendar_text(forwarded)}".strip())
        encodings.extend(body[1])
    return "\n\n".join(text for text in texts if text), encodings


def _mail_box_text(mails: list[Message]) -> tuple[str, list[str]] | None:
    """For the mails read as plain text (`mail_box`): (text, the encodings decoded). With several mails, `## Message N` is placed
    before each one. None if any mail's outer body is HTML only (it is read as text in its original form — `_mail_text`)."""
    bodies = [_mail_text(mail) for mail in mails]
    if not bodies or any(body is None for body in bodies):
        return None
    if len(bodies) == 1:
        return bodies[0]
    text = "\n\n".join(f"## {mail_box_title(number)}\n\n{body[0]}" for number, body in enumerate(bodies, start=1))
    return text, [encoding for body in bodies for encoding in body[1]]


def extract_text(data: bytes, options: ConvertOptions) -> Extraction:
    """TXT (mail and mbox included). The control sequences of a terminal log (`ESC[31m`) are removed whole — only the ESC was
    removed (clean_text) and `[31m`, `[0m` stayed in the text
    (Pinned by: test_core_header_tables_cell_code_and_terminal_codes.py::TestTerminalColorCodesInText). Deliberate: MD is left
    as the original (`extract_markdown`)."""
    extraction = _plain_text(data)
    section = extraction.sections[0]
    if "\x1b" in section.text:
        section.text = _TERMINAL_SEQUENCE.sub("", section.text)
    return extraction


def _plain_text(data: bytes) -> Extraction:
    """The reading shared by TXT and MD: body and attachment warnings for mail and mbox, otherwise the decoded text."""
    mails, total = mail_box(data)
    body = _mail_box_text(mails)
    if body is not None:
        text, encodings = body
    else:
        text, encoding = decode_text(data)
        encodings = [encoding]
    encoding = encodings[0]

    # In a fixed-width export that pads its columns with NULs, the columns run together like '홍길동1000' once the NULs are
    # removed (clean_text). A run of NULs is replaced with the same number of spaces to keep the column boundaries and alignment.
    # A single NUL is stray noise and is removed as before
    # (Pinned by: test_text_html.py::TestLegacyTextWithNulBytes::test_nul_padded_fixed_width_export_keeps_its_columns).
    text = _NUL_PADDING.sub(lambda match: " " * len(match.group()), text)

    extraction = Extraction(sections=[Section(SectionKind.DOCUMENT, 1, text)], metadata={"encoding": encoding})
    extraction.warnings.extend(decoding_warnings(encodings))
    if body is not None:
        extraction.warnings.extend(warning for warning in (mail_attachment_warning(*mails), mail_box_warning(len(mails), total))
                                   if warning)
    return extraction


# data: URI pictures embedded in Markdown, `![alt](data:image/png;base64,… "title")` (the address may be wrapped in `<…>`, and the
# title may be in double or single quotes or parentheses), and HTML pictures `<img … src="data:image/…;base64,…">` (an unquoted
# `src=data:…` included). Parameters after the MIME type (`;charset=utf-8`) are skipped. Not knowing single-quote and parenthesis
# titles, `<…>` addresses, parameters and unquoted src left the base64 in the body as is
# (Pinned by: test_core_mbox_markdown_and_api.py::TestMarkdownDataUriVariants). The base64 field takes only characters that are
# not quotes, parentheses or `>`, so it ends at the title, the closing parenthesis or the closing quote. The field greedily eats
# the trailing whitespace too — with `\s*` after a lazy field the two share the whitespace, and 20,000 spaces after an unclosed
# URI took 3.4 seconds (quadratic) (Pinned by: test_core_text_and_mail.py::TestMarkdownHtmlImageDataUri::
# test_unclosed_data_uri_followed_by_long_whitespace_takes_linear_time). The field of an unquoted src ends only where the base64
# characters end (so that it does not share characters with the attribute field after it). Trailing whitespace is dropped when
# decoding. The attribute fields of `<img` do not cross `<` or `>`. Balanced code fence blocks are found first, line by line, and
# skipped (`_fenced_spans`). The last two branches (`other_…`) are data: URI pictures that are not base64 (percent-encoded SVG
# etc.), which keep only the alt text as in HTML — left as the original, one picture used 140,000 characters
# (Pinned by: test_core_round45.py::TestMarkdownDataUriPicturesThatAreNotBase64). The address field takes, like CommonMark, no
# whitespace and one level of parentheses, and uses possessive quantifiers so that it does not backtrack.
# The alt text field (`_PICTURE_ALT`) stops before the next `![` — looking for `]` up to the end of the line for every `![` made
# a line of repeated `![` without `]` take quadratic time (3.3 seconds for 20KB, 2 minutes for 80KB)
# (Pinned by: test_core_round48.py::TestRepeatedPictureOpenersTakeLinearTime). Known accepted gap: when the alt text contains
# `![` (`![a ![b](data:…)`), the picture starts at the last `![` and the `![a ` before it stays as text (the picture is still
# extracted).
# An `<img` is only examined when the `>` that would close it is there: the base64 branch looks ahead for it (`(?=[^<>]*>)` —
# nothing in that branch can hold `<` or `>`), and the other branch, where a quoted address may hold them
# (`data:image/svg+xml;utf8,<svg …>`), notes whether the tag text runs up to a `>` (`closed`) and otherwise only takes a quoted
# address that itself holds a `<` or `>` — nothing else could reach a `>` from there. Without that, every `src=` of a tag
# that is never closed walked to the end of the tag and back: 8,000 of them in one `<img` (234KB) took 19 seconds. The matches
# are the same as before (Pinned by: test_audit_html_text.py::TestUnclosedImgTagWithManySources).
_PICTURE_ALT = r"(?:[^\]\n!]|!(?!\[))*"
_DATA_IMAGE = r"data:image/[\w.+-]+(?:;[\w.+-]+=[^;,\s()<>\"']*)*;base64,"
_MARKDOWN_DATA_PICTURE = re.compile(
    r"!\[(?P<alt>" + _PICTURE_ALT + r")\]\(\s*(?:<" + _DATA_IMAGE + r"(?P<angle_data>[A-Za-z0-9+/=\s]*)>\s*"
    r"|" + _DATA_IMAGE + r"(?P<data>[A-Za-z0-9+/=\s]*))"
    r"(?:\"[^\"\n]*\"\s*|'[^'\n]*'\s*|\([^()\n]*\)\s*)?\)"
    r"|<img\b(?=[^<>]*>)(?P<before>[^<>]*?)\bsrc\s*=\s*(?:(?P<quote>[\"'])\s*" + _DATA_IMAGE + r"(?P<tag_data>[A-Za-z0-9+/=\s]*)(?P=quote)"
    r"|" + _DATA_IMAGE + r"(?P<bare_data>[A-Za-z0-9+/=]*)(?![A-Za-z0-9+/=]))(?P<after>[^<>]*)>"
    r"|!\[(?P<other_alt>" + _PICTURE_ALT + r")\]\(\s*(?:<data:image/[^<>\n]*+>|data:image/(?:[^\s()]|\([^\s()]*+\))*+)\s*"
    r"(?:\"[^\"\n]*\"\s*|'[^'\n]*'\s*|\([^()\n]*\)\s*)?\)"
    r"|<img\b(?:(?=[^<>]*>)(?P<closed>))?(?P<other_before>[^<>]*?)\bsrc\s*=\s*"
    r"(?:\"\s*data:image/(?(closed)|[^\"<>]*+[<>])[^\"]*+\"|'\s*data:image/(?(closed)|[^'<>]*+[<>])[^']*+'"
    r"|(?(closed)|(?!))data:image/[^\s\"'<>`]*+)"
    r"(?P<other_after>[^<>]*)>",
    re.IGNORECASE)
# The line-start opening of a code fence (three or more backticks or tildes) and the closing line (fence marker + only spaces or
# tabs). The closing line also takes a trailing `\r` — because of the `\r` left on CRLF lines split at `\n`, a data: URI picture
# example inside a block was extracted as a picture (Pinned by: test_core_round48.py::TestCodeFenceWithWindowsLineBreaks).
_FENCE_OPENING = re.compile(r"`{3,}|~{3,}")
_FENCE_CLOSING = re.compile(r"(`{3,}|~{3,})[ \t]*\r?")
# The alt attribute of `<img>` (inside quotes, or an unquoted value).
# Is the picture address (the `](` after Markdown alt text, the src attribute of `<img`) an SVG data: URI.
_SVG_SOURCE = re.compile(r"(?:\]\(\s*<?|src\s*=\s*[\"']?\s*)data:image/svg", re.IGNORECASE)
_IMG_ALT = re.compile(r"""\balt\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'<>=`]+))""", re.IGNORECASE)


def extract_markdown(data: bytes, options: ConvertOptions) -> Extraction:
    """MD is read like TXT, and data: URI pictures embedded in the body (`![alt](data:…)`, HTML `<img src="data:…" alt="alt">`)
    are registered as pictures, like data: URIs in HTML, and replaced with placeholders.

    The base64 became body text as is, so one picture used tens of thousands of characters and there were 0 pictures
    (Pinned by: test_image.py::TestMarkdownDataUriPictures). The HTML `<img>` common in MD is the same — one picture went over
    the default limit with 520,000 characters (Pinned by: test_core_text_and_mail.py::TestMarkdownHtmlImageDataUri).
    When pictures are not extracted (`extract_images=False`), only the alt text is kept, as in HTML. Pictures at external
    addresses are not fetched (SSRF, same as HTML).
    Deliberate: the inside of a code fence block is example code and is left as is. So is the inside of inline code
    (`_pictures_outside_inline_code`). Known accepted gap: the data: URI of the reference form (`![alt][id]` + `[id]: data:…`)
    stays as text.
    """
    extraction = _plain_text(data)
    section = extraction.sections[0]
    if "data:image/" not in section.text:
        return extraction

    pieces = []
    position = 0
    for start, end in [*_fenced_spans(section.text), (len(section.text), len(section.text))]:
        pieces.extend(_pictures_outside_inline_code(section.text[position:start], extraction, options))
        pieces.append(section.text[start:end])
        position = end
    section.text = "".join(pieces)
    return extraction


def _pictures_outside_inline_code(text: str, extraction: Extraction, options: ConvertOptions) -> list[str]:
    """The pieces of the text outside fence blocks, with its data: URI pictures extracted as pictures. The inside of inline code
    (`` `…` ``) is example text and is left as is — when it was extracted as a picture the code span was emptied to `` ` ` `` and
    a picture description was attached (Pinned by: test_core_round44.py::TestDataUriPictureInInlineCode).

    Picture syntax is found first, and inline code is paired only from the backticks outside pictures. A picture being inside
    inline code means that code opened before it covers the start of the picture. Previously inline code was cut out first, so a
    backtick inside the alt text, the title or `<img alt>` split the picture in two and base64 stayed in the body
    (Pinned by: test_core_round45.py::TestBackticksAroundMarkdownPictures). Known accepted gap: code that opens in the alt text
    and closes outside the picture (`` `![a`](…) ``) is seen as a picture, unlike CommonMark.
    """
    pictures = list(_MARKDOWN_DATA_PICTURE.finditer(text))
    code_spans = _inline_code_spans("".join(_without_pictures(text, pictures)))
    code_starts = [start for start, _ in code_spans]

    pieces = []
    position = 0

    for match in pictures:
        before = bisect.bisect_left(code_starts, match.start()) - 1
        if before >= 0 and code_spans[before][1] > match.start():
            continue
        pieces.append(text[position:match.start()])
        pieces.append(_markdown_picture(match, extraction, options))
        position = match.end()
    pieces.append(text[position:])
    return pieces


def _without_pictures(text: str, pictures: list[re.Match]) -> list[str]:
    """The pieces with each picture syntax span masked by the same length of non-backtick characters (for pairing inline code — a
    backtick inside a picture is not code)."""
    pieces = []
    position = 0

    for match in pictures:
        pieces.append(text[position:match.start()])
        pieces.append("x" * (match.end() - match.start()))
        position = match.end()
    pieces.append(text[position:])
    return pieces


_BACKTICK_RUN = re.compile(r"`+")
# Paragraph boundary (blank line). Inline code is not paired across it. CRLF and CR line breaks are taken too — looking only at
# `\n`, in MD saved on Windows code was paired across paragraphs and a picture stayed as text
# (Pinned by: test_core_round47.py::TestInlineCodeAcrossParagraphsWithWindowsLineBreaks).
_BLANK_LINE = re.compile(r"(?:\r\n?|\n)[ \t]*(?:\r\n?|\n)")


def _inline_code_spans(text: str) -> list[tuple[int, int]]:
    """The (start, end) character positions of inline code. Paired only within a paragraph (a chunk separated by blank lines),
    like CommonMark — an unmatched backtick of an earlier paragraph paired with a backtick of a later paragraph and the picture
    between them stayed as text (Pinned by: test_core_round45.py::TestBackticksAroundMarkdownPictures::
    test_unmatched_backtick_in_an_earlier_paragraph_does_not_hide_the_picture)."""
    spans = []
    position = 0

    for blank in [*_BLANK_LINE.finditer(text), None]:
        end = blank.start() if blank else len(text)
        spans.extend((position + start, position + stop) for start, stop in _paragraph_code_spans(text[position:end]))
        position = blank.end() if blank else len(text)
    return spans


def _paragraph_code_spans(text: str) -> list[tuple[int, int]]:
    """The (start, end) of inline code within one paragraph. A run of n backticks closes at the first run of the same length
    after it, and is text if there is no closing run. The positions of runs of the same length are collected and binary-searched
    — close to linear even with many unmatched runs. Deliberate: backticks disabled by a backslash are not treated separately."""
    runs = [match.span() for match in _BACKTICK_RUN.finditer(text)]
    by_length: dict[int, list[int]] = {}

    for index, (start, end) in enumerate(runs):
        by_length.setdefault(end - start, []).append(index)

    spans = []
    index = 0

    while index < len(runs):
        start, end = runs[index]
        same = by_length[end - start]
        after = bisect.bisect_right(same, index)
        if after == len(same):
            index += 1
            continue
        closing = same[after]
        spans.append((start, runs[closing][1]))
        index = closing + 1
    return spans


def _fenced_spans(text: str) -> list[tuple[int, int]]:
    """The (start, end) character positions of balanced code fence blocks: from a line that starts with a fence marker (three or
    more backticks or tildes) to the end of the first line after it that is the same marker + whitespace only. If no closing
    line matches the opening marker, the marker is shortened one character at a time (four → three) and searched again, and if
    there is still none it is not a block (that line is text too).

    Previously a single regex (`^(`{3,}|~{3,})…(?:.*?\n)??(?P=mark)`) looked for a closing line up to the end of the text for
    every opening line, so 32,000 unmatched opening lines + one picture took the square of the number of lines
    (Pinned by: test_core_text_and_mail.py::TestMarkdownHtmlImageDataUri::
    test_many_unclosed_fence_lines_before_a_picture_take_linear_time). The positions of closing lines are collected per marker
    and binary-searched — the pair chosen is the same as with the old regex.
    """
    lines = text.split("\n")
    starts = list(itertools.accumulate((len(line) + 1 for line in lines), initial=0))
    closings: dict[tuple[str, int], list[int]] = {}  # (marker character, marker length) → closing line numbers

    for index, line in enumerate(lines):
        match = _FENCE_CLOSING.fullmatch(line)
        if match:
            closings.setdefault((line[0], len(match.group(1))), []).append(index)

    # For each marker character (backtick, tilde), the marker lengths of the closing lines (ascending). An opening line looks only
    # at lengths up to its own, so it walks no more steps than it has marker characters.
    lengths: dict[str, list[int]] = {}
    for character, length in sorted(closings, key=lambda mark: mark[1]):
        lengths.setdefault(character, []).append(length)
    spans = []
    index = 0

    while index < len(lines):
        closing = _fence_closing(lines[index], index, closings, lengths)
        if closing is None:
            index += 1
            continue
        spans.append((starts[index], starts[closing] + len(lines[closing])))
        index = closing + 1
    return spans


def _fence_closing(line: str, index: int, closings: dict[tuple[str, int], list[int]],
                   lengths: dict[str, list[int]]) -> int | None:
    """If the `index`-th line `line` is a fence opening line, the number of the line that closes its block (the longest marker is
    tried first), otherwise None."""
    opening = _FENCE_OPENING.match(line)
    if opening is None:
        return None
    run = opening.group()
    candidates = lengths.get(run[0], [])

    for position in range(bisect.bisect_right(candidates, len(run)) - 1, -1, -1):
        found = closings[run[0], candidates[position]]
        after = bisect.bisect_right(found, index)
        if after < len(found):
            return found[after]
    return None


def _markdown_picture(match: re.Match, extraction: Extraction, options: ConvertOptions) -> str:
    if match.group("other_before") is not None or match.group("other_alt") is not None:
        alt = _picture_alt(match, "other_before", "other_after", "other_alt")
        return f"[Image: {alt}]" if alt else ""
    alt = _picture_alt(match, "before", "after", "alt")
    source_start = match.end("before") if match.group("before") is not None else match.end("alt")
    if _SVG_SOURCE.match(match.string, source_start, match.end()):
        return f"[Image: {alt}]" if alt else ""  # like HTML, SVG is not extracted (it could not be drawn and only a warning was left)

    if match.group("before") is not None:
        encoded = match.group("tag_data") if match.group("quote") is not None else match.group("bare_data")
    else:
        encoded = match.group("data") if match.group("data") is not None else match.group("angle_data")
    if options.extract_images:
        try:
            picture = base64.b64decode("".join(encoded.split()), validate=False)
        except (binascii.Error, ValueError):
            picture = b""
        if picture:
            return extraction.add_image(picture, alt=alt)
    return f"[Image: {alt}]" if alt else ""


def _picture_alt(match: re.Match, before: str, after: str, alt: str) -> str:
    """The alt text of a picture (whitespace collapsed to one space): the alt attribute for HTML `<img>`, the text of `![…]` for
    Markdown."""
    if match.group(before) is None:
        return " ".join(match.group(alt).split())
    alt_match = _IMG_ALT.search(f"{match.group(before)} {match.group(after)}")
    text = html.unescape(next((value for value in alt_match.groups() if value is not None), "")) if alt_match else ""
    return " ".join(text.split())
