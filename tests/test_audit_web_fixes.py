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

    def test_moderate_chain_is_read(self):
        data = (_HEAD + "Content-Type: multipart/mixed; boundary=o\n\n--o\n" + "Content-Type: message/rfc822\n\n" * 50
                + "Content-Type: text/plain\n\n본문\n--o--\n").encode()

        assert convert(data, "a.eml").text.startswith("Subject: 안내")


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

    def test_inline_code_gets_no_marks(self):
        assert convert(b"<p><code>a<sup>b</sup></code></p>", "a.html").text == "ab"


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


_CALENDAR_MAIL = (_HEAD + "Content-Type: multipart/alternative; boundary=b\n\n--b\nContent-Type: text/plain\n\n본문\n--b\n"
                  "Content-Type: text/calendar; method=REQUEST; charset=utf-8\n\n{ics}\n--b--\n")


class TestReminderInsideAnEvent:
    def test_reminder_properties_are_not_the_event_s(self):
        ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART:20240307T150000Z\r\nBEGIN:VALARM\r\nACTION:EMAIL\r\n"
               "SUMMARY:알림\r\nATTENDEE:mailto:alarm@example.com\r\nTRIGGER:-PT15M\r\nEND:VALARM\r\nSUMMARY:주간 회의\r\n"
               "ATTENDEE;CN=김:mailto:kim@example.com\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")

        text = convert(_CALENDAR_MAIL.format(ics=ics).encode(), "a.eml").text

        assert text.endswith("Event:\n\n- Title: 주간 회의\n- Start: 2024-03-07 15:00 UTC\n- Attendees: 김 <kim@example.com>")


class TestRecurrenceRuleWithATrailingSemicolon:
    def test_rule_is_still_rendered(self):
        ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:회의\r\nRRULE:FREQ=WEEKLY;;BYDAY=TU;\r\nEND:VEVENT\r\n"
               "END:VCALENDAR\r\n")

        assert "- Repeats: weekly on Tuesday" in convert(_CALENDAR_MAIL.format(ics=ics).encode(), "a.eml").text


class TestNoWideEncodingGuessWithoutNulBytes:
    def test_short_broken_utf8_is_not_read_as_utf16(self):
        text, encoding = decode_text(b"ab=ZZ\xc3\xa9\xc3", "utf-8")

        assert not encoding.startswith(("utf_16", "utf_32"))
        assert text.startswith("ab=ZZ")


class TestRowspanZero:
    def test_spans_to_the_end_of_the_row_group(self):
        html = ("<table><thead><tr><th>구분</th><th>값</th></tr></thead><tbody><tr><td rowspan=\"0\">A</td><td>1</td></tr>"
                "<tr><td>2</td></tr></tbody><tbody><tr><td>B</td><td>3</td></tr></tbody></table>")

        assert convert(html.encode(), "a.html").text == "| 구분 | 값 |\n| --- | --- |\n| A | 1 |\n| A | 2 |\n| B | 3 |"


class TestCodeBlockInsideAListItem:
    """A code block in a list item is indented to the item's content column, so it stays inside the item."""

    @pytest.mark.parametrize(("html", "expected"), [
        ("<ul><li>실행:<pre>make\n\n  dev</pre></li><li>다음</li></ul>", "- 실행:\n\n  ```\n  make\n\n    dev\n  ```\n\n- 다음"),
        ("<ol><li>실행:<pre>make</pre></li></ol>", "1. 실행:\n\n   ```\n   make\n   ```"),
        ("<ul><li>a<ul><li>b<pre>x</pre></li></ul></li></ul>", "- a\n  - b\n\n    ```\n    x\n    ```"),
    ])
    def test_fence_is_indented(self, html, expected):
        assert convert(html.encode(), "a.html").text == expected
