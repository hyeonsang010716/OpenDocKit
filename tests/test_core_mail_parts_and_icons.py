"""Mail: HTML body pieces with an attachment between them (Apple Mail), the warning for an attachment shown in the body, RFC 2047
words with extended Hangul, encoded attachment names, an mbox holding several mails. HTML: SVG data: URI icons."""

import base64
import time

import pytest

from opendockit import ConvertOptions, DocumentFormat, convert
from opendockit.extractors import _mail
from tests import builder, needs_html5_tokenizer


def _encoded_word(value: str, codec: str, label: str) -> str:
    return f"=?{label}?B?{base64.b64encode(value.encode(codec)).decode()}?="


_APPLE_MAIL = """From: kim@icloud.com
Content-Type: multipart/alternative; boundary="ALT"
Mime-Version: 1.0 (Mac OS X Mail 16.0)
Subject: contract
To: lee@corp.co.kr

--ALT
Content-Type: text/plain; charset=utf-8

Hello. Please find the draft contract.

Changes: Article 3.
--ALT
Content-Type: multipart/mixed; boundary="MIX"

--MIX
Content-Type: text/html; charset=us-ascii

<html><body><div>Hello. Please find the draft contract.</div></body></html>
--MIX
Content-Disposition: inline; filename=contract_draft.pdf
Content-Type: application/pdf; name="contract_draft.pdf"
Content-Transfer-Encoding: base64

JVBERi0xLjQK
--MIX
Content-Type: text/html; charset=us-ascii

<html><body><div>Changes: Article 3.</div><div>Best regards, Kim</div></body></html>
--MIX--

--ALT--
"""
_ATTACHMENT_WARNING = "1 mail attachment(s) ({}) were not read. Convert attachments separately."


class TestHtmlBodySplitAroundAnInlineAttachment:
    """Apple Mail sends an attachment placed in the middle of the body as `multipart/mixed > (text/html ①, attachment, text/html ②)`.
    Only the first text/html part was read, so body ② after the attachment vanished without a warning, and a PDF with
    `inline; filename=` was not counted as an attachment, so there was no warning either."""

    @needs_html5_tokenizer
    def test_every_html_body_part_is_read_in_order(self):
        data = _APPLE_MAIL.encode()

        result = convert(data, "contract.eml")

        assert result.format is DocumentFormat.HTML
        assert result.text == ("Subject: contract\nFrom: kim@icloud.com\nTo: lee@corp.co.kr\n\n"
                               "Hello. Please find the draft contract.\n\nChanges: Article 3.\n\nBest regards, Kim")
        assert result.warnings == [_ATTACHMENT_WARNING.format("contract_draft.pdf")]

    def test_inline_attachment_of_a_plain_mail_is_reported(self):
        data = (b"From: kim@icloud.com\nTo: lee@corp.co.kr\nSubject: invoice\nMime-Version: 1.0 (1.0)\n"
                b"Content-Type: multipart/mixed; boundary=M\n\n--M\nContent-Type: text/plain; charset=utf-8\n\nInvoice attached.\n"
                b"--M\nContent-Type: application/pdf; name=invoice_0304.pdf\nContent-Disposition: inline; filename=invoice_0304.pdf\n"
                b"Content-Transfer-Encoding: base64\n\nJVBERi0xLjQK\n--M\nContent-Type: text/plain; charset=us-ascii\n\n"
                b"Please pay by 3/31.\n--M--\n")

        result = convert(data, "a.eml")

        assert result.format is DocumentFormat.TEXT
        assert result.text == ("Subject: invoice\nFrom: kim@icloud.com\nTo: lee@corp.co.kr\n\n"
                               "Invoice attached.\n\nPlease pay by 3/31.")
        assert result.warnings == [_ATTACHMENT_WARNING.format("invoice_0304.pdf")]

    def test_inline_picture_shown_in_the_body_is_not_an_attachment(self):
        picture = base64.encodebytes(builder.png_bytes()).decode()
        data = ("From: c@example.com\nTo: d@example.com\nSubject: photo\nMIME-Version: 1.0\n"
                "Content-Type: multipart/related; boundary=RR\n\n--RR\nContent-Type: text/html; charset=utf-8\n\n"
                '<p>사진입니다.</p><img src="cid:pic1" alt="현장">\n--RR\nContent-Type: image/png; name=photo.png\n'
                "Content-Disposition: inline; filename=photo.png\n"
                f"Content-ID: <pic1>\nContent-Transfer-Encoding: base64\n\n{picture}--RR--\n").encode()

        result = convert(data, "mail.eml")

        assert result.warnings == []
        assert len(result.images) == 1

    def test_inline_text_part_with_a_name_is_still_the_body(self):
        data = ("From: c@example.com\nTo: d@example.com\nSubject: memo\nMIME-Version: 1.0\n"
                "Content-Type: multipart/mixed; boundary=M\n\n--M\nContent-Type: text/plain; charset=utf-8\n\n머리 글\n"
                "--M\nContent-Type: text/plain; charset=utf-8; name=note.txt\nContent-Disposition: inline; filename=note.txt\n\n"
                "펼쳐 보인 글\n--M--\n").encode()

        result = convert(data, "mail.eml")

        assert result.text == "Subject: memo\nFrom: c@example.com\nTo: d@example.com\n\n머리 글\n\n펼쳐 보인 글"
        assert result.warnings == []


class TestKoreanEncodedWordsWithExtendedHangul:
    """When an Outlook `=?ks_c_5601-1987?B?…?=` word (labeled EUC-KR) holds extended Hangul (됬, 똠), the EUC-KR codec cannot decode
    it, so the subject and sender stayed as encoded words. They are decoded as CP949, like the body."""

    @pytest.mark.parametrize("label", ["ks_c_5601-1987", "euc-kr", "EUC-KR", "cp949"])
    def test_subject_and_sender_with_extended_hangul_are_decoded(self, label):
        data = (f"From: {_encoded_word('홍길동', 'cp949', label)} <hong@corp.co.kr>\nTo: team@corp.co.kr\n"
                f"Subject: {_encoded_word('결재 완료됬습니다 - 똠얌꿍', 'cp949', label)}\nMIME-Version: 1.0\n"
                f'Content-Type: text/plain; charset="{label}"\nContent-Transfer-Encoding: base64\n\n'
                f"{base64.encodebytes('본문입니다.'.encode('cp949')).decode()}").encode()

        result = convert(data, "a.eml")

        assert result.text.splitlines()[:2] == ["Subject: 결재 완료됬습니다 - 똠얌꿍", "From: 홍길동 <hong@corp.co.kr>"]

    def test_encoded_word_next_to_plain_words_keeps_the_spacing(self):
        data = (f"From: a@example.com\nTo: b@example.com\nSubject: RE: {_encoded_word('완료됬음', 'cp949', 'euc-kr')} (3월)\n"
                "MIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\n본문\n").encode()

        result = convert(data, "a.eml")

        assert result.text.splitlines()[0] == "Subject: RE: 완료됬음 (3월)"

    def test_broken_encoded_word_is_left_as_written(self):
        data = ("From: a@example.com\nTo: b@example.com\nSubject: =?euc-kr?B?sA==?=\n"
                "MIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\n본문\n").encode()

        result = convert(data, "a.eml")

        assert result.text.startswith("Subject: ")
        assert result.text.splitlines()[-1] == "본문"


class TestEncodedAttachmentName:
    """Gmail writes a Korean attachment name as `filename="=?UTF-8?B?…?="` instead of RFC 2231. It appeared in the warning as
    encoded words."""

    def test_attachment_name_in_an_encoded_word_is_decoded_in_the_warning(self):
        name = _encoded_word("견적서.xlsx", "utf-8", "UTF-8")
        data = ("From: a@example.com\nTo: b@example.com\nSubject: quote\nMIME-Version: 1.0\n"
                "Content-Type: multipart/mixed; boundary=M\n\n--M\nContent-Type: text/plain; charset=utf-8\n\n견적 보내드립니다.\n"
                f'--M\nContent-Type: application/octet-stream; name="{name}"\nContent-Disposition: attachment; filename="{name}"\n'
                "Content-Transfer-Encoding: base64\n\nUEsDBAoAAAAAAA==\n--M--\n").encode()

        result = convert(data, "a.eml")

        assert result.warnings == [_ATTACHMENT_WARNING.format("견적서.xlsx")]


def _mbox_message(number: int, *, html: bool) -> str:
    if html:
        content = (f'Content-Type: multipart/alternative; boundary="b{number}"\n\n--b{number}\nContent-Type: text/plain; '
                   f"charset=utf-8\n\n본문 {number} 평문\n--b{number}\nContent-Type: text/html; charset=utf-8\n\n"
                   f"<p>본문 {number} HTML</p>\n--b{number}--\n")
    else:
        content = f"Content-Type: text/plain; charset=utf-8\n\n본문 {number} 평문\n"
    return (f"From kim@example.com Mon Sep  1 10:0{number}:00 2025\nFrom: kim@example.com\nTo: lee@example.com\n"
            f"Subject: 메일 {number}\nMIME-Version: 1.0\n{content}\n")


def _mbox(*kinds: bool) -> bytes:
    return "".join(_mbox_message(number, html=html) for number, html in enumerate(kinds, start=1)).encode()


def _mbox_head(number: int) -> str:
    return f"## Message {number}\n\nSubject: 메일 {number}\nFrom: kim@example.com\nTo: lee@example.com"


class TestMailboxWithSeveralMails:
    """Of an mbox holding several mails in a row only the first was read: with multipart mail the later mails were discarded as the
    epilogue of the first, and with plain-text mail the mbox separator lines and headers (MIME-Version, Content-Type) of the later
    mails became body. Each mail is read as `## Message N` + headers + body."""

    @pytest.mark.parametrize("filename", ["box.mbox", "box.eml", None])
    def test_every_plain_mail_is_read_with_its_own_head(self, filename):
        data = _mbox(False, False, False)

        result = convert(data, filename)

        assert result.format is DocumentFormat.TEXT
        assert result.text == "\n\n".join(f"{_mbox_head(number)}\n\n본문 {number} 평문" for number in (1, 2, 3))
        assert result.warnings == []

    def test_every_html_mail_is_read_with_its_own_head(self):
        data = _mbox(True, True, True)

        result = convert(data, "box.mbox")

        assert result.format is DocumentFormat.HTML
        assert result.text == "\n\n".join(f"{_mbox_head(number)}\n\n본문 {number} HTML" for number in (1, 2, 3))

    def test_html_mail_after_a_plain_one_makes_the_box_html(self):
        data = _mbox(False, True)

        result = convert(data, "box.mbox")

        assert result.format is DocumentFormat.HTML
        assert result.text == f"{_mbox_head(1)}\n\n본문 1 평문\n\n{_mbox_head(2)}\n\n본문 2 HTML"

    def test_body_line_that_looks_like_a_separator_does_not_split_the_mail(self):
        data = ("From kim@example.com Mon Sep  1 10:00:00 2025\nFrom: kim@example.com\nTo: lee@example.com\nSubject: 회의록\n"
                "MIME-Version: 1.0\nContent-Type: text/plain; charset=utf-8\n\n첫 줄\n"
                "From kim@example.com Mon Sep  1 10:00:00 2025 에 보낸 메일 참고\n끝 줄\n").encode()

        result = convert(data, "box.mbox")

        assert result.text == ("Subject: 회의록\nFrom: kim@example.com\nTo: lee@example.com\n\n첫 줄\n"
                               "From kim@example.com Mon Sep  1 10:00:00 2025 에 보낸 메일 참고\n끝 줄")

    def test_single_mail_after_a_separator_line_has_no_number(self):
        data = _mbox(False)

        result = convert(data, "a.eml")

        assert result.text == "Subject: 메일 1\nFrom: kim@example.com\nTo: lee@example.com\n\n본문 1 평문"

    @pytest.mark.parametrize("html", [False, True])
    def test_mails_beyond_the_limit_are_reported(self, monkeypatch, html):
        monkeypatch.setattr(_mail, "MAX_BOX_MAILS", 2)
        data = _mbox(html, html, html)

        result = convert(data, "box.mbox")

        assert "## Message 2" in result.text
        assert "## Message 3" not in result.text
        assert "메일 3" not in result.text
        assert result.warnings == ["The mailbox (mbox) holds 3 messages, but only the first 2 were read. "
                                   "Split the rest off and convert it separately."]


def _html_mbox(*bodies: str) -> bytes:
    return "".join(f"From kim@example.com Mon Sep  1 10:00:00 2025\nFrom: kim@example.com\nTo: lee@example.com\n"
                   f"Subject: 메일 {number}\nMIME-Version: 1.0\nContent-Type: text/html; charset=utf-8\n\n{body}\n\n"
                   for number, body in enumerate(bodies, start=1)).encode()


def _html_parts(*bodies: str) -> bytes:
    """One mail whose body is several HTML parts (multipart/mixed)."""
    parts = "".join(f"--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n{body}\r\n" for body in bodies)
    return (f"From: kim@example.com\r\nTo: lee@example.com\r\nSubject: 조각\r\nMIME-Version: 1.0\r\n"
            f"Content-Type: multipart/mixed; boundary=B\r\n\r\n{parts}--B--\r\n").encode()


class TestMailsAreRenderedApart:
    """The mails of an mbox, their forwarded mails and body parts were joined into one HTML and parsed once: an unclosed
    comment, style, title, attribute quote or hidden element in one mail swallowed every later mail without a warning, and
    an unclosed quote or pre took the later mails into it. Each is a document of its own, as a mail program shows it."""

    @pytest.mark.parametrize("opened", ["<!-- tracking", "<style>p {}", "<script>x = 1", "<title>제목", '<a href="x',
                                        '<div style="display:none">미리보기', "<blockquote>인용", "<pre>코드", "<p><del>삭제",
                                        "<ul><li>항목"])
    def test_what_one_mail_leaves_open_does_not_reach_the_next(self, opened):
        data = _html_mbox(f"<p>첫 메일</p>{opened}", "<p>둘째 메일</p>", "<p>셋째 메일</p>")

        result = convert(data, "box.mbox")

        text = result.text
        assert f"{_mbox_head(2)}\n\n둘째 메일\n\n{_mbox_head(3)}\n\n셋째 메일" in text
        assert text.endswith(f"{_mbox_head(3)}\n\n셋째 메일")

    def test_forwarded_mail_after_an_unclosed_comment_is_read(self):
        data = ('From: kim@example.com\r\nTo: lee@example.com\r\nSubject: 전달\r\nMIME-Version: 1.0\r\n'
                'Content-Type: multipart/mixed; boundary="X"\r\n\r\n--X\r\nContent-Type: text/html; charset=utf-8\r\n\r\n'
                '<p>아래 참고</p><!-- 서명\r\n--X\r\nContent-Type: message/rfc822\r\n\r\nFrom: park@example.com\r\n'
                'To: lee@example.com\r\nSubject: 계약\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n'
                '계약 금액은 5,000,000원입니다.\r\n--X--\r\n').encode()

        result = convert(data, "a.eml")

        assert result.text.endswith("아래 참고\n\n## Forwarded message\n\nSubject: 계약\nFrom: park@example.com\n"
                                    "To: lee@example.com\n\n계약 금액은 5,000,000원입니다.")

    def test_limit_counts_across_the_mails(self):
        data = _html_mbox(*(f"<p>{'가' * 300} {number}</p>" for number in range(1, 11)))

        whole = convert(data, "box.mbox").text
        result = convert(data, "box.mbox", ConvertOptions(max_chars=1000))

        assert result.exceeded
        assert result.text == whole[:1000].rstrip()

    def test_pictures_are_numbered_on_across_the_mails(self):
        mails = ""
        for number in (1, 2):
            mails += (f"From kim@example.com Mon Sep  1 10:00:00 2025\nFrom: kim@example.com\nTo: lee@example.com\n"
                      f"Subject: 메일 {number}\nMIME-Version: 1.0\nContent-Type: multipart/related; boundary=\"R\"\n\n"
                      f"--R\nContent-Type: text/html; charset=utf-8\n\n<p>본문 {number}</p><img src=\"cid:p{number}\">\n"
                      f"--R\nContent-Type: image/png\nContent-ID: <p{number}>\nContent-Transfer-Encoding: base64\n\n"
                      f"{base64.b64encode(builder.png_bytes(seed=number)).decode()}\n--R--\n\n")

        result = convert(mails.encode(), "box.mbox")

        assert [image.id for image in result.images] == [1, 2]
        assert result.text.endswith("본문 1\n\n[Image]\n\n" f"{_mbox_head(2)}\n\n본문 2\n\n[Image]")

    def test_what_the_body_leaves_open_does_not_reach_the_invitation(self):
        """The invitation is a document of its own, not the end of the last body part."""
        data = ("From: kim@example.com\nTo: lee@example.com\nSubject: 회의\nMIME-Version: 1.0\n"
                "Content-Type: multipart/mixed; boundary=b\n\n--b\nContent-Type: text/html; charset=utf-8\n\n"
                "<p>초대합니다</p><!-- 서명\n"
                "--b\nContent-Type: text/calendar; charset=utf-8; method=REQUEST\n\n"
                "BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:주간 회의\nDTSTART:20250901T100000\nEND:VEVENT\nEND:VCALENDAR\n"
                "--b--\n").encode()

        result = convert(data, "a.eml")

        assert result.text.endswith("초대합니다\n\nEvent:\n\n- Title: 주간 회의\n- Start: 2025-09-01 10:00")

    @pytest.mark.parametrize("body", ["<tr><td>가</td><td>나</td></tr><tr><td>다</td><td>라</td></tr>",
                                      "<html><head><title>뉴스레터</title></head><body><p>본문</p></body></html>"])
    def test_header_block_of_a_mail_is_a_document_of_its_own(self, body):
        """Put in front of the first body part, the header block made the bare rows of a body something other than a table,
        and the `<head>` of a body content of a page."""
        data = _html_mbox("<p>첫 메일</p>", body)
        alone = convert(_html_mbox(body), "a.eml")

        result = convert(data, "box.mbox")

        assert result.text.endswith(f"{_mbox_head(2)}\n\n" + alone.text.split("\n\n", 1)[1])
        assert result.metadata.get("title") == alone.metadata.get("title")

    def test_title_is_the_first_one_of_the_mails(self):
        data = _html_mbox("<p>제목 없는 메일</p>", "<title>둘째 제목</title><p>가</p>", "<title>셋째 제목</title><p>나</p>")

        result = convert(data, "box.mbox")

        assert result.metadata["title"] == "둘째 제목"

    def test_title_of_a_part_after_the_limit_is_still_found(self):
        """The mails after the limit are not read, but the title used to be found when all mails were one document."""
        data = _html_mbox(f"<p>{'가' * 300}</p>", "<svg><title>아이콘</title></svg><p>나</p>",
                          "<html><head><title>셋째 제목</title></head><body><p>다</p><title>넷째",
                          "<title>다섯째 제목</title>")

        result = convert(data, "box.mbox", ConvertOptions(max_chars=100))

        assert result.exceeded
        assert "나" not in result.text
        assert result.metadata["title"] == "셋째 제목"

    def test_title_of_a_part_after_the_limit_is_the_one_reading_it_gives(self):
        """A title that is never closed is the title of a part that is read, so it is of one that is not."""
        bodies = ["<p>나</p><title>닫히지 않은 제목"]
        read = convert(_html_mbox(*bodies), "a.eml").metadata["title"]

        result = convert(_html_mbox(f"<p>{'가' * 300}</p>", *bodies), "box.mbox", ConvertOptions(max_chars=100))

        assert result.exceeded
        assert result.metadata["title"] == read == "닫히지 않은 제목"

    def test_title_cut_off_where_the_look_for_a_title_ends_is_not_a_title(self, monkeypatch):
        from opendockit.extractors import html as html_extractor

        monkeypatch.setattr(html_extractor, "_UNREAD_TITLE_CHARS", 60)
        data = _html_parts(f"<p>{'가' * 300}</p>", "<svg><title>아이콘</title></svg><title>" + "긴 제목 " * 20 + "</title>",
                           "<title>보지 않는 제목</title>")

        result = convert(data, "a.eml", ConvertOptions(max_chars=100))

        assert result.exceeded
        assert "title" not in result.metadata

    _LONG_PART = "<p>가나다라마 바사아자차</p>\n" * 600

    @pytest.mark.parametrize("behind", ["<title>둘째 제목</title>", "<title> </title>", "<!-- <title>주석</title> -->",
                                        "<svg><title>아이콘</title></svg>"])
    def test_title_behind_the_front_part_of_the_part_over_the_limit_is_the_title(self, monkeypatch, behind):
        """Only the parts after the one that crossed the limit were looked at: a part read as a front part whose own title
        lay behind that front part gave the mail the title of the next part."""
        data = _html_parts("<p>첫 조각</p>", f"<body>{self._LONG_PART}{behind}{self._LONG_PART}</body>",
                           "<html><head><title>셋째 제목</title></head><body><p>다</p></body></html>")
        whole = convert(data, "a.eml", ConvertOptions(max_chars=None)).metadata.get("title")
        texts = self._documents_read(monkeypatch)

        result = convert(data, "a.eml", ConvertOptions(max_chars=500))

        assert result.exceeded
        assert max(map(len, texts)) < len(self._LONG_PART)  # the part was read as a front part that ends before `behind`
        assert result.metadata.get("title") == whole == ("둘째 제목" if "둘째" in behind else "셋째 제목")

    def test_title_of_a_later_part_is_not_taken_when_the_part_over_the_limit_was_not_looked_at_to_its_end(self, monkeypatch):
        from opendockit.extractors import html as html_extractor

        monkeypatch.setattr(html_extractor, "_UNREAD_TITLE_CHARS", 1_000)
        data = _html_parts("<p>첫 조각</p>", f"<body>{self._LONG_PART}<title>둘째 제목</title></body>",
                           "<html><head><title>셋째 제목</title></head><body><p>다</p></body></html>")

        result = convert(data, "a.eml", ConvertOptions(max_chars=500))

        assert result.exceeded
        assert convert(data, "a.eml", ConvertOptions(max_chars=None)).metadata["title"] == "둘째 제목"
        assert "title" not in result.metadata

    def test_title_after_a_long_part_without_one_is_still_found(self, monkeypatch):
        """A part that holds no `<title` has no title however long it is, so the look goes on behind it."""
        from opendockit.extractors import html as html_extractor

        monkeypatch.setattr(html_extractor, "_UNREAD_TITLE_CHARS", 1_000)
        data = _html_parts("<p>첫 조각</p>", f"<body>{self._LONG_PART * 2}</body>", self._LONG_PART,
                           "<html><head><title>넷째 제목</title></head><body><p>다</p></body></html>")

        result = convert(data, "a.eml", ConvertOptions(max_chars=500))

        assert result.exceeded
        assert result.metadata["title"] == "넷째 제목"

    def test_end_of_head_far_into_a_part_does_not_size_its_front_part(self, monkeypatch):
        """The end of `<head>` is looked for in the start of a document only: a stray `</head>` deep in the body made the
        front part reach it, and all the text before it was parsed to exceed a limit its first lines exceed."""
        from opendockit.extractors import html as html_extractor

        monkeypatch.setattr(html_extractor, "_HEAD_SEARCH_CHARS", 1_000)
        data = _html_parts(f"<body>{self._LONG_PART}</head>{self._LONG_PART * 2}</body>")
        whole = convert(data, "a.eml", ConvertOptions(max_chars=None)).text
        texts = self._documents_read(monkeypatch)

        result = convert(data, "a.eml", ConvertOptions(max_chars=500))

        assert result.exceeded
        assert result.text == whole[:500].rstrip()
        assert max(map(len, texts)) < len(self._LONG_PART) // 2

    def test_warning_of_several_documents_is_given_once(self):
        broken = ("<p>" + "안녕하십니까 회의록입니다 " * 20 + "</p>").encode("cp949").replace(
            "회의록".encode("cp949"), "회의록".encode("cp949") + b"\xc9\xa1", 1)
        part = b"--b\nContent-Type: text/html\n\n"
        data = (b"From: kim@example.com\nTo: lee@example.com\nSubject: parts\nMIME-Version: 1.0\n"
                b"Content-Type: multipart/mixed; boundary=b\n\n" + part + broken + b"\n" + part + b"<p>second part</p>\n"
                + part + b"<p>third part</p>\n--b--\n")

        result = convert(data, "a.eml")

        assert "third part" in result.text
        assert result.warnings == ["The text was read as CP949, but some bytes could not be decoded and were replaced with \ufffd."]

    def test_rows_left_out_of_tables_are_added_up_across_the_documents(self, monkeypatch):
        monkeypatch.setattr("opendockit.markdown._MAX_GRID_CELLS", 20)
        table = "<table><tr><th>이름</th><th>값</th></tr>{}</table>"
        rows = ["".join(f"<tr><td>행{index}</td><td>{index}</td></tr>" for index in range(count)) for count in (30, 12)]
        data = _html_mbox(table.format(rows[0]), "<p>표 없음</p>", table.format(rows[1]))

        result = convert(data, "box.mbox")

        assert result.warnings == ["The table is too large; the bottom 24 row(s) could not be converted."]

    def test_rows_left_out_of_a_front_part_that_exceeds_the_limit_are_reported(self, monkeypatch):
        monkeypatch.setattr("opendockit.markdown._MAX_GRID_CELLS", 20)
        rows = "".join(f"<tr><td>행{index}</td><td>{index}</td></tr>" for index in range(30))
        body = f"<table><tr><th>이름</th><th>값</th></tr>{rows}</table>" + f"<p>{'가' * 50}</p>" * 200
        texts = self._documents_read(monkeypatch)

        result = convert(_html_parts(body), "a.eml", ConvertOptions(max_chars=1_000))

        assert result.exceeded
        assert max(map(len, texts)) < len(body)
        assert result.warnings[0] == "The table is too large; the bottom 21 row(s) could not be converted."
        assert len(result.warnings) == 2  # and the warning that the conversion stopped at the limit

    @staticmethod
    def _documents_read(monkeypatch) -> list[str]:
        """Every document, or front part of one, handed to the HTML renderer."""
        from opendockit.extractors import html as html_extractor

        texts: list[str] = []
        original = html_extractor._extract

        def recording(text, *args, **kwargs):
            texts.append(text)
            return original(text, *args, **kwargs)

        monkeypatch.setattr(html_extractor, "_extract", recording)
        return texts

    @pytest.mark.parametrize("first", [f"<p>{'가' * 300}</p>", "<p>" + "<span>가나다라마</span>" * 2_000 + "</p>"],
                             ids=["counted-after-reading", "front-part"])
    def test_documents_after_the_limit_are_not_read(self, monkeypatch, first):
        texts = self._documents_read(monkeypatch)
        data = _html_mbox(first, "<p>둘째 메일</p>", "<p>셋째 메일</p>")

        result = convert(data, "box.mbox", ConvertOptions(max_chars=100))

        assert result.exceeded
        assert texts and all(first.startswith(text.strip()) for text in texts)
        assert "Message 2" not in result.text

    _SPARSE_ROW = '<p style="margin:0;padding:0;font-family:sans-serif;font-size:12px;line-height:1.5;color:#333333">본문</p>\n'
    _LARGE_PART = ("<html><head><style>" + "p{color:red;} " * 250 + "</style></head><body>"
                   + '<div style="margin:0;padding:0">' * 60 + _SPARSE_ROW * 8_000 + "</body></html>")

    @classmethod
    def _mail_whose_large_part_comes_late(cls, shape: str) -> tuple[bytes, str, int]:
        """(a mail whose last body part is large and starts with an ordinary `<head><style>` and, as mail bodies do, a
        stretch of markup without text, its file name, a character limit that the parts before it leave little of)."""
        if shape == "small-limit":
            return _html_parts("<p>짧은 글</p>", cls._LARGE_PART), "a.eml", 300
        filler = f"<p>{'가나다라마바사아자 ' * 2_200}</p>"  # 19,800 characters without the spaces
        if shape == "mailbox":
            return _html_mbox(filler, cls._LARGE_PART), "box.mbox", 20_000
        return _html_parts(filler, cls._LARGE_PART), "a.eml", 20_000

    @pytest.mark.parametrize("shape", ["limit-nearly-used", "mailbox", "small-limit"])
    def test_large_part_over_what_is_left_of_the_limit_is_not_parsed_whole(self, monkeypatch, shape):
        """The front part of each document was sized from what was left of the limit: a few hundred characters, which an
        ordinary `<head><style>` fills, so no text was found in it and the whole part was parsed only to be cut off."""
        data, name, limit = self._mail_whose_large_part_comes_late(shape)
        whole = convert(data, name, ConvertOptions(max_chars=None)).text
        texts = self._documents_read(monkeypatch)

        result = convert(data, name, ConvertOptions(max_chars=limit))

        assert result.exceeded
        assert result.text == whole[:limit].rstrip()
        assert max(len(text) for text in texts if text.startswith("<html><head><style>")) < len(self._LARGE_PART) // 4

    @pytest.mark.timing
    @pytest.mark.parametrize("shape", ["limit-nearly-used", "small-limit"])
    def test_large_part_over_what_is_left_of_the_limit_takes_little_time(self, monkeypatch, shape):
        """A 21.6MB mail whose first part nearly used up the limit took 12.9 s and 1.3GB (1.5 s before the parts were read
        apart). Here 16MB: 4.8 s parsed whole, under 1 s when only the front of the large part is read."""
        from tests.timing import allowed

        monkeypatch.setattr(TestMailsAreRenderedApart, "_LARGE_PART", self._LARGE_PART.replace(
            self._SPARSE_ROW * 8_000, self._SPARSE_ROW * 150_000))
        data, name, limit = self._mail_whose_large_part_comes_late(shape)

        started = time.perf_counter()
        result = convert(data, name, ConvertOptions(max_chars=limit))
        seconds = time.perf_counter() - started

        assert result.exceeded
        assert seconds < allowed(2.0)

    @pytest.mark.timing
    def test_mail_with_many_body_parts_takes_linear_time(self):
        def seconds(count: int) -> float:
            parts = "".join(f"--B\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>조각 {number}</p>\r\n"
                            for number in range(count))
            data = (f"From: kim@example.com\r\nTo: lee@example.com\r\nSubject: 조각\r\nMIME-Version: 1.0\r\n"
                    f"Content-Type: multipart/mixed; boundary=B\r\n\r\n{parts}--B--\r\n").encode()
            started = time.perf_counter()
            result = convert(data, "a.eml")
            assert f"조각 {count - 1}" in result.text
            return time.perf_counter() - started

        small, large = seconds(250), seconds(1_000)

        assert large < 4 * small * 2 + 0.5


_SVG_ICON = base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16"><circle r="6"/></svg>').decode()


class TestSvgDataUriIcons:
    """A `data:image/svg+xml` icon is decoration like an `<svg>` placed in the body, but it was registered as a picture, so each
    icon left an `[Image]` and raised an off-target warning about 'WMF/EMF vectors, etc.' (the vision model cannot read SVG). It is
    removed like `<svg>` and only its alt text is left."""

    def test_svg_icons_leave_no_placeholder_and_no_warning(self):
        icon = f'<img src="data:image/svg+xml;base64,{_SVG_ICON}" width="16" height="16">'
        data = f"<html><body><ul><li>{icon} 메뉴 1</li><li>{icon} 메뉴 2</li></ul><p>{icon} 공지</p></body></html>".encode()

        result = convert(data, "page.html")

        assert result.text == "- 메뉴 1\n- 메뉴 2\n\n공지"
        assert result.warnings == []
        assert result.images == []

    def test_alt_text_of_an_svg_picture_is_kept(self):
        data = '<html><body><p><img src="data:image/svg+xml;utf8,<svg/>" alt="조직도"> 아래 참고</p></body></html>'.encode()

        result = convert(data, "page.html")

        assert result.text == "[Image: 조직도] 아래 참고"
        assert result.warnings == []


_INVITE_CALENDAR = (
    "BEGIN:VCALENDAR\r\nMETHOD:REQUEST\r\nBEGIN:VTIMEZONE\r\nTZID:Korea Standard Time\r\nEND:VTIMEZONE\r\nBEGIN:VEVENT\r\n"
    "SUMMARY;LANGUAGE=ko-KR:2분기 예산 검토\r\nDTSTART;TZID=Korea Standard Time:20240307T150000\r\n"
    "DTEND;TZID=Korea Standard Time:20240307T160000\r\nLOCATION:본사 3층 대회의실\r\n"
    "ORGANIZER;CN=홍길동:mailto:hong@x.com\r\nATTENDEE;ROLE=REQ-PARTICIPANT;CN=\"김철수\":mailto:kim@x.com\r\n"
    "ATTENDEE;CN=이영희:MAILTO:lee@x.com\r\nDESCRIPTION:자료 미리 봐 주세요.\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
_INVITE_BLOCK = ("Event:\n\n- Title: 2분기 예산 검토\n- Start: 2024-03-07 15:00 (Korea Standard Time)\n"
                 "- End: 2024-03-07 16:00 (Korea Standard Time)\n- Location: 본사 3층 대회의실\n- Organizer: 홍길동 <hong@x.com>\n"
                 "- Attendees: 김철수 <kim@x.com>, 이영희 <lee@x.com>")


def _invite(calendar: str, *, html: bool = True, calendar_head: str = 'text/calendar; method=REQUEST; charset="utf-8"') -> bytes:
    encoded = base64.encodebytes(calendar.encode()).decode()
    html_part = ('--ALT\nContent-Type: text/html; charset="utf-8"\n\n<html><body><p>예산 검토 회의입니다.</p></body></html>\n'
                 if html else "")
    return (f"From: hong@x.com\nTo: kim@x.com\nSubject: meeting\nMIME-Version: 1.0\n"
            f'Content-Type: multipart/alternative; boundary="ALT"\n\n'
            f'--ALT\nContent-Type: text/plain; charset="utf-8"\n\n예산 검토 회의입니다.\n'
            f"{html_part}"
            f"--ALT\nContent-Type: {calendar_head}\nContent-Transfer-Encoding: base64\n\n{encoded}\n--ALT--\n").encode()


class TestMeetingInvitation:
    """The date/time and location of an Outlook meeting invitation (alternative = text/plain + text/html + text/calendar
    method=REQUEST) vanished without a warning. The title, start, end (time zone as is), location, organizer and attendees of the
    text/calendar part are placed after the body as an `Event:` block."""

    @pytest.mark.parametrize("html", [True, False])
    @needs_html5_tokenizer
    def test_invitation_details_follow_the_body(self, html):
        result = convert(_invite(_INVITE_CALENDAR, html=html), "invite.eml")

        assert result.text.endswith("예산 검토 회의입니다.\n\n" + _INVITE_BLOCK)
        assert result.warnings == []

    @needs_html5_tokenizer
    def test_folded_lines_escapes_utc_and_all_day_times_are_readable(self):
        calendar = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:분기 \r\n 워크숍\\, 1일차\r\nDTSTART:20240307T060000Z\r\n"
                    "DTEND;VALUE=DATE:20240308\r\nLOCATION:제주\\; 본관\\nA동\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nSUMMARY:2일차\r\n"
                    "DTSTART;VALUE=DATE:20240308\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")

        result = convert(_invite(calendar), "invite.eml")

        assert result.text.endswith("Event:\n\n- Title: 분기 워크숍, 1일차\n- Start: 2024-03-07 06:00 UTC\n- End: 2024-03-08\n"
                                    "- Location: 제주; 본관 A동\n\nEvent:\n\n- Title: 2일차\n- Start: 2024-03-08")

    @needs_html5_tokenizer
    def test_colon_inside_a_quoted_parameter_does_not_split_the_line(self):
        calendar = ('BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART;TZID="(UTC+09:00) 서울":20240307T150000\r\n'
                    'ORGANIZER;CN="홍길동: 팀장":mailto:hong@x.com\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n')

        result = convert(_invite(calendar), "invite.eml")

        assert result.text.endswith("Event:\n\n- Start: 2024-03-07 15:00 ((UTC+09:00) 서울)\n- Organizer: 홍길동: 팀장 <hong@x.com>")

    @needs_html5_tokenizer
    def test_cancelled_meeting_is_marked(self):
        calendar = _INVITE_CALENDAR.replace("METHOD:REQUEST", "METHOD:CANCEL")

        result = convert(_invite(calendar), "invite.eml")

        assert "Event (cancelled):\n\n- Title: 2분기 예산 검토" in result.text

    @pytest.mark.parametrize("calendar", ["BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n", "아무 글", "BEGIN:VEVENT\r\nSUMMARY:"])
    def test_calendar_without_a_readable_event_adds_nothing(self, calendar):
        result = convert(_invite(calendar), "invite.eml")

        assert result.text.endswith("예산 검토 회의입니다.")
        assert result.warnings == []

    def test_named_ics_attachment_is_still_only_a_warning(self):
        data = _invite(_INVITE_CALENDAR, calendar_head='text/calendar; charset="utf-8"; name="invite.ics"\n'
                                                       'Content-Disposition: attachment; filename="invite.ics"')

        result = convert(data, "invite.eml")

        assert "Event:" not in result.text
        assert result.warnings == ["1 mail attachment(s) (invite.ics) were not read. Convert attachments separately."]


def _signed_mail(signature_type: str, name: str, *, html: bool = False, extra: str = "") -> bytes:
    body = "<p>서명된 본문입니다.</p>" if html else "서명된 본문입니다."
    return (f'From: a@b.com\r\nTo: c@d.com\r\nSubject: signed\r\nMIME-Version: 1.0\r\n'
            f'Content-Type: multipart/signed; protocol="{signature_type}"; micalg=sha-256; boundary="S"\r\n\r\n'
            f'--S\r\nContent-Type: text/{"html" if html else "plain"}; charset=utf-8\r\n\r\n{body}\r\n'
            f'--S\r\nContent-Type: {signature_type}; name="{name}"\r\nContent-Disposition: attachment; filename="{name}"\r\n'
            f'Content-Transfer-Encoding: base64\r\n\r\nMIAGCSqGSIb3DQEHAqCAMIACAQEx\r\n{extra}--S--\r\n').encode()


class TestSignaturesAndTnefAttachments:
    """S/MIME and PGP signatures (smime.p7s) got the 'Convert attachments separately' warning — a signature is not content.
    Outlook's winmail.dat (application/ms-tnef) is a format that cannot be converted separately, so the same warning was wrong
    advice."""

    @pytest.mark.parametrize(("signature_type", "name"), [("application/pkcs7-signature", "smime.p7s"),
                                                          ("application/x-pkcs7-signature", "smime.p7s"),
                                                          ("application/pgp-signature", "signature.asc")])
    @pytest.mark.parametrize("html", [False, True], ids=["plain", "html"])
    def test_signature_part_is_not_reported_as_an_unread_attachment(self, signature_type, name, html):
        data = _signed_mail(signature_type, name, html=html)

        result = convert(data, "s.eml")

        assert result.text.endswith("서명된 본문입니다.")
        assert result.warnings == []

    def test_other_attachments_beside_a_signature_are_still_reported(self):
        extra = ('--S\r\nContent-Type: application/pdf; name="contract.pdf"\r\nContent-Disposition: attachment; filename="contract.pdf"\r\n'
                 'Content-Transfer-Encoding: base64\r\n\r\nJVBERi0=\r\n')
        data = _signed_mail("application/pkcs7-signature", "smime.p7s", extra=extra)

        result = convert(data, "s.eml")

        assert result.warnings == ["1 mail attachment(s) (contract.pdf) were not read. Convert attachments separately."]

    def test_tnef_part_gets_a_warning_that_it_cannot_be_converted(self):
        data = _signed_mail("application/ms-tnef", "winmail.dat")

        result = convert(data, "s.eml")

        assert result.warnings == ["1 Outlook-only attachment(s) (winmail.dat) cannot be read. Attachments or formatted body text "
                                   "inside may be missing."]
