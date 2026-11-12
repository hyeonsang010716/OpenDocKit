"""Mail (.eml) headers and attachments, and detection of JSON and source code that hold HTML strings."""

import base64
import json
import os
import time

import pytest

from opendockit import DocumentFormat, convert
from opendockit.detect import detect_format


_SUBJECT = "=?utf-8?b?" + base64.b64encode("주간 회의 일정 변경".encode()).decode() + "?="
_HTML = "<html><body><p>회의는 3시에 시작합니다.</p></body></html>"


def _single_html_mail(encoding: str) -> bytes:
    body = base64.encodebytes(_HTML.encode()).decode() if encoding == "base64" else _HTML
    return (f"From: kim@example.com\r\nTo: lee@example.com\r\nSubject: {_SUBJECT}\r\nDate: Mon, 1 Sep 2025 10:00:00 +0900\r\n"
            f"MIME-Version: 1.0\r\nContent-Type: text/html; charset=utf-8\r\nContent-Transfer-Encoding: {encoding}\r\n\r\n"
            f"{body}").encode()


class TestSinglePartHtmlMail:
    """A mail whose body is a single text/html part was caught by MHTML detection (text/html Content-Type) and lost its subject,
    sender and date. It is told apart from a web page saved by IE or Chrome (`From: <Saved by …>`, no recipient) by the mail headers."""

    @pytest.mark.parametrize("encoding", ["base64", "8bit"])
    @pytest.mark.parametrize("filename", ["mail.eml", None])
    def test_mail_headers_are_kept(self, encoding, filename):
        data = _single_html_mail(encoding)

        result = convert(data, filename)

        assert result.format is DocumentFormat.HTML
        assert result.text == ("Subject: 주간 회의 일정 변경\nFrom: kim@example.com\nTo: lee@example.com\n"
                               "Date: Mon, 1 Sep 2025 10:00:00 +0900\n\n회의는 3시에 시작합니다.")

    def test_web_page_saved_by_a_browser_has_no_mail_header_block(self):
        data = ("From: <Saved by Windows Internet Explorer 8>\r\nSubject: 공지\r\nDate: Mon, 1 Sep 2025 10:00:00 +0900\r\n"
                "MIME-Version: 1.0\r\nContent-Type: text/html; charset=utf-8\r\nContent-Transfer-Encoding: 8bit\r\n\r\n"
                f"{_HTML}").encode()

        result = convert(data, "page.mht")

        assert result.format is DocumentFormat.HTML
        assert result.text == "회의는 3시에 시작합니다."

    def test_html_mail_with_an_inline_picture_keeps_its_headers(self):
        """An HTML mail with a photo placed in its body is multipart/related. It is read as MHTML and keeps its headers."""
        data = ("From: kim@example.com\r\nTo: lee@example.com\r\nSubject: photo\r\nMIME-Version: 1.0\r\n"
                'Content-Type: multipart/related; boundary="R"\r\n\r\n--R\r\nContent-Type: text/html; charset=utf-8\r\n\r\n'
                f"{_HTML}\r\n--R--\r\n").encode()

        result = convert(data, "mail.eml")

        assert result.text == "Subject: photo\nFrom: kim@example.com\nTo: lee@example.com\n\n회의는 3시에 시작합니다."


def _mail_with_base64_attachment(body_encoding: str = "8bit") -> bytes:
    attachment = base64.encodebytes(os.urandom(3000)).decode()
    return ("From: kim@example.com\nTo: lee@example.com\nSubject: report\nMIME-Version: 1.0\n"
            "Content-Type: multipart/mixed; boundary=BB\n\n--BB\nContent-Type: text/plain; charset=utf-8\n"
            f"Content-Transfer-Encoding: {body_encoding}\n\n첨부한 보고서를 확인해 주세요.\n\n--BB\n"
            "Content-Type: application/pdf; name=report.pdf\nContent-Disposition: attachment; filename=report.pdf\n"
            f"Content-Transfer-Encoding: base64\n\n{attachment}--BB--\n").encode()


class TestPlainMailWithAnEncodedAttachment:
    """A mail with an 8bit plain-text body and only the attachment in base64 (the Thunderbird default) was judged a 'readable
    plain-text mail', so the MIME headers, boundaries and the base64 of the attachment became the result as is. As with a
    transfer-encoded mail, only the body is read and the attachment is only named."""

    @pytest.mark.parametrize("filename", ["mail.eml", "mail.txt", None])
    def test_body_is_read_and_the_attachment_is_only_named(self, filename):
        data = _mail_with_base64_attachment()

        result = convert(data, filename)

        assert result.format is DocumentFormat.TEXT
        assert result.text == "Subject: report\nFrom: kim@example.com\nTo: lee@example.com\n\n첨부한 보고서를 확인해 주세요."
        assert result.warnings == ["1 mail attachment(s) (report.pdf) were not read. Convert attachments separately."]


class TestHtmlInsideJsonAndCode:
    """JSON and source code that start as text but hold HTML strings (closing tags included) were read as HTML, which mangled the
    keys, indentation and code lines."""

    DOCUMENT = {"id": 17, "title": "공지", "body": "<p>안내</p><table><tr><td>항목</td><td>값</td></tr></table>", "price": 1200}

    @pytest.mark.parametrize("filename", ["export.json", "export.log", "export.txt", None])
    def test_json_with_html_strings_is_text(self, filename):
        data = json.dumps(self.DOCUMENT, ensure_ascii=False, indent=2).encode()

        result = convert(data, filename)

        assert result.format is DocumentFormat.TEXT
        assert result.text == json.dumps(self.DOCUMENT, ensure_ascii=False, indent=2)

    def test_json_lines_with_html_strings_are_text(self):
        data = "\n".join(json.dumps({"n": index, "html": f"<div><p>{index}</p></div>"}) for index in range(3)).encode()

        result = convert(data, None)

        assert result.format is DocumentFormat.TEXT

    def test_large_json_is_decided_by_its_opening_without_parsing_it_all(self, monkeypatch):
        monkeypatch.setattr("opendockit.detect._JSON_PARSE_BYTES", 64)
        data = json.dumps([self.DOCUMENT] * 20, ensure_ascii=False).encode()

        result = convert(data, None)

        assert result.format is DocumentFormat.TEXT

    @pytest.mark.parametrize("filename", ["app.py", "view.js", "Page.tsx", "config.yaml"])
    def test_source_code_with_html_strings_is_text(self, filename):
        data = b'def render():\n    return "<html><body><p>hi</p></body></html>"\n\nif x < 2 and y > 3:\n    pass\n'

        result = convert(data, filename)

        assert result.format is DocumentFormat.TEXT
        assert "    return" in result.text

    @pytest.mark.parametrize("filename", ["list.jsp", "download.php", "export.asp", None])
    def test_html_served_under_a_server_script_name_is_still_html(self, filename):
        """An 'Excel download' HTML fetched under a server script name (.jsp, .php) is still HTML."""
        data = "<html><body><table><tr><th>이름</th></tr><tr><td>홍</td></tr></table></body></html>".encode()

        result = convert(data, filename)

        assert result.format is DocumentFormat.HTML


_THUNDERBIRD_ALTERNATIVE = (
    "From: =?UTF-8?B?" + base64.b64encode("김철수".encode()).decode() + "?= <kim@example.com>\nTo: lee@example.com\n"
    f"Subject: {_SUBJECT}\nDate: Thu, 02 Oct 2026 10:00:00 +0900\nMIME-Version: 1.0\n"
    'Content-Type: multipart/alternative; boundary="------------abc123"\n\nThis is a multi-part message in MIME format.\n'
    "--------------abc123\nContent-Type: text/plain; charset=UTF-8; format=flowed\nContent-Transfer-Encoding: 8bit\n\n"
    "회의는 3시에 시작합니다.\n\n--------------abc123\nContent-Type: text/html; charset=UTF-8\nContent-Transfer-Encoding: 8bit\n\n"
    "<html><body><p>회의는 3시에 시작합니다.</p></body></html>\n\n--------------abc123--\n"
).replace("\n", "\r\n").encode()

_GMAIL_ASCII_ALTERNATIVE = (
    b"MIME-Version: 1.0\r\nDate: Thu, 2 Oct 2026 10:00:00 +0900\r\nMessage-ID: <CAF=abc@mail.gmail.com>\r\nSubject: Q3 budget review\r\n"
    b"From: Alice Kim <alice@example.com>\r\nTo: Bob Lee <bob@example.com>\r\n"
    b'Content-Type: multipart/alternative; boundary="000000000000a1b2"\r\n\r\n--000000000000a1b2\r\n'
    b'Content-Type: text/plain; charset="UTF-8"\r\n\r\nHi Bob,\r\n\r\nThe Q3 budget is $1.2M.\r\n\r\n--000000000000a1b2\r\n'
    b'Content-Type: text/html; charset="UTF-8"\r\n\r\n<div dir="ltr">Hi Bob,<div><br></div><div>The Q3 budget is $1.2M.</div></div>\r\n\r\n'
    b"--000000000000a1b2--\r\n"
)


class TestMailWithoutTransferEncoding:
    """A mail whose parts are sent as 7bit/8bit (the Thunderbird default, Gmail's ASCII mail) was judged a 'readable plain-text
    mail': the MIME headers and boundaries stayed as is, the RFC 2047 subject (`=?utf-8?b?…?=`) was not decoded, and the same body
    of the multipart/alternative came out twice. As with a transfer-encoded mail, the headers are decoded and placed first, and the
    body is read only once (the HTML part if there is one)."""

    @pytest.mark.parametrize("filename", ["mail.eml", None])
    def test_eight_bit_alternative_mail_is_read_once_with_decoded_headers(self, filename):
        result = convert(_THUNDERBIRD_ALTERNATIVE, filename)

        assert result.format is DocumentFormat.HTML
        assert result.text == ("Subject: 주간 회의 일정 변경\nFrom: 김철수 <kim@example.com>\nTo: lee@example.com\n"
                               "Date: Thu, 02 Oct 2026 10:00:00 +0900\n\n회의는 3시에 시작합니다.")

    def test_ascii_alternative_mail_without_transfer_encoding_headers_is_read_once(self):
        result = convert(_GMAIL_ASCII_ALTERNATIVE, "mail.eml")

        assert result.format is DocumentFormat.HTML
        assert result.text == ("Subject: Q3 budget review\nFrom: Alice Kim <alice@example.com>\nTo: Bob Lee <bob@example.com>\n"
                               "Date: Thu, 2 Oct 2026 10:00:00 +0900\n\nHi Bob,\n\nThe Q3 budget is $1.2M.")

    @pytest.mark.parametrize("filename", ["mail.eml", "mail.txt"])
    def test_single_part_eight_bit_mail_has_its_encoded_subject_decoded(self, filename):
        data = (f"From: kim@example.com\nTo: lee@example.com\nSubject: {_SUBJECT}\nMIME-Version: 1.0\n"
                "Content-Type: text/plain; charset=UTF-8\nContent-Transfer-Encoding: 8bit\n\n본문입니다. 금액 300원.\n").encode()

        result = convert(data, filename)

        assert result.format is DocumentFormat.TEXT
        assert result.text == "Subject: 주간 회의 일정 변경\nFrom: kim@example.com\nTo: lee@example.com\n\n본문입니다. 금액 300원."

    def test_eight_bit_body_in_the_declared_legacy_charset_is_decoded_with_it(self):
        data = (b"From: kim@example.com\nSubject: =?euc-kr?b?" + base64.b64encode("견적".encode("euc-kr")) + b"?=\n"
                b"MIME-Version: 1.0\nContent-Type: text/plain; charset=euc-kr\nContent-Transfer-Encoding: 8bit\n\n"
                + "견적서를 보냅니다.\n".encode("euc-kr"))

        result = convert(data, "mail.eml")

        assert result.text == "Subject: 견적\nFrom: kim@example.com\n\n견적서를 보냅니다."

    def test_html_attachment_of_a_plain_mail_is_not_read_as_its_body(self):
        """A mail whose body is plain text and whose HTML is an attachment has its plain body read and the attachment only named."""
        data = ("From: kim@example.com\nTo: lee@example.com\nSubject: report\nMIME-Version: 1.0\n"
                "Content-Type: multipart/mixed; boundary=BB\n\n--BB\nContent-Type: text/plain; charset=utf-8\n\n보고서를 첨부합니다.\n"
                "--BB\nContent-Type: text/html; charset=utf-8\nContent-Disposition: attachment; filename=report.html\n\n"
                "<p>첨부된 보고서 본문</p>\n--BB--\n").encode()

        result = convert(data, "mail.eml")

        assert result.format is DocumentFormat.TEXT
        assert result.text == "Subject: report\nFrom: kim@example.com\nTo: lee@example.com\n\n보고서를 첨부합니다."
        assert result.warnings == ["1 mail attachment(s) (report.html) were not read. Convert attachments separately."]


def _base64_body_mail(extra_headers: str = "", unixfrom: str = "") -> bytes:
    body = base64.encodebytes("3분기 정산 결과를 공유드립니다.".encode()).decode()
    return (f"{unixfrom}{extra_headers}Subject: {_SUBJECT}\r\nFrom: kim@example.com\r\nTo: lee@example.com\r\n"
            "MIME-Version: 1.0\r\nContent-Type: multipart/alternative; boundary=\"B1\"\r\n\r\n--B1\r\n"
            "Content-Type: text/plain; charset=utf-8\r\nContent-Transfer-Encoding: base64\r\n\r\n"
            f"{body}\r\n--B1--\r\n").encode()


def _long_received_headers(size: int) -> str:
    """Make the header block exceed `size` bytes with Received, ARC and X-MS-Exchange lines, like Office 365 mail (folded lines
    included)."""
    signature = base64.b64encode(bytes(range(256)) * (size // 256 + 1)).decode()
    folded = "\r\n\t".join(signature[i:i + 70] for i in range(0, size, 70))
    return f"Received: from a.prod.outlook.com\r\n by b.prod.outlook.com with HTTPS\r\nX-Microsoft-Antispam-Message-Info: {folded}\r\n"


class TestMailWithALongHeaderBlock:
    """The header block was looked for only in the first 8KB, so an Office 365 mail whose headers exceed 8KB with Received, ARC,
    DKIM and X-MS-Exchange lines was not detected as mail and passed as text, and its base64 body became a 'successful' result as is."""

    @pytest.mark.parametrize("filename", ["mail.eml", None])
    def test_mail_with_headers_longer_than_8kb_is_read_as_mail(self, filename):
        data = _base64_body_mail(_long_received_headers(40_000))

        result = convert(data, filename)

        assert result.format is DocumentFormat.TEXT
        assert result.text.startswith("Subject: 주간 회의 일정 변경\nFrom: kim@example.com\nTo: lee@example.com")
        assert result.text.endswith("3분기 정산 결과를 공유드립니다.")

    @pytest.mark.timing
    def test_long_header_like_text_without_a_blank_line_is_decided_in_linear_time(self):
        """Widening the range searched for headers must stay linear: a large text of header and folded-line shapes with no blank
        line."""
        data = (b"Key: value\n" + b"  folded line\n" * 20_000 + b"MIME-Version: 1.0\n") * 5

        started = time.perf_counter()
        result = detect_format(data, None)
        elapsed = time.perf_counter() - started

        assert result is DocumentFormat.TEXT
        assert elapsed < 0.5


class TestMailWithAnMboxFromLine:
    """An .eml saved by Thunderbird and others starts with the mbox `From - date` line. That is not a header line shape, so the
    file was not detected as mail and became text, base64 body and all. The line is skipped only when a valid header block follows it."""

    @pytest.mark.parametrize("filename", ["mail.eml", None])
    def test_mail_after_an_mbox_from_line_is_read_as_mail(self, filename):
        data = _base64_body_mail("X-Mozilla-Status: 0001\r\n", unixfrom="From - Thu Mar 07 10:15:00 2024\r\n")

        result = convert(data, filename)

        assert result.format is DocumentFormat.TEXT
        assert result.text == ("Subject: 주간 회의 일정 변경\nFrom: kim@example.com\nTo: lee@example.com\n\n"
                               "3분기 정산 결과를 공유드립니다.")

    def test_html_mail_after_an_mbox_from_line_keeps_its_headers(self):
        data = ("From kim@example.com Mon Sep  1 10:00:00 2025\n" + _single_html_mail("base64").decode()).encode()

        result = convert(data, "mail.eml")

        assert result.format is DocumentFormat.HTML
        assert result.text.endswith("회의는 3시에 시작합니다.")
        assert "Subject: 주간 회의 일정 변경" in result.text

    def test_prose_that_starts_with_from_is_still_text(self):
        data = b"From the start of the quarter,\nMIME-Version: 1.0\nContent-Type: text/plain\n\nbody"

        result = convert(data, None)

        assert result.format is DocumentFormat.TEXT
        assert result.text.startswith("From the start of the quarter,")
