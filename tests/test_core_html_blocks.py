"""HTML blocks: an aside inside the main content, blank lines inside a quote block, Notion to-do lists, a name | value table with a
full-width row in it."""


from opendockit import convert


class TestAsideInsideTheMainContent:
    """`<aside>` was taken for a site menu or advertisement and removed whole, so warning and note boxes inside the main content
    (article, main) vanished without a warning. An aside inside the main content is kept; an aside outside it (a side menu) is
    removed as before."""

    def test_warning_and_note_inside_an_article_are_kept(self):
        html = ('<html><body><aside class="sidebar">사이드 메뉴</aside><main><article><h1>API 가이드</h1>'
                '<p>요청 한도는 분당 60회입니다.</p><aside class="warning"><strong>경고:</strong> API 키를 넣지 마세요.</aside>'
                '<aside class="note">참고: v1 은 종료됩니다.</aside><p>다음 단계로 진행합니다.</p></article></main></body></html>')

        result = convert(html.encode(), "a.html")

        assert result.text == ("# API 가이드\n\n요청 한도는 분당 60회입니다.\n\n**경고:** API 키를 넣지 마세요.\n\n참고: v1 은 종료됩니다.\n\n"
                               "다음 단계로 진행합니다.")

    def test_aside_inside_main_without_an_article_is_kept(self):
        html = "<main><p>본문</p><aside>참고 상자</aside></main>"

        result = convert(html.encode(), "a.html")

        assert result.text == "본문\n\n참고 상자"

    def test_sidebar_aside_outside_the_main_content_is_still_dropped(self):
        html = "<body><div class=wrap><aside><ul><li>메뉴 1</li><li>메뉴 2</li></ul></aside><div><p>본문</p></div></div></body>"

        result = convert(html.encode(), "a.html")

        assert result.text == "본문"


class TestBlankLinesInsideAQuote:
    """Every blank line at a block boundary inside a quote block turned into a `>`, so 3 to 8 empty `>` lines appeared between the
    `<div><br></div>` paragraphs of a Gmail reply (1.6 times the characters of the quoted body). Between paragraphs there is one
    empty `>` line."""

    def test_paragraphs_in_a_quote_are_separated_by_one_empty_quote_line(self):
        html = "<blockquote><div>a</div><div>b</div><div><br></div><div>c</div></blockquote>"

        result = convert(html.encode(), "a.html")

        assert result.text == "> a\n>\n> b\n>\n> c"

    def test_nested_quote_keeps_its_markers(self):
        html = "<blockquote><p>바깥</p><blockquote><div>안 1</div><div><br></div><div>안 2</div></blockquote><p>다시 바깥</p></blockquote>"

        result = convert(html.encode(), "a.html")

        assert result.text == "> 바깥\n>\n> > 안 1\n> >\n> > 안 2\n>\n> 다시 바깥"

    def test_blank_lines_inside_a_code_block_in_a_quote_are_kept(self):
        html = "<blockquote><pre>def a():\n    pass\n\n\ndef b():\n    pass</pre></blockquote>"

        result = convert(html.encode(), "a.html")

        assert "pass\n>\n>\n> def b():" in result.text


class TestNotionToDoList:
    """Whether each item of a to-do list in a Notion export (`ul.to-do-list` > `div.checkbox-on` / `checkbox-off`) is done was lost.
    They get the same symbols as a Confluence checklist."""

    def test_done_and_open_items_are_marked(self):
        html = ('<ul class="to-do-list"><li><div class="checkbox checkbox-on"></div> <span class="to-do-children-checked">자료 송부'
                '</span></li><li><div class="checkbox checkbox-off"></div> <span class="to-do-children-unchecked">계약서 검토</span>'
                '</li></ul><ul><li><div class="checkbox-on"></div>보통 목록</li></ul>')

        result = convert(html.encode(), "a.html")

        assert result.text == "- ☒ 자료 송부\n- ☐ 계약서 검토\n\n- 보통 목록"


class TestNameValueTableWithAFullWidthRow:
    """When an approval-request table whose name cells are th had a row with a full-width body cell (`td colspan=4`) in it, the
    `th, td` pair test broke and the values of the first row became the header (th). A row with a single cell is skipped by the
    test."""

    def test_first_row_stays_a_data_row(self):
        html = ("<table><tr><th>문서번호</th><td>경영-031</td><th>기안일자</th><td>2024-03-05</td></tr>"
                "<tr><th>기안부서</th><td>경영지원팀</td><th>기안자</th><td>홍길동</td></tr>"
                "<tr><th>제목</th><td colspan=3>노트북 구매의 건</td></tr>"
                "<tr><td colspan=4>1. 목적: 노후 장비 교체</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text == ("<table>\n<tr><td>문서번호</td><td>경영-031</td><td>기안일자</td><td>2024-03-05</td></tr>\n"
                               "<tr><td>기안부서</td><td>경영지원팀</td><td>기안자</td><td>홍길동</td></tr>\n"
                               '<tr><td>제목</td><td colspan="3">노트북 구매의 건</td></tr>\n'
                               '<tr><td colspan="4">1. 목적: 노후 장비 교체</td></tr>\n</table>')

    def test_one_pair_row_with_a_full_width_row_keeps_its_header(self):
        html = "<table><tr><th>구분</th><td>값</td></tr><tr><td colspan=2>설명</td></tr></table>"

        result = convert(html.encode(), "a.html")

        assert result.text.startswith("<table>\n<tr><th>구분</th><th>값</th></tr>")
