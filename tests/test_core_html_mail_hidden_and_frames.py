"""Hidden preview text and invisible characters in the HTML alternative of a mail, a meeting invitation whose folded lines split a
UTF-8 character, aria-hidden on the app root (modal), alt text of lazy-loading placeholder pictures, plain + HTML bodies in
multipart/mixed, CP949 header words labelled ISO-8859-1, `<noframes>`, MHTML whose body is inside a frame, `<noembed>`."""

import base64
import quopri

import pytest

from opendockit import convert


_PLAIN = "[가을 세일] 10월 10일~20일 전 품목 30% 할인\n쿠폰 코드: FALL30"
_HEAD = "Subject: 세일\nFrom: shop@example.com\nTo: me@example.com"


def _alternative(html: str) -> bytes:
    return ("From: shop@example.com\nTo: me@example.com\nSubject: 세일\nMIME-Version: 1.0\n"
            "Content-Type: multipart/alternative; boundary=A\n\n"
            f"--A\nContent-Type: text/plain; charset=utf-8\n\n{_PLAIN}\n"
            f"--A\nContent-Type: text/html; charset=utf-8\n\n{html}\n--A--\n").encode()


class TestHtmlAlternativeWithOnlyHiddenText:
    """The HTML alternative of a promotional mail hides the inbox preview text (preheader) with `display:none` and fills blank
    space with `&zwnj;&nbsp;`. That was taken as 'there is text', so the body of the plain-text alternative (sale period, coupon
    code) vanished without a warning and only the headers remained."""

    @pytest.mark.parametrize("html", [
        '<html><body><div style="display:none;max-height:0;overflow:hidden">가을 세일 시작!</div>'
        '<img src="https://cdn.example.com/banner.jpg"></body></html>',
        '<html><body><div>&zwnj;&nbsp;&zwnj;&nbsp;&#8203;&shy;</div>'
        '<table><tr><td><img src="https://cdn.example.com/banner.jpg"></td></tr></table></body></html>',
        '<html><body><span hidden>미리보기</span><p aria-hidden="true">&nbsp;</p></body></html>',
    ])
    def test_plain_body_is_used_when_the_html_part_shows_no_text(self, html):
        result = convert(_alternative(html), "a.eml")

        assert result.text == f"{_HEAD}\n\n{_PLAIN}"

    def test_html_part_with_visible_text_besides_a_hidden_preheader_is_still_preferred(self):
        html = '<div style="display:none">미리보기</div><p>HTML 본문</p>'

        result = convert(_alternative(html), "a.eml")

        assert result.text == f"{_HEAD}\n\nHTML 본문"


def _fold(line: bytes, width: int = 75) -> bytes:
    """iCalendar folding (RFC 5545): the line is split every 75 bytes, with a space before each continuation line. It works on
    bytes, so it also splits in the middle of a UTF-8 character."""
    pieces = [line[:width]]
    line = line[width:]

    while line:
        pieces.append(b" " + line[:width - 1])
        line = line[width - 1:]
    return b"\r\n".join(pieces)


class TestCalendarLinesFoldedInsideACharacter:
    """iCalendar folding works on bytes (75 octets), so Outlook splits a line between the bytes of a Hangul character. The folded
    lines were unfolded after decoding to text, so that character was garbled like '예��'. They are unfolded on the bytes first."""

    @pytest.mark.parametrize("transfer", ["base64", "8bit"])
    def test_folded_korean_values_are_read_whole(self, transfer):
        lines = ["BEGIN:VCALENDAR", "METHOD:REQUEST", "BEGIN:VEVENT",
                 "SUMMARY;LANGUAGE=ko-KR:2024년 4분기 사업계획 검토 회의 및 예산 조정 논의 (전 부서장 필참)",
                 "LOCATION;LANGUAGE=ko-KR:본사 12층 대회의실(A동) 및 화상회의 병행", "END:VEVENT", "END:VCALENDAR"]
        calendar = b"\r\n".join(_fold(line.encode()) for line in lines)
        body = base64.encodebytes(calendar) if transfer == "base64" else calendar
        data = ("From: a@b.c\nTo: d@e.f\nSubject: w\nMIME-Version: 1.0\nContent-Type: multipart/alternative; boundary=A\n\n"
                "--A\nContent-Type: text/plain; charset=utf-8\n\n회의에 초대합니다.\n"
                f"--A\nContent-Type: text/calendar; charset=utf-8; method=REQUEST\nContent-Transfer-Encoding: {transfer}\n\n"
                ).encode() + body + b"\n--A--\n"

        result = convert(data, "invite.eml")

        assert "- Title: 2024년 4분기 사업계획 검토 회의 및 예산 조정 논의 (전 부서장 필참)" in result.text
        assert "- Location: 본사 12층 대회의실(A동) 및 화상회의 병행" in result.text
        assert "�" not in result.text


_ARTICLE = "<article><h1>2024 하반기 채용 공고</h1><p>모집 분야: 백엔드 개발</p><p>접수 기간: 10월 1일 ~ 10월 31일</p></article>"


class TestAriaHiddenAppRootUnderAModal:
    """react-modal and MUI put `aria-hidden="true"` on the app root (`#root`, `#__next`) while a modal is open. A page saved in that
    state had its whole body removed as a hidden element, leaving only the text of the cookie notice modal. An aria-hidden element
    holding the main content (main, article, h1) is not removed."""

    @pytest.mark.parametrize("root", ['<div id="root" aria-hidden="true">{}</div>',
                                      '<div id="__next" aria-hidden="true"><div class="layout">{}</div></div>'])
    def test_page_under_an_aria_hidden_app_root_is_kept(self, root):
        html = (f"<html><head><title>채용</title></head><body>{root.format(_ARTICLE)}"
                '<div class="ReactModal__Portal"><div role="dialog" aria-modal="true"><p>쿠키를 사용합니다.</p></div></div>'
                "</body></html>")

        result = convert(html.encode(), "saved.html")

        assert "모집 분야: 백엔드 개발" in result.text
        assert "# 2024 하반기 채용 공고" in result.text

    def test_root_without_headings_that_holds_most_of_the_page_is_kept(self):
        body = "".join(f"<p>{n}번 공지: 서버 점검이 10월 {n}일에 있습니다.</p>" for n in range(1, 6))
        html = f'<html><body><div id="app" aria-hidden="true">{body}</div><div role="dialog"><p>로그인</p></div></body></html>'

        result = convert(html.encode(), "saved.html")

        assert "3번 공지: 서버 점검이 10월 3일에 있습니다." in result.text

    def test_small_aria_hidden_decorations_are_still_removed(self):
        html = ('<html><body><h1>안내</h1><p><span aria-hidden="true">★</span>별점 5점</p>'
                '<div aria-hidden="true"><p>복제된 슬라이드 글</p></div><p>본문 끝</p></body></html>')

        result = convert(html.encode(), "a.html")

        assert result.text == "# 안내\n\n별점 5점\n\n본문 끝"

    def test_root_is_kept_when_a_display_none_layer_beside_it_holds_more_text(self):
        """'Half of the body text' also counted the text of a hidden sibling layer (terms of service, `display:none`), so #root was
        removed — only the 14 characters of the modal text remained."""
        root = ('<div id="root" aria-hidden="true">'
                + "".join(f"<p>공지 본문 {n}번째 문단: 신청 기간과 제출 서류를 확인하세요.</p>" for n in range(8)) + "</div>")
        terms = ('<div class="layer-terms" style="display:none">'
                 + "".join(f"<p>제{n}조 개인정보 수집 및 이용에 관한 약관 조항 내용이 길게 이어집니다.</p>" for n in range(1, 30)) + "</div>")
        html = f'<html><body>{root}{terms}<div role="dialog"><p>쿠키 사용에 동의하십니까?</p></div></body></html>'

        result = convert(html.encode(), "page.html")

        assert "공지 본문 7번째 문단" in result.text
        assert "약관 조항" not in result.text

    @pytest.mark.parametrize("layout", ["siblings", "nested"])
    @pytest.mark.timing
    def test_many_aria_hidden_elements_take_linear_time(self, layout):
        """For every aria-hidden element the whole body text was measured again (children of body) and the main-content elements
        were searched for again in the subtree (nested elements), taking 39–47 s (quadratic) on 468KB."""
        import time

        def seconds(count: int) -> float:
            if layout == "siblings":
                body = "".join(f'<span aria-hidden="true">★</span><p>문단 {n} 내용</p>' for n in range(count))
            else:
                body = '<div aria-hidden="true"><p>장식</p>' * count + "</div>" * count + "<p>본문</p>"
            data = f"<html><body>{body}</body></html>".encode()
            started = time.perf_counter()
            convert(data, "a.html")
            return time.perf_counter() - started

        seconds(500)

        small, large = seconds(1_500), seconds(6_000)

        assert large < 8 * small + 0.5

    def test_early_stop_on_a_large_page_does_not_count_text_the_whole_page_hides(self):
        """In the check that reads only the front of a large document, an aria-hidden block was kept as 'at least half of the front
        text', so the 966-character limit was exceeded by text that is not in the whole conversion (916 characters) (char_count
        1580). The front check must not have more text than the whole check."""
        from opendockit import ConvertOptions

        hidden = '<div aria-hidden="true">' + "".join(f"<p>숨김문장{n:03d}가나다라마바사</p>" for n in range(50)) + "</div>"
        front = "".join(f"<p>본문앞{n:03d}가나다라마바사아자</p>" for n in range(46))
        back = "".join(f"<p>본문뒤{n:03d}가나다라마바사아자</p>" for n in range(8))
        data = f"<html><body>{hidden}{front}<!--{'x' * 20000}-->{back}</body></html>".encode()
        whole = convert(data, "a.html")

        result = convert(data, "a.html", ConvertOptions(max_chars=whole.char_count + 50))

        assert "숨김문장" not in whole.text
        assert not result.exceeded
        assert result.text == whole.text


_PIXEL = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"


class TestLazyLoadedPicturePlaceholders:
    """Lazy-loading pages put a 1×1 transparent GIF in `src` and the real address in `data-src`. That placeholder picture was
    extracted and filtered out as a small picture, removing its alt text (the figures of a graph) as well. The alt text is kept, as
    for an external picture."""

    @pytest.mark.parametrize("attribute", ['data-src="https://cdn.example.com/q3.png"', 'data-lazy-src="/q3.png"',
                                           'data-original="q3.png"', ""])
    def test_alt_text_of_a_placeholder_picture_is_kept(self, attribute):
        html = f'<p>지역별 매출입니다.</p><img src="{_PIXEL}" {attribute} alt="서울 120억, 부산 80억">'

        result = convert(html.encode(), "news.html")

        assert result.text == "지역별 매출입니다.\n\n[Image: 서울 120억, 부산 80억]"
        assert result.images == []

    def test_placeholder_without_alt_text_leaves_nothing(self):
        result = convert(f'<p>본문</p><img src="{_PIXEL}" data-src="a.png">'.encode(), "news.html")

        assert result.text == "본문"


def _mixed(*parts: str) -> bytes:
    body = "".join(f"--B\n{part}\n" for part in parts)
    return ("From: a@corp.co.kr\nTo: b@corp.co.kr\nSubject: 결재\nMIME-Version: 1.0\n"
            f"Content-Type: multipart/mixed; boundary=B\n\n{body}--B--\n").encode()


class TestPlainAndHtmlBodiesSideBySide:
    """In a multipart/mixed with a plain-text body + an HTML part (signature, mailing-list footer), only the HTML was read, so the
    plain-text body (what is to be approved, the amount) vanished without a warning. Only alternatives, two representations of the
    same body, have one chosen; the parts of a mixed are all read in order."""

    def test_plain_body_and_html_signature_are_both_read_in_order(self):
        data = _mixed("Content-Type: text/plain; charset=utf-8\n\n출장비 정산 결재 요청드립니다. 금액은 352,000원입니다.",
                      "Content-Type: text/html; charset=utf-8\n\n<p>[서명] 홍길동 | 재무팀</p>")

        result = convert(data, "m.eml")

        assert result.text == ("Subject: 결재\nFrom: a@corp.co.kr\nTo: b@corp.co.kr\n\n"
                               "출장비 정산 결재 요청드립니다. 금액은 352,000원입니다.\n\n[서명] 홍길동 | 재무팀")

    def test_alternative_body_followed_by_a_plain_footer(self):
        alternative = ("Content-Type: multipart/alternative; boundary=A\n\n"
                       "--A\nContent-Type: text/plain; charset=utf-8\n\n본문(평문)\n"
                       "--A\nContent-Type: text/html; charset=utf-8\n\n<p>본문(HTML)</p>\n--A--")
        data = _mixed(alternative, "Content-Type: text/plain; charset=utf-8\n\n-- 수신 거부: unsubscribe@corp.co.kr")

        result = convert(data, "m.eml")

        assert result.text.endswith("\n\n본문(HTML)\n\n-- 수신 거부: unsubscribe@corp.co.kr")
        assert "본문(평문)" not in result.text

    def test_alternative_without_html_text_is_not_read_twice_next_to_a_plain_part(self):
        alternative = ("Content-Type: multipart/alternative; boundary=A\n\n"
                       "--A\nContent-Type: text/plain; charset=utf-8\n\n본문(평문)\n"
                       '--A\nContent-Type: text/html; charset=utf-8\n\n<img src="cid:logo">\n--A--')
        data = _mixed("Content-Type: text/plain; charset=utf-8\n\n머리 글", alternative)

        result = convert(data, "m.eml")

        assert result.text.endswith("\n\n머리 글\n\n본문(평문)")


class TestEncodedWordLabeledLatinButKorean:
    """Old mail programs write a CP949 subject as `=?iso-8859-1?B?…?=`. Decoded as Windows-1252 per the charset of the word, the
    subject became `[°øÁö] 10¿ù …`. As with an ISO-8859-1 declaration in a body, strict CP949 decoding is tried first
    (`decode_text`)."""

    def test_cp949_subject_labeled_latin1_is_read_as_korean(self):
        subject = base64.b64encode("[공지] 10월 정기 점검 안내".encode("cp949")).decode()
        data = (f"From: admin@corp.co.kr\nTo: all@corp.co.kr\nSubject: =?iso-8859-1?B?{subject}?=\nMIME-Version: 1.0\n"
                "Content-Type: text/plain; charset=utf-8\n\n본문").encode()

        result = convert(data, "notice.eml")

        assert result.text.startswith("Subject: [공지] 10월 정기 점검 안내\n")

    @pytest.mark.parametrize(("word", "expected"), [("=?iso-8859-1?Q?caf=E9_cr=E8me?=", "café crème"),
                                                    ("Re: =?windows-1252?Q?M=FCller=92s?= report", "Re: Müller’s report")])
    def test_western_subject_is_still_read_as_western(self, word, expected):
        data = (f"From: a@b.c\nTo: d@e.f\nSubject: {word}\nMIME-Version: 1.0\n"
                "Content-Type: text/plain; charset=utf-8\n\nbody").encode()

        result = convert(data, "a.eml")

        assert result.text.startswith(f"Subject: {expected}\n")


class TestNoFrames:
    """The parser leaves the content of `<noframes>` as raw text, so `<body><p>…</p></body>` came out in the result, tags and all.
    It is removed, like noscript."""

    def test_noframes_content_is_not_output_as_raw_markup(self):
        html = ("<html><head><title>기관 홈페이지</title></head><frameset cols='200,*'><frame src=menu.html></frameset>"
                "<noframes><body><p>프레임을 지원하지 않는 브라우저용 안내</p></body></noframes></html>")

        result = convert(html.encode(), "index.html")

        assert "<" not in result.text

    def test_noembed_content_is_not_output_as_raw_markup(self):
        """The parser leaves `<noembed>` as raw text too, so `<p>대체 문단</p>` came out, tags and all."""
        html = "<html><body><noembed><p>대체 문단</p></noembed><p>본문</p></body></html>"

        result = convert(html.encode(), "index.html")

        assert "<" not in result.text


class TestRawTextElementsLeftOpen:
    """The parser leaves the content of textarea and xmp as raw text. An unclosed `<textarea name=a/>` (it is not self-closing)
    swallowed everything to the end of the document as raw text, so the body after it came out as `<p>본문</p></body></html>`, tags
    and all, and with xmp `<b>코드</b>` came out as paragraph text."""

    @pytest.mark.parametrize("tag", ["textarea name=a/", "xmp", "plaintext"])
    def test_rest_of_the_page_swallowed_by_an_unclosed_element_is_read_as_html(self, tag):
        html = f"<html><body><p>앞</p><{tag}><p>본문 문단 하나</p><table><tr><th>구분</th><th>값</th></tr><tr><td>가</td><td>1</td></tr></table></body></html>"

        result = convert(html.encode(), "a.html")

        assert "<" not in result.text
        assert "본문 문단 하나" in result.text
        assert result.text == "앞\n\n본문 문단 하나\n\n| 구분 | 값 |\n| --- | --- |\n| 가 | 1 |"

    @pytest.mark.timing
    def test_many_unclosed_elements_take_linear_time(self):
        """If the re-read remainder has another unclosed element, what follows it is read again; re-reading them one at a time is
        quadratic in their number."""
        import time

        def seconds(count: int) -> float:
            data = ("<html><body>" + "<textarea><p>문단</p>" * count + "</body></html>").encode()
            started = time.perf_counter()
            convert(data, "a.html")
            return time.perf_counter() - started

        seconds(200)

        small, large = seconds(2_000), seconds(8_000)

        assert large < 8 * small + 0.5

    def test_closed_xmp_is_a_code_block(self):
        html = "<html><body><xmp><b>코드</b></xmp><p>본문</p></body></html>"

        result = convert(html.encode(), "a.html")

        assert result.text == "```\n<b>코드</b>\n```\n\n본문"

    def test_closed_textarea_keeps_its_value(self):
        html = "<html><body><form><textarea name=a>값 <b>x</b></textarea></form><p>본문</p></body></html>"

        result = convert(html.encode(), "a.html")

        assert "값 <b>x</b>" in result.text
        assert "본문" in result.text


def _mhtml(page: str, frame: str) -> bytes:
    parts = [("text/html", "<frame-1@mhtml.blink>", "https://blog.example.com/post/1", page),
             ("text/html", "<frame-2@mhtml.blink>", "https://blog.example.com/PostView?no=1", frame)]
    body = "".join(f"------B\r\nContent-Type: {kind}\r\nContent-ID: {cid}\r\nContent-Transfer-Encoding: quoted-printable\r\n"
                   f"Content-Location: {location}\r\n\r\n{quopri.encodestring(html.encode()).decode()}\r\n"
                   for kind, cid, location, html in parts)
    head = ("From: <Saved by Blink>\r\nSnapshot-Content-Location: https://blog.example.com/post/1\r\nSubject: blog\r\n"
            'MIME-Version: 1.0\r\nContent-Type: multipart/related;\r\n\ttype="text/html";\r\n\tboundary="----B"\r\n\r\n\r\n')
    return f"{head}{body}------B--\r\n".encode()


_FRAME_POST = ("<html><body><div class='se-main-container'><h3>10월 제주 여행기</h3>"
               + "<p>첫째 날은 성산일출봉에 올랐습니다. 바람이 많이 불었지만 경치가 좋았습니다.</p>" * 5 + "</div></body></html>")


class TestMhtmlWhoseBodyIsInAFrame:
    """In an MHTML saved from a Naver blog or cafe the outer page is only a shell (title, iframe) and the body is in a frame part
    saved with it (`mainFrame`). Only the first HTML part is read, deliberately, so the 'successful' result was a single title
    line, without a warning. A warning is given when the frame has more text than the outer page."""

    @pytest.mark.parametrize("src", ["cid:frame-2@mhtml.blink", "/PostView?no=1"])
    def test_warns_when_the_body_is_in_a_saved_frame(self, src):
        page = f'<html><head><title>블로그</title></head><body><iframe id="mainFrame" src="{src}"></iframe></body></html>'

        result = convert(_mhtml(page, _FRAME_POST), "blog.mhtml")

        assert any("frame" in warning for warning in result.warnings)

    def test_page_with_its_own_body_and_a_small_frame_does_not_warn(self):
        page = ("<html><body><h2>보안 점검 안내</h2>" + "<p>10월 10일 오후 6시부터 서버 점검이 있습니다. 접속이 불가합니다.</p>" * 3
                + '<iframe src="cid:frame-2@mhtml.blink"></iframe></body></html>')

        result = convert(_mhtml(page, "<p>광고: 할인</p>"), "notice.mhtml")

        assert result.warnings == []
