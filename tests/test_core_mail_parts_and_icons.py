"""Mail: HTML body pieces with an attachment between them (Apple Mail), the warning for an attachment shown in the body, RFC 2047
words with extended Hangul, encoded attachment names, an mbox holding several mails. HTML: SVG data: URI icons."""

import base64

import pytest

from opendockit import DocumentFormat, convert
from opendockit.extractors import text as text_module
from tests import builder


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
        monkeypatch.setattr(text_module, "MAX_BOX_MAILS", 2)
        data = _mbox(html, html, html)

        result = convert(data, "box.mbox")

        assert "## Message 2" in result.text
        assert "## Message 3" not in result.text
        assert "메일 3" not in result.text
        assert result.warnings == ["The mailbox (mbox) holds 3 messages, but only the first 2 were read. "
                                   "Split the rest off and convert it separately."]


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
    def test_invitation_details_follow_the_body(self, html):
        result = convert(_invite(_INVITE_CALENDAR, html=html), "invite.eml")

        assert result.text.endswith("예산 검토 회의입니다.\n\n" + _INVITE_BLOCK)
        assert result.warnings == []

    def test_folded_lines_escapes_utc_and_all_day_times_are_readable(self):
        calendar = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:분기 \r\n 워크숍\\, 1일차\r\nDTSTART:20240307T060000Z\r\n"
                    "DTEND;VALUE=DATE:20240308\r\nLOCATION:제주\\; 본관\\nA동\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nSUMMARY:2일차\r\n"
                    "DTSTART;VALUE=DATE:20240308\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")

        result = convert(_invite(calendar), "invite.eml")

        assert result.text.endswith("Event:\n\n- Title: 분기 워크숍, 1일차\n- Start: 2024-03-07 06:00 UTC\n- End: 2024-03-08\n"
                                    "- Location: 제주; 본관 A동\n\nEvent:\n\n- Title: 2일차\n- Start: 2024-03-08")

    def test_colon_inside_a_quoted_parameter_does_not_split_the_line(self):
        calendar = ('BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nDTSTART;TZID="(UTC+09:00) 서울":20240307T150000\r\n'
                    'ORGANIZER;CN="홍길동: 팀장":mailto:hong@x.com\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n')

        result = convert(_invite(calendar), "invite.eml")

        assert result.text.endswith("Event:\n\n- Start: 2024-03-07 15:00 ((UTC+09:00) 서울)\n- Organizer: 홍길동: 팀장 <hong@x.com>")

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
