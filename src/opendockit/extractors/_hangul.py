"""Paragraph rules shared by HWP and HWPX, the binary and the XML file format of Hangul.

The two extractors read different containers and arrive at the same things for a paragraph: a head (number or bullet), a
style name, a left margin, and text pieces with tables and text boxes between them. What is done with those is decided
here once, so the two formats cannot drift apart.
"""

from collections.abc import Callable
import re

from opendockit.extractors._inline import resolve_scripts, resolve_strikes
from opendockit.extractors._lists import BulletNesting, is_placeholder_only, paragraph_head


# Hangul's "개요 1" to "개요 7" (Outline 1-7) paragraph styles are the chapter/section structure of the document. They become Markdown headings.
OUTLINE_STYLE = re.compile(r"^(?:개요|Outline)\s*([1-9])$")
# A paragraph longer than this stays body text, not a heading, even with an outline style (some documents write body text in outline styles).
HEADING_MAX_CHARS = 120
# Table cell marks in the preview text (HWP `PrvText`, HWPX `Preview/PrvText.txt`). A preview with nothing but these has no text.
PREVIEW_CELL_MARKS = str.maketrans("", "", "<>")


def decorated_text(text: str, mark: str, level: int, style_name: str, margin: int, nesting: BulletNesting, *, in_cell: bool,
                   check: str = "") -> str:
    """The first text piece of a paragraph with its head (`_lists.paragraph_head`) and, for an outline style, the heading mark.

    `mark` and `level` are the paragraph head the format read, `style_name` the name of the paragraph style, `margin` the left
    margin of the paragraph shape, `check` the check state of a check bullet (HWPX).
    """
    match = None if in_cell else OUTLINE_STYLE.match(style_name.strip())
    heading = match is not None and len(f"{mark} {text}" if mark else text) <= HEADING_MAX_CHARS and "\n" not in text

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
    from `finish`. `decorate` gives a text piece its head (`decorated_text`).
    """

    def __init__(self, decorate: Callable[[str], str]) -> None:
        self.blocks: list[str] = []
        self.buffer: list[str] = []
        self._decorate = decorate
        self._headed = False
        # first piece emitted without a head, only placeholders. If the paragraph ends up with no text the number goes here
        self._bare: int | None = None

    def flush(self) -> None:
        text = resolve_scripts(resolve_strikes("".join(self.buffer))).strip()  # strikethrough comes first (`resolve_strikes`)
        self.buffer.clear()
        if not text:
            return
        if self._headed:
            self.blocks.append(text)
        elif is_placeholder_only(text):
            self._bare = len(self.blocks) if self._bare is None else self._bare
            self.blocks.append(text)
        else:
            self.blocks.append(self._decorate(text))
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
