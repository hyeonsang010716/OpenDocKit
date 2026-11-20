"""Paragraph rules shared by HWP and HWPX, the binary and the XML file format of Hangul.

The two extractors read different containers and arrive at the same things for a paragraph: a head (number or bullet), a
style name, a left margin, and text pieces with tables and text boxes between them. What is done with those is decided
here once, so the two formats cannot drift apart.
"""

from collections.abc import Callable
import re

from opendockit.extractors._inline import (
    LINK_OPEN,
    link_close,
    link_target,
    plain_inline,
    resolve_inline,
    resolve_scripts,
    resolve_strikes,
)
from opendockit.extractors._lists import BulletNesting, is_placeholder_only, paragraph_head


# Hangul's "개요 1" to "개요 7" (Outline 1-7) paragraph styles are the chapter/section structure of the document. They become Markdown headings.
OUTLINE_STYLE = re.compile(r"^(?:개요|Outline)\s*([1-9])$")
# A paragraph longer than this stays body text, not a heading, even with an outline style (some documents write body text in outline styles).
HEADING_MAX_CHARS = 120
# Table cell marks in the preview text (HWP `PrvText`, HWPX `Preview/PrvText.txt`). A preview with nothing but these has no text.
PREVIEW_CELL_MARKS = str.maketrans("", "", "<>")

# The temporary link marks of `_inline` (U+000E–U+0014). Document text never holds them in a paragraph that has a link: the text
# of a control (ruby text, composed characters) is read from a binary HWP as it is, and could otherwise forge a link.
_WITHOUT_INLINE_MARKS = str.maketrans("", "", "".join(map(chr, range(0x0E, 0x15))))
# The address of a hyperlink field command: the part before the first `;` that is not escaped (`http\://a.example;1;0;0;`).
_COMMAND_ADDRESS = re.compile(r"(?:[^\\;]|\\.)*", re.DOTALL)
_COMMAND_ESCAPE = re.compile(r"\\(.)", re.DOTALL)


class _LinkMark(str):
    """A link mark in the text pieces of a paragraph, told apart from the text of the document (`ParagraphPieces.flush`)."""

    __slots__ = ()


def link_start() -> str:
    """The piece that starts a hyperlink in a paragraph (`ParagraphPieces`); `link_end` ends it."""
    return _LinkMark(LINK_OPEN)


def link_end(url: str) -> str:
    """The piece that ends a hyperlink started with `link_start`. `url` must come from `command_link`."""
    return _LinkMark(link_close(url))


# The guide of a click-here field (누름틀) in its command: `Clickhere:set:44:Direction:wstring:2:날짜 HelpState:…`, the length
# of the text in characters before it.
_CLICK_HERE_GUIDE = re.compile(r"Direction:wstring:(\d{1,5}):")


def click_here_guide(command: str) -> str | None:
    """The guide text of a click-here field from its command (HWP the `%clk` field control, HWPX the `Command` parameter of
    `hp:fieldBegin type="CLICK_HERE"`), or None when there is none."""
    found = _CLICK_HERE_GUIDE.search(command)
    if found is None:
        return None
    guide = command[found.end():found.end() + int(found.group(1))]
    return guide.strip() or None


def drop_unfilled_guide(pieces: list[str], start: int, guide: str | None) -> None:
    """Remove the pieces from `start` on when they are nothing but the guide text of a click-here field that was not filled in.

    Hangul shows the guide of an empty field (`이곳을 마우스로 누르고 내용을 입력하세요`, or a word such as `날짜`) in its place
    on screen and does not print it, and it was read as the content of the form (Pinned by:
    test_hwp.py::TestClickHereFields). What a person typed into the field stays, as does a field that ends in another paragraph.
    """
    if guide is None or not 0 <= start <= len(pieces):
        return
    if "".join(piece for piece in pieces[start:] if type(piece) is not _LinkMark).strip() == guide:
        del pieces[start:]


def has_text(pieces: list[str]) -> bool:
    """Whether the text pieces hold anything but whitespace and link marks."""
    return any(type(piece) is not _LinkMark and piece.strip() for piece in pieces)


def command_link(command: str) -> str | None:
    """The vetted address (`_inline.link_target`: http, https and mailto only) of a Hangul hyperlink field, from its command —
    HWP the command string of the `%hlk` field control, HWPX the `Command` parameter of `hp:fieldBegin type="HYPERLINK"`. The
    command is `address;…` with `:` and `;` of the address escaped by a backslash (`http\\://a.example/;1;0;0;`). A link to a
    bookmark, a file or another document gives None, and its text stays plain."""
    return link_target(_COMMAND_ESCAPE.sub(r"\1", _COMMAND_ADDRESS.match(command).group()))


class _SizeCandidate(str):
    """A top-level paragraph that may be a heading by its character size (`SizeHeadings`). Its value is the paragraph as body text;
    `heading` is the same paragraph written as a heading (the number without a list bullet), `size` its character size. (A `str`
    subclass cannot have `__slots__` that hold values.)"""

    heading: str
    size: int


# Ratio of a paragraph's character size to the body size → heading level (`SizeHeadings`)
_SIZE_HEADING_LEVELS = ((1.5, 1), (1.3, 2), (1.15, 3))
# A paragraph longer than this is not taken for a heading by its size (a sentence written large, a cover-page blurb)
_SIZE_HEADING_MAX_CHARS = 80
# A word in any script: a paragraph without one (a date `2025.12.12`, a blank form date `202 . .`, a page number) is no heading.
_LETTER = re.compile(r"[^\W\d_]")
_EQUATION = re.compile(r"\$[^$\n]*\$")
# A sentence: the Korean sentence endings `…다` (`…합니다`, `…한다`) and `…세요`, or a closing full stop, question or exclamation
# mark. Not `…요` alone: 개요, 필요 and 주요 end many titles ('1. 사업 개요').
_SENTENCE_END = re.compile(r"(?:다|세요)[.!?]?\Z|[.!?]\Z")
# The addressee line of a Korean form or letter (`…사업단장 귀하`, `… 귀중`), printed large at the bottom: not a heading.
_ADDRESSEE = re.compile(r"(?:귀하|귀중)\Z")
# What follows the last tab of a table-of-contents line: a page label (`7`, `3-12`, `ii`, `n+1`, `2 3`)
_PAGE_LABEL = re.compile(r"[\divxlcIVXLCn+\-–—~., ]{1,12}")


def _size_heading_text(plain: str) -> bool:
    """Whether a paragraph can be a heading by its size, from its text: short, one line, with a word, not a sentence, not the
    addressee line of a form, and not a table-of-contents line (text, tab, page label)."""
    text = plain.strip()
    if not text or len(text) > _SIZE_HEADING_MAX_CHARS or "\n" in text:
        return False
    if not _LETTER.search(_EQUATION.sub("", text)) or _SENTENCE_END.search(text) or _ADDRESSEE.search(text):
        return False
    return "\t" not in text or not _PAGE_LABEL.fullmatch(text.rsplit("\t", 1)[1].strip())


class SizeHeadings:
    """Headings told by character size, for Hangul documents that set their titles by size alone.

    Most Hangul documents write every paragraph in the 바탕글 (Normal) style and make a title by setting a larger (usually bold)
    character size on it, not by the 개요 (outline) styles or paragraph heads. In 26 real HWP files and 8 HWPX files, none had an
    outline style or head on a title, so no heading came out at all. A top-level paragraph whose smallest character size is at least
    1.15 / 1.3 / 1.5 times the body size becomes a level 3 / 2 / 1 heading; the body size is the size most characters of the
    document have (counted while it is read). The paragraph must also look like a title (`_size_heading_text`): the same files
    otherwise gave a heading for a cover-page date, every table-of-contents line, the addressee line of a form and a sentence written
    large. A document with any outline heading (style or paragraph head) keeps those only: there the author marked the structure.

    The paragraphs are rendered as they are read, before the body size is known, so a candidate is kept as a `_SizeCandidate` in the
    blocks and settled by `resolve` at the end (Pinned by: test_hwp_size_headings.py).
    """

    def __init__(self) -> None:
        self._sizes: dict[int, int] = {}   # character size → number of characters written in it
        self.outlined = False              # whether the document has an outline heading

    def count(self, size: int, characters: int) -> None:
        if size > 0 and characters > 0:
            self._sizes[size] = self._sizes.get(size, 0) + characters

    def candidate(self, body: str, heading: str, size: int) -> str:
        candidate = _SizeCandidate(body)
        candidate.heading, candidate.size = heading, size
        return candidate

    def resolve(self, blocks: list[str]) -> list[str]:
        """The blocks with each candidate settled into a heading or left as body text (a plain `str`)."""
        if not any(type(block) is _SizeCandidate for block in blocks):
            return blocks
        # The body size: the size of most characters, the smaller one when two sizes are as frequent
        body = max(self._sizes.items(), key=lambda item: (item[1], -item[0]))[0] if self._sizes else 0
        settled = []
        for block in blocks:
            if type(block) is not _SizeCandidate:
                settled.append(block)
                continue
            level = 0 if self.outlined or body <= 0 else next(
                (level for ratio, level in _SIZE_HEADING_LEVELS if block.size >= body * ratio), 0)
            settled.append(f"{'#' * level} {block.heading}" if level else str(block))
        return settled


def decorated_text(text: str, mark: str, level: int, style_name: str, margin: int, nesting: BulletNesting, *, in_cell: bool,
                   check: str = "", plain: str | None = None, outline: bool = False, size: int = 0,
                   size_headings: SizeHeadings | None = None, top: bool = False, in_note: bool = False) -> str:
    """The first text piece of a paragraph with its head (`_lists.paragraph_head`) and, for an outline style or outline head, the
    heading mark.

    `mark` and `level` are the paragraph head the format read, `outline` whether that head is an outline head (개요 번호),
    `style_name` the name of the paragraph style, `margin` the left margin of the paragraph shape, `check` the check state of a
    check bullet (HWPX). `plain` is the text without link marks and addresses: whether the paragraph is a heading is decided on it,
    so a link does not change that (as in DOCX). `size` is the smallest character size of the paragraph's text when it is a
    top-level body paragraph (0 otherwise); with `size_headings` such a paragraph can become a heading by its size (`SizeHeadings`).

    A paragraph with an outline head is a heading of its outline level + 1, as one in a 개요 style is: Hangul builds the outline
    (and the table of contents) from the head, and a real document put it on 바탕글 paragraphs, which came out as a numbered list
    (Pinned by: test_hwp_size_headings.py::TestOutlineHeads). The style decides the level when both are there, as before.
    An outline head makes a heading only in a top-level body paragraph (`top`); in a text box, a caption or a header it stays a
    number. No paragraph of a footnote or endnote (`in_note`) is a heading: `[^1]: # 1. 항목` is no Markdown heading and broke
    the note (Pinned by: test_hwp_size_headings.py::TestOutlineHeads::test_no_heading_in_a_note_or_text_box).
    """
    plain = text if plain is None else plain
    match = None if in_cell or in_note else OUTLINE_STYLE.match(style_name.strip())
    heading_level = int(match.group(1)) if match is not None else level + 1 if outline and top else 0
    fits = len(f"{mark} {plain}" if mark else plain) <= HEADING_MAX_CHARS and "\n" not in plain
    heading = bool(heading_level) and fits
    if heading and top and size_headings is not None:
        size_headings.outlined = True

    # The level of a bullet paragraph is set from its left margin compared with the preceding items (`BulletNesting`). Other
    # paragraphs close the more deeply indented items
    if mark == "-" and not heading:
        level = nesting.bullet(level, margin)
    else:
        nesting.other(margin)

    head = paragraph_head(mark, level, in_cell=in_cell, heading=heading) + check
    if heading:
        return f"{'#' * min(heading_level, 6)} {head}{text}"
    if size and size_headings is not None and not in_cell and mark != "-" and _size_heading_text(plain):
        heading_head = paragraph_head(mark, level, in_cell=False, heading=True) + check
        return size_headings.candidate(head + text, heading_head + text, size)
    return head + text


class ParagraphPieces:
    """The blocks of one paragraph: its text pieces, and the tables and text boxes that stand between them.

    The paragraph head (number or bullet) and the outline heading mark are attached only to the first text piece of the paragraph.
    Text that continues after a table or text box is the rest of the same paragraph — it got the heading mark too and became
    '# 은 아래와 같다' ('# is as follows') (Pinned by: test_hwp_heads_and_shapes.py::TestOutlineParagraphSplitByATable).

    The extractor appends text to `buffer`, calls `flush` before it appends a table or text box to `blocks`, and takes the blocks
    from `finish`. `decorate` gives a text piece its head (`decorated_text`), from the piece and its text without link marks.

    A hyperlink is a `link_start` and a `link_end` piece around its text, written by the shared rules (`_inline.resolve_inline`);
    a link that does not end in the same piece of text (across a table, a text box or a paragraph end) stays text.
    """

    def __init__(self, decorate: Callable[[str, str], str]) -> None:
        self.blocks: list[str] = []
        self.buffer: list[str] = []
        self._decorate = decorate
        self._headed = False
        # first piece emitted without a head, only placeholders. If the paragraph ends up with no text the number goes here
        self._bare: int | None = None

    def flush(self) -> None:
        buffer = self.buffer
        linked = any(type(piece) is _LinkMark for piece in buffer)
        text = "".join(piece if type(piece) is _LinkMark else piece.translate(_WITHOUT_INLINE_MARKS) for piece in buffer) if linked \
            else "".join(buffer)
        text = resolve_scripts(resolve_strikes(text))  # strikethrough comes first (`resolve_strikes`), links last, as in DOCX
        plain = None
        if linked:
            plain = plain_inline(text).strip()
            text = resolve_inline(text, emphasis=False)
        text = text.strip()
        buffer.clear()
        if not text:
            return
        if self._headed:
            self.blocks.append(text)
        elif is_placeholder_only(text):
            self._bare = len(self.blocks) if self._bare is None else self._bare
            self.blocks.append(text)
        else:
            self.blocks.append(self._decorate(text, text if plain is None else plain))
            self._headed = True

    def finish(self, head: Callable[[], tuple[str, int]], *, in_cell: bool) -> list[str]:
        """The blocks of the paragraph. `head` reads the paragraph head (mark, level); it is called only when no text piece took
        the head, because reading a numbered head counts the number."""
        self.flush()

        # A numbered paragraph holding only a picture also counts its number — otherwise the later numbers shift up. The heading mark and
        # bullet are not attached (they would be left alone).
        # A paragraph holding only a table or text box has no piece to attach to, so just the number goes on the line before it. Without
        # counting it, '3. 추진 일정' after '2. [table]' on the Hangul screen shifted up to '2.'
        # (Pinned by: test_hwp_object_only_numbers.py).
        if not self._headed and not any(self.blocks):
            # An empty numbered paragraph takes its number too, as Hangul draws it, and writes nothing — the rule of DOCX, ODT
            # and RTF. Not counted, the items after it came one number too low ('1. 하나', '2. 셋' where Hangul shows 3.)
            # (Pinned by: test_hwp_heads_and_shapes.py::TestEmptyNumberedParagraph).
            head()
        elif not self._headed:
            mark, level = head()
            number = paragraph_head(mark, level, in_cell=in_cell, heading=False) if mark != "-" else ""
            if self._bare is not None:
                self.blocks[self._bare] = number + self.blocks[self._bare]
            elif number:
                self.blocks.insert(0, number.rstrip())
        return self.blocks
