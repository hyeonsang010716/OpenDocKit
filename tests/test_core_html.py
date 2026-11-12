"""HTML rendering: content placed outside the body, MathML equations, block elements."""

import warnings

import pytest

from opendockit import DocumentFormat, convert
from tests import needs_html5_tokenizer


class TestContentOutsideTheBody:
    """lxml puts content after `</html>` and a second concatenated document outside the body (a second html root and so on). Only
    the body was read, so that content vanished without a warning. A browser shows all of it in document order."""

    @pytest.mark.parametrize(
        ("html", "expected"),
        [
            ("<html><body><p>본문</p></body></html><p>꼬리 글</p>", "본문\n\n꼬리 글"),
            ("<html><body><p>본문</p></body><p>바디 뒤</p></html>", "본문\n\n바디 뒤"),
            ("<html><body><p>본문</p></body></html>\n꼬리 텍스트", "본문\n\n꼬리 텍스트"),
            ("<html><body><p>표1</p></body></html><table><tr><td>a</td><td>1</td></tr></table>",
             "표1\n\n| a | 1 |\n| --- | --- |"),
        ],
    )
    @needs_html5_tokenizer
    def test_content_after_the_body_is_kept_in_document_order(self, html, expected):
        data = html.encode()

        result = convert(data, "a.html")

        assert result.text == expected

    @needs_html5_tokenizer
    def test_second_concatenated_document_is_kept(self):
        html = ('<html><head><meta charset="utf-8"><title>내보내기</title></head><body><table><tr><th>이름</th><th>금액</th></tr>'
                '<tr><td>홍</td><td>100</td></tr></table></body></html>\n'
                '<html><body><table><tr><th>이름</th><th>금액</th></tr><tr><td>김</td><td>200</td></tr></table></body></html>')

        result = convert(html.encode(), "a.html")

        assert result.text == ("# 내보내기\n\n| 이름 | 금액 |\n| --- | --- |\n| 홍 | 100 |\n\n"
                               "| 이름 | 금액 |\n| --- | --- |\n| 김 | 200 |")
        assert result.metadata["title"] == "내보내기"

    def test_doctype_and_comments_outside_the_document_are_not_text(self):
        html = "<!DOCTYPE html>\n<!-- 앞 주석 -->\n<html><head><title>t</title></head><body><p>x</p></body></html>\n<!-- 뒤 -->\n"

        result = convert(html.encode(), "a.html")

        assert result.text == "# t\n\nx"


class TestMathMl:
    """MathML was read by joining its characters only, so `r²π` read as `r2π` and the fraction 3/4 as `34`. The LaTeX the document
    wrote (annotation, alttext) is used; without it the structure is linearized LaTeX-style. The same ` $…$ ` notation as Office
    and Hangul (HWP) equations."""

    def test_tex_annotation_is_used_once(self):
        """LaTeXML (arXiv HTML) output: the characters of the presentation MathML and of the annotation were joined, giving
        `x2+12x^{2}+\\frac{1}{2}`."""
        html = ('<p>손실은 <math alttext="x^{2}+\\frac{1}{2}" display="inline"><semantics><mrow><msup><mi>x</mi><mn>2</mn></msup>'
                '<mo>+</mo><mfrac><mn>1</mn><mn>2</mn></mfrac></mrow>'
                '<annotation encoding="application/x-tex">x^{2}+\\frac{1}{2}</annotation></semantics></math> 이다.</p>')

        result = convert(html.encode(), "a.html")

        assert result.text == "손실은 $x^{2}+\\frac{1}{2}$ 이다."

    def test_alttext_is_used_when_there_is_no_annotation(self):
        html = '<p>값 <math alttext="{\\displaystyle a_{n}}"><msub><mi>a</mi><mi>n</mi></msub></math> 을 본다.</p>'

        result = convert(html.encode(), "a.html")

        assert result.text == "값 $a_{n}$ 을 본다."

    def test_presentation_mathml_is_linearized(self):
        html = ("<p>면적 <math><msup><mi>r</mi><mn>2</mn></msup><mi>π</mi></math>, 비율 <math><mfrac><mn>3</mn><mn>4</mn></mfrac></math>, "
                "근 <math><msqrt><mn>2</mn></msqrt></math>, 첨자 <math><msub><mi>a</mi><mn>10</mn></msub></math>, "
                "세제곱근 <math><mroot><mi>x</mi><mn>3</mn></mroot></math>, "
                "합 <math><munderover><mo>∑</mo><mrow><mi>i</mi><mo>=</mo><mn>1</mn></mrow><mi>n</mi></munderover><mi>i</mi></math></p>")

        result = convert(html.encode(), "a.html")

        assert result.text == ("면적 $r^2π$, 비율 $\\frac{3}{4}$, 근 $\\sqrt{2}$, 첨자 $a_{10}$, 세제곱근 $\\sqrt[3]{x}$, "
                               "합 $∑_{i=1}^{n}i$")

    def test_math_inside_a_table_cell_stays_in_the_cell(self):
        html = ("<table><tr><th>항목</th><th>식</th></tr>"
                "<tr><td>넓이</td><td><math><mi>π</mi><msup><mi>r</mi><mn>2</mn></msup></math></td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text == "| 항목 | 식 |\n| --- | --- |\n| 넓이 | $πr^2$ |"


class TestBlockElements:
    def test_legend_is_a_line_of_its_own(self):
        """`<legend>` was read as inline and ran into the following text, as in '신청인 정보이름: 홍길동'."""
        html = "<form><fieldset><legend>신청인 정보</legend>이름: 홍길동</fieldset></form>"

        result = convert(html.encode(), "a.html")

        assert result.text == "신청인 정보\n\n이름: 홍길동"


class TestRowsLeftOutByTheGridLimit:
    """HTML also reports, with a warning like the other formats (DOCX, PPTX, HWP, HWPX), the bottom rows dropped by the grid cell
    limit (`markdown._MAX_GRID_CELLS`). In a table with very many rows, such as an ERP's 'Excel download' HTML, the bottom rows
    vanished without a warning."""

    def test_rows_left_out_are_reported(self, monkeypatch):
        monkeypatch.setattr("opendockit.markdown._MAX_GRID_CELLS", 20)
        rows = "".join(f"<tr><td>행{index}</td><td>{index}</td></tr>" for index in range(30))
        data = f"<table><tr><th>이름</th><th>값</th></tr>{rows}</table>".encode()

        result = convert(data, "a.html")

        assert "행29" not in result.text
        assert "The table is too large; the bottom 21 row(s) could not be converted." in result.warnings

    def test_small_table_has_no_warning(self):
        data = b"<table><tr><th>A</th><th>B</th></tr><tr><td>1</td><td>2</td></tr></table>"

        result = convert(data, "a.html")

        assert result.warnings == []


class TestNestedListInsideATableCell:
    """A nested list was detached into a placeholder while rendering and not put back. A table with a table inside a cell renders
    its cells twice (flattened, and embedded), and the second rendering saw only the placeholder text and could not find the
    detached list, so the whole document was rejected as 'corrupted' with an IndexError."""

    def test_list_cell_next_to_a_cell_holding_a_table_is_rendered(self):
        html = ("<table border=1><tr><th>항목</th><th colspan=2>내용</th></tr>"
                "<tr><td>사유</td><td colspan=2><ol><li>장비 교체<ul><li>노트북 5대</li><li>모니터 3대</li></ul></li>"
                "<li>신규 지급</li></ol></td></tr>"
                "<tr><td>품목</td><td colspan=2><table><tr><th>품목</th><th>수량</th></tr><tr><td>노트북</td><td>5</td></tr>"
                "</table></td></tr><tr><td>결재</td><td>팀장</td><td>본부장</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert "1. 장비 교체<br>- 노트북 5대<br>- 모니터 3대<br>2. 신규 지급" in result.text
        assert "<td>노트북</td><td>5</td>" in result.text


_DATASET_XML = ('<?xml version="1.0" standalone="yes"?>\n<NewDataSet>\n  <Table>\n    <EMP_NO>1001</EMP_NO>\n'
                '    <EMP_NM>홍길동</EMP_NM>\n  </Table>\n  <Table>\n    <EMP_NO>1002</EMP_NO>\n    <EMP_NM>김철수</EMP_NM>\n'
                '  </Table>\n</NewDataSet>')


class TestXmlDataWithHtmlElementNames:
    """XML data using an element with the same name as an HTML one (`<Table>`), like the `<NewDataSet><Table>…` of .NET
    `DataSet.WriteXml`, was detected as HTML, so cell values ran together as '1001홍길동' and bs4's XMLParsedAsHTMLWarning leaked
    out to the caller. The same XML without `<Table>` is text, exactly as written."""

    @pytest.mark.parametrize("filename", ["export.xml", None])
    def test_xml_data_whose_root_is_not_html_is_text(self, filename):
        data = _DATASET_XML.encode()

        result = convert(data, filename)

        assert result.format is DocumentFormat.TEXT
        assert result.text == _DATASET_XML

    @pytest.mark.parametrize("html", [
        '<?xml version="1.0" encoding="utf-8"?>\n<!DOCTYPE html>\n<html xmlns="http://www.w3.org/1999/xhtml"><body>'
        '<table><tr><td>a</td><td>1</td></tr></table></body></html>',
        '<?xml version="1.0" encoding="utf-8"?>\n<TABLE><TR><TD>a</TD><TD>1</TD></TR></TABLE>',
    ])
    def test_xhtml_and_html_exports_with_an_xml_declaration_stay_html_without_leaking_a_warning(self, html):
        data = html.encode()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = convert(data, None)

        assert result.format is DocumentFormat.HTML
        assert result.text == "| a | 1 |\n| --- | --- |"
        assert [warning.category.__name__ for warning in caught] == []


_ARIA_GRID = """<html><body><h2>주문 목록</h2>
<div role="grid" class="ag-root">
<div role="row" class="ag-header-row"><div role="columnheader">주문번호</div><div role="columnheader">고객</div>
<div role="columnheader">금액</div><div role="columnheader">상태</div></div>
<div role="row"><div role="gridcell">A-1001</div><div role="gridcell"></div><div role="gridcell">15,000</div>
<div role="gridcell">배송중</div></div>
<div role="row"><div role="gridcell">A-1002</div><div role="gridcell"><span>김철수</span></div><div role="gridcell">230,000</div>
<div role="gridcell">취소</div></div>
</div></body></html>"""
_ORDER_TABLE = ("| 주문번호 | 고객 | 금액 | 상태 |\n| --- | --- | --- | --- |\n| A-1001 | | 15,000 | 배송중 |\n"
                "| A-1002 | 김철수 | 230,000 | 취소 |")


class TestAriaGridTables:
    """A div table built with ARIA roles (`role=grid|table|treegrid` > `row` > `cell|gridcell|columnheader|rowheader`) and no
    `<table>` (AG Grid, the list screens of in-house systems) was scattered into a paragraph per cell, and an empty cell shifted
    the columns. It is rendered as a grid, like a real table."""

    def test_div_grid_is_a_table_and_an_empty_cell_keeps_its_column(self):
        result = convert(_ARIA_GRID.encode(), "a.html")

        assert result.text == "## 주문 목록\n\n" + _ORDER_TABLE

    def test_role_table_with_row_groups_and_row_headers(self):
        html = ('<div role="table"><div role="rowgroup"><div role="row"><span role="columnheader">이름</span>'
                '<span role="columnheader">점수</span></div></div><div role="rowgroup"><div role="row">'
                '<span role="rowheader">홍길동</span><span role="cell">90</span></div><div role="row">'
                '<span role="rowheader">김철수</span><span role="cell">80</span></div></div></div>')

        result = convert(html.encode(), "a.html")

        assert result.text == "| 이름 | 점수 |\n| --- | --- |\n| 홍길동 | 90 |\n| 김철수 | 80 |"

    def test_column_index_places_cells_of_a_row_with_missing_cells(self):
        html = ('<div role="grid"><div role="row"><div role="columnheader" aria-colindex="1">일자</div>'
                '<div role="columnheader" aria-colindex="2">입금</div><div role="columnheader" aria-colindex="3">출금</div></div>'
                '<div role="row"><div role="gridcell" aria-colindex="1">03-01</div>'
                '<div role="gridcell" aria-colindex="3">5,000</div></div></div>')

        result = convert(html.encode(), "a.html")

        assert result.text == "| 일자 | 입금 | 출금 |\n| --- | --- | --- |\n| 03-01 | | 5,000 |"

    def test_grid_role_without_rows_is_read_as_before(self):
        html = '<div role="grid"><p>목록이 비었습니다.</p></div><table role="grid"><tr><td>가</td><td>1</td></tr></table>'

        result = convert(html.encode(), "a.html")

        assert result.text == "목록이 비었습니다.\n\n| 가 | 1 |\n| --- | --- |"


_CONTRACT = """<html><body><h3>계약 개요</h3>
<table border=1>
<tr><th>계약명</th><td>2024 서버 유지보수</td><th>계약번호</th><td>C-2024-031</td></tr>
<tr><th>계약금액</th><td>13,200,000원</td><th>계약기간</th><td>2024.04.01~2025.03.31</td></tr>
<tr><th>발주처</th><td>대한물산</td><th>담당자</th><td>김부장</td></tr>
</table>
<table border=1>
<tr><th>계약명</th><td>2024 서버 유지보수</td></tr>
<tr><th>계약금액</th><td>13,200,000원</td></tr>
</table></body></html>"""


class TestNameValueTablesWithRowHeaders:
    """In a name | value table whose every row has the shape `th, td, th, td` (the name cells are th), the values of the first row
    became the header (an LLM reads '계약명' as a column name). It is rendered as a table with no header (an empty header row). A
    table whose first row is a column header ('역할 | 이름 | 연락처', only the first cell th) is unchanged."""

    def test_name_value_rows_are_all_data_rows(self):
        result = convert(_CONTRACT.encode(), "a.html")

        assert ("| --- | --- | --- | --- |\n| 계약명 | 2024 서버 유지보수 | 계약번호 | C-2024-031 |\n"
                "| 계약금액 | 13,200,000원 | 계약기간 | 2024.04.01~2025.03.31 |") in result.text
        assert "| --- | --- |\n| 계약명 | 2024 서버 유지보수 |\n| 계약금액 | 13,200,000원 |" in result.text

    def test_name_value_form_with_a_wide_value_has_no_header_cells(self):
        html = ("<table><tr><th>성명</th><td>홍길동</td><th>직급</th><td>과장</td></tr>"
                "<tr><th>부서</th><td>재무팀</td><th>입사일</th><td>2019-03-02</td></tr>"
                "<tr><th>연락처</th><td colspan=3>010-1234-5678</td></tr></table>")

        result = convert(html.encode(), "a.html")

        assert result.text == ("<table>\n<tr><td>성명</td><td>홍길동</td><td>직급</td><td>과장</td></tr>\n"
                               "<tr><td>부서</td><td>재무팀</td><td>입사일</td><td>2019-03-02</td></tr>\n"
                               '<tr><td>연락처</td><td colspan="3">010-1234-5678</td></tr>\n</table>')

    @pytest.mark.parametrize("html", [
        "<table><tr><th>역할</th><td>이름</td><td>연락처</td></tr><tr><th>배포 책임</th><td>홍길동</td><td>010</td></tr></table>",
        "<table><tr><th>구분</th><th>값</th></tr><tr><th>가</th><td>1</td></tr><tr><th>나</th><td>2</td></tr></table>",
        "<table><tr><td>계약명</td><td>서버</td></tr><tr><th>금액</th><td>1,000</td></tr></table>",
    ])
    def test_tables_whose_first_row_can_be_a_header_keep_it(self, html):
        result = convert(html.encode(), "a.html")

        assert "| --- |" in result.text and not result.text.startswith("|  |")


class TestConfluenceTaskList:
    """Whether each item of a Confluence checklist (`<ul class="inline-task-list"><li class="checked">`) is done was lost. They get
    the same symbols as form check boxes."""

    def test_checked_and_open_tasks_are_marked(self):
        html = ('<ul class="inline-task-list"><li class="checked">스테이징 검증</li><li>DB 백업</li>'
                '<li class="checked">릴리스 노트 작성</li></ul><ul><li class="checked">보통 목록</li></ul>')

        result = convert(html.encode(), "a.html")

        assert result.text == "- ☒ 스테이징 검증\n- ☐ DB 백업\n- ☒ 릴리스 노트 작성\n\n- 보통 목록"


_WORD_HTML_LIST = """<html><head><meta http-equiv=Content-Type content="text/html; charset=utf-8"></head>
<body><div class=WordSection1>
<p class=MsoListParagraph style='mso-list:l0 level1 lfo1'><![if !supportLists]><span style='font-family:Wingdings'>§<span
style='font:7.0pt "Times New Roman"'>&nbsp; </span></span><![endif]>1단계: 현황 조사</p>
<p class=MsoListParagraph style='mso-list:l0 level2 lfo1'><![if !supportLists]><span style='font-family:Wingdings;mso-fareast-font-family:Wingdings'>Ø<span
style='font:7.0pt "Times New Roman"'>&nbsp; </span></span><![endif]>세부: 설문</p>
<p class=MsoListParagraph style='mso-list:l1 level1 lfo2'><![if !supportLists]><span style='font-family:Symbol'>·<span>&nbsp;&nbsp;</span></span><![endif]>항목</p>
<p class=MsoNormal><span style='font-family:Wingdings'>þ</span> 동의 <span style='font-family:Wingdings'>¨</span> 미동의</p>
<p class=MsoNormal><span style='font-family:"Wingdings 2"'>R</span> 확인함</p>
<p class=MsoNormal><font face="Wingdings">x</font> 옛 메일 체크 <span style="font-family: Symbol">a</span>=0.5</p>
</div></body></html>"""


class TestSymbolFontsInHtml:
    """An .htm saved by Word and Outlook HTML mail write bullets and check boxes drawn with a symbol font (Wingdings, Wingdings 2,
    Symbol) as the character at that code in the font (`§`, `Ø`, `þ`, `¨`, `R`). The font was not looked at, so checked and
    unchecked items came out as `þ` and `¨`. They are converted with the same per-font table as DOCX and PPTX
    (`_shared.SYMBOL_GLYPHS`)."""

    def test_symbol_font_characters_become_the_symbols_they_show(self):
        data = _WORD_HTML_LIST.encode()

        result = convert(data, "a.htm")

        # the bullet symbols (▪, ➢, •) of list paragraphs are replaced by the `-` of the nested list
        # (test_core_word_lists_cell_blocks_and_mail_lines.py)
        assert result.text == ("- 1단계: 현황 조사\n  - 세부: 설문\n- 항목\n\n☑ 동의 □ 미동의\n\n☑ 확인함\n\n"
                               "☒ 옛 메일 체크 α=0.5")

    def test_text_in_an_ordinary_font_inside_a_symbol_font_span_is_kept(self):
        data = ("<p><span style='font-family:Wingdings'>þ<span style='font-family:\"맑은 고딕\"'> 동의 þ</span></span>"
                "<span style='font-family:Symbol'> 가 </span></p>").encode()

        result = convert(data, "a.html")

        assert result.text == "☑ 동의 þ 가"

    @pytest.mark.timing
    def test_symbol_font_in_the_font_shorthand_and_deep_nesting(self):
        import time

        data = ("<p><span style='font: 10pt Wingdings'>þ</span> 확인</p>" + "<font face='Wingdings'>þ" * 3000).encode()
        started = time.perf_counter()

        result = convert(data, "a.html")

        assert time.perf_counter() - started < 5.0
        assert result.text.startswith("☑ 확인")
        assert "þ" not in result.text


class TestOlderHtmlParser:
    """lxml's Windows wheels bundle libxml2 2.11, without the HTML5 tokenizer the extractor relies on. There, text after
    `</html>` and further HTML parts of a mail went missing with no sign that anything was wrong."""

    def test_missing_html5_tokenizer_is_reported_once(self, monkeypatch):
        monkeypatch.setattr("opendockit.extractors.html.HTML5_TOKENIZER", False)

        result = convert("<html><body><p>본문</p></body></html>".encode(), "a.html")

        assert [warning for warning in result.warnings if "libxml2 2.14" in warning] == [
            "The installed lxml bundles an HTML parser older than libxml2 2.14 (as its Windows builds do), so content of "
            "malformed or multi-part HTML and mail (text after the end of the page, further mail parts) may be missing."]

    def test_current_parser_adds_no_warning(self, monkeypatch):
        monkeypatch.setattr("opendockit.extractors.html.HTML5_TOKENIZER", True)

        assert convert("<html><body><p>본문</p></body></html>".encode(), "a.html").warnings == []

