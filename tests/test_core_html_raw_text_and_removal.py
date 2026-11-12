"""Unclosed raw-text elements (textarea, xmp, plaintext) and the front check of a large HTML, an HTML example inside a closed
textarea, the time to remove many hidden or dropped elements under the same parent."""

import time

import pytest

from opendockit import ConvertOptions, convert
from tests import needs_html5_tokenizer


def _stock_page(rows: int, opening: str = "<textarea name=memo/>") -> bytes:
    """A page with an unclosed raw-text element before an 'Excel download' table that has format attributes on every cell."""
    style = "mso-number-format:'\\@';border:.5pt solid windowtext;font-family:맑은 고딕;font-size:10pt;text-align:center"
    cells = "".join(f"<tr><td class=xl65 style=\"{style}\">{row}</td><td class=xl66 style=\"{style}\">항목{row}</td></tr>"
                    for row in range(rows))
    return f"<html><body><form><p>메모</p>{opening}\n<h1>재고 현황</h1><table>{cells}</table></form></body></html>".encode()


class TestUnclosedRawTextElementInALargePage:
    """For a large HTML only the front is read first to check the limit (`_extract_front`). The cut-off front has no `</body>` or
    `</html>`, so the unclosed textarea was not re-read, and the front counted the rest of the markup as raw text, tags and all:
    the whole conversion (50,000 characters) was within the limit, yet it was exceeded with 410,000 characters."""

    @pytest.mark.parametrize("opening", ["<textarea name=memo/>", "<xmp>", "<plaintext>"])
    def test_limit_at_the_whole_length_is_not_exceeded(self, opening):
        data = _stock_page(4_000, opening)
        whole = convert(data, "a.html")

        result = convert(data, "a.html", ConvertOptions(max_chars=whole.char_count))

        assert not whole.exceeded
        assert not result.exceeded
        assert result.text == whole.text
        assert "<td" not in result.text

    def test_limit_below_the_whole_length_is_exceeded_without_markup(self):
        data = _stock_page(4_000)
        whole = convert(data, "a.html")

        result = convert(data, "a.html", ConvertOptions(max_chars=whole.char_count // 4))

        assert result.exceeded
        assert "<td" not in result.text

    def test_textarea_closed_after_the_front_is_not_read_as_html(self):
        """When the front is cut in the middle of a closed textarea, that textarea is not an unclosed one — re-reading it counts text
        that is not in the whole conversion."""
        style = "font-family:맑은 고딕;font-size:10pt;border:.5pt solid windowtext;text-align:center;vertical-align:middle"
        value = "".join(f"<td style=\"{style}\">{row}</td>" for row in range(6_000))
        data = f"<html><body><p>메모</p><textarea name=memo>{value}</textarea><p>끝</p></body></html>".encode()
        whole = convert(data, "a.html")

        results = [convert(data, "a.html", ConvertOptions(max_chars=limit))
                   for limit in (whole.char_count, whole.char_count - 1, 2_000)]

        assert [result.exceeded for result in results] == [False, True, True]


# A fragment with nested unclosed selects, found by fuzzing (reduced). The text of the whole conversion and of the front check differed.
_TANGLED_SELECTS = (
    "<select><option selected><ol start=5>12,345<blockquote>항목<dl><dt>항목<p>12,345<dd>항목<th rowspan=2><tr><dl><dt>"
    "<div aria-hidden=true>12,345한12,345</li>글 x<input type=checkbox checked>글 x<ol start=5>글 x<sup>글 x<pre><details>"
    "12,345<td colspan=3>12,345<details><br>글 x<dd>12,345<input type=checkbox checked>12,345<summary>글 x<details>12,345"
    "<select><option selected>12,345<th rowspan=2>글 x<a href=#>12,345<a href=#>12,345<sup><th rowspan=2>12,345"
    "<input type=checkbox checked><table>12,345<dl><dt>항목<dl><dt>항목<a href=#><dl><dt>글 x<br>12,345<pre>글 x"
    "<div aria-hidden=true>글 x&nbsp;<p>글 x<h2>글 x<sup>12,345<h1><blockquote>12,345<dd>12,345<sup>항목<td colspan=3>글 x"
    "<dl><dt>글 x<tr><select><option selected>12,345<summary>항목<select><option selected>12,345<br>항목<ul><li>12,345")


class TestUnclosedSelectInALargePage:
    """lxml drops the elements inside an unclosed select differently depending on the markup that follows. The front check counted
    text that is not in the whole conversion, blocking a document within the limit (46 characters) as exceeded."""

    def test_exceeded_matches_the_whole_conversion_at_every_limit(self):
        data = _TANGLED_SELECTS.encode()
        whole = convert(data, "a.html")

        wrong = [limit for limit in range(1, whole.char_count + 60)
                 if convert(data, "a.html", ConvertOptions(max_chars=limit)).exceeded != (whole.char_count > limit)]

        assert wrong == []


class TestHtmlExampleInsideAClosedTextarea:
    """The textarea of a saved HTML editor or CMS admin page holds HTML source. Even a closed textarea was re-read if its text
    contained `</html>`, so the heading of the example became a heading of the body (`# 내 제목`)."""

    @needs_html5_tokenizer
    def test_closed_textarea_holding_a_whole_page_keeps_it_as_its_value(self):
        html = ("<html><body><h2>템플릿 편집</h2><form><textarea name=src><html><body><h1>내 제목</h1><p>예제 문단</p></body></html>"
                "</textarea><p>저장하려면 아래를 누르세요.</p></form></body></html>")

        result = convert(html.encode(), "a.html")

        assert "# 내 제목" not in result.text
        assert "<h1>내 제목</h1>" in result.text
        assert "저장하려면 아래를 누르세요." in result.text

    @needs_html5_tokenizer
    def test_closed_textarea_at_the_end_of_the_page_keeps_its_value(self):
        html = "<html><body><p>본문</p><form><textarea name=src><p>예제</p></body></html></textarea></form></body></html>"

        result = convert(html.encode(), "a.html")

        assert "<p>예제</p>" in result.text

    @needs_html5_tokenizer
    def test_unclosed_textarea_without_closing_page_tags_is_read_as_html(self):
        html = "<html><body><p>앞</p><textarea name=a/><p>본문 문단</p><table><tr><th>구분</th><th>값</th></tr><tr><td>가</td><td>1</td></tr></table>"

        result = convert(html.encode(), "a.html")

        assert result.text == "앞\n\n본문 문단\n\n| 구분 | 값 |\n| --- | --- |\n| 가 | 1 |"


def _seconds(build, count: int) -> float:
    data = build(count)
    started = time.perf_counter()
    convert(data, "a.html")
    return time.perf_counter() - started


class TestManyRemovedSiblingsTakeLinearTime:
    """bs4's `decompose` searches the parent's child list for its own position from the start. An order table (4.5MB) with a
    `display:none` detail row after every row took 17 s, and a page of 64,000 alternating paragraphs and buttons took 30 s."""

    @pytest.mark.parametrize("build", [
        lambda count: ("<html><body><table>" + "".join(
            f"<tr><td>주문 {row}</td><td>10,000원</td></tr><tr style='display:none'><td colspan=2>상세 {row}</td></tr>"
            for row in range(count)) + "</table></body></html>").encode(),
        lambda count: ("<html><body><div>" + "<p>문단</p><button>열기</button>" * count + "</div></body></html>").encode(),
        lambda count: ("<html><body><div>" + "<p>문단</p><!-- 주석 -->" * count + "</div></body></html>").encode(),
        lambda count: ("<html><body><div>" + "<p>문단</p><i aria-hidden='true'>★</i>" * count + "</div></body></html>").encode(),
    ], ids=["display-none", "button", "comment", "aria-hidden"])
    @pytest.mark.timing
    def test_removal_time_grows_linearly(self, build):
        _seconds(build, 500)

        small, large = _seconds(build, 2_000), _seconds(build, 16_000)

        assert large < 8 * small * 2 + 0.5

    def test_removed_elements_leave_the_same_text(self):
        html = ("<html><body><table><tr><td>주문 1</td><td>10,000원</td></tr><tr style='display:none'><td>상세</td></tr>"
                "<tr><td>주문 2</td><td>20,000원</td></tr></table><div><p>가</p><button>열기</button><p>나</p><!-- 주석 -->"
                "<span aria-hidden='true'>★</span><p>다<i hidden>숨김<b>안쪽</b></i></p></div></body></html>")

        result = convert(html.encode(), "a.html")

        assert result.text == "| 주문 1 | 10,000원 |\n| --- | --- |\n| 주문 2 | 20,000원 |\n\n가\n\n나\n\n다"
