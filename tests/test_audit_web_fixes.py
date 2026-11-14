"""Fixes from the audit of the web, mail and text extractors: forwarded mails nested in odd shapes, mailboxes under a text name,
MHTML-shaped mail with an HTML attachment, `visibility:visible` under a hidden element, the page title, marks inside code, alt
text that could make a link, list items (empty ones, code blocks), meeting invitations, encoding guesses and `rowspan="0"`.

Every document is built in memory.
"""

import base64
import io
import time

from PIL import Image
import pytest

from opendockit import CorruptedDocumentError, DocumentFormat, convert
from opendockit.extractors._encoding import decode_text
from opendockit.extractors._mail import _mime_nesting_exceeds
from opendockit.markdown import clean_text
from tests.timing import allowed


_HEAD = "MIME-Version: 1.0\nFrom: a@example.com\nTo: b@example.com\nSubject: 안내\n"


def _mbox_mail(hour: int, body: str) -> str:
    return f"From a@example.com Mon Sep  1 {hour:02d}:00:00 2025\n{_HEAD}{body}\n"


class TestTopLevelForwardedMail:
    """A mail whose top-level type is message/rfc822 (a forwarded mail saved on its own) is mail."""

    def test_mail_inside_is_read(self):
        data = (_HEAD + "Content-Type: message/rfc822\n\nSubject: 원래 메일\nContent-Type: text/plain; charset=utf-8\n\n"
                "원래 본문\n").encode()

        result = convert(data, "a.eml")

        assert "Content-Type" not in result.text
        assert result.text == "Subject: 안내\nFrom: a@example.com\nTo: b@example.com\n\n## Forwarded message\n\nSubject: 원래 메일\n\n원래 본문"


class TestForwardedMailThatIsItselfAForward:
    """multipart > message/rfc822 (whose own type is message/rfc822) > the mail: the innermost body vanished."""

    @pytest.mark.parametrize("name", ["a.eml", "a.txt"])
    def test_innermost_body_is_read(self, name):
        data = (_HEAD + "Content-Type: multipart/mixed; boundary=o\n\n--o\nContent-Type: text/plain\n\n바깥 본문\n--o\n"
                "Content-Type: message/rfc822\n\nSubject: 전달 1\nContent-Type: message/rfc822\n\n"
                "Subject: 전달 2\nContent-Type: text/plain; charset=utf-8\n\n안쪽 본문\n--o--\n").encode()

        result = convert(data, name)

        assert result.text.endswith("## Forwarded message\n\nSubject: 전달 1\n\n## Forwarded message\n\nSubject: 전달 2\n\n안쪽 본문")
        assert result.text.count("안쪽 본문") == 1

    def test_html_inside_is_not_read_twice(self):
        data = (_HEAD + "Content-Type: multipart/mixed; boundary=o\n\n--o\nContent-Type: text/plain\n\n바깥 본문\n--o\n"
                "Content-Type: message/rfc822\n\nSubject: 전달 1\nContent-Type: message/rfc822\n\n"
                "Subject: 전달 2\nContent-Type: text/html; charset=utf-8\n\n<p>안쪽 HTML</p>\n--o--\n").encode()

        result = convert(data, "a.eml")

        assert result.text.count("안쪽 HTML") == 1


class TestDeeplyNestedForwardedMails:
    """Forwarded mails nested in each other are read by recursion in the parser; past a bound the mail is rejected with its own
    sentence instead of a recursion error."""

    def test_deep_chain_is_rejected_with_a_fixed_sentence(self):
        data = (_HEAD + "Content-Type: multipart/mixed; boundary=o\n\n--o\n" + "Content-Type: message/rfc822\n\n" * 3000
                + "Content-Type: text/plain\n\n본문\n--o--\n").encode()

        with pytest.raises(CorruptedDocumentError, match="forwarded mails more than 100 levels deep"):
            convert(data, "a.eml")

    def test_many_forwarded_mails_side_by_side_are_read(self):
        parts = "".join(f"--o\nContent-Type: message/rfc822\n\nSubject: 전달 {n}\nContent-Type: text/plain\n\n본문 {n}\n"
                        for n in range(150))
        data = (_HEAD + "Content-Type: multipart/mixed; boundary=o\n\n" + parts + "--o--\n").encode()

        result = convert(data, "a.eml")

        assert "본문 0" in result.text
        assert any("attachment" in warning for warning in result.warnings)  # beyond 20 forwarded mails, as before

    @pytest.mark.parametrize("name", ["a.eml", "a.txt"])
    def test_forward_lines_in_a_body_are_text(self, name):
        body = "Content-Type: message/rfc822\n" * 120 + "\n" + "Content-Type: message/rfc822\n\n" * 120
        data = (_HEAD + f"Content-Type: text/plain; charset=utf-8\n\n{body}").encode()

        assert convert(data, name).text.count("message/rfc822") == 240

    def test_forward_lines_in_a_text_attachment_are_text(self):
        data = (_HEAD + "Content-Type: multipart/mixed; boundary=o\n\n--o\nContent-Type: text/plain\n\n본문\n--o\n"
                "Content-Type: text/plain; name=log.txt\nContent-Disposition: attachment; filename=log.txt\n\n"
                + "Content-Type: message/rfc822\n\n" * 200 + "--o--\n").encode()

        result = convert(data, "a.eml")

        assert result.text.endswith("본문")
        assert any("log.txt" in warning for warning in result.warnings)

    @pytest.mark.parametrize("separator", ["\n", "\r\n"])
    def test_chain_of_blocks_opened_by_mbox_lines(self, separator):
        data = (_HEAD + "Content-Type: multipart/mixed; boundary=o\n\n--o\n"
                + "Content-Type: message/rfc822\n\nFrom c@example.com Mon Sep  1 10:00:00 2025\nFrom: c@example.com\n" * 3000
                + "Content-Type: text/plain\n\n본문\n--o--\n").replace("\n", separator).encode()

        with pytest.raises(CorruptedDocumentError, match="forwarded mails more than 100 levels deep"):
            convert(data, "a.eml")

    def test_one_forward_header_block_counts_once(self):
        data = (_HEAD + "Content-Type: multipart/mixed; boundary=o\n\n--o\n" + "Content-Type: message/rfc822\n" * 300
                + "\nSubject: 전달\nContent-Type: text/plain\n\n본문\n--o--\n").encode()

        assert convert(data, "a.eml").text.endswith("본문")

    @pytest.mark.timing
    def test_many_forward_lines_in_a_body_are_scanned_in_linear_time(self):
        """Each line walks up only to the line examined before it (500,000 lines, about 0.3 s)."""
        data = (_HEAD + "Content-Type: text/plain\n\n" + "Content-Type: message/rfc822\n" * 500_000).encode()
        start = time.perf_counter()
        exceeded = _mime_nesting_exceeds(data, 32)
        seconds = time.perf_counter() - start

        assert exceeded is None
        assert seconds < allowed(3)

    def test_moderate_chain_is_read(self):
        data = (_HEAD + "Content-Type: multipart/mixed; boundary=o\n\n--o\n" + "Content-Type: message/rfc822\n\n" * 50
                + "Content-Type: text/plain\n\n본문\n--o--\n").encode()

        assert convert(data, "a.eml").text.startswith("Subject: 안내")


_QUOTED_PART_HEADERS = "".join(f"--=_Part_{n}\nContent-Type: message/rfc822\nContent-Disposition: inline\n\n(전달 {n} 생략)\n\n"
                               for n in range(150))


class TestForwardLevelsFollowTheMultipartStructure:
    """A forwarded mail is a level only where the parser descends into one: a part of the declared multipart structure. The same
    lines in a body, in a text attachment or after a `--` line that is no delimiter of that structure are text, and forwarded
    mails side by side stay side by side whichever way the boundary is declared."""

    @pytest.mark.parametrize("name", ["a.eml", "notes.txt"])
    def test_part_headers_quoted_in_a_plain_body_are_text(self, name):
        data = (_HEAD + "Content-Type: text/plain; charset=utf-8\n\n서버 로그:\n\n" + _QUOTED_PART_HEADERS).encode()

        text = convert(data, name).text

        assert text.count("Content-Type: message/rfc822") == 150
        assert "(전달 149 생략)" in text

    def test_part_headers_quoted_in_a_text_attachment_are_text(self):
        data = (_HEAD + "Content-Type: multipart/mixed; boundary=o\n\n--o\nContent-Type: text/plain\n\n본문\n--o\n"
                "Content-Type: text/plain; name=mime.log\nContent-Disposition: attachment; filename=mime.log\n\n"
                + _QUOTED_PART_HEADERS + "--o--\n").encode()

        result = convert(data, "a.eml")

        assert result.text.endswith("본문")
        assert any("mime.log" in warning for warning in result.warnings)

    @pytest.mark.parametrize("line", ["-- ", "--- 항목 {n}"])
    def test_header_like_lines_under_a_dash_line_of_a_body_are_text(self, line):
        body = "".join(line.format(n=n) + f"\nContent-Type: message/rfc822\n\n메모 {n}\n\n" for n in range(150))
        data = (_HEAD + "Content-Type: text/plain; charset=utf-8\n\n" + body).encode()

        assert "메모 149" in convert(data, "a.eml").text

    @pytest.mark.parametrize("declaration", ["boundary*=us-ascii''o", 'boundary*0="o"', "boundary*0=o"])
    def test_forwarded_mails_side_by_side_under_an_extended_boundary_are_read(self, declaration):
        parts = "".join(f"--o\nContent-Type: message/rfc822\n\nSubject: 전달 {n}\nContent-Type: text/plain\n\n본문 {n}\n"
                        for n in range(150))
        data = (_HEAD + f"Content-Type: multipart/mixed; {declaration}\n\n" + parts + "--o--\n").encode()

        result = convert(data, "a.eml")

        assert "본문 0" in result.text
        assert any("attachment" in warning for warning in result.warnings)  # beyond 20 forwarded mails, as before

    def test_deep_chain_of_another_message_type_is_rejected_with_the_same_sentence(self):
        # the parser reads the mail inside any message/* part by recursion, not only message/rfc822
        data = (_HEAD + "Content-Type: multipart/mixed; boundary=o\n\n--o\n" + "Content-Type: message/global\n\n" * 3000
                + "Content-Type: text/plain\n\n본문\n--o--\n").encode()

        with pytest.raises(CorruptedDocumentError, match="forwarded mails more than 100 levels deep"):
            convert(data, "a.eml")


class TestMultipartWithAnEmptyBoundary:
    """`boundary=""` is a boundary for the parser (its delimiter line is `--`); the nesting check stopped on it with an error
    of its own and the mail was rejected as corrupted."""

    def test_mail_is_read(self):
        data = (_HEAD + 'Content-Type: multipart/mixed; boundary=""\n\n--\nContent-Type: text/plain\n\n본문\n----\n').encode()

        assert convert(data, "a.eml").text.endswith("본문")


class TestMultipartsInsideADeliveryReport:
    """The parser reads the body of a message/delivery-status part as header blocks and descends into a block that declares a
    multipart. The scan before parsing took a delivery report for a leaf, so multiparts opened one inside the other in one
    block were not counted: 900 levels followed by 200,000 lines (1MB) took 43 seconds."""

    @staticmethod
    def _report(blocks: str) -> bytes:
        return (_HEAD + "Content-Type: multipart/report; report-type=delivery-status; boundary=o\n\n"
                "--o\nContent-Type: text/plain\n\n전달하지 못했습니다\n"
                "--o\nContent-Type: message/delivery-status\n\n" + blocks + "--o--\n").encode()

    @pytest.mark.parametrize("levels", [33, 900])
    def test_multiparts_nested_in_one_block_count_towards_the_limit(self, levels):
        nested = "".join(f"Content-Type: multipart/mixed; boundary=b{level}\n--b{level}\n" for level in range(levels))
        data = self._report(nested + "Content-Type: text/plain\n" + "line\n" * 1_000)

        assert _mime_nesting_exceeds(data, 32) == "multipart"
        with pytest.raises(CorruptedDocumentError, match="nested deeper than 32 levels"):
            convert(data, "a.eml")

    def test_ordinary_delivery_report_is_read(self):
        """The fields of a report, and a few that happen to name a multipart type, are no nesting."""
        blocks = ("Reporting-MTA: dns; mail.example.com\n\n"
                  "Final-Recipient: rfc822; kim@example.com\nAction: failed\nStatus: 5.1.1\n"
                  "Diagnostic-Code: smtp; 550 multipart/mixed is not accepted here\n\n")
        data = self._report(blocks)

        assert _mime_nesting_exceeds(data, 32) is None
        text = convert(data, "a.eml").text
        assert "전달하지 못했습니다" in text
        assert "Status: 5.1.1" in text


class TestForwardScanOfHeaderLikeBodies:
    """The scan before parsing reads a header block once and skips bodies; it looked for the end of the header block again for
    every `--` line followed by a forwarded-mail line, to the end of the data when no blank line followed."""

    @pytest.mark.timing
    def test_header_like_lines_after_many_dash_blocks_are_scanned_once(self):
        """99 blocks followed by 200,000 `k: v` lines (1MB) took 12 s; about 0.01 s."""
        data = (_HEAD + "Content-Type: text/plain\n\n" + "--x:\nContent-Type: message/rfc822\n" * 99 + "k: v\n" * 200_000).encode()
        start = time.perf_counter()
        exceeded = _mime_nesting_exceeds(data, 32)
        seconds = time.perf_counter() - start

        assert exceeded is None
        assert seconds < allowed(1)

    @pytest.mark.timing
    def test_dash_blocks_without_a_blank_line_are_text_and_scanned_once(self):
        """140,000 blocks with no blank line (5MB) were rejected as forwarded mails after 9 s; about 0.01 s."""
        data = (_HEAD + "Content-Type: text/plain\n\n" + "--x:\nContent-Type: message/rfc822\n" * 140_000).encode()
        start = time.perf_counter()
        exceeded = _mime_nesting_exceeds(data, 32)
        seconds = time.perf_counter() - start

        assert exceeded is None
        assert seconds < allowed(1)

    @pytest.mark.timing
    def test_many_parts_of_one_multipart_are_scanned_in_linear_time(self):
        """Every part is closed by the next delimiter line: 100,000 forwarded mails side by side (5MB), about 0.3 s."""
        parts = "--o\nContent-Type: message/rfc822\n\nSubject: s\n\n본문\n" * 100_000
        data = (_HEAD + "Content-Type: multipart/mixed; boundary=o\n\n" + parts + "--o--\n").encode()
        start = time.perf_counter()
        exceeded = _mime_nesting_exceeds(data, 32)
        seconds = time.perf_counter() - start

        assert exceeded is None
        assert seconds < allowed(3)


class TestMailboxWithAnHtmlOnlyMailUnderATextName:
    """A mailbox named .txt holding one HTML-only mail was left raw as a whole, base64 bodies and all."""

    def test_other_mails_are_decoded(self):
        plain = ("Content-Type: text/plain; charset=utf-8\nContent-Transfer-Encoding: base64\n\n"
                 + base64.b64encode("회의 안내 본문".encode()).decode())
        html = "Content-Type: multipart/alternative; boundary=b\n\n--b\nContent-Type: text/html\n\n<p>html only</p>\n--b--"
        data = (_mbox_mail(10, plain) + "\n" + _mbox_mail(11, html)).encode()

        result = convert(data, "box.txt")

        assert result.format is DocumentFormat.TEXT
        assert result.text == ("## Message 1\n\nSubject: 안내\nFrom: a@example.com\nTo: b@example.com\n\n회의 안내 본문\n\n"
                               "## Message 2\n\nSubject: 안내\nFrom: a@example.com\nTo: b@example.com\n\n<p>html only</p>")

    def test_single_html_only_mail_stays_as_written(self):
        """Deliberate (unchanged): one HTML-only mail under a text name is text in its original form."""
        data = (_HEAD + "Content-Type: text/html\n\n<p>html only</p>\n").encode()

        assert convert(data, "a.txt").text.startswith("MIME-Version: 1.0")


class TestHtmlAttachmentBeforeTheBodyOfAnMhtmlShapedMail:
    def test_body_is_read_and_the_attachment_is_reported(self):
        data = (_HEAD + "Content-Type: multipart/related; boundary=b\n\n--b\nContent-Type: text/html\n"
                "Content-Disposition: attachment; filename=x.html\n\n<p>첨부</p>\n--b\nContent-Type: text/html\n\n"
                "<p>본문</p>\n--b--\n").encode()

        result = convert(data, "a.eml")

        assert result.text.endswith("\n\n본문")
        assert "첨부" not in result.text
        assert any("x.html" in warning for warning in result.warnings)

    def test_saved_web_page_still_reads_its_first_html_part(self):
        data = (b"MIME-Version: 1.0\nFrom: <Saved by Blink>\nContent-Type: multipart/related; boundary=b\n\n--b\n"
                b"Content-Type: text/html\n\n<p>page</p>\n--b\nContent-Type: text/html\n\n<p>frame</p>\n--b--\n")

        assert convert(data, "a.mht").text == "page"


class TestVisibilityHiddenWithAVisibleDescendant:
    def test_visible_descendant_is_kept_and_the_rest_removed(self):
        html = ('<div style="visibility:hidden">숨김<div><p style="visibility: visible">보임</p><p>숨김2</p></div></div>'
                "<p>끝</p>")

        assert convert(html.encode(), "a.html").text == "보임\n\n끝"

    def test_anti_flicker_body_is_not_hidden(self):
        html = '<html><body style="visibility:hidden"><h1>제목</h1><p>본문</p><p style="visibility:hidden">숨김</p></body></html>'

        assert convert(html.encode(), "a.html").text == "# 제목\n\n본문"

    @pytest.mark.parametrize("hiding", ['style="display:none"', "hidden", 'style="content-visibility:hidden"'])
    def test_hiding_that_cannot_be_undone_still_removes_everything(self, hiding):
        html = f'<div {hiding}><p style="visibility:visible">숨김</p></div><p>끝</p>'

        assert convert(html.encode(), "a.html").text == "끝"

    def test_display_none_between_keeps_the_visible_descendant_hidden(self):
        html = '<div style="visibility:hidden"><div style="display:none"><p style="visibility:visible">숨김</p></div></div><p>끝</p>'

        assert convert(html.encode(), "a.html").text == "끝"

    @pytest.mark.timing
    def test_many_nested_hidden_elements_take_linear_time(self):
        html = '<div style="visibility:hidden">x' * 4000 + '<p style="visibility:visible">보임</p>' + "</div>" * 4000
        start = time.perf_counter()
        text = convert(html.encode(), "a.html").text
        seconds = time.perf_counter() - start

        assert text == "보임"
        assert seconds < allowed(2)


class TestTitleOfAPageWhoseCodeHasCommentLines:
    def test_comment_line_in_code_is_not_a_heading(self):
        html = "<html><head><title>설치 안내</title></head><body><p>본문</p><pre># install\npip x</pre></body></html>"

        assert convert(html.encode(), "a.html").text.startswith("# 설치 안내\n\n본문")

    @pytest.mark.parametrize("stray", ["<p>```</p>", "<pre>a</pre><p>```</p>"])
    def test_stray_fence_line_does_not_hide_a_heading(self, stray):
        html = f"<html><head><title>Site | Notice</title></head><body>{stray}<h2>Notice</h2><p>body</p></body></html>"

        assert "# Site | Notice" not in convert(html.encode(), "a.html").text

    def test_a_real_heading_still_keeps_the_title_out(self):
        html = "<html><head><title>사이트 | 공지</title></head><body><h2>공지</h2><pre># x</pre></body></html>"

        assert convert(html.encode(), "a.html").text.startswith("## 공지")


class TestTitleOverSeveralLines:
    def test_title_whitespace_collapses(self):
        result = convert("<html><head><title>보고서\n   2024\n- 항목</title></head><body><p>본문</p></body></html>".encode(), "a.html")

        assert result.text == "# 보고서 2024 - 항목\n\n본문"
        assert result.metadata["title"] == "보고서 2024 - 항목"


class TestNoMarksInsideCode:
    def test_strikethrough_and_scripts_inside_a_code_block_are_left_as_written(self):
        html = "<pre>a <del>b</del> c\nx<sup>2</sup> H<sub>2</sub>O</pre><p><del>취소</del> 10<sup>6</sup></p>"

        assert convert(html.encode(), "a.html").text == "```\na b c\nx2 H2O\n```\n\n~~취소~~ 10^6"

    def test_strikethrough_around_a_code_block_marks_only_the_text_outside(self):
        html = "<del><p>앞</p><pre>코드</pre></del>"

        assert convert(html.encode(), "a.html").text == "~~앞~~\n\n```\n코드\n```"

    def test_code_block_in_the_middle_of_a_strikethrough_splits_the_marks(self):
        html = "<del><p>앞</p><pre>코드</pre><p>뒤</p></del>"

        assert convert(html.encode(), "a.html").text == "~~앞~~\n\n```\n코드\n```\n\n~~뒤~~"

    def test_table_with_a_code_block_inside_a_struck_cell_is_marked_per_cell(self):
        html = "<table><tr><th>h</th></tr><tr><td><del>a<table><tr><td><pre>x\ny</pre></td></tr></table>b</del></td></tr></table>"

        assert convert(html.encode(), "a.html").text == "h\n\n~~a~~\n\n```\nx\ny\n```\n\n~~b~~"

    @pytest.mark.timing
    def test_deeply_nested_tables_in_a_struck_cell_take_linear_time(self):
        """Which tables hold a code block is found once, climbing up from each block (8,000 levels took 19 s, now about 0.4 s)."""
        n = 8000
        html = ("<table><tr><td><del>a" + "<table><tr><td>x" * n + "<pre>c</pre>" + "</td></tr></table>" * n
                + "b</del></td></tr></table>")
        start = time.perf_counter()
        convert(html.encode(), "a.html")

        assert time.perf_counter() - start < allowed(4)

    @pytest.mark.timing
    def test_many_code_blocks_in_one_strikethrough_take_linear_time(self):
        """Every piece of text between two code blocks is a run of its own. The marks are written together, with the child
        positions measured once per parent (20,000 blocks took 6 s and 40,000 23 s, now about 1 s and 2 s)."""
        html = "<del>" + "a<pre>c</pre>" * 20000 + "</del>"
        start = time.perf_counter()
        result = convert(html.encode(), "a.html")

        assert time.perf_counter() - start < allowed(3)
        assert result.text.startswith("~~a~~\n\n```\nc\n```\n\n~~a~~\n\n```\nc\n```")

    @pytest.mark.timing
    def test_many_tables_and_code_blocks_in_one_struck_cell_take_linear_time(self):
        """The marks before and after a table grouped with its strikethrough are written the same way (10,000 tables, each
        after a code block, took 7 s, now under 1 s)."""
        pieces = "a<pre>c</pre><table><tr><td>x</td></tr></table>" * 10000
        html = f"<table><tr><td><del>{pieces}</del></td><td>z</td></tr></table>"
        start = time.perf_counter()
        result = convert(html.encode(), "a.html")

        assert time.perf_counter() - start < allowed(3)
        assert result.text.count("~~") == 2 * 10001  # the first `a`, then every table with the `a` after it

    def test_strikethrough_set_on_a_code_block_itself_is_not_marked(self):
        assert convert(b'<pre style="text-decoration:line-through">x</pre>', "a.html").text == "```\nx\n```"

    def test_inline_code_keeps_its_marks(self):
        """Inline code is written without backticks, so a mark is the only sign that it is struck or raised (as before)."""
        html = "<p>use <del><code>foo()</code></del> instead, <code>x<sup>2</sup></code></p>"

        assert convert(html.encode(), "a.html").text == "use ~~foo()~~ instead, x^2"


class TestAdjacentStruckRuns:
    """Row of html.md: struck runs with nothing between them get one pair of marks (`_html_dom._joined_runs`). Marked one by
    one they came out as `~~가~~~~나~~`, and four tildes in a row are not a strikethrough mark."""

    @pytest.mark.parametrize(("html", "expected"), [
        ("<p><del>가</del><del>나</del></p>", "~~가나~~"),
        ("<p><del>가</del><s><span>나</span></s><strike>다</strike> 라</p>", "~~가나다~~ 라"),
        ("<p><del>가</del><code><del>나</del></code></p>", "~~가나~~"),
        ("<p><del>가</del><del>나</del></p><p><del>다</del></p>", "~~가나~~\n\n~~다~~"),
        # a span that lies wholly inside the second run: its half of the pair was moved out next to the first run's
        ('<p><del>가</del><s><a href="https://example.com/">나</a> 다</s></p>', "~~가[나](https://example.com/) 다~~"),
        # two bold spans side by side are written as one, so no mark lay between the two pairs
        ("<p><b>가 <del>나</del></b><strong><del>다</del> 라</strong> 마</p>", "**가 ~~나다~~ 라** 마"),
        # no emphasis is written in a heading
        ("<h2><del>가</del><b><del>나</del> 다</b></h2>", "## ~~가나~~ 다"),
    ])
    def test_runs_with_nothing_between_them_get_one_pair_of_marks(self, html, expected):
        assert convert(html.encode(), "a.html").text == expected

    @pytest.mark.parametrize(("html", "expected"), [
        ("<p><del>가</del> <del>나</del></p>", "~~가~~ ~~나~~"),
        ("<p><del>가</del><br><del>나</del></p>", "~~가~~\n~~나~~"),
        ("<ul><li><del>가</del></li><li><del>나</del></li></ul>", "- ~~가~~\n- ~~나~~"),
        ("<table><tr><th>a</th><th>b</th></tr><tr><td><del>가</del></td><td><del>나</del></td></tr></table>",
         "| a | b |\n| --- | --- |\n| ~~가~~ | ~~나~~ |"),
        # the mark of a span that holds an end of the pair lies between the two: joined, the pair would cross the span
        ("<p><del>가</del><b><del>나</del> 다</b></p>", "~~가~~**~~나~~ 다**"),
        ("<p><del>가</del><b><del>나</del></b></p>", "~~가~~**~~나~~**"),
    ])
    def test_runs_that_are_apart_already_keep_their_own_marks(self, html, expected):
        assert convert(html.encode(), "a.html").text == expected

    @pytest.mark.parametrize(("inner", "expected"), [
        # a table holding a code block is marked cell by cell (`TestNoMarksInsideCode`): its last cell met the text after it
        ("<del>x<table><tr><td><pre>in1</pre></td><td>in2</td></tr></table>y</del>", "~~x~~in1 ~~in2~~ ~~y~~"),
        ("<del>x<table><tr><td>in2</td><td><pre>in1</pre></td></tr></table>y</del>", "~~x~~ ~~in2~~ in1~~y~~"),
        ("앞<table><tr><td>10</td><td><del>20</del></td></tr></table><del>끝</del>", "앞10 ~~20~~ ~~끝~~"),
        # grouped with its strikethrough as a whole: unchanged
        ("<del>x<table><tr><td>in1</td><td>in2</td></tr></table>y</del>", "~~xin1 in2y~~"),
    ])
    def test_run_inside_a_table_flattened_into_a_cell_is_kept_apart_from_the_run_next_to_the_table(self, inner, expected):
        html = f"<table><tr><td>{inner}</td><td>z</td></tr></table>"

        assert convert(html.encode(), "a.html").text == f"| {expected} | z |\n| --- | --- |"

    @pytest.mark.parametrize(("html", "expected"), [
        ('<p><a href="https://e.example/u">more <del>old <img src="x.png" alt="pic"> name</del></a><del>10,000</del></p>',
         "[more ~~old [Image: pic] name~~](https://e.example/u)~~10,000~~"),
        ('<p><del>10,000</del><a href="https://e.example/u"><del>old <img src="x.png" alt="pic"></del> more</a></p>',
         "~~10,000~~[~~old [Image: pic]~~ more](https://e.example/u)"),
        ('<p><s>was</s><i><del>old <img src="x.png" alt="pic"></del> kept</i></p>', "~~was~~*~~old [Image: pic]~~ kept*"),
        ('<p><b>Now <s><img src="x.png" alt="pic">old</s></b><s>10,000</s></p>', "**Now ~~[Image: pic] old~~**~~10,000~~"),
    ])
    def test_runs_are_not_joined_over_the_edge_of_a_span_with_a_picture(self, html, expected):
        """A link or emphasis span that holds a picture placeholder is written like any other. Taken for unwritten, the runs
        on both sides of its edge were joined: the pair opened inside the link text and closed after the address
        (`[more ~~old [Image: pic] name](https://e.example/u)10,000~~`), and emphasis around a word outside the
        strikethrough was dropped."""
        assert convert(html.encode(), "a.html").text == expected



class TestAltTextCannotMakeALink:
    """Rows of inline-formatting.md: link text has its unbalanced brackets escaped (`_inline._escape_link_text`). Alt text inside
    `[Image: …]` is escaped the same way; plain text that looks like Markdown stays as written (accepted gap)."""

    def test_html_alt_text(self):
        html = '<p>보기 <img src="x.png" alt="a](https://evil.example) [b"> 끝</p>'

        assert convert(html.encode(), "a.html").text == "보기 [Image: a\\](https://evil.example) \\[b] 끝"

    def test_balanced_brackets_stay(self):
        assert convert('<p><img src="x.png" alt="그림 [1]"></p>'.encode(), "a.html").text == "[Image: 그림 [1]]"

    def test_markdown_data_uri_alt_text(self):
        data = b"see ![a [b](data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=) end"

        assert convert(data, "a.md").text == "see [Image: a \\[b] end"

    def test_alt_text_of_an_extracted_picture(self):
        """A picture the converter extracts keeps `[Image: alt]` as placeholder text (no description): escaped the same way. The
        alt text handed to the describer stays as written."""
        picture = io.BytesIO()
        Image.new("RGB", (200, 120), (10, 200, 30)).save(picture, "PNG")
        source = "data:image/png;base64," + base64.b64encode(picture.getvalue()).decode()
        html = f'<p>보기</p><p><img src="{source}" alt="a](https://evil.example) [b"></p>'

        result = convert(html.encode(), "a.html")

        assert result.text == "보기\n\n[Image: a\\](https://evil.example) \\[b]"
        assert [image.alt for image in result.images] == ["a](https://evil.example) [b"]

    def test_escaped_alt_inside_a_link(self):
        """`[Image: logo\\]]` is still a placeholder, so a picture inside a link is handled as one with plain alt text is."""
        html = '<p>x <a href="https://x.com"><img src="y.png" alt="{alt}"></a> 끝</p>'

        plain = convert(html.format(alt="logo").encode(), "a.html").text
        bracketed = convert(html.format(alt="logo]").encode(), "a.html").text

        assert plain == "x [Image: logo] 끝"
        assert bracketed == "x [Image: logo\\]] 끝"

    def test_plain_text_that_looks_like_a_link_stays(self):
        assert convert(b"<p>[click](https://example.com)</p>", "a.html").text == "[click](https://example.com)"


class TestEmptyListItemHoldingANestedList:
    def test_nesting_is_kept(self):
        html = "<ul><li><ul><li><ul><li>x</li></ul></li></ul></li></ul>"

        assert convert(html.encode(), "a.html").text == "-\n  -\n    - x"

    @pytest.mark.parametrize(("html", "expected"), [
        ("<ul><li><ul><li><ul><li><ul><li>x</li></ul></li></ul></li></ul></li></ul>", "-\n  -\n    -\n      - x"),
        ("<ul><li><ul><li><ul><li>t<ul><li>x</li></ul></li></ul></li></ul></li></ul>", "-\n  -\n    - t\n      - x"),
        ("<ul><li>a</li><li><ul><li><ul><li>b</li></ul></li></ul></li></ul>", "- a\n-\n  -\n    - b"),
    ])
    def test_chain_of_empty_items(self, html, expected):
        assert convert(html.encode(), "a.html").text == expected

    @pytest.mark.parametrize(("html", "expected"), [
        # Right under an item line with text `  -` would be a setext underline that makes the text a heading
        ("<ul><li>a<ul><li><ul><li>b</li></ul></li></ul></li></ul>", "- a\n-\n    - b"),
        # Under `1.` (content column 3) a two-space `  1.` does not nest, and `    1. x` would become an indented code block
        ("<ol><li><ol><li><ol><li>x</li></ol></li></ol></li></ol>", "1.\n1.\n    1. x"),
        ("<ul><li><ol><li><ul><li>x</li></ul></li></ol></li></ul>", "-\n1.\n    - x"),
    ])
    def test_shapes_that_cannot_nest_keep_the_old_form(self, html, expected):
        assert convert(html.encode(), "a.html").text == expected

    def test_ordered_empty_item(self):
        assert clean_text("1.\n   1.\n      - x") == "1.\n   1.\n      - x"

    @pytest.mark.parametrize(("text", "expected"), [
        ("Intro\n\n     2.\n\nnext", "Intro\n\n2.\n\nnext"),  # five spaces would make an indented code block
        ("Amount\n\n   -\n\n   2025.", "Amount\n\n-\n\n2025."),
        ("  -\nplain", "-\nplain"),
        ("  -\n  - same depth", "-\n  - same depth"),
        ("- a\n  -\n    - b", "- a\n-\n    - b"),
        ("-\n   -\n    - b", "-\n-\n    - b"),
    ])
    def test_a_bare_marker_not_holding_a_deeper_list_is_stripped(self, text, expected):
        assert clean_text(text) == expected

    def test_bare_marker_paragraphs_of_html(self):
        html = "<p>Amount</p><p>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;-</p><p>&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;2025.</p>"

        assert convert(html.encode(), "a.html").text == "Amount\n\n-\n\n2025."


_CALENDAR_MAIL = (_HEAD + "Content-Type: multipart/alternative; boundary=b\n\n--b\nContent-Type: text/plain\n\n본문\n--b\n"
                  "Content-Type: text/calendar; method=REQUEST; charset=utf-8\n\n{ics}\n--b--\n")


class TestReminderInsideAnEvent:
    def test_reminder_properties_are_not_the_event_s(self):
        ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART:20240307T150000Z\r\nBEGIN:VALARM\r\nACTION:EMAIL\r\n"
               "SUMMARY:알림\r\nATTENDEE:mailto:alarm@example.com\r\nTRIGGER:-PT15M\r\nEND:VALARM\r\nSUMMARY:주간 회의\r\n"
               "ATTENDEE;CN=김:mailto:kim@example.com\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")

        text = convert(_CALENDAR_MAIL.format(ics=ics).encode(), "a.eml").text

        assert text.endswith("Event:\n\n- Title: 주간 회의\n- Start: 2024-03-07 15:00 UTC\n- Attendees: 김 <kim@example.com>")


class TestUnclosedComponentInsideAnEvent:
    """A component opened inside an event and never closed holds nothing: what follows its BEGIN is still the event's."""

    def test_properties_after_a_stray_begin_are_read(self):
        ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:주간 회의\r\nBEGIN:X-THING\r\nLOCATION:3층 회의실\r\nEND:VEVENT\r\n"
               "END:VCALENDAR\r\n")

        text = convert(_CALENDAR_MAIL.format(ics=ics).encode(), "a.eml").text

        assert text.endswith("Event:\n\n- Title: 주간 회의\n- Location: 3층 회의실")

    def test_a_closed_component_before_the_stray_begin_is_still_left_out(self):
        ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nBEGIN:VALARM\r\nSUMMARY:알림\r\nEND:VALARM\r\nBEGIN:X-THING\r\n"
               "SUMMARY:주간 회의\r\nLOCATION:3층 회의실\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")

        text = convert(_CALENDAR_MAIL.format(ics=ics).encode(), "a.eml").text

        assert text.endswith("Event:\n\n- Title: 주간 회의\n- Location: 3층 회의실")


class TestRecurrenceRuleWithATrailingSemicolon:
    def test_rule_is_still_rendered(self):
        ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:회의\r\nRRULE:FREQ=WEEKLY;;BYDAY=TU;\r\nEND:VEVENT\r\n"
               "END:VCALENDAR\r\n")

        assert "- Repeats: weekly on Tuesday" in convert(_CALENDAR_MAIL.format(ics=ics).encode(), "a.eml").text


class TestNoWideEncodingGuessWithoutNulBytes:
    """A UTF-16/32 guess for bytes without NUL is taken only when it reads as plausible CJK or Hangul text."""

    def test_short_broken_utf8_is_not_read_as_utf16(self):
        text, encoding = decode_text(b"ab=ZZ\xc3\xa9\xc3", "utf-8")

        assert not encoding.startswith(("utf_16", "utf_32"))
        assert text.startswith("ab=ZZ")

    @pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be"])
    @pytest.mark.parametrize("text", ["日本語のテキストファイルです。これは改行も空白もありません。東京は首都です。",
                                      "中文文本文件没有空格也没有换行这是一个测试文件北京是首都。",
                                      "안녕하세요반갑습니다", "大韓民國의首都는서울이다"])
    @pytest.mark.parametrize("name", ["a.txt", "a.html"])
    def test_cjk_text_without_ascii_stays_utf16(self, codec, text, name):
        result = convert(text.encode(codec), name)

        assert result.text == text
        assert result.warnings == []


class TestWideEncodingGuessOutsidePlainCjk:
    """UTF-16 without NUL bytes that is not plain CJK text — another script written without spaces, a kanji outside the Basic
    Multilingual Plane, a Korean name in Japanese or Chinese text — is read as it was before the guess was checked, and broken
    byte-oriented text is still not taken for UTF-16."""

    @pytest.mark.parametrize("codec", ["utf-16-le", "utf-16-be"])
    @pytest.mark.parametrize("text", ["សួស្តីពិភពលោក", "བོད་སྐད་ཡིག་གཟུགས", "ሰላምዓለምይህአማርኛነው", "নমস্কারবিশ্বএটিবাংলাপাঠ",
                                      "Καλημέρακόσμεαυτόείναιελληνικόκείμενο"])
    @pytest.mark.parametrize("name", ["a.txt", "a.html"])
    def test_text_of_another_script_keeps_its_byte_order(self, codec, text, name):
        # Khmer was read in the other byte order as Han and Hangul (`鼗봗鼗툗…`) without a warning; the others as UTF-8 with �
        result = convert(text.encode(codec), name)

        assert result.text == text
        assert result.metadata["encoding"] == codec.replace("-", "_")
        assert result.warnings == []

    @pytest.mark.parametrize(("text", "codec"), [("𠮷野家で牛丼を食べました", "utf-16-le"), ("髙﨑さんは𠮷野家で𩸽を食べた", "utf-16-le"),
                                                 ("髙﨑さんは𠮷野家で𩸽を食べた", "utf-16-be")])
    def test_kanji_outside_the_basic_multilingual_plane(self, text, codec):
        result = convert(text.encode(codec), "a.txt")

        assert result.text == text
        assert result.warnings == []

    @pytest.mark.parametrize(("text", "codec"), [("東京とソウル서울", "utf-16-le"), ("부산是", "utf-16-le"), ("부산是", "utf-16-be"),
                                                 ("首尔서울是韩国的首都", "utf-16-le"), ("首尔서울是韩国的首都", "utf-16-be")])
    def test_everyday_han_with_a_little_hangul(self, text, codec):
        result = convert(text.encode(codec), "a.txt")

        assert result.text == text
        assert result.warnings == []

    @pytest.mark.parametrize("data", [
        bytes.fromhex("d180d183a9b1"),              # UTF-8 `ру` + two stray bytes: `톀톃ꦱ`
        bytes.fromhex("96d79420d798"),              # cut UTF-8 Hebrew: `雗鐠힘`, an ideograph and a syllable nobody writes
        bytes.fromhex("8885c7206cd5"),              # Mac Roman with a space: `袅유法`, everyday characters all three
        bytes.fromhex("4f4b20d2c1c0"),              # `OK ` + TIS-620 Thai: `佋⃒쇀`
        bytes.fromhex("20d8b9ded8b1"),              # a surrogate pair by accident: Tangut `𘊹` and `뇘`
        bytes.fromhex("6b0a3d0ad4ffcf0ad20ad50a"),  # one character per line: `權㴊퓿켊툊픊`, the line breaks are no sign of UTF-16
    ])
    def test_broken_byte_text_is_still_not_utf16(self, data):
        assert not decode_text(data)[1].startswith(("utf_16", "utf_32"))


class TestRowspanZero:
    def test_spans_to_the_end_of_the_row_group(self):
        html = ("<table><thead><tr><th>구분</th><th>값</th></tr></thead><tbody><tr><td rowspan=\"0\">A</td><td>1</td></tr>"
                "<tr><td>2</td></tr></tbody><tbody><tr><td>B</td><td>3</td></tr></tbody></table>")

        assert convert(html.encode(), "a.html").text == "| 구분 | 값 |\n| --- | --- |\n| A | 1 |\n| A | 2 |\n| B | 3 |"


class TestCodeBlockInsideAListItem:
    """A code block inside a list item stays at the left margin, as it always did (the indentation tried in this audit was
    reverted: the item's paragraphs stay at the margin, so an indented fence after them became an indented code block)."""

    @pytest.mark.parametrize(("html", "expected"), [
        ("<ul><li>실행:<pre>make\n\n  dev</pre></li><li>다음</li></ul>", "- 실행:\n\n```\nmake\n\n  dev\n```\n\n- 다음"),
        ("<ol><li>실행:<pre>make</pre></li></ol>", "1. 실행:\n\n```\nmake\n```"),
        ("<ul><li>a<ul><li>b<pre>x</pre></li></ul></li></ul>", "- a\n  - b\n\n```\nx\n```"),
        ("<ul><li>a<ul><li>b<p>para</p><pre>code\n  indented</pre></li></ul></li></ul>",
         "- a\n  - b\n\npara\n\n```\ncode\n  indented\n```"),
        ("<ol start=10><li>x<p>para</p><pre>code</pre></li></ol>", "10. x\n\npara\n\n```\ncode\n```"),
        ("<ul><li>x<ul><li>a<ul><li>b<p>para</p></li></ul><pre>code</pre></li></ul></li></ul>",
         "- x\n  - a\n    - b\n\npara\n\n```\ncode\n```"),
    ])
    def test_fence_stays_at_the_margin(self, html, expected):
        assert convert(html.encode(), "a.html").text == expected
