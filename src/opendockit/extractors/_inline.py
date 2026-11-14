"""Inline notation shared by every format: superscripts and subscripts (`10^6`, `H_2O`), struck-through text (`~~text~~`),
and hyperlinks and bold / italic (`[text](url)`, `**bold**`, `*italic*`).

An extractor only marks where a span starts and ends, with the temporary marks defined here. The marks are resolved into
Markdown once per block, so the same rule does not drift between formats.
"""

from dataclasses import dataclass
import re

from opendockit.models import IMAGE_MARKER


# ---- Superscripts and subscripts ---------------------------------------------------------------------------------

SUPERSCRIPT, SUBSCRIPT = "^", "_"
# The temporary marks of links and emphasis (see "Links and emphasis" below). They are still in the text when scripts and
# strikethrough are resolved and are skipped where those look at the neighbouring text.
_INLINE_MARK_CHARS = "\x0e\x0f\x10\x11\x12"
_WITHOUT_INLINE_MARKS = dict.fromkeys(map(ord, _INLINE_MARK_CHARS))
# Script text longer than this is not an exponent or chemical formula but a sentence written small (trademark notices etc.), so it
# is left unmarked.
_SCRIPT_MAX_CHARS = 10
# Temporary marks that the formats read run by run (DOCX, PPTX) put around a script span. Control characters that cannot occur in
# XML text.
_SCRIPT_OPEN = {SUPERSCRIPT: "\x02", SUBSCRIPT: "\x04"}
_SCRIPT_CLOSE = "\x03"
_SCRIPT_OPENS = frozenset(_SCRIPT_OPEN.values())
_SCRIPT_SPLIT = re.compile(r"([\x02\x04])([^\x02-\x04]*)\x03\1")
_SCRIPT_SPAN = re.compile(r"([\x02\x04])([^\x02-\x04]*)\x03")


# Hangul (jamo, compatibility jamo, syllables), Han characters, kana. A script containing these is not an exponent but a raised unit
# or footnote mark ('1,000원', '주1)').
_CJK_SCRIPT = re.compile("[\u1100-\u11ff\u3040-\u30ff\u3130-\u318f\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7a3\uf900-\ufaff]")
# Ordinal suffixes written small and raised after a number (21st, 2nd, 3ème). Not exponents; plain text is right for them.
_ORDINAL_SUFFIXES = frozenset({"st", "nd", "rd", "th", "er", "re", "ère", "ere", "ème", "eme", "ième", "ieme", "nde"})


def format_script(text: str, mark: str, before: str = "") -> str:
    """Prefix `^` to a superscript and `_` to a subscript (`10^6` for one character, `10^{-3}` for several). Every format uses the
    same shape.

    Joined as plain text, 10⁶ read as 106 and km² as km2. Only short text starting with a letter, digit or sign is marked. Footnote
    marks (`[1]`, `[^1]`), symbols (†), page marks (`: 125`) and long text are left as is
    (Pinned by: test_text_html.py::test_html_superscript_and_subscript_are_marked).
    """
    inner = " ".join(text.split())
    if not inner or not (inner[0].isalnum() or inner[0] in "+-−") or len(inner) > _SCRIPT_MAX_CHARS:
        return text
    # Scripts containing Hangul or Han characters are not marked (the same rule as the PDF script guess). They became '1,000^원' and
    # '각주^{주1)}' (Pinned by: test_text_html.py::test_html_superscript_with_hangul_or_hanja_is_left_as_plain_text,
    # test_office.py::TestSuperscriptAndTextBoxes::test_docx_superscript_with_hangul_is_left_as_plain_text).
    if _CJK_SCRIPT.search(inner):
        return text
    # `before` is the text right before the script. An ordinal suffix after a number is not marked — 21st became 21^{st}
    # (Pinned by: test_text_html.py::test_html_ordinal_suffix_after_a_number_is_not_marked_as_superscript).
    if mark == SUPERSCRIPT and before[-1:].isdigit() and inner.lower() in _ORDINAL_SUFFIXES:
        return text
    lead, trail = text[:len(text) - len(text.lstrip())], text[len(text.rstrip()):]
    return f"{lead}{mark}{inner if len(inner) == 1 else '{' + inner + '}'}{trail}"


def mark_script(text: str, mark: str) -> str:
    """Put temporary marks around the text of a script run. `resolve_scripts` converts them once the paragraph text is collected."""
    return f"{_SCRIPT_OPEN[mark]}{text}{_SCRIPT_CLOSE}" if text else text


def resolve_scripts(text: str) -> str:
    """Turn the temporary marks into script notation. Consecutive runs of the same kind ('-' and '3') are merged into one first."""
    if _SCRIPT_CLOSE not in text:
        return text
    while _SCRIPT_SPLIT.search(text):
        text = _SCRIPT_SPLIT.sub(lambda match: match.group(1) + match.group(2), text)
    marks = {opened: mark for mark, opened in _SCRIPT_OPEN.items()}
    return _SCRIPT_SPAN.sub(
        lambda match: format_script(match.group(2), marks[match.group(1)], _character_before(match.string, match.start())),
        text)


def _character_before(text: str, position: int) -> str:
    """The character before `position`, not counting the temporary link and emphasis marks (the script marks are resolved while
    those are still in the text: '21' in bold followed by a raised 'st' is still an ordinal)."""
    while position > 0 and text[position - 1] in _INLINE_MARK_CHARS:
        position -= 1
    return text[position - 1:position] if position else ""


# ---- Strikethrough -----------------------------------------------------------------------------------------------

# Temporary marks that the formats read run by run (DOCX, PPTX, HWP, HWPX) put around a struck-through run. Control characters that
# do not clash with the script marks.
_STRIKE_OPEN, _STRIKE_CLOSE = "\x05", "\x06"
_STRIKE_JOINT = re.compile(r"\x06(\s*)\x05")
_STRIKE_SPAN = re.compile(r"\x05([^\x05\x06]*)\x06")
# Line-leading bullets kept outside the strikethrough marks: Markdown list markers and hand-typed bullet symbols (only when
# followed by whitespace — '-5%' is a negative number).
_STRIKE_LEAD = re.compile(r"[-*+·•●○◦▪◆◇▶►➢]\s+")


def mark_strike(text: str) -> str:
    """Put temporary marks around the text of a struck-through run. `resolve_strikes` converts them once the paragraph text is
    collected."""
    return f"{_STRIKE_OPEN}{text}{_STRIKE_CLOSE}" if text else text


def resolve_strikes(text: str) -> str:
    """Turn the temporary marks into `~~text~~`. The same shape as `<s>` / `<del>` in the HTML extractor — the marks go before the
    first character and after the last (whitespace stays outside), consecutive struck-through runs are merged into one even when
    only whitespace lies between them, and whitespace-only spans are not marked. When the leading characters are struck through it
    is `~~가나~~`, not `~~가~~~~나~~`, even if the runs are split per character. Resolved before the script marks (`resolve_scripts`)
    — with a strikethrough mark in between, consecutive script runs do not merge (Pinned by:
    test_office.py::TestDocxStruckDeletedAndSymbolRuns, TestPptxStrikethroughTitlesAndHiddenSlides, test_hwp.py::TestStrikethrough).

    A strikethrough spanning a line break is closed and reopened on each line. A bullet at the start of a line ('- ', '•' etc.,
    `_STRIKE_LEAD`) stays outside the marks. Wrapped in one mark, a line starting with '~~- 1차 …' was not read as a sub-item (the
    w:br lines of a DOCX list item. Pinned by: test_office.py::
    TestOfficeLineBreaksNestedFieldsAndMetadata::test_docx_strikethrough_across_a_line_break_is_marked_per_line)."""
    if _STRIKE_CLOSE not in text:
        return text
    text = _STRIKE_JOINT.sub(r"\1", text)

    # Whether a span is at the start of a line is carried on from the end of the previous span. Scanning back to the line start for
    # every mark was quadratic when one line had many marks (40,000 pairs on one line: 2 s. Pinned by:
    # test_office_symbols_fields_notes.py::TestStrikeResolutionIsLinear). The closing mark of the previous span is not whitespace, so
    # the next span on the same line is not at the line start.
    pieces = []
    position = 0
    line_start = True

    for match in _STRIKE_SPAN.finditer(text):
        gap = text[position:match.start()]
        newline = gap.rfind("\n")
        # The temporary link and emphasis marks (resolved later) are not text: a struck-through bullet line in bold is still at
        # the start of its line
        line_start = _is_blank(gap[newline + 1:]) if newline >= 0 else line_start and _is_blank(gap)
        lines = match.group(1).split("\n")
        pieces.append(gap)
        pieces.append("\n".join(_strike_line(line, index > 0 or line_start) for index, line in enumerate(lines)))
        position = match.end()
        line_start = False

    pieces.append(text[position:])
    return "".join(pieces)


def _is_blank(text: str) -> bool:
    return not text.translate(_WITHOUT_INLINE_MARKS).strip()


def _strike_line(inner: str, at_line_start: bool) -> str:
    """A struck-through span within one line → `~~text~~`. Surrounding whitespace and, at the start of a line, the bullet stay
    outside the marks."""
    # Whitespace just inside the marks of a script run (`\x02 … \x03`) is at the edge of the span too: a struck superscript
    # whose run ends in a space gave `~~^2 ~~`, which a renderer does not read as strikethrough, and a struck script run of
    # spaces alone `~~ ~~` (Pinned by: test_inline_formatting.py::TestStruckScriptRuns). The marks stay, the whitespace goes out.
    start, end = 0, len(inner)
    while start < end and (inner[start].isspace() or inner[start] in _SCRIPT_OPENS):
        start += 1
    while end > start and (inner[end - 1].isspace() or inner[end - 1] == _SCRIPT_CLOSE):
        end -= 1
    if start == end:
        return inner
    before, after = inner[:start], inner[end:]
    body = "".join(char for char in before if not char.isspace()) + inner[start:end] + "".join(
        char for char in after if not char.isspace())
    lead = _STRIKE_LEAD.match(body) if at_line_start else None
    head = lead.group(0) if lead and lead.end() < len(body) else ""
    leading = "".join(char for char in before if char.isspace())
    trailing = "".join(char for char in after if char.isspace())
    return f"{leading}{head}~~{body[len(head):]}~~{trailing}"


# ---- Links and emphasis ------------------------------------------------------------------------------------------
#
# DOCX, PPTX and HTML keep hyperlinks as `[text](url)` and bold / italic as `**bold**`, `*italic*`, `***both***`
# (`ConvertOptions.keep_links`, `keep_emphasis`). The decisions are recorded in docs/decisions/inline-formatting.md.
#
# The extractors only say where a span starts and ends; one place (`resolve_inline`) turns the spans into Markdown, so that every
# format merges runs, places whitespace and escapes text by the same rules. A span is announced either with temporary marks inside
# the text (DOCX and PPTX, which read run by run; control characters that cannot occur in XML text and that are not whitespace for
# `str.split`) or as `InlineMark` items between the pieces of text (HTML, whose text may hold any character).

_BOLD_OPEN, _BOLD_CLOSE, _ITALIC_OPEN, _ITALIC_CLOSE = "\x0e", "\x0f", "\x10", "\x11"
LINK_OPEN, _LINK_TARGET, _LINK_CLOSE = "\x12", "\x13", "\x14"
_INLINE_TOKEN = re.compile(r"[\x0e-\x12]|\x13([^\x13\x14]*)\x14")
_HAS_INLINE_MARK = re.compile(r"[\x0e-\x14]")
# Runs with the same emphasis that touch (or have only whitespace between them) are one span. Bold is always the outer mark and
# italic the inner one (`mark_emphasis`), so two replacements are enough.
_BOLD_JOINT = re.compile(r"\x0f(\s*)\x0e")
_ITALIC_JOINT = re.compile(r"\x11(\s*)\x10")
# Link targets that are kept. Everything else (relative addresses, `#fragment`, `javascript:`, `file:`, `data:` …) stays text.
_LINK_SCHEMES = ("http://", "https://", "mailto:")
# Characters of an address that would end the link early, split a table row, or be changed by `clean_text` (whitespace, control
# characters, `( ) < > | \`, the brackets of a picture placeholder). They are percent-encoded.
_LINK_UNSAFE = re.compile(r"[\s\x00-\x1f\x7f-\x9f()<>|\\⟦⟧​-‏‪-‮⁠-⁤﻿­]")
# Text that stands for an object, not words: picture placeholders, `[Image: alt]` of pictures that are not extracted, OLE objects,
# charts, videos and footnote references. Emphasis is not put around them, and at the edge of a link they stay outside the brackets
# (the converter turns a picture placeholder into a block below its line).
# Deliberate: no `[` inside `[Image: …]` or `[^…]`. An opening with no `]` was scanned to the end of the line from every `[`
# (10,000 `[Image: ` next to a bold word took 5 seconds); with a `[` inside, the text is no longer taken for a placeholder
# (Pinned by: test_audit_core.py::TestUnclosedImagePlaceholderText). A backslash is read together with the character after it unless
# that is `[`, so `\]` (alt text escaped like link text) is part of the placeholder: stopping at it left the placeholder's own `]`
# outside and broke a picture inside a link (`[Image: logo\][\]](…)`, Pinned by:
# test_audit_web_fixes.py::TestAltTextCannotMakeALink::test_escaped_alt_inside_a_link). The pair never takes a `[`, so a scan still
# ends at the next `[` and stays linear — `\.` took `\[` too and made `[Image: \` repeated quadratic again
# (test_many_unclosed_placeholder_starts_with_backslashes_take_linear_time).
_INLINE_PLACEHOLDER = re.compile(
    rf"({IMAGE_MARKER.pattern}|\[Image(?:: (?:[^\[\]\n\\]|\\[^\[\n])*)?\]|\[OLE object\]|\[Chart\]|\[Video\]|\[\^[^\[\]\s]+\])")
_EQUATION_ONLY = re.compile(r"\$[^$\n]+\$")
# A bullet or number typed at the start of a line. It stays outside the marks (the same idea as `_STRIKE_LEAD`): `**- 항목**` is
# not read as a list item, and the extractors look for these at the start of a line (`HAND_TYPED_BULLET`).
_INLINE_LEAD = re.compile(r"[ \t]*(?:[-*+][ \t]+|[·•●○◦▪■□◆◇▶►➢✓☐☒☑][ \t]*|\d{1,3}[.)][ \t]+|#{1,6}[ \t]+)")
_LEAD_PROBE = 16  # a lead is a few characters: only this much of the start of a line is looked at
_BLOCK_GAP = re.compile(r"(\n[ \t]*\n\s*)")
_STARTS_WITH_LINE_BREAK = re.compile(r"[ \t]*\n")
_ENDS_WITH_LINE_BREAK = re.compile(r"\n[ \t]*\Z")
_BRACKET_OR_ESCAPE = re.compile(r"\\.?|[\[\]]", re.DOTALL)
_STRIKE_MARK = re.compile("~~")
_LINE_ENDS = ("break", "gap")


@dataclass(frozen=True, slots=True)
class InlineMark:
    """Start or end of a span, placed between pieces of text (`resolve_inline`). `kind` is "b" (bold), "i" (italic) or "a" (link);
    the end of a link carries the vetted address (`link_target`)."""

    kind: str
    opens: bool
    url: str | None = None


BOLD_ON, BOLD_OFF = InlineMark("b", True), InlineMark("b", False)
ITALIC_ON, ITALIC_OFF = InlineMark("i", True), InlineMark("i", False)
LINK_ON = InlineMark("a", True)
# A table, list, code block or quote block written between the pieces (HTML). No link or emphasis span holds it.
BLOCK_INSIDE = InlineMark("x", True)
_CHAR_MARKS = {_BOLD_OPEN: BOLD_ON, _BOLD_CLOSE: BOLD_OFF, _ITALIC_OPEN: ITALIC_ON, _ITALIC_CLOSE: ITALIC_OFF, LINK_OPEN: LINK_ON}


# A HYPERLINK field (DOCX `w:instrText`, RTF `\fldinst`): `HYPERLINK "address" \l "bookmark" \o "tip"`. The address may be
# unquoted; a first argument that is a switch (`\l "bookmark"` alone) is a jump inside the document and is not a link.
_HYPERLINK_FIELD = re.compile(r'\s*HYPERLINK\s+(?:"([^"]*)"|([^\s"\\]\S*))', re.IGNORECASE)
_HYPERLINK_BOOKMARK = re.compile(r'\\l\s+"([^"]*)"', re.IGNORECASE)


def field_link(instruction: str) -> str | None:
    """The vetted address of a HYPERLINK field instruction (`link_target`), with `\\l "bookmark"` appended as the fragment. None
    for any other field, and for an address that is not kept: a link to a bookmark alone or to a file keeps only its text."""
    match = _HYPERLINK_FIELD.match(instruction)
    if match is None:
        return None
    address = match.group(1) if match.group(1) is not None else match.group(2)
    bookmark = _HYPERLINK_BOOKMARK.search(instruction, match.end())
    return link_target(f"{address}#{bookmark.group(1)}" if bookmark and bookmark.group(1) and "#" not in address else address)


def link_target(address: str | None) -> str | None:
    """The address to write into `[text](address)`, or None when the link is not kept.

    Only `http`, `https` and `mailto` addresses are kept — a relative address or a `#fragment` points nowhere outside the original
    page, and `javascript:`, `file:` or `data:` must not become something a reader of the Markdown can follow. Nothing is resolved or
    fetched. Characters that would end the link early or break a table row are percent-encoded, so the text of a document cannot
    close the link and open another one (Pinned by: test_inline_formatting.py::TestLinkTargets).
    """
    if not address:
        return None
    url = address.strip()
    scheme = next((scheme for scheme in _LINK_SCHEMES if url[:len(scheme)].lower() == scheme), None)
    if scheme is None or len(url) == len(scheme):
        return None
    return _LINK_UNSAFE.sub(lambda match: "".join(f"%{byte:02X}" for byte in match.group().encode("utf-8", "replace")), url)


def link_off(url: str) -> InlineMark:
    return InlineMark("a", False, url)


def link_close(url: str) -> str:
    """The temporary mark that ends a link started with `LINK_OPEN`. `url` must come from `link_target`."""
    return f"{_LINK_TARGET}{url}{_LINK_CLOSE}"


def mark_emphasis(text: str, bold: bool, italic: bool) -> str:
    """Put temporary marks around the text of a bold and/or italic run. Whitespace-only text is left unmarked — it joins the runs
    around it (`resolve_inline`)."""
    if not (bold or italic) or not text.strip():
        return text
    if italic:
        text = f"{_ITALIC_OPEN}{text}{_ITALIC_CLOSE}"
    return f"{_BOLD_OPEN}{text}{_BOLD_CLOSE}" if bold else text


def merge_emphasis_marks(text: str) -> str:
    """Join the temporary marks of neighbouring runs that have the same emphasis. Done before the strikethrough and script marks
    are resolved: with an emphasis mark between them, struck-through runs that Word split per character would each get their own
    `~~` pair."""
    if _BOLD_CLOSE not in text and _ITALIC_CLOSE not in text:
        return text
    return _ITALIC_JOINT.sub(r"\1", _BOLD_JOINT.sub(r"\1", text))


def plain_inline(text: str) -> str:
    """The text without the temporary link and emphasis marks (and without the link addresses)."""
    return _INLINE_TOKEN.sub("", text) if _HAS_INLINE_MARK.search(text) else text


@dataclass(slots=True)
class _Atom:
    """A piece of text with the formatting that applies to it. `kind`: "text", "space" (whitespace only: takes the formatting both
    neighbours share), "plain" (a placeholder or a bullet at the start of a line: never marked), "break" (ends a line: a line
    break, or unformatted lines from one line break to another kept in one piece) or "gap" (a blank line: ends a block)."""

    text: str
    bold: bool
    italic: bool
    url: str | None
    kind: str


def resolve_inline(source: str | list[str | InlineMark], *, emphasis: bool = True) -> str:
    """Turn link and emphasis spans into Markdown: `[text](url)`, `**bold**`, `*italic*`, `***both***`.

    `source` is text holding temporary marks (`mark_emphasis`, `LINK_OPEN` … `link_close`) or a list of text pieces and
    `InlineMark`s. With `emphasis` off only links are written (headings). Rules, each recorded in
    docs/decisions/inline-formatting.md and pinned in test_inline_formatting.py:

    - Neighbouring pieces with the same formatting are one span, also across whitespace (Word splits a sentence into many runs).
    - Whitespace goes outside the marks and whitespace-only spans are not marked. A span is closed and reopened at a line break,
      and a bullet or number at the start of a line stays in front of the marks.
    - A span whose words are in different blocks (a blank line between them, or a table, list or code block inside it —
      `BLOCK_INSIDE`) is not written: Markdown emphasis and links do not cross paragraphs.
    - Placeholders (pictures, footnote references …) are never emphasized and stay outside a link at its edges; a link with no
      text left is not written. A link whose text is its own address stays the bare address.
    - Bold or italic that covers a whole block (paragraph, table cell line) from its first to its last word is not written:
      it is how the block is styled, not an emphasis inside it.
    - A `*` at the edge of an emphasized span and unbalanced or link-forming brackets inside link text are escaped with `\\`.

    Everything is done in one pass over the pieces (Pinned by: test_inline_formatting.py::TestLinearTime).
    """
    if isinstance(source, str):
        if not _HAS_INLINE_MARK.search(source):
            return source
        tokens: list[str | InlineMark] = []
        position = 0
        for match in _INLINE_TOKEN.finditer(source):
            if match.start() > position:
                tokens.append(source[position:match.start()])
            mark = match.group()
            tokens.append(_CHAR_MARKS[mark] if len(mark) == 1 else link_off(match.group(1)))
            position = match.end()
        tokens.append(source[position:])
        # A stray half of a link mark (the extractors do not produce one) goes the way of the other control characters
        tokens = [_HAS_INLINE_MARK.sub("", token) if isinstance(token, str) else token for token in tokens]
    else:
        tokens = source
        if not any(isinstance(token, InlineMark) and token.kind != "x" for token in tokens):
            return "".join(token for token in tokens if isinstance(token, str))

    return _write_atoms(_settle_lines(_inline_atoms(tokens, emphasis)))


def _inline_atoms(tokens: list[str | InlineMark], emphasis: bool) -> list[_Atom]:
    """Pieces and marks → atoms carrying their formatting. A span is valid when it was closed and all its words are in one block;
    a piece is bold / italic when any valid span of that kind encloses it, and linked to the address of the innermost valid link."""
    # Per span, in the order they open: kind, the enclosing span of the same kind (-1 when none), the number of worded pieces
    # seen when it opened, whether it is valid, and (links) its address
    kinds: list[str] = []
    parents: list[int] = []
    first_word: list[int] = []
    valid: list[bool] = []
    urls: list[str | None] = []
    open_spans: dict[str, list[int]] = {"b": [], "i": [], "a": []}
    # text, innermost bold span, italic span, link span (-1 when none), whether the piece is a blank line between blocks
    pieces: list[tuple[str, int, int, int, bool]] = []
    word_blocks: list[int] = []  # for every worded piece so far, the number of the block it is in
    block = 0
    after_line_break = False

    for token in tokens:
        if isinstance(token, InlineMark):
            if token.kind == "x":  # a table, list, code block or quote: a block of its own that no span can hold
                word_blocks.append(block + 1)
                block += 2
                continue
            stack = open_spans[token.kind]
            if token.opens:
                stack.append(len(kinds))
                kinds.append(token.kind)
                parents.append(stack[-2] if len(stack) > 1 else -1)
                first_word.append(len(word_blocks))
                valid.append(False)
                urls.append(None)
            elif stack:
                span = stack.pop()
                # No words, or the first and the last word in different blocks: not written
                valid[span] = len(word_blocks) > first_word[span] and word_blocks[first_word[span]] == word_blocks[-1]
                urls[span] = token.url if valid[span] else None
            continue
        for position, part in enumerate(_BLOCK_GAP.split(token)):
            if not part:
                continue
            if position % 2:
                block += 1
                after_line_break = True
            elif part.strip(" \t"):
                if after_line_break and _STARTS_WITH_LINE_BREAK.match(part):
                    block += 1
                after_line_break = _ENDS_WITH_LINE_BREAK.search(part) is not None
                if part.strip():
                    word_blocks.append(block)
            pieces.append((part, open_spans["b"][-1] if open_spans["b"] else -1, open_spans["i"][-1] if open_spans["i"] else -1,
                           open_spans["a"][-1] if open_spans["a"] else -1, position % 2 == 1))

    # The enclosing span opened earlier, so it is settled before the spans inside it
    for span, parent in enumerate(parents):
        if parent < 0:
            continue
        if kinds[span] == "a":
            urls[span] = urls[span] or urls[parent]
        else:
            valid[span] = valid[span] or valid[parent]

    atoms: list[_Atom] = []
    for text, bold_span, italic_span, link_span, is_gap in pieces:
        bold = emphasis and bold_span >= 0 and valid[bold_span]
        italic = emphasis and italic_span >= 0 and valid[italic_span]
        url = urls[link_span] if link_span >= 0 else None
        if is_gap:
            atoms.append(_Atom(text, False, False, url, "gap"))
            continue
        lines = text.split("\n")
        # Unformatted lines (a table or list rendered on its own) stay one piece from the first line break to the last: only
        # the first and the last line can touch a span. A page with a 100,000-row table is not cut into as many pieces.
        whole = len(lines) > 2 and not (bold or italic or url)
        if whole:
            lines = [lines[0], lines[-1]]
        for index, line in enumerate(lines):
            if index:
                between = text[len(lines[0]):len(text) - len(lines[-1])] if whole else "\n"
                atoms.append(_Atom(between, False, False, url, "break"))
            if not line:
                continue
            body = line.strip()
            if not body:
                atoms.append(_Atom(line, bold, italic, url, "space"))
            elif _is_placeholder_text(body):
                atoms.append(_Atom(line, False, False, url, "plain"))
            else:
                atoms.append(_Atom(line, bold, italic, url, "text"))
    return atoms


def _is_placeholder_text(body: str) -> bool:
    """Whether the (stripped) piece is nothing but object placeholders, or one equation (`$…$`)."""
    if body[0] not in "⟦[$":
        return False
    if _EQUATION_ONLY.fullmatch(body):
        return True
    return not "".join(_INLINE_PLACEHOLDER.split(body)[::2]).strip()


def _settle_lines(atoms: list[_Atom]) -> list[_Atom]:
    """Per line: take a bullet or number at its start out of the spans (`_without_lead`). Per block (the lines up to a blank
    line): drop bold, or italic, that every word of the block carries (`_drop_uniform_emphasis`). Then whitespace between two
    pieces takes the formatting both share."""
    settled: list[_Atom] = []
    block_start = 0
    line: list[_Atom] = []
    for atom in (*atoms, None):
        if atom is not None and atom.kind not in _LINE_ENDS:
            line.append(atom)
            continue
        blank = all(piece.kind == "space" for piece in line)
        settled.extend(_without_lead(line))
        if blank or atom is None or atom.kind == "gap":
            _drop_uniform_emphasis(settled, block_start)
            block_start = len(settled) + 1
        if atom is not None:
            settled.append(atom)
        line = []

    before: _Atom | None = None
    waiting: list[_Atom] = []
    for atom in (*settled, None):
        if atom is not None and atom.kind == "space":
            waiting.append(atom)
            continue
        same_line = before is not None and atom is not None and atom.kind not in _LINE_ENDS
        for space in waiting:
            space.bold = same_line and before.bold and atom.bold
            space.italic = same_line and before.italic and atom.italic
            space.url = before.url if same_line and before.url == atom.url else None
        waiting.clear()
        before = None if atom is None or atom.kind in _LINE_ENDS else atom
    _drop_emphasis_inside_words(settled)
    return settled


def _drop_emphasis_inside_words(atoms: list[_Atom]) -> None:
    """Clear bold (or italic) that styles part of a word instead of stressing words: a span that starts in the middle of a word
    (`1<b>2</b>,345`, `인사<em>발령</em>`, a letter in another colour), and a single character glued to the rest of its word
    (`<b>부</b>분`, a decorated initial). Marked, they came out as `1**2**,345` and `**부**분`: noise inside a word, and marks
    Markdown readers do not agree on.

    Deliberate: a span that starts a word and stops before its ending is kept — `**매출**은`, `**신규 요금제**를`. In Korean the
    particle is written onto the stressed word without a space, and most emphasis in real documents has this shape. Known accepted
    gap: an emphasized one-syllable word followed by a particle (`<b>값</b>이`) and a stressed initial (`<b>S</b>trength`) are left
    unmarked too (Pinned by: test_inline_formatting.py::TestPartOfAWord).
    """
    count = len(atoms)
    for name in ("bold", "italic"):
        index = 0
        while index < count:
            if not getattr(atoms[index], name):
                index += 1
                continue
            end = index
            while end < count and getattr(atoms[end], name):
                end += 1
            first, last = atoms[index].text, atoms[end - 1].text
            before = atoms[index - 1].text[-1:] if index else ""
            after = atoms[end].text[:1] if end < count else ""
            starts_inside = before.isalnum() and first[:1].isalnum()
            is_glued_character = (end - index == 1 and len(first.strip()) == 1 and last[-1:].isalnum() and after.isalnum())
            if starts_inside or is_glued_character:
                for position in range(index, end):
                    setattr(atoms[position], name, False)
            index = end


def _without_lead(line: list[_Atom]) -> list[_Atom]:
    """The line with the bullet or number at its start (`_INLINE_LEAD`) made a plain piece of its own, when formatted text
    follows it. The words after a heading mark (`#`) lose their emphasis: a heading is not emphasized."""
    if not any(atom.bold or atom.italic or atom.url for atom in line):
        return line
    head = ""
    for atom in line:
        head += atom.text
        if len(head) >= _LEAD_PROBE:
            break
    lead = _INLINE_LEAD.match(head)
    if lead is None or not lead.group().strip():
        return line
    if lead.group().lstrip().startswith("#"):
        for atom in line:
            atom.bold = atom.italic = False
    left = lead.end()
    result: list[_Atom] = []
    for index, atom in enumerate(line):
        if not left:
            result.extend(line[index:])
            break
        if len(atom.text) <= left:
            left -= len(atom.text)
            result.append(_Atom(atom.text, False, False, None, "plain"))
            continue
        if not atom.text[left:].strip() and all(piece.kind == "space" for piece in line[index + 1:]):
            return line  # nothing but the bullet on this line
        result.append(_Atom(atom.text[:left], False, False, None, "plain"))
        result.append(_Atom(atom.text[left:], atom.bold, atom.italic, atom.url, atom.kind if atom.text[left:].strip() else "space"))
        left = 0
    return result


def _drop_uniform_emphasis(atoms: list[_Atom], start: int) -> None:
    """Clear bold (and italic) in the block `atoms[start:]` when every word of the block carries it.

    Bold over a whole paragraph, list item or table cell line is how that block is styled (a label, a header cell, a slide
    caption), not a stress inside it. Marked, 5,939 of the 7,777 marked lines of 143 real DOCX / PPTX files were whole lines in
    `**…**` — in slide decks a quarter of all lines (Pinned by: test_inline_formatting.py::TestWholeBlocks).
    """
    block = range(start, len(atoms))  # indices, not a slice: a document of many short blocks stays linear
    words = [atoms[index] for index in block if atoms[index].kind == "text"]
    if not words:
        return
    for name in ("bold", "italic"):
        if all(getattr(atom, name) for atom in words):
            for index in block:
                setattr(atoms[index], name, False)


def _write_atoms(atoms: list[_Atom]) -> str:
    out: list[str] = []
    index = 0
    while index < len(atoms):
        url = atoms[index].url
        end = index + 1
        while end < len(atoms) and atoms[end].url == url:
            end += 1
        group = atoms[index:end]
        text = _write_emphasis(group)
        out.append(_write_link(text, url, group) if url else text)
        index = end
    return "".join(out)


def _write_link(markup: str, url: str, group: list[_Atom]) -> str:
    """`[text](url)` with whitespace and placeholders at the edges left outside. Text that is the address itself stays as it is —
    `[https://a.example](https://a.example)` says the same thing twice."""
    parts = _INLINE_PLACEHOLDER.split(markup)
    worded = [position for position in range(0, len(parts), 2) if parts[position].strip()]
    if not worded:
        return markup
    first, last = worded[0], worded[-1]
    head, tail = parts[first], parts[last]
    lead = "".join(parts[:first]) + head[:len(head) - len(head.lstrip())]
    trail = tail[len(tail.rstrip()):] + "".join(parts[last + 1:])
    core = markup[len(lead):len(markup) - len(trail)]
    edges = _without_edge_strike(core)
    if edges is not None and edges[1]:
        lead, core, trail = lead + edges[0], edges[1], edges[2] + trail

    label = "".join(atom.text for atom in group if atom.kind != "plain").strip()
    addresses = {url.rstrip("/"), url.removeprefix("mailto:").rstrip("/")}
    if label.rstrip("/") in addresses or (link_target(label) or "").rstrip("/") in addresses:
        return markup
    return f"{lead}[{_escape_link_text(core)}]({url}){trail}"


def _escape_link_text(text: str) -> str:
    """Escape the brackets that would end the link text early or start another link inside it: a `]` without its `[`, a `[`
    without its `]`, and a bracket pair followed by `(` or `[` (`[a](b)` and `[a][b]` are links). Other balanced brackets stay
    (`[Image: logo]`, `[1]`). A lone backslash at the very end is doubled so that it does not escape the closing bracket."""
    if "[" not in text and "]" not in text and not text.endswith("\\"):
        return text
    escaped: list[int] = []
    opened: list[int] = []
    lone_backslash = False
    for match in _BRACKET_OR_ESCAPE.finditer(text):
        token = match.group()
        if token[0] == "\\":
            lone_backslash = len(token) == 1
        elif token == "[":
            opened.append(match.start())
        elif not opened:
            escaped.append(match.start())
        else:
            opening = opened.pop()
            if text[match.end():match.end() + 1] in ("(", "["):
                escaped.extend((opening, match.start()))
    escaped.extend(opened)
    pieces = []
    position = 0
    for place in sorted(escaped):
        pieces.append(text[position:place])
        pieces.append("\\")
        position = place
    pieces.append(text[position:])
    return "".join(pieces) + ("\\" if lone_backslash else "")


def _write_emphasis(atoms: list[_Atom]) -> str:
    """The atoms of one link (or of the text between links) with their emphasis marks. Bold is the outer mark: `**a *b* c**`."""
    if not any(atom.bold or atom.italic for atom in atoms):
        return "".join(atom.text for atom in atoms)
    out: list[str] = []
    index = 0
    while index < len(atoms):
        atom = atoms[index]
        end = index + 1
        if not atom.bold:
            while end < len(atoms) and not atoms[end].bold and atoms[end].italic == atom.italic:
                end += 1
            text = "".join(piece.text for piece in atoms[index:end])
            out.append(_wrap(_guard(text), "*") if atom.italic else text)
            index = end
            continue
        while end < len(atoms) and atoms[end].bold:
            end += 1
        group = atoms[index:end]
        if all(piece.italic for piece in group):
            out.append(_wrap(_guard("".join(piece.text for piece in group)), "***"))
            index = end
            continue
        inner: list[str] = []
        start = 0
        while start < len(group):
            stop = start + 1
            while stop < len(group) and group[stop].italic == group[start].italic:
                stop += 1
            text = "".join(piece.text for piece in group[start:stop])
            if group[start].italic:
                inner.append(_wrap(_guard(text), "*"))
            else:
                inner.append(_guard(text, leading=start == 0, trailing=stop == len(group)))
            start = stop
        out.append(_wrap("".join(inner), "**"))
        index = end
    return "".join(out)


def _guard(text: str, *, leading: bool = True, trailing: bool = True) -> str:
    """Escape what would change the meaning of the mark written next to `text`: a literal `*` at the edge (it would lengthen the
    mark) and a lone backslash at the end (it would escape the mark)."""
    body = text.strip()
    if not body:
        return text
    lead, trail = text[:len(text) - len(text.lstrip())], text[len(text.rstrip()):]
    is_single = len(body) == 1
    if leading and body[0] == "*":
        body = "\\" + body
    if trailing and not (is_single and len(body) == 2):  # a lone `*` is escaped once
        if body[-1] == "*":
            body = body[:-1] + "\\*"
        elif body[-1] == "\\" and (len(body) - len(body.rstrip("\\"))) % 2:
            body += "\\"
    return f"{lead}{body}{trail}"


def _wrap(text: str, mark: str) -> str:
    """`mark` before the first and after the last character of `text`; whitespace stays outside, and so does half of a
    strikethrough pair (`_without_edge_strike`). A span that strikethrough marks cross in its middle is left unmarked."""
    edges = _without_edge_strike(text.strip())
    if edges is None or not edges[1]:
        return text
    before, body, after = edges
    return f"{text[:len(text) - len(text.lstrip())]}{before}{mark}{body}{mark}{after}{text[len(text.rstrip()):]}"


def _without_edge_strike(body: str) -> tuple[str, str, str] | None:
    """(`~~` moved in front, body, `~~` moved behind), or None when the strikethrough marks in `body` cannot be made to nest with
    a mark put around it.

    The HTML extractor writes `~~` into the text before the spans are resolved, so a span can hold only one half of a pair. At
    the edge of the span that half goes outside: `<s>구 <b>판</b></s>` is `~~구 **판**~~`, not `~~구 **판~~**`. A half in the
    middle (two bold words in two different strikethroughs, joined across the space between them) cannot be moved
    (Pinned by: test_inline_formatting.py::TestHtmlEmphasis::test_strikethrough_and_emphasis_nest).
    """
    if "~~" not in body:
        return "", body, ""
    depth = 0
    places = [match.start() for match in _STRIKE_MARK.finditer(body)]
    lone_closing = lone_opening = -1
    for place in places:
        opens = body[place + 2:place + 3].strip() != ""
        closes = place > 0 and not body[place - 1].isspace()
        if opens and (not closes or depth == 0):
            depth += 1
            if depth == 1:
                lone_opening = place
        elif depth:
            depth -= 1
        elif lone_closing < 0:
            lone_closing = place
        else:
            return None
    if depth > 1 or (depth and lone_closing >= 0):
        return None
    if depth:
        if lone_opening != 0:
            return None
        inner = body[2:].lstrip()
        return body[:len(body) - len(inner)], inner, ""
    if lone_closing >= 0:
        if lone_closing != len(body) - 2:
            return None
        inner = body[:-2].rstrip()
        return "", inner, body[len(inner):]
    return "", body, ""
