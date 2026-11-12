"""List levels in HTML saved by Word, headings and code blocks inside table cells, format=flowed plain-text mail, `>From` lines of
an mbox archive."""

import pytest

from opendockit import convert


def _word_item(level: int, marker: str, text: str, font: str = "Symbol") -> str:
    return (f"<p class=MsoListParagraph style='margin-left:{level * 20}pt;text-indent:-20pt;mso-list:l0 level{level} lfo1'>"
            f"<![if !supportLists]><span style='font-family:{font}'><span style='mso-list:Ignore'>{marker}"
            "<span style='font:7.0pt \"Times New Roman\"'>&nbsp;&nbsp;&nbsp; </span></span></span><![endif]>"
            f"{text}<o:p></o:p></p>")


def _word_html(*paragraphs: str) -> bytes:
    body = "\n".join(paragraphs)
    return ("<html xmlns:o=\"urn:schemas-microsoft-com:office:office\"><head><meta charset=utf-8></head><body lang=KO>"
            f"<div class=WordSection1>\n{body}\n</div></body></html>").encode()


class TestWordSavedHtmlListLevels:
    """HTML that Word 'saved as a web page' writes list items as paragraphs with an `mso-list:lN levelK` style and gives the
    indentation only in CSS. Sub-items were read as paragraphs at the same level as their parent item, so there was no telling
    which item they detail."""

    def test_list_levels_from_the_mso_list_style_are_nested(self):
        data = _word_html(_word_item(1, "·", "첫 항목"), _word_item(2, "§", "하위 항목", "Wingdings"),
                          _word_item(1, "·", "둘째 항목"), "<p class=MsoNormal>끝 문단<o:p></o:p></p>")

        result = convert(data, "a.htm")

        assert result.text == "- 첫 항목\n  - 하위 항목\n- 둘째 항목\n\n끝 문단"

    def test_numbered_marker_stays_as_text_of_the_item(self):
        data = _word_html(_word_item(1, "1.", "개요", "\"맑은 고딕\""), _word_item(2, "가.", "배경", "\"맑은 고딕\""),
                          _word_item(2, "o", "세부", "\"Courier New\""))

        result = convert(data, "a.htm")

        assert result.text == "- 1. 개요\n  - 가. 배경\n  - 세부"

    def test_skipped_level_nests_one_step_deeper(self):
        data = _word_html(_word_item(1, "·", "상위"), _word_item(3, "·", "깊은 항목"), _word_item(1, "·", "다음"))

        result = convert(data, "a.htm")

        assert result.text == "- 상위\n  - 깊은 항목\n- 다음"


class TestHeadingAndCodeInsideATableCell:
    """A heading (`<h2>`) and a code block (`<pre>`) inside a cell of a data table stayed in the cell with their Markdown marks
    (`| ## 칸제목 |`, ```` ```<br>x=1 ````). Cell text keeps only the text and line breaks, without heading marks or fences."""

    def test_heading_and_code_block_in_cells_become_plain_lines(self):
        html = ("<table><tr><th>A</th><th>B</th></tr>"
                "<tr><td><h2>칸제목</h2>설명</td><td><pre>x = 1\ny = 2</pre></td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text == "| A | B |\n| --- | --- |\n| 칸제목<br>설명 | x = 1<br>y = 2 |"

    def test_heading_outside_a_table_keeps_its_mark(self):
        html = "<h2>절 제목</h2><pre>code</pre>"

        result = convert(html.encode(), "a.html")

        assert result.text == "## 절 제목\n\n```\ncode\n```"


def _flowed_mail(body: str, params: str = "format=flowed") -> bytes:
    head = f"From: a@example.com\r\nSubject: f\r\nMIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8; {params}\r\n\r\n"
    return (head + body.replace("\n", "\r\n")).encode()


class TestFormatFlowedPlainMail:
    """A `format=flowed` (RFC 3676) plain-text mail marks 'continues on the next line' with a space at the end of the line.
    Paragraphs written as running text were broken at every line."""

    @pytest.mark.parametrize("name", ["a.eml", "a.txt"])
    def test_soft_broken_lines_are_joined_and_quote_depth_and_signature_are_kept(self, name):
        data = _flowed_mail("이것은 아주 긴 문장으로 \n이어지는 줄입니다.\n> 인용한 \n> 문장\n>> 깊은 \n>> 인용\n끝 줄\n-- \n서명\n")

        result = convert(data, name)

        assert result.text.endswith("이것은 아주 긴 문장으로 이어지는 줄입니다.\n> 인용한 문장\n>> 깊은 인용\n끝 줄\n--\n서명")

    def test_delsp_removes_the_space_that_marked_the_break(self):
        data = _flowed_mail("super \ncalifragilistic\n", "format=flowed; delsp=yes")

        result = convert(data, "a.eml")

        assert result.text.endswith("supercalifragilistic")

    def test_line_ending_in_a_space_in_a_fixed_mail_is_not_joined(self):
        data = _flowed_mail("첫 줄 \n둘째 줄\n", "format=fixed")

        result = convert(data, "a.eml")

        assert result.text.endswith("첫 줄\n둘째 줄")


_MAILBOX = (b"From z@z Mon Oct  5 10:00:00 2026\nFrom: z@z\nSubject: s1\nMIME-Version: 1.0\n"
            b"Content-Type: text/plain; charset=utf-8\n\nfirst\n>From here\n>>From there\n> From quoted\n\n"
            b"From z@z Mon Oct  5 10:01:00 2026\nFrom: z@z\nSubject: s2\nMIME-Version: 1.0\n"
            b"Content-Type: text/plain; charset=utf-8\n\nsecond\n")


class TestMailboxFromLineEscapes:
    """An mbox archive rewrites a line-start `From ` in a body as `>From ` (mboxrd adds one more `>` to `>From` lines too). In the
    body of a mail read from an archive, `>From here` stayed as is."""

    @pytest.mark.parametrize("name", ["a.mbox", "a.txt"])
    def test_one_quote_mark_is_removed_from_escaped_from_lines(self, name):
        data = _MAILBOX

        result = convert(data, name)

        assert "first\nFrom here\n>From there\n> From quoted\n\n## Message 2" in result.text

    def test_single_mail_after_an_mbox_line_is_unescaped_too(self):
        data = b"From z@z Mon Oct  5 10:00:00 2026\nFrom: z@z\nSubject: s1\nMIME-Version: 1.0\nContent-Type: text/plain\n\na\n>From b\n"

        result = convert(data, "a.eml")

        assert result.text.endswith("a\nFrom b")

    def test_eml_without_an_mbox_line_keeps_its_from_lines(self):
        data = b"From: z@z\nSubject: s1\nMIME-Version: 1.0\nContent-Type: text/plain\n\na\n>From b\n"

        result = convert(data, "a.eml")

        assert result.text.endswith("a\n>From b")
