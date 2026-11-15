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


def has_text(pieces: list[str]) -> bool:
    """Whether the text pieces hold anything but whitespace and link marks."""
    return any(type(piece) is not _LinkMark and piece.strip() for piece in pieces)


def command_link(command: str) -> str | None:
    """The vetted address (`_inline.link_target`: http, https and mailto only) of a Hangul hyperlink field, from its command —
    HWP the command string of the `%hlk` field control, HWPX the `Command` parameter of `hp:fieldBegin type="HYPERLINK"`. The
    command is `address;…` with `:` and `;` of the address escaped by a backslash (`http\\://a.example/;1;0;0;`). A link to a
    bookmark, a file or another document gives None, and its text stays plain."""
    return link_target(_COMMAND_ESCAPE.sub(r"\1", _COMMAND_ADDRESS.match(command).group()))


def decorated_text(text: str, mark: str, level: int, style_name: str, margin: int, nesting: BulletNesting, *, in_cell: bool,
                   check: str = "", plain: str | None = None) -> str:
    """The first text piece of a paragraph with its head (`_lists.paragraph_head`) and, for an outline style, the heading mark.

    `mark` and `level` are the paragraph head the format read, `style_name` the name of the paragraph style, `margin` the left
    margin of the paragraph shape, `check` the check state of a check bullet (HWPX). `plain` is the text without link marks and
    addresses: whether the paragraph is a heading is decided on it, so a link does not change that (as in DOCX).
    """
    plain = text if plain is None else plain
    match = None if in_cell else OUTLINE_STYLE.match(style_name.strip())
    heading = match is not None and len(f"{mark} {plain}" if mark else plain) <= HEADING_MAX_CHARS and "\n" not in plain

    # The level of a bullet paragraph is set from its left margin compared with the preceding items (`BulletNesting`). Other
    # paragraphs close the more deeply indented items
    if mark == "-" and not heading:
        level = nesting.bullet(level, margin)
    else:
        nesting.other(margin)

    head = paragraph_head(mark, level, in_cell=in_cell, heading=heading) + check
    return f"{'#' * min(int(match.group(1)), 6)} {head}{text}" if heading else head + text


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
        if not self._headed and any(self.blocks):
            mark, level = head()
            number = paragraph_head(mark, level, in_cell=in_cell, heading=False) if mark != "-" else ""
            if self._bare is not None:
                self.blocks[self._bare] = number + self.blocks[self._bare]
            elif number:
                self.blocks.insert(0, number.rstrip())
        return self.blocks
