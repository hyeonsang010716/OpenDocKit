"""A `#` or a `>` that starts its line only once the text is cleaned is text too (`markdown.escape_block_marks`).

The readers escaped the mark that stood first in a line as it was read. What cleaning takes away in front of a mark left
it first all the same, and the line was a heading or a quote as before: the spaces a line of code is indented with after
a line break (`    # comment`), a private-use character no glyph is known for (a comment line of a real Word
document, `# Give OLS processes more heap`), a zero-width space, and the mark after a line separator (U+2028), which cleaning makes a line break.
Whether something is gone is asked of the cleaning itself, so the two cannot drift apart.
"""

from html import escape
from io import BytesIO
import re
import sys

from docx import Document
from pptx import Presentation
from pptx.util import Inches
import pytest

from opendockit import convert
from opendockit.markdown import clean_characters, clean_text, escape_block_marks
from tests import builder
from tests.timing import fastest


_MARK_FIRST = re.compile(r"^(?:#{1,6}(?=\s|$)|>)", re.MULTILINE)


class TestEscapeBlockMarks:
    @pytest.mark.parametrize(("text", "expected"), [
        (" # one space", " \\# one space"),
        ("    # four spaces", "    \\# four spaces"),
        ("\t> tab", "\t\\> tab"),
        ("\xa0# no-break space", "\xa0\\# no-break space"),
        ("\u3000> wide space", "\u3000\\> wide space"),
        ("\u200b# zero-width space", "\u200b\\# zero-width space"),
        ("\ufeff> byte order mark", "\ufeff\\> byte order mark"),
        ("\xad# soft hyphen", "\xad\\# soft hyphen"),
        ("\u200e\u200f# direction marks", "\u200e\u200f\\# direction marks"),
        ("\ue000 > private use and a space", "\ue000 \\> private use and a space"),
        ("\x01# control", "\x01\\# control"),
        ("def f():\n    # comment\n    return 1", "def f():\n    \\# comment\n    return 1"),
        ("a\n\u200b> b", "a\n\u200b\\> b"),
    ])
    def test_a_mark_behind_what_cleaning_removes_is_escaped(self, text, expected):
        assert escape_block_marks(text) == expected
        assert not _MARK_FIRST.search(clean_text(expected))

    @pytest.mark.parametrize(("text", "expected"), [
        ("a\u2028# b", "a\u2028\\# b"),
        ("a\u2029> b", "a\u2029\\> b"),
        ("a\u0085# b", "a\u0085\\# b"),
        ("a\x0b# b", "a\x0b\\# b"),
        ("a\x0c> b", "a\x0c\\> b"),
        ("a\r# b", "a\r\\# b"),
        ("a\u2028 \u200b# b", "a\u2028 \u200b\\# b"),
    ])
    def test_a_mark_after_a_character_cleaning_makes_a_line_break_is_escaped(self, text, expected):
        assert escape_block_marks(text) == expected
        assert not _MARK_FIRST.search(clean_text(expected))

    @pytest.mark.parametrize("text", [
        "- # item", "* > item", "1. # item", "( > 3 )", "\u25cb # after a bullet", "\u203b > after a mark", "\ufe0f# variation selector",
        "\u3164# filler", "\u0301# combining mark", "\U0001f600 # pictograph", "a\n- > item", "\u200b#hashtag", "  ####### seven",
        "\u200bx # y", "_# underscore",
    ])
    def test_a_mark_behind_something_that_stays_is_as_typed(self, text):
        assert escape_block_marks(text) == text

    def test_no_character_in_front_of_a_mark_leaves_it_a_mark(self):
        """Every character of the basic plane, the tags and variation selectors of plane 14 and the start of both private
        planes, alone in front of a mark: once escaped and cleaned, no line starts with one."""
        codes = [*range(0xD800), *range(0xE000, 0x10000), *range(0xE0000, 0xE0200), *range(0xF0000, 0xF0100),
                 *range(0x100000, 0x100100)]

        left = [f"U+{code:04X}" for code in codes for mark in ("# x", "> x")
                if _MARK_FIRST.search(clean_text(escape_block_marks(f"가\n{chr(code)}{mark}")))]

        assert left == []

    def test_what_may_stand_in_front_of_a_mark_and_vanish_is_no_letter_and_no_digit(self):
        """The search stops at the first letter, digit or `_` of a line: cleaning removes none of them."""
        removed = [code for code in range(sys.maxunicode + 1)
                   if re.match(r"\w", chr(code)) and not clean_characters(chr(code)).strip()]

        assert removed == []

    def test_escaping_twice_changes_nothing_more(self):
        once = escape_block_marks("\u200b# a\n    > b\n# c")

        assert escape_block_marks(once) == once

    @pytest.mark.timing
    def test_lines_of_what_is_no_letter_take_linear_time(self):
        def text(lines: int) -> str:
            return "\n".join(["·" * 200 + "# x", "\u200b" * 200 + "> y", "·" * 400] * lines)

        small, large = text(400), text(1600)

        assert fastest(lambda: escape_block_marks(large)) < 8 * fastest(lambda: escape_block_marks(small))


def _docx(build) -> str:
    document = Document()
    document.add_paragraph("본문")
    build(document)
    stream = BytesIO()
    document.save(stream)
    return convert(stream.getvalue(), "a.docx").text


def _pptx(*lines: str) -> str:
    presentation = Presentation()
    frame = presentation.slides.add_slide(presentation.slide_layouts[6]).shapes.add_textbox(
        Inches(1), Inches(1), Inches(6), Inches(3)).text_frame
    for index, line in enumerate(lines):
        (frame.paragraphs[0] if index == 0 else frame.add_paragraph()).text = line
    stream = BytesIO()
    presentation.save(stream)
    return convert(stream.getvalue(), "a.pptx").text


def _hwpx(*texts: str) -> str:
    return convert(builder.hwpx_bytes("".join(f"<hp:p><hp:run><hp:t>{escape(text)}</hp:t></hp:run></hp:p>" for text in texts)),
                   "a.hwpx").text


class TestInADocument:
    def test_docx_the_comment_of_indented_code_after_a_line_break_is_no_heading(self):
        def build(document):
            run = document.add_paragraph().add_run("def f():")
            run.add_break()
            run.add_text("    # comment")
            run.add_break()
            run.add_text("    return 1")

        assert _docx(build) == "본문\n\ndef f():\n\\# comment\nreturn 1"

    def test_docx_a_mark_behind_a_private_use_character_is_text(self):
        text = _docx(lambda document: [document.add_paragraph("\uec03# of samples"), document.add_paragraph("\u200b> 21 days")])

        assert text == "본문\n\n\\# of samples\n\n\\> 21 days"

    def test_docx_a_mark_after_a_line_separator_is_text(self):
        assert _docx(lambda document: document.add_paragraph("명령\u2028# 주석")) == "본문\n\n명령\n\\# 주석"

    def test_pptx_a_mark_behind_a_private_use_character_is_text(self):
        assert _pptx("본문", "\ue000 > 3 months", "\u200b# of users").endswith("본문\n\\> 3 months\n\\# of users")

    def test_hwpx_a_mark_behind_what_cleaning_removes_is_text(self):
        assert _hwpx("본문", "\u200b# 주석", "\ufeff > 결과", "명령\u2028# 주석") == "본문\n\n\\# 주석\n\n\\> 결과\n\n명령\n\\# 주석"

    def test_what_stays_in_front_of_a_mark_keeps_it_as_typed(self):
        assert _docx(lambda document: document.add_paragraph("\u25cb # 항목")) == "본문\n\n\u25cb # 항목"
        assert _hwpx("본문", "\u203b > 참고") == "본문\n\n\u203b > 참고"
