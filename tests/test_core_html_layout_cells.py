"""HTML: indent marks of layout cells, layout tables without a nested table, inline text of a table inside a cell, a <title> missing
from the body, Google Docs list levels, SVG text."""


from opendockit import convert


class TestIndentMarksOnlyForLabelCells:
    """The first-column indent mark (`└`) was also added to one-column layout boxes, cells spanning every column and cells holding
    blocks (headings, paragraphs), giving '└ ## 결재 요청 알림' and '└ 이달의 소식'. The indent mark is added only to the name cells
    of sub-accounts."""

    def test_one_column_layout_box_gets_no_mark(self):
        html = ('<table width="640"><tr><td style="padding:15px">그룹웨어</td></tr>'
                '<tr><td style="padding:30px"><h2>결재 요청 알림</h2><p>새로운 결재 문서가 도착했습니다.</p></td></tr>'
                '<tr><td style="padding:15px">본 메일은 발신 전용입니다.</td></tr></table>')

        result = convert(html.encode(), "a.html")

        assert "└" not in result.text
        assert "## 결재 요청 알림" in result.text

    def test_cell_spanning_every_column_gets_no_mark(self):
        html = ('<table><tr><td>로고</td><td>9월호</td></tr><tr><td colspan="2" style="padding:20px">이달의 소식: 신제품 출시</td></tr>'
                '<tr><td>A</td><td>B</td></tr></table>')

        result = convert(html.encode(), "a.html")

        assert "└" not in result.text
        assert "이달의 소식: 신제품 출시" in result.text

    def test_cell_holding_several_blocks_gets_no_mark(self):
        html = ('<table><tr><th>구분</th><th>내용</th></tr><tr><td>가</td><td>1</td></tr>'
                '<tr><td style="padding-left:20px"><p>나 설명</p><p>둘째 문단</p></td><td>2</td></tr></table>')

        result = convert(html.encode(), "a.html")

        assert "└" not in result.text

    def test_label_wrapped_in_one_paragraph_is_still_marked(self):
        """HTML saved by Word wraps the text of every cell in `<p style="margin-left:…">` — the sub-account mark stays."""
        html = ("<table><tr><td><p>과목</p></td><td><p>금액</p></td></tr><tr><td><p>매출원가</p></td><td><p>600</p></td></tr>"
                '<tr><td><p style="margin-left:20px">재료비</p></td><td><p>400</p></td></tr></table>')

        result = convert(html.encode(), "a.html")

        assert "| └ 재료비 | 400 |" in result.text


class TestLayoutTableWithoutANestedTable:
    """A layout table without a nested table (left menu | body) was rendered as a data table, so the whole body was joined with
    `<br>` in a single header cell and headings stayed inside the cell, '## ' and all. A table of three rows or fewer with a
    heading (h1–h6) in a cell, and a one-row table with three or more blocks in one cell, are unfolded as layouts."""

    def test_menu_and_body_cells_become_blocks(self):
        html = ('<table width="900"><tr><td width="180"><ul><li><a href="#">기관소개</a></li><li><a href="#">공지사항</a></li></ul></td>'
                "<td><h2>공지사항</h2><h3>2024년 하반기 채용 공고</h3><p>우리 기관은 신규 직원을 채용합니다.</p>"
                "<ul><li>제출서류: 이력서</li><li>문의: 02-123-4567</li></ul></td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text == ("- 기관소개\n- 공지사항\n\n## 공지사항\n\n### 2024년 하반기 채용 공고\n\n"
                               "우리 기관은 신규 직원을 채용합니다.\n\n- 제출서류: 이력서\n- 문의: 02-123-4567")

    def test_one_row_table_whose_cell_has_many_paragraphs_becomes_blocks(self):
        html = ("<table><tr><td><p>바로가기</p></td><td><p>첫째 문단입니다.</p><p>둘째 문단입니다.</p><p>셋째 문단입니다.</p></td>"
                "</tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text == "바로가기\n\n첫째 문단입니다.\n\n둘째 문단입니다.\n\n셋째 문단입니다."

    def test_data_table_with_a_list_and_paragraphs_in_cells_stays_a_table(self):
        html = ("<table><tr><th>항목</th><th>내용</th></tr><tr><td>제출서류</td><td><ul><li>이력서</li><li>자기소개서</li></ul></td></tr>"
                "<tr><td>접수</td><td><p>온라인</p><p>방문</p></td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text.startswith("| 항목 | 내용 |\n| --- | --- |\n| 제출서류 |")

    def test_pricing_table_with_headings_in_many_rows_stays_a_table(self):
        rows = "".join(f"<tr><td>기능 {index}</td><td>O</td><td>X</td></tr>" for index in range(4))
        html = f"<table><tr><th></th><th><h3>기본</h3></th><th><h3>고급</h3></th></tr>{rows}</table>"

        result = convert(html.encode(), "a.html")

        assert "| 기능 0 | O | X |" in result.text

    def test_heading_inside_a_small_cell_or_a_nested_one_row_table_keeps_the_table(self):
        inner = "<table><tr><td>번호<h1>제목</h1>합계</td></tr></table>"
        html = (f"<table><tr><th>구분</th><th>내용</th></tr><tr><td>가</td><td>{inner}</td></tr>"
                "<tr><td><h4>나</h4>설명</td><td>아주 긴 내용이 이어지는 칸입니다. 이 칸이 표 글의 대부분을 차지합니다.</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text.startswith("| 구분 | 내용 |\n| --- | --- |\n| 가 | 번호 제목 합계 |")


class TestNestedTableFlattenedIntoACell:
    """A table inside a Markdown table cell was flattened with `get_text(" ")`, inserting a space at every inline tag —
    `매출 1 2 ,345원`, `CO _2 10 ^6 톤`. It is flattened cell by cell with the same inline rendering as headings."""

    def test_inline_tags_inside_a_flattened_nested_table_do_not_split_words(self):
        inner = "<table><tr><td>매출 1<b>2</b>,345원, 인사<em>발령</em></td><td>CO<sub>2</sub> 10<sup>6</sup>톤</td></tr></table>"
        html = (f"<table border=1><tr><th>구분</th><th>내용</th><th>비고</th></tr><tr><td>가</td><td>{inner}</td><td>x</td></tr>"
                "<tr><td>나</td><td>일반</td><td>y</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert "| 가 | 매출 12,345원, 인사발령 CO_2 10^6톤 | x |" in result.text


class TestTitleWhenTheBodyHasNoHeading:
    """The `<title>` was only in the metadata and not in the body, so the text had no title. When the body has no heading element
    at all, it is placed at the very front as `# title`."""

    def test_title_goes_on_top_of_a_body_without_headings(self):
        html = "<html><head><title>2024년 개인정보 처리방침</title></head><body><p>제1조(목적) 이 방침은 ...</p></body></html>"

        result = convert(html.encode(), "a.html")

        assert result.text == "# 2024년 개인정보 처리방침\n\n제1조(목적) 이 방침은 ..."

    def test_body_with_a_heading_keeps_its_own_text(self):
        html = "<html><head><title>사이트 | 공지</title></head><body><h2>공지</h2><p>내용</p></body></html>"

        result = convert(html.encode(), "a.html")

        assert result.text == "## 공지\n\n내용"

    def test_title_already_at_the_start_is_not_repeated(self):
        html = "<html><head><title>회의록</title></head><body><p><b>회의록</b></p><p>내용</p></body></html>"

        result = convert(html.encode(), "a.html")

        assert result.text == "회의록\n\n내용"

    def test_html_mail_does_not_get_its_title_as_a_heading(self):
        mail = ("From: a@b.com\r\nTo: c@d.com\r\nSubject: 안내\r\nMIME-Version: 1.0\r\nContent-Type: text/html; charset=utf-8\r\n\r\n"
                "<html><head><title>메일 제목</title></head><body><p>본문</p></body></html>")

        result = convert(mail.encode(), "a.eml")

        assert "# 메일 제목" not in result.text


class TestGoogleDocsListLevels:
    """HTML exported from Google Docs puts a separate `<ul class="lst-kix_…-N">` per list level side by side and indents with CSS
    only. The levels were lost, so sub-items read at the same level as their parents. The `-N` at the end of the class is the
    level."""

    def test_list_level_from_the_class_suffix_is_nested(self):
        html = ('<html><head><style>.c1{margin-left:36pt}.c2{margin-left:72pt}</style></head><body>'
                '<ul class="c4 lst-kix_a-0 start"><li class="c1">안건 1</li></ul>'
                '<ul class="c4 lst-kix_a-1 start"><li class="c2">세부 1-1</li><li class="c2">세부 1-2</li></ul>'
                '<ul class="c4 lst-kix_a-0"><li class="c1">안건 2</li></ul></body></html>')

        result = convert(html.encode(), "g.html")

        assert result.text == "- 안건 1\n  - 세부 1-1\n  - 세부 1-2\n- 안건 2"

    def test_numbered_google_docs_list_keeps_its_numbers(self):
        html = ('<ol class="lst-kix_b-0 start" start="1"><li>첫째</li></ol><ol class="lst-kix_b-1 start" start="1"><li>하위</li></ol>'
                '<ol class="lst-kix_b-0" start="2"><li>둘째</li></ol>')

        result = convert(html.encode(), "g.html")

        assert result.text == "1. 첫째\n  1. 하위\n2. 둘째"


class TestTextInsideInlineSvg:
    """Inline `<svg>` was removed whole, so text written with `<text>` ('매출 120억') vanished. Only the text of a picture with ten
    text elements or fewer is kept."""

    def test_short_svg_text_is_kept(self):
        html = '<p>요약</p><svg width="100" height="50"><text x="0" y="20">매출 120억</text></svg><p>끝</p>'

        result = convert(html.encode(), "a.html")

        assert result.text == "요약\n\n매출 120억\n\n끝"

    def test_icon_without_text_and_chart_with_many_labels_are_dropped(self):
        ticks = "".join(f"<text>{index * 10}</text>" for index in range(20))
        html = f'<p>가</p><svg><path d="M0 0"/></svg><svg>{ticks}</svg><p>나</p>'

        result = convert(html.encode(), "a.html")

        assert result.text == "가\n\n나"
