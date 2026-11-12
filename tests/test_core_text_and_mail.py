"""Forwarded mail and mail parts that carry only a name, space characters in UTF-16 without a BOM, HTML `<img>` data: URIs in MD,
a `.csv` holding an HTML table, text after a nested list in an HTML list item, a picture in a Markdown table with no spaces
around its cells."""

import base64

import pytest

from opendockit import ConvertOptions, DocumentFormat, convert
from tests import builder, needs_html5_tokenizer


def _forward(outer_body: str, inner: str, *, outer_type: str = "text/plain", disposition: str = "") -> bytes:
    """A mail with `outer_body` as its body and `inner` (a mail written from its headers on) attached as a message/rfc822 part
    ('forward as attachment')."""
    return ("From: c@example.com\nTo: d@example.com\nSubject: 전달: 견적\nMIME-Version: 1.0\n"
            "Content-Type: multipart/mixed; boundary=OUT\n\n"
            f"--OUT\nContent-Type: {outer_type}; charset=utf-8\n\n{outer_body}\n"
            f"--OUT\nContent-Type: message/rfc822\n{disposition}\n{inner}\n--OUT--\n").encode()


_INNER_ALTERNATIVE = ("From: a@example.com\nTo: b@example.com\nSubject: 견적\nMIME-Version: 1.0\n"
                      "Content-Type: multipart/alternative; boundary=IN\n\n"
                      "--IN\nContent-Type: text/plain; charset=utf-8\n\n견적 금액은 300만 원입니다.\n"
                      "--IN\nContent-Type: text/html; charset=utf-8\n\n<p>견적 금액은 300만 원입니다(HTML).</p>\n--IN--")
_INNER_HTML_ONLY = ("From: a@example.com\nTo: b@example.com\nSubject: 견적\nMIME-Version: 1.0\n"
                    "Content-Type: text/html; charset=utf-8\n\n<html><body><p>견적 금액은 300만 원입니다.</p></body></html>")
_OUTER_HEAD = "Subject: 전달: 견적\nFrom: c@example.com\nTo: d@example.com"
_INNER_HEAD = "Subject: 견적\nFrom: a@example.com\nTo: b@example.com"


class TestForwardedMail:
    """For a mail forwarded 'as attachment' (a message/rfc822 part), the walk looking for body parts went down into the forwarded
    mail, so the HTML body of the forwarded mail became the body under the outer headers instead of the plain body the forwarder
    wrote (the outer body was lost), and the warning said '1 unnamed attachment'. The outer body is placed first, and the forwarded
    mail follows as a '## Forwarded message' heading + headers + body (it gives the LLM context)."""

    def test_outer_plain_body_comes_first_and_the_forwarded_mail_follows(self):
        data = _forward("아래 메일 확인 바랍니다.", _INNER_ALTERNATIVE)

        result = convert(data, "fwd.eml")

        assert result.format is DocumentFormat.TEXT
        assert result.text == (f"{_OUTER_HEAD}\n\n아래 메일 확인 바랍니다.\n\n## Forwarded message\n\n{_INNER_HEAD}\n\n"
                               "견적 금액은 300만 원입니다.")
        assert result.warnings == []

    def test_plain_mail_is_not_sniffed_as_html_because_of_a_forwarded_html_document(self):
        """Even when the 8bit HTML part (`<html>…</html>`) of the forwarded mail shows near the start of the mail, it is read as
        mail. The raw source was read as HTML, so the MIME headers and boundaries became body."""
        inner = _INNER_ALTERNATIVE.replace("<p>견적 금액은 300만 원입니다(HTML).</p>",
                                           "<html><body><p>견적 금액은 300만 원입니다(HTML).</p></body></html>")
        data = _forward("아래 메일 확인 바랍니다.", inner)

        result = convert(data, "fwd.eml")

        assert result.format is DocumentFormat.TEXT
        assert result.text == (f"{_OUTER_HEAD}\n\n아래 메일 확인 바랍니다.\n\n## Forwarded message\n\n{_INNER_HEAD}\n\n"
                               "견적 금액은 300만 원입니다.")

    @pytest.mark.parametrize("disposition", ["", 'Content-Disposition: attachment; filename="견적.eml"\n'])
    def test_forwarded_html_only_mail_is_read_as_html_after_the_outer_plain_body(self, disposition):
        data = _forward("아래 메일 확인 바랍니다.", _INNER_HTML_ONLY, disposition=disposition)

        result = convert(data, "fwd.eml")

        assert result.format is DocumentFormat.HTML
        assert result.text == (f"{_OUTER_HEAD}\n\n아래 메일 확인 바랍니다.\n\n## Forwarded message\n\n{_INNER_HEAD}\n\n"
                               "견적 금액은 300만 원입니다.")
        assert result.warnings == []

    @needs_html5_tokenizer
    def test_outer_html_body_comes_first_and_the_forwarded_mail_follows(self):
        data = _forward("<html><body><p>아래 메일 <b>확인</b> 바랍니다.</p></body></html>", _INNER_ALTERNATIVE,
                        outer_type="text/html")

        result = convert(data, "fwd.eml")

        assert result.format is DocumentFormat.HTML
        assert result.text.startswith(f"{_OUTER_HEAD}\n\n아래 메일 확인 바랍니다.\n\n## Forwarded message\n\n{_INNER_HEAD}\n\n")
        assert result.text.endswith("견적 금액은 300만 원입니다(HTML).")

    def test_attachment_inside_the_forwarded_mail_is_named_in_the_warning(self):
        inner = ("From: a@example.com\nTo: b@example.com\nSubject: 견적\nMIME-Version: 1.0\n"
                 "Content-Type: multipart/mixed; boundary=IN\n\n--IN\nContent-Type: text/plain; charset=utf-8\n\n견적서 첨부합니다.\n"
                 "--IN\nContent-Type: application/pdf\nContent-Disposition: attachment; filename=quote.pdf\n"
                 "Content-Transfer-Encoding: base64\n\nJVBERi0xLjQK\n--IN--")
        data = _forward("확인 바랍니다.", inner)

        result = convert(data, "fwd.eml")

        assert result.text.endswith(f"## Forwarded message\n\n{_INNER_HEAD}\n\n견적서 첨부합니다.")
        assert result.warnings == ["1 mail attachment(s) (quote.pdf) were not read. Convert attachments separately."]

    @pytest.mark.parametrize("parts", [("plain", "html"), ("html", "plain")])
    def test_forwarded_mixed_mail_keeps_both_its_plain_and_html_bodies(self, parts):
        """When the outer mail is plain text and the forwarded mail has separate plain and HTML bodies inside a mixed, both are read
        in order. Because the forwarded mail had a plain part, the text extractor read only the plain text and the HTML body (the
        delivery date) vanished without a warning (with an HTML outer mail both came out)."""
        bodies = {"plain": "--IN\nContent-Type: text/plain; charset=utf-8\n\n계약 금액은 1억 원입니다.\n",
                  "html": "--IN\nContent-Type: text/html; charset=utf-8\n\n<p>납기일은 10월 31일입니다.</p>\n"}
        inner = ("From: a@example.com\nTo: b@example.com\nSubject: 견적\nMIME-Version: 1.0\n"
                 "Content-Type: multipart/mixed; boundary=IN\n\n" + "".join(bodies[part] for part in parts) + "--IN--")
        data = _forward("아래 메일 확인 바랍니다.", inner)

        result = convert(data, "fwd.eml")

        texts = {"plain": "계약 금액은 1억 원입니다.", "html": "납기일은 10월 31일입니다."}
        assert result.format is DocumentFormat.HTML
        assert result.text == (f"{_OUTER_HEAD}\n\n아래 메일 확인 바랍니다.\n\n## Forwarded message\n\n{_INNER_HEAD}\n\n"
                               + "\n\n".join(texts[part] for part in parts))
        assert result.warnings == []

    def test_forwarded_alternative_with_an_html_footer_keeps_the_footer(self):
        """When the forwarded mail has a separate HTML footer part after its alternative (plain, HTML), the footer is read too."""
        inner = ("From: a@example.com\nTo: b@example.com\nSubject: 견적\nMIME-Version: 1.0\n"
                 "Content-Type: multipart/mixed; boundary=MX\n\n--MX\n"
                 + _INNER_ALTERNATIVE.split("MIME-Version: 1.0\n", 1)[1]
                 + "\n--MX\nContent-Type: text/html; charset=utf-8\n\n<p>문의: 영업팀 02-123-4567</p>\n--MX--")
        data = _forward("아래 메일 확인 바랍니다.", inner)

        result = convert(data, "fwd.eml")

        assert result.text.endswith("견적 금액은 300만 원입니다(HTML).\n\n문의: 영업팀 02-123-4567")

    def test_mail_forwarded_twice_reads_every_level_in_order(self):
        middle = ("From: m@example.com\nTo: c@example.com\nSubject: 전달: 견적\nMIME-Version: 1.0\n"
                  "Content-Type: multipart/mixed; boundary=MID\n\n--MID\nContent-Type: text/plain; charset=utf-8\n\n중간 전달 글\n"
                  f"--MID\nContent-Type: message/rfc822\n\n{_INNER_ALTERNATIVE}\n--MID--")
        data = _forward("맨 바깥 글", middle)

        result = convert(data, "fwd.eml")

        assert result.text == (f"{_OUTER_HEAD}\n\n맨 바깥 글\n\n## Forwarded message\n\nSubject: 전달: 견적\nFrom: m@example.com\n"
                               f"To: c@example.com\n\n중간 전달 글\n\n## Forwarded message\n\n{_INNER_HEAD}\n\n견적 금액은 300만 원입니다.")

    def test_forwarded_mails_beyond_the_limit_are_named_in_the_warning(self):
        """Up to 20 forwarded mails are read. Those beyond are reported as not read (whole, with the forwards inside them)."""
        inner = "From: z@example.com\nSubject: 맨 안\nMIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\n맨 안 글"
        for level in range(21):
            inner = (f"From: x@example.com\nSubject: 단계 {level}\nMIME-Version: 1.0\nContent-Type: multipart/mixed; boundary=B{level}\n\n"
                     f"--B{level}\nContent-Type: text/plain; charset=utf-8\n\n단계 {level} 글\n"
                     f"--B{level}\nContent-Type: message/rfc822\n\n{inner}\n--B{level}--")
        data = _forward("바깥 글", inner)

        result = convert(data, "fwd.eml")

        assert result.text.count("## Forwarded message") == 20
        assert "맨 안 글" not in result.text
        assert result.warnings == ["1 mail attachment(s) (단계 0) were not read. Convert attachments separately."]


class TestNamedMailPartWithoutDisposition:
    """An attachment with only `name=` on its Content-Type and no Content-Disposition (old Outlook, groupware) was read as body, so
    the first text/html part pushed out the plain body, with no warning. A part that is not the top one, has only a name and has no
    Content-ID or Content-Location is treated as an attachment."""

    def test_named_html_part_is_an_attachment_not_the_body(self):
        data = ("From: c@example.com\nTo: d@example.com\nSubject: report\nMIME-Version: 1.0\n"
                "Content-Type: multipart/mixed; boundary=BB\n\n--BB\nContent-Type: text/plain; charset=utf-8\n\n"
                "첨부한 보고서를 확인해 주세요.\n--BB\nContent-Type: text/html; charset=utf-8; name=report.html\n\n"
                "<p>첨부 보고서 내용</p>\n--BB--\n").encode()

        result = convert(data, "mail.eml")

        assert result.format is DocumentFormat.TEXT
        assert result.text == "Subject: report\nFrom: c@example.com\nTo: d@example.com\n\n첨부한 보고서를 확인해 주세요."
        assert result.warnings == ["1 mail attachment(s) (report.html) were not read. Convert attachments separately."]

    def test_named_picture_shown_in_the_body_is_not_reported_as_an_attachment(self):
        picture = base64.encodebytes(builder.png_bytes()).decode()
        data = ("From: c@example.com\nTo: d@example.com\nSubject: photo\nMIME-Version: 1.0\n"
                "Content-Type: multipart/related; boundary=RR\n\n--RR\nContent-Type: text/html; charset=utf-8\n\n"
                '<p>사진입니다.</p><img src="cid:pic1" alt="현장">\n--RR\nContent-Type: image/png; name=image001.png\n'
                f"Content-ID: <pic1>\nContent-Transfer-Encoding: base64\n\n{picture}--RR--\n").encode()

        result = convert(data, "mail.eml")

        assert result.warnings == []
        assert len(result.images) == 1

    def test_single_part_mail_with_a_name_is_still_the_body(self):
        data = ("From: c@example.com\nTo: d@example.com\nSubject: memo\nMIME-Version: 1.0\n"
                "Content-Type: text/plain; charset=utf-8; name=memo.txt\n\n메모 본문\n").encode()

        result = convert(data, "mail.eml")

        assert result.text == "Subject: memo\nFrom: c@example.com\nTo: d@example.com\n\n메모 본문"
        assert result.warnings == []


class TestUtf16WithoutBomAndSpaceCharacters:
    """The printable check (`str.isprintable`) of BOM-less UTF-16 detection saw NBSP, the ideographic space (U+3000) and the
    zero-width space (U+200B) as non-printable, so a memo or CSV with even one such character was rejected as 'This binary file
    format is not supported.' Spaces (Zs) and format characters (Cf) are allowed."""

    @pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
    @pytest.mark.parametrize("space", ["\xa0", "　", "​", "﻿"])
    def test_memo_with_a_space_character_is_text(self, encoding, space):
        data = f"제목{space}보고서\n내용입니다\n".encode(encoding)

        result = convert(data, "memo.txt")

        assert result.format is DocumentFormat.TEXT
        assert result.metadata["encoding"] == encoding
        assert "보고서" in result.text and "내용입니다" in result.text

    @pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be"])
    def test_csv_with_a_no_break_space_is_a_table(self, encoding):
        data = "이름\t금액\n홍길동\t1,000\xa0원\n".encode(encoding)

        result = convert(data, "data.csv")

        assert result.text == "| 이름 | 금액 |\n| --- | --- |\n| 홍길동 | 1,000 원 |"

    def test_binary_is_still_rejected(self):
        from opendockit import UnsupportedFormatError

        data = bytes(range(256)) * 8

        with pytest.raises(UnsupportedFormatError):
            convert(data, "blob.txt")


def _markdown_with_html_picture(attributes: str) -> bytes:
    encoded = base64.b64encode(builder.png_bytes()).decode()
    return f"# 제목\n\n본문 문단.\n\n<img {attributes.format(data=f'data:image/png;base64,{encoded}')}>\n\n끝 문단.\n".encode()


class TestMarkdownHtmlImageDataUri:
    """A picture embedded in MD as an HTML `<img src="data:…">` was not caught by picture extraction, which looked only at
    `![](data:…)`, so the base64 became body (520,000 characters for one picture; with the default limit exceeded even the
    paragraphs after it were cut off). Like `![](data:…)`, it is registered as a picture and its alt is passed on as alt text."""

    @pytest.mark.parametrize("attributes", ['src="{data}" alt="구조도" width="300"', "alt='구조도' src='{data}'",
                                            'src = "{data}"\n  alt="구조도"'])
    def test_html_image_with_a_data_uri_is_extracted(self, attributes):
        data = _markdown_with_html_picture(attributes)

        result = convert(data, "a.md")

        assert result.text == "# 제목\n\n본문 문단.\n\n[Image: 구조도]\n\n끝 문단."
        assert len(result.images) == 1
        assert not result.exceeded

    def test_without_picture_extraction_only_the_alt_text_is_left(self):
        data = _markdown_with_html_picture('src="{data}" alt="A &amp; B"')

        result = convert(data, "a.md", ConvertOptions(extract_images=False))

        assert result.text == "# 제목\n\n본문 문단.\n\n[Image: A & B]\n\n끝 문단."
        assert not result.images

    @pytest.mark.timing
    def test_many_unclosed_fence_lines_before_a_picture_take_linear_time(self):
        """For every unmatched fence opening line the regex searched to the end of the text for a closing line, so 32,000 opening
        lines + one picture took the square of the number of lines."""
        import time

        data = b"```py\n" * 32_000 + b"![a](data:image/png;base64,iVBORw0KGgo=)\n"
        started = time.perf_counter()

        result = convert(data, "a.md")

        assert time.perf_counter() - started < 5.0
        assert "data:image" not in result.text

    @pytest.mark.parametrize("opening", ["![a](data:image/png;base64,", '<img src="data:image/png;base64,'])
    @pytest.mark.timing
    def test_unclosed_data_uri_followed_by_long_whitespace_takes_linear_time(self, opening):
        """The base64 part (a lazy repeat) and the `\\s*` after it both accepted whitespace, so 20,000 spaces after an unclosed
        data: URI took 3.4 seconds (quadratic)."""
        import time

        data = (opening + " " * 40_000 + "#").encode()
        started = time.perf_counter()

        convert(data, "a.md")

        assert time.perf_counter() - started < 2.0

    def test_data_uri_with_whitespace_before_the_closing_parenthesis_is_still_a_picture(self):
        data = b'![a](data:image/png;base64,iVBORw0KGgo=  \n  "title"  )\n<img src="data:image/png;base64,iVBORw0KGgo=  " alt="b">'

        result = convert(data, "a.md")

        assert "data:image" not in result.text

    @pytest.mark.parametrize(("opening", "closing"), [("```", "```"), ("~~~", "~~~ "), ("````", "```"), ("```", "~~~")])
    def test_picture_between_fences_is_code_only_when_the_fence_closes(self, opening, closing):
        picture = '<img src="data:image/png;base64,iVBORw0KGgo=">'
        data = f"{opening}html\n{picture}\n{closing}\n\n{picture}\n".encode()

        result = convert(data, "a.md")

        assert result.text.count("data:image") == (0 if opening[0] != closing[0] else 1)

    def test_html_image_inside_a_code_block_is_left_as_code(self):
        data = b'```html\n<img src="data:image/png;base64,iVBORw0KGgo=">\n```\n\nafter\n'

        result = convert(data, "a.md")

        assert result.text == '```html\n<img src="data:image/png;base64,iVBORw0KGgo=">\n```\n\nafter'
        assert not result.images


_HTML_TABLE = ('<html><head><meta charset="utf-8"></head><body><table border="1"><tr><th>이름</th><th>금액</th></tr>'
               "<tr><td>홍길동</td><td>1,000</td></tr><tr><td>김철수</td><td>2,500</td></tr></table></body></html>")


class TestHtmlTableServedAsCsv:
    """When an 'Excel download' serves an HTML table as `.csv`/`.tsv`, it was read as CSV by its extension: the tags became cell
    text and `1,000` was split into two cells. Conservatively: it is read as HTML, with an extension warning, only when it starts
    with an HTML tag and has table structure (`<table`, `<tr`, `<td`) and closing tags."""

    @pytest.mark.parametrize("filename", ["export.csv", "export.tsv"])
    @pytest.mark.parametrize("prefix", ["", "﻿", "\n\n  ", "<!DOCTYPE html>\n"])
    def test_html_table_with_a_delimited_text_extension_is_read_as_html(self, filename, prefix):
        data = (prefix + _HTML_TABLE).encode()

        result = convert(data, filename)

        assert result.format is DocumentFormat.HTML
        assert result.text == "| 이름 | 금액 |\n| --- | --- |\n| 홍길동 | 1,000 |\n| 김철수 | 2,500 |"
        extension = filename.rsplit(".", 1)[1]
        assert result.warnings == [f"The file extension (.{extension}) does not match the actual format (html), "
                                   "so the file was converted as the actual format."]

    def test_bare_table_rows_served_as_csv_are_html(self):
        data = "<table><tr><td>이름</td><td>금액</td></tr><tr><td>홍길동</td><td>1,000</td></tr></table>".encode()

        result = convert(data, "export.csv")

        assert result.format is DocumentFormat.HTML

    @pytest.mark.parametrize("text", ["<b>굵게</b>,설명\n<i>기울임</i>,설명 2\n", "<table>,<tr>\n1,2\n",
                                      "<html> 태그,설명\n<td>,칸\n", '<meta name="x">,값\n가,나\n'])
    def test_csv_whose_first_cell_starts_with_a_tag_stays_csv(self, text):
        result = convert(text.encode(), "data.csv")

        assert result.format is DocumentFormat.CSV
        assert result.warnings == []

    def test_text_extension_keeps_html_as_text(self):
        """Deliberate: .txt is text by its extension, as both are text formats (the extension warning row in docs/decisions/detect.md)."""
        result = convert(_HTML_TABLE.encode(), "export.txt")

        assert result.format is DocumentFormat.TEXT


class TestTextAfterANestedList:
    """Text that follows a nested list in an HTML list item was rendered after the nested list was detached, so it moved before
    the nested list (joined onto the item's text line). In source order it is item text → sub-items → the text after (a block
    under the item)."""

    def test_loose_text_after_a_nested_list_stays_after_it(self):
        html = ("<ol>\n<li>신청서 작성\n<ul>\n<li>성명</li>\n<li>주소</li>\n</ul>\n작성 후 서명합니다.\n</li>\n"
                "<li>제출</li>\n</ol>")

        result = convert(html.encode(), "a.html")

        assert result.text == "1. 신청서 작성\n  - 성명\n  - 주소\n\n작성 후 서명합니다.\n\n2. 제출"

    def test_paragraph_after_a_nested_list_stays_after_it(self):
        html = ("<ul><li><p>1단계: 준비</p><ul><li>재료 구매</li><li>도구 확인</li></ul><p>준비가 끝나면 다음 단계로 갑니다.</p></li>"
                "<li><p>2단계: 조리</p></li></ul>")

        result = convert(html.encode(), "a.html")

        assert result.text == "- 1단계: 준비\n  - 재료 구매\n  - 도구 확인\n\n준비가 끝나면 다음 단계로 갑니다.\n\n- 2단계: 조리"

    def test_text_between_two_nested_lists_stays_between_them(self):
        html = "<ul><li>상위<ul><li>가</li></ul>가운데 글<ol><li>나</li></ol></li></ul>"

        result = convert(html.encode(), "a.html")

        assert result.text == "- 상위\n  - 가\n\n가운데 글\n\n  1. 나"


class TestPictureInACompactMarkdownTable:
    """For a Markdown table with no spaces around its cells (`|a|b|`), assembly recognized table rows only as lines ending in
    `… |`, so a picture in a cell was handled as a paragraph picture — the cell was empty and the picture block cut the table in
    two. A line that starts and ends with `|` is treated as a table row."""

    def test_picture_in_a_compact_table_cell_stays_in_the_cell(self):
        encoded = base64.b64encode(builder.png_bytes()).decode()
        data = (f"|제품|사진|가격|\n|---|---|---|\n|사과|![사과 사진](data:image/png;base64,{encoded})|1000|\n"
                "|배|없음|2000|\n").encode()

        result = convert(data, "a.md")

        assert result.text == "|제품|사진|가격|\n|---|---|---|\n|사과|[Image: 사과 사진]|1000|\n|배|없음|2000|"


class TestRawEightBitMailHeaders:
    """Headers written as raw 8bit without RFC 2047 encoding (groupware, old mail programs) are wrapped by the parser as
    `unknown-8bit`, and `str()` gives a replacement character per character, so the subject and sender became `������`. They are
    turned back into the original bytes and decoded with the same detection as the body (`decode_text`)."""

    @pytest.mark.parametrize("encoding", ["utf-8", "cp949"])
    def test_raw_subject_and_sender_are_decoded(self, encoding):
        data = ("From: 김철수 <kim@example.com>\nTo: lee@example.com\nSubject: 주간 회의 일정 변경 안내\nMIME-Version: 1.0\n"
                f"Content-Type: text/plain; charset={encoding}\nContent-Transfer-Encoding: 8bit\n\n회의는 3시입니다.\n").encode(encoding)

        result = convert(data, "mail.eml")

        assert result.text == ("Subject: 주간 회의 일정 변경 안내\nFrom: 김철수 <kim@example.com>\nTo: lee@example.com\n\n"
                               "회의는 3시입니다.")

    def test_raw_header_mixed_with_an_encoded_word_is_decoded_whole(self):
        word = "=?utf-8?b?" + base64.b64encode("견적".encode()).decode() + "?="
        data = (f"From: kim@example.com\nSubject: 전달: {word}\nMIME-Version: 1.0\n"
                "Content-Type: text/plain; charset=utf-8\n\n본문\n").encode()

        result = convert(data, "mail.eml")

        assert result.text == "Subject: 전달: 견적\nFrom: kim@example.com\n\n본문"

    def test_backslash_in_a_raw_header_is_kept_as_written(self):
        data = ("From: kim@example.com\nSubject: 경로 C:\\users\\uc804 확인\nMIME-Version: 1.0\n"
                "Content-Type: text/plain; charset=utf-8\n\n본문\n").encode()

        result = convert(data, "mail.eml")

        assert result.text == "Subject: 경로 C:\\users\\uc804 확인\nFrom: kim@example.com\n\n본문"

    @pytest.mark.parametrize("encoding", ["utf-8", "cp949"])
    def test_raw_headers_of_a_forwarded_mail_are_decoded(self, encoding):
        inner = ("From: 박영희 <a@example.com>\nTo: b@example.com\nSubject: 견적 요청\nMIME-Version: 1.0\n"
                 f"Content-Type: text/plain; charset={encoding}\n\n견적 금액은 300만 원입니다.")
        data = ("From: c@example.com\nTo: d@example.com\nSubject: 전달: 견적 요청\nMIME-Version: 1.0\n"
                "Content-Type: multipart/mixed; boundary=OUT\n\n"
                f"--OUT\nContent-Type: text/plain; charset={encoding}\n\n확인 바랍니다.\n"
                f"--OUT\nContent-Type: message/rfc822\n\n{inner}\n--OUT--\n").encode(encoding)

        result = convert(data, "fwd.eml")

        assert result.text == ("Subject: 전달: 견적 요청\nFrom: c@example.com\nTo: d@example.com\n\n확인 바랍니다.\n\n"
                               "## Forwarded message\n\nSubject: 견적 요청\nFrom: 박영희 <a@example.com>\nTo: b@example.com\n\n"
                               "견적 금액은 300만 원입니다.")
