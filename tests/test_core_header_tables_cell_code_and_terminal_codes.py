"""A small HTML data table with a header row, indentation of code inside a table cell, the number of names in the mail attachment
warning, deep Word list levels, terminal color codes in TXT."""

import time

import pytest

from opendockit import convert


class TestSmallTableWithAHeaderRowIsNotLayout:
    """When one cell of a data table of 3 rows or fewer with a header (th) row held a heading (`<h4>`) and a long description, the
    table was unwrapped as a layout table: '버전 / 변경 사항 / 1.2 / #### 새 기능 …' scattered into paragraphs and the pairing of
    each change with its version was lost."""

    def test_heading_in_a_cell_under_a_header_row_keeps_the_table(self):
        html = ("<table><tr><th>버전</th><th>변경 사항</th></tr>"
                f"<tr><td>1.2</td><td><h4>새 기능</h4><p>{'상세 설명 문장입니다. ' * 20}</p></td></tr>"
                "<tr><td>1.1</td><td>사소한 수정</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text.startswith("| 버전 | 변경 사항 |\n| --- | --- |\n| 1.2 | 새 기능<br>상세 설명 문장입니다.")
        assert result.text.endswith("| 1.1 | 사소한 수정 |")
        assert "####" not in result.text

    def test_data_table_inside_a_layout_cell_keeps_its_rows(self):
        inner = "<table border=1><tr><th>x</th><th>y</th></tr><tr><td><h4>칸제목</h4>1</td><td>2</td></tr></table>"
        html = (f"<table width=100%><tr><td width=200><h3>메뉴</h3><ul><li>a</li></ul></td>"
                f"<td><h1>본문 제목</h1><p>본문 내용이 깁니다 아주 깁니다.</p>{inner}</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert "# 본문 제목" in result.text
        assert result.text.endswith("| x | y |\n| --- | --- |\n| 칸제목<br>1 | 2 |")

    def test_layout_without_a_header_row_still_spreads_into_paragraphs(self):
        html = ("<table><tr><td>메뉴</td><td><h2>공지</h2><p>본문 문단이 길게 이어집니다. 본문 문단이 길게 이어집니다.</p></td></tr>"
                "</table>")

        result = convert(html.encode(), "a.html")

        assert result.text == "메뉴\n\n## 공지\n\n본문 문단이 길게 이어집니다. 본문 문단이 길게 이어집니다."


class TestIndentedCodeInsideATableCell:
    """A `<pre>` inside a table cell keeps only its line breaks as cell text (`_in_cell`), but the leading indentation of the lines
    was erased by whitespace cleanup, so the block structure of Python code was lost (`def f():<br>if x:<br>return 1`)."""

    def test_leading_indentation_of_code_in_a_cell_is_kept(self):
        html = ("<table><tr><th>설명</th><th>코드</th></tr>"
                "<tr><td>함수</td><td><pre>def f():\n    if x:\n\treturn 1</pre></td></tr></table>")

        result = convert(html.encode(), "a.html")

        nbsp4 = "&nbsp;" * 4
        assert result.text.endswith(f"| 함수 | def f():<br>{nbsp4}if x:<br>{nbsp4}return 1 |")

    def test_code_block_outside_a_table_is_unchanged(self):
        html = "<pre>def f():\n    return 1</pre>"

        result = convert(html.encode(), "a.html")

        assert result.text == "```\ndef f():\n    return 1\n```"


class TestManyMailAttachmentNames:
    """Attachment names were all written on the one warning line with no upper limit, so the warning of a mail with 20,000
    attachments was 229KB. Up to 10 are written, like the PDF attachment warning."""

    def test_warning_names_ten_attachments_and_counts_the_rest(self):
        attachments = b"".join(b"--b\r\nContent-Type: application/octet-stream\r\n"
                               b"Content-Disposition: attachment; filename=\"f%d.bin\"\r\n\r\nxx\r\n" % i for i in range(12))
        data = (b"From: a@x.com\r\nTo: b@y.com\r\nSubject: s\r\nMIME-Version: 1.0\r\n"
                b"Content-Type: multipart/mixed; boundary=b\r\n\r\n--b\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
                + attachments + b"--b--\r\n")

        result = convert(data, "a.eml")

        names = ", ".join(f"f{i}.bin" for i in range(10))
        assert f"12 mail attachment(s) ({names} and 2 more) were not read." in " ".join(result.warnings)


class TestDeepWordListLevels:
    """When the Word list level (`mso-list:l0 levelK`) rises from 1 to tens of thousands, the nested `<ul>` becomes tens of
    thousands of levels deep and each item added walked down to the last descendant, taking quadratic time (1.75MB, 39 seconds).
    Word's list levels go up to 9."""

    def test_levels_beyond_nine_stay_at_the_ninth_level(self):
        paragraphs = "".join(f'<p style="mso-list:l0 level{level} lfo1">항목{level}</p>' for level in (1, 9, 10, 50))

        result = convert(f"<html><body>{paragraphs}</body></html>".encode(), "a.htm")

        assert result.text == "- 항목1\n  - 항목9\n  - 항목10\n  - 항목50"

    @pytest.mark.timing
    def test_ever_deeper_levels_take_linear_time(self):
        paragraphs = "".join(f'<p style="mso-list:l0 level{level + 1} lfo1">a</p>' for level in range(20_000))
        started = time.perf_counter()

        result = convert(f"<html><body>{paragraphs}</body></html>".encode(), "a.htm")

        assert time.perf_counter() - started < 5.0
        assert result.text.count("a") == 20_000


class TestTerminalColorCodesInText:
    """In a TXT that saved a terminal log, only the ESC of the color codes (`ESC[31m`) was removed and `[31m`/`[0m` stayed in the
    text."""

    def test_terminal_control_sequences_are_removed_from_text_files(self):
        data = b"plain \x1b[31mERROR\x1b[0m done\n\x1b[1;32mOK\x1b[m \x1b[2K\x1b[?25lend"

        result = convert(data, "log.txt")

        assert result.text == "plain ERROR done\nOK end"

    def test_markdown_keeps_its_text_as_is(self):
        data = b"# Log\n\nplain \x1b[31mERROR\x1b[0m"

        result = convert(data, "a.md")

        assert result.text == "# Log\n\nplain [31mERROR[0m"
