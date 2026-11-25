"""A `#` or a `>` typed at the start of a line of an HWP or HWPX paragraph is text (`markdown.escape_block_marks`).

The text of a document is text, never structure. A manual that shows what is typed at a prompt (`# arch -k`) came out with
273 headings and lost its own among them, and the `>>` in front of the summary lines of a press release made each a quote
in a quote. The PDF reader escaped these marks already (`test_pdf_headings.py::TestBodyTextMarks`), so the same release
read `\\>> …` from its PDF and `>> …` from its HWPX.
"""

from html import escape

import pytest

from opendockit import convert
from opendockit.extractors import _hangul
from opendockit.extractors.hwp import TAG_PARA_HEADER, TAG_PARA_TEXT
from opendockit.markdown import escape_block_marks
from tests import builder
from tests.test_hwp import _fake_hwp


def _hwpx(*texts: str, style: str = "0") -> bytes:
    return builder.hwpx_bytes("".join(f'<hp:p styleIDRef="{style}"><hp:run><hp:t>{escape(text)}</hp:t></hp:run></hp:p>'
                                      for text in texts))


def _hwp(*texts: str, style: int = 0) -> bytes:
    records = b"".join(builder.hwp_record(TAG_PARA_HEADER, 0, builder.hwp_para_header(style_id=style))
                       + builder.hwp_record(TAG_PARA_TEXT, 1, builder.hwp_text(text)) for text in texts)
    return _fake_hwp(None, sections=[records])


def _both(*texts: str) -> tuple[str, str]:
    return convert(_hwpx(*texts), "a.hwpx").text, convert(_hwp(*texts), "a.hwp").text


class TestEscapeBlockMarks:
    @pytest.mark.parametrize(("text", "expected"), [
        ("# arch -k", "\\# arch -k"),
        ("###### six", "\\###### six"),
        ("#", "\\#"),
        ("#\ttab", "\\#\ttab"),
        ("> quoted", "\\> quoted"),
        (">> twice", "\\>> twice"),
        (">", "\\>"),
        ("first\n# second\n> third", "first\n\\# second\n\\> third"),
    ])
    def test_a_mark_at_the_start_of_a_line_is_escaped(self, text, expected):
        assert escape_block_marks(text) == expected

    @pytest.mark.parametrize("text", [
        "", "plain", "#hashtag", "####### seven", "a # b", "a > b", "1 > 0\nx #1", "<tag>", "- item", "1. item", "$x$",
    ])
    def test_everything_else_is_as_it_was(self, text):
        assert escape_block_marks(text) == text


class TestTypedMarksInTheBody:
    def test_a_heading_mark_typed_at_the_start_of_a_paragraph_is_text(self):
        for text in _both("본문", "# arch -k", "## 주석", "끝"):
            assert text == "본문\n\n\\# arch -k\n\n\\## 주석\n\n끝"

    def test_a_quote_mark_typed_at_the_start_of_a_paragraph_is_text(self):
        for text in _both(">> 세계 최고의 전자정부", "> CREATE TABLESPACE data_ts", ">"):
            assert text == "\\>> 세계 최고의 전자정부\n\n\\> CREATE TABLESPACE data_ts\n\n\\>"

    def test_what_is_no_mark_stays_as_typed(self):
        for text in _both("#해시태그", "####### 일곱", "가 # 나 > 다", "C#"):
            assert text == "#해시태그\n\n####### 일곱\n\n가 # 나 > 다\n\nC#"

    def test_a_typed_list_stays_a_list(self):
        """A dash or a number typed in front of an item is what Markdown reads it as."""
        for text in _both("본문", "- 첫째", "1. 하나"):
            assert "\\" not in text
            assert "- 첫째" in text and "1. 하나" in text

    def test_a_mark_after_a_line_break_inside_a_paragraph_is_text(self):
        body = "<hp:p><hp:run><hp:t>명령<hp:lineBreak/># arch -k<hp:lineBreak/>&gt; 결과</hp:t></hp:run></hp:p>"

        assert convert(builder.hwpx_bytes(body), "a.hwpx").text == "명령\n\\# arch -k\n\\> 결과"

    def test_the_text_of_a_heading_is_not_escaped(self):
        """After the mark of the heading the reader writes, a `#` starts no line."""
        assert convert(_hwpx("# 번호", style="2"), "a.hwpx").text == "# # 번호"
        assert convert(_hwp("# 번호", style=2), "a.hwp").text == "# # 번호"

    def test_a_mark_in_a_cell_of_a_table_is_as_typed(self):
        """A row of a table is one line that starts with `|`: nothing in a cell starts a line."""
        table = ('<hp:p><hp:run><hp:tbl rowCnt="2" colCnt="2"><hp:tr>' + builder._hwpx_cell(0, 0, "# 건수") + builder._hwpx_cell(0, 1, "&gt; 50")
                 + "</hp:tr><hp:tr>" + builder._hwpx_cell(1, 0, "3") + builder._hwpx_cell(1, 1, "예") + "</hp:tr></hp:tbl></hp:run></hp:p>")

        assert convert(builder.hwpx_bytes(table), "a.hwpx").text == "| # 건수 | > 50 |\n| --- | --- |\n| 3 | 예 |"


class TestOutlineStyleLevel:
    def test_the_level_of_a_style_name(self):
        for name, level in (("개요 1", 1), ("개요 7", 7), (" 개요 2 ", 2), ("Outline 3", 3), ("개요3", 3), ("바탕글", 0), ("개요 10", 0), ("", 0)):
            assert _hangul._outline_style_level(name) == level
            assert _hangul._outline_style_level(name) == level   # remembered

    def test_names_are_remembered_up_to_a_bound(self, monkeypatch):
        """A server that reads documents for months meets style names without end: a long name or one over the bound is
        answered and not kept."""
        monkeypatch.setattr(_hangul, "_OUTLINE_STYLE_LEVELS", {})
        monkeypatch.setattr(_hangul, "_OUTLINE_STYLE_LEVELS_MAX", 2)

        assert [_hangul._outline_style_level(name) for name in ("개요 1", "가" * 65, "본문", "개요 4", "개요 5")] == [1, 0, 0, 4, 5]
        assert _hangul._OUTLINE_STYLE_LEVELS == {"개요 1": 1, "본문": 0}
