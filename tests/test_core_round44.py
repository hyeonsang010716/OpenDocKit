"""Headings, paragraphs and code blocks inside a one-column HTML box table, terminal escapes other than CSI in TXT, data: URI
pictures inside MD inline code, the warning for unnamed mail attachments."""

import base64
import io
import time

from PIL import Image
import pytest

from opendockit import convert


def _mail_with_attachments(filenames: list[str | None]) -> bytes:
    parts = b"".join(
        b"--b\r\nContent-Type: application/pdf\r\nContent-Disposition: attachment"
        + (b'; filename="%s"' % name.encode() if name else b"") + b"\r\n\r\nxx\r\n"
        for name in filenames)
    return (b"From: a@x.com\r\nTo: b@y.com\r\nSubject: s\r\nMIME-Version: 1.0\r\n"
            b"Content-Type: multipart/mixed; boundary=b\r\n\r\n--b\r\nContent-Type: text/plain\r\n\r\nbody\r\n"
            + parts + b"--b--\r\n")


def _png_base64() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (100, 100), (5, 5, 5)).save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode()


class TestOneColumnBoxTable:
    """A one-column table is a box that is unwrapped into paragraphs, but it was rendered as cell text (`_in_cell`), so the `##` of
    a heading in the cell and the blank lines between paragraphs vanished, and a code block came out in the body without fences,
    with its `&nbsp;` indentation. The round-43 guard 'a table with a th header row is a data table' also caught notice boxes
    with a single th cell."""

    def test_notice_box_with_a_header_cell_keeps_its_heading_and_paragraphs(self):
        html = ("<table><tr><th>공지사항</th></tr><tr><td><h2>시스템 점검 안내</h2><p>점검 일시는 10월 5일입니다.</p>"
                "<p>점검 중에는 접속할 수 없습니다.</p><ul><li>대상: 전체</li></ul></td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text == ("공지사항\n\n## 시스템 점검 안내\n\n점검 일시는 10월 5일입니다.\n\n점검 중에는 접속할 수 없습니다.\n\n"
                               "- 대상: 전체")

    def test_code_in_a_one_column_box_stays_a_code_block(self):
        html = "<table><tr><td><p>예제 코드:</p><pre>def f():\n    if x:\n        return 1</pre></td></tr></table>"

        result = convert(html.encode(), "a.html")

        assert result.text == "예제 코드:\n\n```\ndef f():\n    if x:\n        return 1\n```"

    def test_code_in_a_two_row_box_stays_a_code_block(self):
        html = "<table><tr><td>설명</td></tr><tr><td><pre>for i in range(3):\n    print(i)</pre></td></tr></table>"

        result = convert(html.encode(), "a.html")

        assert result.text == "설명\n\n```\nfor i in range(3):\n    print(i)\n```"

    def test_one_column_list_of_values_is_still_one_paragraph_per_row(self):
        html = "<table><tr><th>이름</th></tr><tr><td>홍길동</td></tr><tr><td>김철수<br>(대리)</td></tr></table>"

        result = convert(html.encode(), "a.html")

        assert result.text == "이름\n\n홍길동\n\n김철수\n(대리)"

    def test_code_in_a_cell_of_a_two_column_table_is_still_cell_text(self):
        html = "<table><tr><th>설명</th><th>코드</th></tr><tr><td>함수</td><td><pre>def f():\n    return 1</pre></td></tr></table>"

        result = convert(html.encode(), "a.html")

        assert result.text.endswith(f"| 함수 | def f():<br>{'&nbsp;' * 4}return 1 |")


class TestTerminalEscapesOtherThanCsi:
    """Of the terminal controls in TXT only CSI (`ESC[…m`) was removed, so for the `ESC(B` of `tput sgr0`, a window title OSC
    (`ESC]0;title BEL`) and `ESC c`, only the ESC was removed and `(B`, `]0;title` and `c` stayed in the text."""

    def test_osc_and_two_byte_escapes_are_removed_from_text_files(self):
        data = b"\x1b]0;my title\x07hello \x1b[31mred\x1b(B\x1b[m\nline2\x1bc done \x1b]2;t\x1b\\ok \x1b[38:2:1:2:3mx\x1b7y\x1b8"

        result = convert(data, "log.txt")

        assert result.text == "hello red\nline2 done ok xy"

    def test_text_after_an_unterminated_title_sequence_is_kept(self):
        data = b"\x1b]0;title without end\nnext line"

        result = convert(data, "log.txt")

        assert result.text.endswith("next line")

    def test_markdown_keeps_its_text_as_is(self):
        data = b"# Log\n\n\x1b]0;t\x07x"

        result = convert(data, "a.md")

        assert result.text == "# Log\n\n]0;tx"


class TestDataUriPictureInInlineCode:
    """Data: URI picture syntax inside MD inline code (`` `…` ``) is example text, but it was extracted as a picture, so the code
    span was left empty as `` ` ` `` and a picture description was attached. It also differed from the accepted gap in the module
    docstring ('a data: URI inside inline code stays as text')."""

    def test_picture_syntax_inside_inline_code_stays_text(self):
        source = f"![x](data:image/png;base64,{_png_base64()})"
        data = f"인라인 코드 `{source}` 끝\n".encode()

        result = convert(data, "a.md")

        assert result.images == []
        assert source in result.text

    def test_picture_inside_a_double_backtick_span_stays_text(self):
        source = f"![x](data:image/png;base64,{_png_base64()})"
        data = f"코드 `` a ` {source} `` 끝\n".encode()

        result = convert(data, "a.md")

        assert result.images == []

    def test_picture_after_an_inline_code_span_is_still_a_picture(self):
        data = f"코드 `x` 다음 그림 ![y](data:image/png;base64,{_png_base64()})\n".encode()

        result = convert(data, "a.md")

        assert len(result.images) == 1
        assert "`x`" in result.text

    def test_unmatched_backtick_does_not_hide_a_later_picture(self):
        data = f"따옴표 ` 하나 ![y](data:image/png;base64,{_png_base64()})\n".encode()

        result = convert(data, "a.md")

        assert len(result.images) == 1

    @pytest.mark.timing
    def test_many_unmatched_backticks_take_linear_time(self):
        data = ("` ``` " * 30000 + f"![y](data:image/png;base64,{_png_base64()})").encode()

        started = time.perf_counter()
        convert(data, "a.md")

        assert time.perf_counter() - started < 3


class TestUnnamedMailAttachments:
    """Unnamed attachments were repeated 10 times in the warning as 'unnamed, unnamed, …'."""

    def test_unnamed_attachments_are_counted_instead_of_listed(self):
        data = _mail_with_attachments([None] * 12)

        result = convert(data, "a.eml")

        assert "12 mail attachment(s) (12 unnamed attachment(s)) were not read." in " ".join(result.warnings)

    def test_named_and_unnamed_attachments_together(self):
        data = _mail_with_attachments(["a.pdf", None, "b.pdf", None])

        result = convert(data, "a.eml")

        assert "4 mail attachment(s) (a.pdf, b.pdf, 2 unnamed attachment(s)) were not read." in " ".join(result.warnings)

    def test_unnamed_ones_do_not_take_the_places_of_named_ones(self):
        data = _mail_with_attachments([None] * 5 + [f"f{i}.pdf" for i in range(12)])

        result = convert(data, "a.eml")

        names = ", ".join(f"f{i}.pdf" for i in range(10))
        assert f"17 mail attachment(s) ({names} and 2 more, 5 unnamed attachment(s)) were not read." in " ".join(result.warnings)
