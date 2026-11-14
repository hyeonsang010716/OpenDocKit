"""OpenDocument (ODT, ODS, ODP): structure, options, the character limit and hostile input.

Every document is an ODF package built in memory (`_package`): a ZIP with `mimetype`, `content.xml` and, when a test needs them,
`styles.xml`, `meta.xml`, a manifest and extra parts. Decisions are recorded in docs/decisions/odf.md.
"""

from io import BytesIO
import time
import zipfile

import pytest

from opendockit import ConvertOptions, CorruptedDocumentError, EncryptedDocumentError, convert
from opendockit.extractors import odf as odf_module
from opendockit.extractors.odf import extract_odt
from opendockit.models import DocumentFormat, SectionKind
from tests import builder


_NAMESPACES = " ".join(f'xmlns:{prefix}="{uri}"' for prefix, uri in {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "text": "urn:oasis:names:tc:opendocument:xmlns:text:1.0",
    "table": "urn:oasis:names:tc:opendocument:xmlns:table:1.0",
    "draw": "urn:oasis:names:tc:opendocument:xmlns:drawing:1.0",
    "style": "urn:oasis:names:tc:opendocument:xmlns:style:1.0",
    "fo": "urn:oasis:names:tc:opendocument:xmlns:xsl-fo-compatible:1.0",
    "svg": "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0",
    "presentation": "urn:oasis:names:tc:opendocument:xmlns:presentation:1.0",
    "chart": "urn:oasis:names:tc:opendocument:xmlns:chart:1.0",
    "meta": "urn:oasis:names:tc:opendocument:xmlns:meta:1.0",
    "dc": "http://purl.org/dc/elements/1.1/",
    "xlink": "http://www.w3.org/1999/xlink",
    "math": "http://www.w3.org/1998/Math/MathML",
}.items())
_KINDS = {"text": "odt", "spreadsheet": "ods", "presentation": "odp"}
# Named styles of a Writer document: a bulleted and a numbered list style (level 2 of the numbered one uses Korean syllables).
_LIST_STYLES = (
    '<office:styles>'
    '<text:list-style style:name="Bullets"><text:list-level-style-bullet text:level="1" text:bullet-char="•"/>'
    '<text:list-level-style-bullet text:level="2" text:bullet-char="–"/></text:list-style>'
    '<text:list-style style:name="Numbers"><text:list-level-style-number text:level="1" style:num-format="1" style:num-suffix="."/>'
    '<text:list-level-style-number text:level="2" style:num-format="가, 나, 다, ..." style:num-suffix="."/></text:list-style>'
    '</office:styles>'
)


def _package(kind: str, body: str, *, automatic: str = "", styles: str | None = None, title: str | None = None,
             manifest: str | None = None, parts: dict[str, bytes] | None = None, content: bytes | None = None) -> bytes:
    """An ODF package. `body` goes inside `office:text` / `office:spreadsheet` / `office:presentation`, `automatic` inside the
    automatic styles of content.xml, `styles` inside the root of styles.xml. `content` replaces content.xml as raw bytes."""
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(zipfile.ZipInfo("mimetype"), f"application/vnd.oasis.opendocument.{kind}")
        archive.writestr("content.xml", content if content is not None else (
            f'<?xml version="1.0" encoding="UTF-8"?><office:document-content {_NAMESPACES}>'
            f'<office:automatic-styles>{automatic}</office:automatic-styles>'
            f'<office:body><office:{kind}>{body}</office:{kind}></office:body></office:document-content>').encode())
        if styles is not None:
            archive.writestr("styles.xml", f'<office:document-styles {_NAMESPACES}>{styles}</office:document-styles>'.encode())
        if title is not None:
            archive.writestr("meta.xml", f'<office:document-meta {_NAMESPACES}><office:meta><dc:title>{title}</dc:title>'
                                         '<meta:initial-creator>김민수</meta:initial-creator></office:meta></office:document-meta>')
        if manifest is not None:
            archive.writestr("META-INF/manifest.xml", manifest)
        for name, data in (parts or {}).items():
            archive.writestr(name, data)
    return buffer.getvalue()


def _convert(kind: str, body: str, options: ConvertOptions | None = None, **package):
    return convert(_package(kind, body, **package), f"sample.{_KINDS[kind]}", options)


def _odt(body: str, options: ConvertOptions | None = None, **package) -> str:
    return _convert("text", body, options, **package).text


def _row(*cells: str) -> str:
    return "<table:table-row>" + "".join(cells) + "</table:table-row>"


def _cell(text: str = "", attributes: str = "") -> str:
    return f"<table:table-cell {attributes}>{f'<text:p>{text}</text:p>' if text else ''}</table:table-cell>"


def _sheet(name: str, *rows: str, attributes: str = "") -> str:
    return f'<table:table table:name="{name}" {attributes}>{"".join(rows)}</table:table>'


def _slide(*shapes: str, attributes: str = "") -> str:
    return f"<draw:page {attributes}>{''.join(shapes)}</draw:page>"


def _text_frame(content: str, attributes: str = "") -> str:
    return f"<draw:frame {attributes}><draw:text-box>{content}</draw:text-box></draw:frame>"


class TestTextDocument:
    def test_format_is_detected_and_title_comes_from_meta(self):
        """A package whose mimetype says text converts as ODT into one DOCUMENT section; the title of meta.xml becomes metadata as
        in the other formats."""
        result = _convert("text", "<text:p>본문입니다.</text:p>", title="분기 보고서")
        assert result.format is DocumentFormat.ODT
        assert [section.kind for section in result.sections] == [SectionKind.DOCUMENT]
        assert result.metadata["title"] == "분기 보고서"
        assert result.metadata["author"] == "김민수"
        assert result.text == "본문입니다."

    def test_headings_follow_the_outline_level_and_the_title_style(self):
        """`text:h` with outline levels 1, 2 and 9 and a paragraph in the 'Title' style: `#` per level (at most six), and the
        title is a level-1 heading as in the DOCX the document came from."""
        text = _odt('<text:p text:style-name="Title">사업 보고서</text:p>'
                    '<text:h text:outline-level="1">1. 개요</text:h>'
                    '<text:h text:outline-level="2">1.1 배경</text:h>'
                    '<text:h text:outline-level="9">깊은 제목</text:h>'
                    '<text:p>본문</text:p>')
        assert text == "# 사업 보고서\n\n# 1. 개요\n\n## 1.1 배경\n\n###### 깊은 제목\n\n본문"

    def test_paragraph_in_a_heading_style_is_a_heading(self):
        """LibreOffice may write a heading as `text:p` with an automatic style derived from 'Heading 1' (seen in its HTML import).
        The style chain decides; a table cell in the same style stays plain text."""
        automatic = '<style:style style:name="P1" style:family="paragraph" style:parent-style-name="Heading_20_2"/>'
        text = _odt('<text:p text:style-name="P1">제1장 개요</text:p><text:p>본문</text:p><table:table>'
                    + _row('<table:table-cell><text:p text:style-name="P1">머리</text:p></table:table-cell>', _cell("값"))
                    + _row(_cell("가"), _cell("1")) + '</table:table>', automatic=automatic)
        assert text == "## 제1장 개요\n\n본문\n\n| 머리 | 값 |\n| --- | --- |\n| 가 | 1 |"

    def test_bulleted_and_numbered_lists_are_told_apart_by_the_list_style(self):
        """Two lists that differ only in their list style (bullet level style / number level style in styles.xml)."""
        text = _odt('<text:list text:style-name="Bullets"><text:list-item><text:p>사과</text:p></text:list-item>'
                    '<text:list-item><text:p>배</text:p></text:list-item></text:list>'
                    '<text:p>절차</text:p>'
                    '<text:list text:style-name="Numbers"><text:list-item><text:p>수집</text:p></text:list-item>'
                    '<text:list-item><text:p>검증</text:p></text:list-item></text:list>', styles=_LIST_STYLES)
        assert text == "- 사과\n- 배\n\n절차\n\n1. 수집\n2. 검증"

    def test_nested_list_levels_are_indented_and_numbered_per_level(self):
        """A numbered list whose first item holds a nested list (level 2: Korean syllable numbers), then a second top-level
        item: the nested items are indented two spaces and take the '- 가.' form, and the outer numbering goes on."""
        text = _odt('<text:list text:style-name="Numbers">'
                    '<text:list-item><text:p>준비</text:p><text:list>'
                    '<text:list-item><text:p>자료</text:p></text:list-item>'
                    '<text:list-item><text:p>인원</text:p></text:list-item></text:list></text:list-item>'
                    '<text:list-item><text:p>실행</text:p></text:list-item></text:list>', styles=_LIST_STYLES)
        assert text == "1. 준비\n  - 가. 자료\n  - 나. 인원\n2. 실행"

    def test_numbering_continues_across_lists_only_when_the_file_says_so(self):
        """Three lists with the same style separated by paragraphs: the second continues (`text:continue-numbering`), the third
        starts again."""
        item = '<text:list-item><text:p>{}</text:p></text:list-item>'
        text = _odt(f'<text:list text:style-name="Numbers">{item.format("하나")}</text:list><text:p>사이</text:p>'
                    f'<text:list text:style-name="Numbers" text:continue-numbering="true">{item.format("둘")}</text:list>'
                    f'<text:p>사이</text:p><text:list text:style-name="Numbers">{item.format("다시")}</text:list>',
                    styles=_LIST_STYLES)
        assert text == "1. 하나\n\n사이\n\n2. 둘\n\n사이\n\n1. 다시"

    def test_nested_list_with_its_own_style_restarts_under_each_item(self):
        """Nested lists that name a style of their own (as pandoc writes them): the letters restart under every item of the
        outer list and in the next list, as on screen. Counted per style, they ran on to 'c) b1' and 'c) x1'."""
        styles = ('<office:styles>'
                  '<text:list-style style:name="L1"><text:list-level-style-number text:level="1" style:num-format="1"/>'
                  '</text:list-style>'
                  '<text:list-style style:name="L2"><text:list-level-style-number text:level="1" style:num-format="1"/>'
                  '<text:list-level-style-number text:level="2" style:num-format="a" style:num-suffix=")"/></text:list-style>'
                  '</office:styles>')

        def item(text: str, *subitems: str) -> str:
            nested = ('<text:list text:style-name="L2">' + "".join(item(sub) for sub in subitems) + "</text:list>") if subitems else ""
            return f"<text:list-item><text:p>{text}</text:p>{nested}</text:list-item>"

        text = _odt(f'<text:list text:style-name="L1">{item("가", "a1", "a2")}{item("나", "b1")}</text:list>'
                    f'<text:p>사이</text:p><text:list text:style-name="L1">{item("다", "x1")}</text:list>', styles=styles)
        assert text == "1. 가\n  - a) a1\n  - b) a2\n2. 나\n  - a) b1\n\n사이\n\n1. 다\n  - a) x1"

    def test_continue_list_continues_the_list_it_names(self):
        """`text:continue-list` names the list it continues by `xml:id`: across a list of the same style that started again in
        between, the numbers go on from the named list (and the style comes from it when the continuing list names none)."""
        item = '<text:list-item><text:p>{}</text:p></text:list-item>'
        text = _odt(f'<text:list xml:id="list1" text:style-name="Numbers">{item.format("하나")}{item.format("둘")}</text:list>'
                    f'<text:list text:style-name="Numbers">{item.format("다른 목록")}</text:list>'
                    f'<text:list text:continue-list="list1">{item.format("셋")}</text:list>', styles=_LIST_STYLES)
        assert text == "1. 하나\n2. 둘\n1. 다른 목록\n3. 셋"

    def test_heading_in_a_numbered_list_counts_as_an_item(self):
        """A heading inside a numbered list item takes its number after the `#`, and the next item counts on from it."""
        item = '<text:list-item>{}</text:list-item>'
        text = _odt('<text:list text:style-name="Numbers">'
                    + item.format('<text:h text:outline-level="1">개요</text:h>') + item.format("<text:p>본문</text:p>")
                    + "</text:list>", styles=_LIST_STYLES)
        assert text == "# 1. 개요\n\n2. 본문"

    def test_heading_number_label_is_kept_apart_from_the_text(self):
        """The label a numbered heading carries (`text:number`) is followed by a space: it read '# 1.Intro'. Inside a list item
        it is the label already, so it is not written twice."""
        assert _odt('<text:h text:outline-level="1"><text:number>1.</text:number>Intro</text:h>') == "# 1. Intro"
        text = _odt('<text:list text:style-name="Numbers"><text:list-item><text:h text:outline-level="2"><text:number>1.</text:number>'
                    '개요</text:h></text:list-item><text:list-item><text:p>본문</text:p></text:list-item></text:list>',
                    styles=_LIST_STYLES)
        assert text == "## 1. 개요\n\n2. 본문"

    def test_heading_label_is_written_once_where_the_heading_is_a_paragraph(self):
        """In a table cell or a footnote a heading is a paragraph. A list item heading that carries its label gave '1. 1. H';
        an empty `text:number` is no label, and the list gives the number."""
        item = '<text:list-item><text:h text:outline-level="1">{}</text:h></text:list-item>'
        in_list = '<text:list text:style-name="Numbers">' + item.format("<text:number>1.</text:number>제목") + "</text:list>"
        assert _odt("<table:table><table:table-row><table:table-cell>" + in_list
                    + "</table:table-cell></table:table-row></table:table>", styles=_LIST_STYLES) == "1. 제목"
        assert _odt('<text:p>본문<text:note text:note-class="footnote"><text:note-citation>1</text:note-citation><text:note-body>'
                    + in_list + "</text:note-body></text:note></text:p>", styles=_LIST_STYLES) == "본문[^1]\n\n[^1]: 1. 제목"
        text = _odt('<text:list text:style-name="Numbers">' + item.format("<text:number/>제목")
                    + "<text:list-item><text:p>본문</text:p></text:list-item></text:list>", styles=_LIST_STYLES)
        assert text == "# 1. 제목\n\n2. 본문"


class TestHiddenText:
    """Hidden text is not text of the document (as DOCX w:vanish and RTF `\\v`): it read like a valid value."""

    def test_characters_in_a_hidden_text_style_are_left_out(self):
        automatic = ('<style:style style:name="T1" style:family="text"><style:text-properties text:display="none"/></style:style>'
                     '<style:style style:name="T2" style:family="text" style:parent-style-name="T1">'
                     '<style:text-properties fo:font-weight="bold"/></style:style>')
        text = _odt('<text:p>금액 <text:span text:style-name="T1">1,000원</text:span>2,000원 '
                    '<text:span text:style-name="T2">초안</text:span>끝</text:p>', automatic=automatic)
        assert text == "금액 2,000원 끝"

    def test_hidden_fields_and_conditional_sections_are_left_out_only_while_hidden(self):
        text = _odt('<text:p>보임<text:hidden-text text:condition="ooow:1" text:string-value="숨김" text:is-hidden="true">숨김'
                    '</text:hidden-text><text:hidden-text text:condition="ooow:0" text:string-value="표시">표시</text:hidden-text></text:p>'
                    '<text:p>숨은 문단<text:hidden-paragraph text:condition="ooow:1" text:is-hidden="true"/></text:p>'
                    '<text:p>보이는 문단<text:hidden-paragraph text:condition="ooow:0"/></text:p>'
                    '<text:section text:name="S1" text:display="condition" text:condition="ooow:1" text:is-hidden="true">'
                    '<text:p>숨은 구역</text:p></text:section>'
                    '<text:section text:name="S2" text:display="condition" text:condition="ooow:0"><text:p>보이는 구역</text:p>'
                    '</text:section>')
        assert text == "보임표시\n\n보이는 문단\n\n보이는 구역"

    def test_hidden_paragraph_in_a_sheet_cell_is_left_out(self):
        """Sheet cells are read without the block renderer, so a hidden paragraph there was still read."""
        cell = ('<table:table-cell office:value-type="string"><text:p>보임</text:p>'
                '<text:p>숨김<text:hidden-paragraph text:condition="ooow:1" text:is-hidden="true"/></text:p></table:table-cell>')
        assert _convert("spreadsheet", _sheet("S", _row(cell))).text == "## Sheet: S\n\n보임"

    def test_merged_cells_repeat_their_value_on_every_row(self):
        """A 3-row table whose first column has a cell spanning rows 2 and 3 (`number-rows-spanned` + a covered cell): the merged
        category is repeated on each row, as for the same table in DOCX."""
        text = _odt('<table:table>'
                    + _row(_cell("구분"), _cell("지역"), _cell("매출"))
                    + _row(_cell("국내", 'table:number-rows-spanned="2"'), _cell("서울"), _cell("150"))
                    + _row("<table:covered-table-cell/>", _cell("부산"), _cell("95"))
                    + '</table:table>')
        assert text == "| 구분 | 지역 | 매출 |\n| --- | --- | --- |\n| 국내 | 서울 | 150 |\n| 국내 | 부산 | 95 |"

    def test_horizontally_merged_cell_makes_an_html_table(self):
        """A data row with a cell spanning two columns: Markdown cannot draw it, so the table goes through `TableGrid` to HTML
        with a colspan."""
        text = _odt('<table:table>'
                    + _row(_cell("항목"), _cell("1월"), _cell("2월"))
                    + _row(_cell("합계"), _cell("300", 'table:number-columns-spanned="2"'), "<table:covered-table-cell/>")
                    + '</table:table>')
        assert '<td colspan="2">300</td>' in text
        assert "<table>" in text

    def test_nested_table_keeps_its_rows_and_columns(self):
        """A table inside a cell of another table is handed to the outer grid, not flattened into the cell text."""
        inner = "<table:table>" + _row(_cell("안1"), _cell("안2")) + _row(_cell("안3"), _cell("안4")) + "</table:table>"
        text = _odt("<table:table>" + _row(_cell("밖1"), f"<table:table-cell><text:p>설명</text:p>{inner}</table:table-cell>")
                    + _row(_cell("밖2"), _cell("밖3")) + "</table:table>")
        assert text.count("<table>") == 2
        assert "<table><tr><th>안1</th><th>안2</th></tr><tr><td>안3</td><td>안4</td></tr></table>" in text
        assert "밖2" in text

    def test_footnotes_and_endnotes_are_numbered_in_order_with_definitions_at_the_end(self):
        """A footnote in the first paragraph and an endnote in the second; the definitions follow the body."""
        note = ('<text:note text:note-class="{}"><text:note-citation>{}</text:note-citation>'
                '<text:note-body><text:p>{}</text:p></text:note-body></text:note>')
        body = (f'<text:p>매출이 늘었다{note.format("footnote", "1", "연결 기준")}.</text:p>'
                f'<text:p>비용은 줄었다{note.format("endnote", "i", "감사 전 수치")}.</text:p>')
        assert _odt(body) == "매출이 늘었다[^1].\n\n비용은 줄었다[^2].\n\n[^1]: 연결 기준\n[^2]: 감사 전 수치"
        assert _odt(body, ConvertOptions(include_footnotes=False)) == "매출이 늘었다.\n\n비용은 줄었다."

    def test_spaces_tabs_and_line_breaks_come_from_their_elements_not_from_xml_white_space(self):
        """A paragraph written over several indented XML lines with `text:s`, `text:tab` and `text:line-break`: the XML line ends
        are one space, the elements are the real white space."""
        text = _odt('<text:p>첫 줄\n      이어서<text:line-break/>둘째<text:s text:c="3"/>줄<text:tab/>끝</text:p>')
        assert text == "첫 줄 이어서\n둘째 줄 끝"

    def test_tracked_deletion_is_left_out_and_insertion_kept(self):
        """Change tracking: the deleted text is stored in `text:tracked-changes`, the body holds only markers and the inserted
        text. Deleted text as body text would read as still valid."""
        text = _odt('<text:tracked-changes><text:changed-region text:id="c1"><text:deletion>'
                    '<text:p>삭제된 금액 1,000원</text:p></text:deletion></text:changed-region>'
                    '<text:changed-region text:id="c2"><text:insertion/></text:changed-region></text:tracked-changes>'
                    '<text:p>금액은 <text:change text:change-id="c1"/><text:change-start text:change-id="c2"/>2,000원'
                    '<text:change-end text:change-id="c2"/>입니다.</text:p>')
        assert text == "금액은 2,000원입니다."

    def test_picture_is_registered_and_replaced_by_a_placeholder_in_place(self):
        """A paragraph holding a `draw:frame` with a picture from Pictures/: one image is extracted and '[Image…]' stands between
        the paragraphs around it. With `extract_images` off nothing is registered."""
        body = ('<text:p>앞</text:p><text:p><draw:frame><draw:image xlink:href="Pictures/a.png"/>'
                '<svg:title>지역별 매출 그래프</svg:title></draw:frame></text:p><text:p>뒤</text:p>')
        parts = {"Pictures/a.png": builder.png_bytes()}
        result = _convert("text", body, parts=parts)
        assert len(result.images) == 1
        assert result.images[0].alt == "지역별 매출 그래프"
        assert result.text.index("앞") < result.text.index("[Image") < result.text.index("뒤")
        off = _convert("text", body, ConvertOptions(extract_images=False), parts=parts)
        assert not off.images and off.text == "앞\n\n뒤"

    def test_headers_and_footers_only_with_the_option(self):
        """A master page with a header and a footer (the footer also has a page-number field): left out by default, written
        once around the body with `include_headers_footers`, without the cached page number."""
        styles = ('<office:master-styles><style:master-page style:name="Standard">'
                  '<style:header><text:p>대외비</text:p></style:header>'
                  '<style:footer><text:p>주식회사 가나다 <text:page-number>3</text:page-number></text:p></style:footer>'
                  '</style:master-page></office:master-styles>')
        assert _odt("<text:p>본문</text:p>", styles=styles) == "본문"
        text = _odt("<text:p>본문</text:p>", ConvertOptions(include_headers_footers=True), styles=styles)
        assert text == "대외비\n\n본문\n\n주식회사 가나다"

    def test_text_box_section_and_table_of_contents_keep_their_text(self):
        """A section wrapping a paragraph, a text box anchored in a paragraph and a generated table of contents: all are
        containers whose paragraphs are body text; the index settings (`…-source`) are not."""
        text = _odt('<text:table-of-content><text:table-of-content-source><text:index-title-template>설정'
                    '</text:index-title-template></text:table-of-content-source><text:index-body>'
                    '<text:index-title><text:p>목차</text:p></text:index-title><text:p>1. 개요</text:p></text:index-body>'
                    '</text:table-of-content>'
                    '<text:section text:name="s1"><text:p>구역 안</text:p></text:section>'
                    '<text:section text:name="s2" text:display="none"><text:p>숨긴 구역</text:p></text:section>'
                    f'<text:p>{_text_frame("<text:p>글상자</text:p>")}</text:p>')
        assert text == "목차\n\n1. 개요\n\n구역 안\n\n글상자"

    def test_comment_is_written_at_the_end(self):
        """A comment (`office:annotation`) inside a paragraph is not body text; it becomes a comment line after the body."""
        text = _odt('<text:p>초안<office:annotation><dc:creator>이영희</dc:creator><text:p>수치 확인 필요</text:p>'
                    '</office:annotation>입니다.</text:p>')
        assert text == "초안입니다.\n\n> Comment(이영희): 수치 확인 필요"

    def test_ruby_text_follows_its_base_in_parentheses(self):
        text = _odt('<text:p><text:ruby><text:ruby-base>漢字</text:ruby-base><text:ruby-text>한자</text:ruby-text></text:ruby>'
                    ' 표기</text:p>')
        assert text == "漢字(한자) 표기"


class TestInlineFormatting:
    AUTOMATIC = ('<style:style style:name="B" style:family="text"><style:text-properties fo:font-weight="bold"/></style:style>'
                 '<style:style style:name="I" style:family="text"><style:text-properties fo:font-style="italic"/></style:style>'
                 '<style:style style:name="BI" style:family="text" style:parent-style-name="B">'
                 '<style:text-properties fo:font-style="italic"/></style:style>'
                 '<style:style style:name="N" style:family="text"><style:text-properties fo:font-weight="normal"/></style:style>'
                 '<style:style style:name="Sup" style:family="text"><style:text-properties style:text-position="super 58%"/>'
                 '</style:style>'
                 '<style:style style:name="Del" style:family="text">'
                 '<style:text-properties style:text-line-through-style="solid"/></style:style>')

    def _text(self, paragraph: str, options: ConvertOptions | None = None) -> str:
        return _odt(paragraph, options, automatic=self.AUTOMATIC)

    def test_bold_and_italic_spans_become_markdown_emphasis(self):
        """Spans with automatic text styles: bold, italic, and a style that inherits bold from its parent and adds italic."""
        text = self._text('<text:p>매출은 <text:span text:style-name="B">크게</text:span> 늘고 '
                          '<text:span text:style-name="I">비용</text:span>은 <text:span text:style-name="BI">소폭</text:span> '
                          '줄었다.</text:p>')
        assert text == "매출은 **크게** 늘고 *비용*은 ***소폭*** 줄었다."

    def test_emphasis_marks_never_wrap_white_space_and_adjacent_runs_merge(self):
        """Two bold spans separated by a space and a bold span that ends in a space: one pair of marks, with the trailing space
        outside (`**bold **next` does not render)."""
        text = self._text('<text:p><text:span text:style-name="B">중요</text:span> <text:span text:style-name="B">공지 </text:span>'
                          '입니다</text:p>')
        assert text == "**중요 공지** 입니다"

    def test_span_that_turns_bold_off_inside_a_bold_span_is_plain(self):
        text = self._text('<text:p><text:span text:style-name="B">굵게 <text:span text:style-name="N">보통</text:span></text:span>'
                          '</text:p>')
        assert text == "**굵게** 보통"

    def test_bold_korean_text_is_read_from_the_asian_font_properties(self):
        """Styles that set only `style:font-weight-asian` / `style:font-style-asian` (what LibreOffice writes for emphasized
        Korean text in some imports): Korean text in such a span is emphasized, Latin text in the same style is not."""
        automatic = ('<style:style style:name="KB" style:family="text"><style:text-properties style:font-weight-asian="bold"/>'
                     '</style:style><style:style style:name="KI" style:family="text">'
                     '<style:text-properties style:font-style-asian="italic"/></style:style>')
        text = _odt('<text:p>본문 <text:span text:style-name="KB">굵게</text:span> 그리고 <text:span text:style-name="KI">기울임</text:span>'
                    ', <text:span text:style-name="KB">plain</text:span></text:p>', automatic=automatic)
        assert text == "본문 **굵게** 그리고 *기울임*, plain"

    def test_keep_emphasis_off_flattens_emphasis(self):
        text = self._text('<text:p>매출은 <text:span text:style-name="B">크게</text:span> 늘었다.</text:p>',
                          ConvertOptions(keep_emphasis=False))
        assert text == "매출은 크게 늘었다."

    def test_no_emphasis_inside_headings(self):
        text = self._text('<text:h text:outline-level="1"><text:span text:style-name="B">제1장</text:span> 총칙</text:h>')
        assert text == "# 제1장 총칙"

    def test_link_inside_a_heading_is_kept(self):
        """A heading with a bold linked word: the emphasis is dropped, the link stays (the DOCX rule)."""
        text = self._text('<text:h text:outline-level="2"><text:a xlink:href="https://example.com/guide">'
                          '<text:span text:style-name="B">안내서</text:span></text:a> 개정</text:h>')
        assert text == "## [안내서](https://example.com/guide) 개정"

    def test_paragraph_that_is_bold_as_a_whole_is_left_unmarked(self):
        """A paragraph whose every word is bold (split into two spans, as LibreOffice writes it) and one that is italic
        throughout: bold over a whole block is how the block is styled, not emphasis — the DOCX of the same content gives the
        plain sentence, and `**This whole paragraph is bold.**` here made the two formats disagree."""
        text = self._text('<text:p><text:span text:style-name="B">This whole </text:span>'
                          '<text:span text:style-name="B">paragraph is bold.</text:span></text:p>'
                          '<text:p><text:span text:style-name="I">전체가 기울임인 문단</text:span></text:p>')
        assert text == "This whole paragraph is bold.\n\n전체가 기울임인 문단"

    def test_bold_part_of_a_paragraph_is_marked(self):
        """The same sentence with only two of its words bold: that is emphasis and is written."""
        text = self._text('<text:p>This paragraph is <text:span text:style-name="B">partly bold</text:span> only.</text:p>')
        assert text == "This paragraph is **partly bold** only."

    def test_bold_korean_word_before_its_particle_is_marked(self):
        """In Korean the particle is written onto the stressed word without a space: bold that starts a word and stops before
        its ending is emphasis."""
        text = self._text('<text:p>분기 <text:span text:style-name="B">매출</text:span>은 늘었다.</text:p>')
        assert text == "분기 **매출**은 늘었다."

    def test_bold_that_starts_inside_a_word_or_covers_one_glued_character_is_not_marked(self):
        """A digit in another weight inside a number, bold on the second half of a word, and a decorated first syllable: styling
        of part of a word, not emphasis (the shared part-of-a-word rule)."""
        text = self._text('<text:p>1<text:span text:style-name="B">2</text:span>,345원, '
                          '인사<text:span text:style-name="B">발령</text:span>, '
                          '<text:span text:style-name="B">부</text:span>분</text:p>')
        assert text == "12,345원, 인사발령, 부분"

    def test_list_item_that_is_bold_as_a_whole_is_left_unmarked_and_a_partly_bold_one_is_marked(self):
        """Two bullets: one bold throughout (a label), one with a bold word. The bullet is not counted as a word of the item."""
        bold = '<text:span text:style-name="B">{}</text:span>'
        text = self._text(f'<text:list><text:list-item><text:p>{bold.format("준비물")}</text:p></text:list-item>'
                          f'<text:list-item><text:p>신분증 {bold.format("원본")} 지참</text:p></text:list-item></text:list>')
        assert text == "- 준비물\n- 신분증 **원본** 지참"

    def test_bold_and_italic_nest_with_bold_outside(self):
        """An italic word inside a bold phrase: one bold span with the italic inside it, not three spans."""
        text = self._text('<text:p>결과는 <text:span text:style-name="B">매우 </text:span><text:span text:style-name="BI">중요한'
                          '</text:span><text:span text:style-name="B"> 변화</text:span>다.</text:p>')
        assert text == "결과는 **매우 *중요한* 변화**다."

    def test_emphasis_is_closed_and_reopened_at_a_line_break(self):
        """A bold span that holds a `text:line-break`, in a paragraph that is not bold throughout: each line gets its own marks."""
        text = self._text('<text:p>참고: <text:span text:style-name="B">첫째 줄<text:line-break/>둘째 줄</text:span> 끝</text:p>')
        assert text == "참고: **첫째 줄**\n**둘째 줄** 끝"

    def test_cell_that_is_bold_as_a_whole_is_left_plain(self):
        """A header row whose cells are bold throughout and a data cell with one bold word: the header stays plain (styling), the
        partial emphasis is kept."""
        bold = '<text:span text:style-name="B">{}</text:span>'
        text = self._text("<table:table>" + _row(_cell(bold.format("항목")), _cell(bold.format("비고")))
                          + _row(_cell("서버"), _cell(f"교체 {bold.format('필수')}")) + "</table:table>")
        assert text == "| 항목 | 비고 |\n| --- | --- |\n| 서버 | 교체 **필수** |"

    def test_hyperlink_becomes_a_markdown_link_only_for_web_and_mail_targets(self):
        """Three links: https, mailto and a bookmark inside the document. Only the first two become links; with `keep_links`
        off only the text stays."""
        paragraph = ('<text:p><text:a xlink:href="https://example.com/a b(1)">공지 사항</text:a>, '
                     '<text:a xlink:href="mailto:help@example.com">문의</text:a>, '
                     '<text:a xlink:href="#1.개요|outline">개요</text:a></text:p>')
        assert self._text(paragraph) == ("[공지 사항](https://example.com/a%20b%281%29), [문의](mailto:help@example.com), 개요")
        assert self._text(paragraph, ConvertOptions(keep_links=False)) == "공지 사항, 문의, 개요"

    def test_addresses_that_are_not_web_or_mail_stay_text_and_the_scheme_is_compared_without_case(self):
        """javascript, file, a relative address and a protocol-relative one keep their text only (`_inline.link_target`);
        `HTTPS://` is a web address."""
        links = "".join(f'<text:a xlink:href="{address}">{label}</text:a> ' for address, label in (
            ("javascript:alert(1)", "스크립트"), ("file:///etc/passwd", "파일"), ("../other.odt", "상대"), ("//host/x", "호스트"),
            ("HTTPS://example.com/a", "대문자")))
        assert self._text(f"<text:p>{links}끝</text:p>") == "스크립트 파일 상대 호스트 [대문자](HTTPS://example.com/a) 끝"

    def test_address_and_link_text_cannot_close_the_link_or_split_a_table_row(self):
        """An address holding `)`, a space and `|`, and link text holding `](`: the address is percent-encoded and the bracket
        escaped, so the document cannot write a second link of its own choosing."""
        text = self._text('<text:p><text:a xlink:href="https://a.example/x) [y](https://evil.example/|z">여기](https://evil.example) [x'
                          '</text:a></text:p>')
        assert text == "[여기\\](https://evil.example) \\[x](https://a.example/x%29%20[y]%28https://evil.example/%7Cz)"

    def test_mail_address_linked_to_itself_and_a_trailing_slash_difference_stay_the_bare_address(self):
        text = self._text('<text:p><text:a xlink:href="mailto:team@example.com">team@example.com</text:a>, '
                          '<text:a xlink:href="https://example.com/">https://example.com</text:a></text:p>')
        assert text == "team@example.com, https://example.com"

    def test_link_split_into_spans_is_one_link_with_white_space_outside_the_brackets(self):
        """A link whose text is split into a plain and a bold span is one link with the emphasis inside it; white space at
        the edge of the link stays outside the brackets."""
        text = self._text('<text:p>자세한 내용은<text:a xlink:href="https://example.com/doc"> 운영 '
                          '<text:span text:style-name="B">지침</text:span>서 </text:a>참고</text:p>')
        assert text == "자세한 내용은 [운영 **지침**서](https://example.com/doc) 참고"

    def test_emphasis_stops_at_the_edge_of_a_link(self):
        """Bold that runs from the text before a link into the link: each side gets its own marks."""
        text = self._text('<text:p>이것은 <text:span text:style-name="B">꼭 </text:span><text:a xlink:href="https://example.com">'
                          '<text:span text:style-name="B">신청</text:span></text:a> 하세요</text:p>')
        assert text == "이것은 **꼭** [**신청**](https://example.com) 하세요"

    def test_link_whose_text_is_its_address_is_written_once(self):
        text = self._text('<text:p><text:a xlink:href="https://example.com">https://example.com</text:a></text:p>')
        assert text == "https://example.com"

    def test_bold_text_inside_a_link_keeps_both(self):
        """A bold link in a sentence that is otherwise plain. (A paragraph that is nothing but the bold link is bold as a
        whole and keeps only the link.)"""
        link = '<text:a xlink:href="https://example.com"><text:span text:style-name="B">신청</text:span></text:a>'
        assert self._text(f"<text:p>지금 {link} 하세요</text:p>") == "지금 [**신청**](https://example.com) 하세요"
        assert self._text(f"<text:p>{link}</text:p>") == "[신청](https://example.com)"

    def test_superscript_and_strikethrough_use_the_shared_notation(self):
        """10 with a raised 6 and a struck-through price: `10^6` and `~~…~~` as in the other formats (10⁶ must not read 106)."""
        text = self._text('<text:p>10<text:span text:style-name="Sup">6</text:span> 단위, '
                          '<text:span text:style-name="Del">정가 10,000원</text:span> 8,000원</text:p>')
        assert text == "10^6 단위, ~~정가 10,000원~~ 8,000원"

    def test_strikethrough_and_script_stay_inside_the_bold_span_around_them(self):
        """A bold struck price and a bold formula with a subscript, in a sentence that is not bold throughout: emphasis is the
        outer mark (`**~~…~~**`) and the formula is one span, not `**H**_2**O**`."""
        automatic = self.AUTOMATIC + (
            '<style:style style:name="BDel" style:family="text" style:parent-style-name="B">'
            '<style:text-properties style:text-line-through-style="solid"/></style:style>'
            '<style:style style:name="BSub" style:family="text" style:parent-style-name="B">'
            '<style:text-properties style:text-position="sub 58%"/></style:style>')
        text = _odt('<text:p>정가 <text:span text:style-name="BDel">1,000</text:span>원, '
                    '<text:span text:style-name="B">H</text:span><text:span text:style-name="BSub">2</text:span>'
                    '<text:span text:style-name="B">O</text:span> 사용</text:p>', automatic=automatic)
        assert text == "정가 **~~1,000~~**원, **H_2O** 사용"

    def test_heading_inside_a_table_cell_is_a_paragraph_and_keeps_its_emphasis(self):
        """A `text:h` in a cell is not a heading (no `#`), so the heading rule does not apply to it: a bold word stays marked."""
        text = self._text("<table:table>" + _row(_cell("구분"), _cell("내용")) + _row(
            _cell("1"), '<table:table-cell><text:h text:outline-level="2">추진 <text:span text:style-name="B">배경</text:span>'
                        '</text:h></table:table-cell>') + "</table:table>")
        assert text == "| 구분 | 내용 |\n| --- | --- |\n| 1 | 추진 **배경** |"

    def test_slide_text_box_follows_the_same_rules(self):
        """ODP: a caption box that is bold throughout stays plain, a bold word in a sentence is marked, the title is plain."""
        bold = '<text:span text:style-name="B">{}</text:span>'
        result = _convert("presentation", _slide(
            _text_frame(f"<text:p>{bold.format('분기')} 실적</text:p>", 'presentation:class="title"'),
            _text_frame(f"<text:p>{bold.format('핵심 요약')}</text:p><text:p>분기 {bold.format('매출')}은 늘었다.</text:p>")),
            automatic=self.AUTOMATIC)
        assert result.sections[0].title == "분기 실적"
        assert result.sections[0].text == "핵심 요약\n\n분기 **매출**은 늘었다."


class TestSpreadsheet:
    def test_one_sheet_section_per_table_with_displayed_cell_text(self):
        """Two sheets. Cells carry both the stored value (`office:value`) and the displayed text (`text:p`): the displayed text
        is what comes out ('46.4%', '$120k'), as LibreOffice formatted it."""
        number = 'office:value-type="float" office:value="120"'
        share = 'office:value-type="percentage" office:value="0.464458"'
        result = _convert("spreadsheet",
                          _sheet("매출", _row(_cell("지역"), _cell("1분기"), _cell("비중")),
                                 _row(_cell("서울"), _cell("$120k", number), _cell("46.4%", share)))
                          + _sheet("메모", _row(_cell("단위: 천 달러"))))
        assert result.format is DocumentFormat.ODS
        assert [(section.kind, section.title) for section in result.sections] == [(SectionKind.SHEET, "매출"),
                                                                                  (SectionKind.SHEET, "메모")]
        assert result.sections[0].text == "| 지역 | 1분기 | 비중 |\n| --- | --- | --- |\n| 서울 | $120k | 46.4% |"
        assert result.sections[1].text == "단위: 천 달러"
        assert result.metadata["sheet_count"] == "2"

    def test_two_tables_on_one_sheet_are_split_like_xlsx(self):
        """A title row merged across the table, a table, an empty row, a one-cell caption and a second table with other
        columns — the layout of benchmarks/cases/sales.ods. The spreadsheet extractor's splitting gives a title paragraph, two
        tables and the caption between them."""
        text = _convert("spreadsheet", _sheet(
            "Sales",
            _row(_cell("지역별 매출, 2025", 'table:number-columns-spanned="3"'), '<table:covered-table-cell table:number-columns-repeated="2"/>'),
            _row(_cell("지역"), _cell("1분기"), _cell("2분기")),
            _row(_cell("서울"), _cell("120"), _cell("135")),
            _row(_cell("부산"), _cell("80"), _cell("85")),
            _row('<table:table-cell table:number-columns-repeated="16384"/>'),
            _row(_cell("주요 계약")),
            _row(_cell("고객"), _cell("체결일"), _cell("금액")),
            _row(_cell("가나상사"), _cell("2025-03-14"), _cell("48,500.00")),
            _row(_cell("다라물산"), _cell("2025-09-02"), _cell("73,250.50")))).sections[0].text
        assert text == ("지역별 매출, 2025\n\n| 지역 | 1분기 | 2분기 |\n| --- | --- | --- |\n| 서울 | 120 | 135 |\n| 부산 | 80 | 85 |\n\n"
                        "주요 계약\n\n| 고객 | 체결일 | 금액 |\n| --- | --- | --- |\n| 가나상사 | 2025-03-14 | 48,500.00 |\n"
                        "| 다라물산 | 2025-09-02 | 73,250.50 |")

    def test_vertically_merged_cell_is_repeated_on_the_rows_below(self):
        text = _convert("spreadsheet", _sheet(
            "S", _row(_cell("구분"), _cell("지역"), _cell("매출")),
            _row(_cell("국내", 'table:number-rows-spanned="2"'), _cell("서울"), _cell("150")),
            _row("<table:covered-table-cell/>", _cell("부산"), _cell("95")))).sections[0].text
        assert text == "| 구분 | 지역 | 매출 |\n| --- | --- | --- |\n| 국내 | 서울 | 150 |\n| 국내 | 부산 | 95 |"

    def test_horizontal_merge_in_a_data_row_fills_the_named_columns(self):
        """'육아휴직' spans the 3월 and 4월 columns of a data row: both months get the value (the XLSX rule, `_fill_across`)."""
        text = _convert("spreadsheet", _sheet(
            "S", _row(_cell("이름"), _cell("3월"), _cell("4월")),
            _row(_cell("김민수"), _cell("육아휴직", 'table:number-columns-spanned="2"'), "<table:covered-table-cell/>"),
            _row(_cell("이영희"), _cell("근무"), _cell("근무")))).sections[0].text
        assert "| 김민수 | 육아휴직 | 육아휴직 |" in text

    def test_repeated_cells_and_rows_with_text_are_expanded(self):
        """`number-columns-repeated="3"` on a cell with text and `number-rows-repeated="2"` on a row: LibreOffice writes equal
        neighbours this way, so they are real cells and rows."""
        text = _convert("spreadsheet", _sheet(
            "S", _row(_cell("이름"), _cell("1월"), _cell("2월"), _cell("3월")),
            f'<table:table-row table:number-rows-repeated="2">{_cell("미정")}{_cell("0", "table:number-columns-repeated=\'3\'")}'
            '</table:table-row>')).sections[0].text
        assert text == "| 이름 | 1월 | 2월 | 3월 |\n| --- | --- | --- | --- |\n| 미정 | 0 | 0 | 0 |\n| 미정 | 0 | 0 | 0 |"

    def test_hidden_sheet_is_skipped_with_a_warning_unless_included(self):
        """The second sheet's table style has `table:display="false"` (how LibreOffice stores a hidden sheet)."""
        automatic = ('<style:style style:name="ta1" style:family="table"><style:table-properties table:display="true"/></style:style>'
                     '<style:style style:name="ta2" style:family="table"><style:table-properties table:display="false"/></style:style>')
        body = (_sheet("공개", _row(_cell("보이는 값")), attributes='table:style-name="ta1"')
                + _sheet("작업용", _row(_cell("내부 자료")), attributes='table:style-name="ta2"'))
        result = _convert("spreadsheet", body, automatic=automatic)
        assert [section.title for section in result.sections] == ["공개"]
        assert "내부 자료" not in result.text
        assert "Hidden sheet '작업용' was excluded." in result.warnings
        shown = _convert("spreadsheet", body, ConvertOptions(include_hidden_sheets=True), automatic=automatic)
        assert [section.title for section in shown.sections] == ["공개", "작업용"]
        assert not shown.warnings

    def test_columns_beyond_the_limit_are_excluded_with_a_warning(self):
        result = _convert("spreadsheet", _sheet("S", _row(*(_cell(f"열{index}") for index in range(1, 6))),
                                                _row(*(_cell(str(index)) for index in range(1, 6)))),
                          ConvertOptions(max_sheet_cols=3))
        assert result.sections[0].text == "| 열1 | 열2 | 열3 |\n| --- | --- | --- |\n| 1 | 2 | 3 |"
        assert "Sheet 'S': columns beyond column 3 were excluded." in result.warnings

    def test_cell_without_displayed_text_falls_back_to_its_stored_value(self):
        """Files from other writers may store only `office:value` and no `text:p`. The stored value is written plainly."""
        text = _convert("spreadsheet", _sheet(
            "S", _row(_cell("항목"), _cell("값")),
            _row(_cell("수량"), '<table:table-cell office:value-type="float" office:value="12.0"/>'),
            _row(_cell("비율"), '<table:table-cell office:value-type="percentage" office:value="0.25"/>'),
            _row(_cell("날짜"), '<table:table-cell office:value-type="date" office:date-value="2025-03-14T00:00:00"/>'),
            _row(_cell("시간"), '<table:table-cell office:value-type="time" office:time-value="PT09H30M00S"/>'),
            _row(_cell("여부"), '<table:table-cell office:value-type="boolean" office:boolean-value="true"/>'))).sections[0].text
        assert [line.split(" | ")[1].rstrip(" |") for line in text.splitlines()[2:]] == ["12", "25%", "2025-03-14", "09:30:00", "TRUE"]

    def test_cell_comment_and_chart_title_follow_the_cells(self):
        """A cell with a comment and a cell anchoring a chart (an embedded object whose sub-document has a chart title): the
        sheet text ends with `[Chart] title` and the comment line naming the cell. The chart values are the sheet's own cells, so
        they are not repeated."""
        chart = (f'<office:document-content {_NAMESPACES}><office:body><office:chart><chart:chart>'
                 '<chart:title><text:p>분기별 매출</text:p></chart:title>'
                 f'<table:table table:name="local-table">{_row(_cell(""), _cell("매출"))}{_row(_cell("1분기"), _cell("120"))}'
                 '</table:table></chart:chart></office:chart></office:body></office:document-content>').encode()
        text = _convert("spreadsheet", _sheet(
            "S", _row(_cell("분기"), _cell("매출")),
            _row(_cell("1분기"), '<table:table-cell><office:annotation><dc:creator>박철수</dc:creator><text:p>잠정치</text:p>'
                                 '</office:annotation><text:p>120</text:p></table:table-cell>',
                 '<table:table-cell><draw:frame><draw:object xlink:href="./Object 1"/>'
                 '<draw:image xlink:href="./ObjectReplacements/Object 1"/></draw:frame></table:table-cell>')),
            parts={"Object 1/content.xml": chart, "ObjectReplacements/Object 1": b"VCLMTF"}).sections[0].text
        assert text == ("| 분기 | 매출 |\n| --- | --- |\n| 1분기 | 120 |\n\n[Chart] 분기별 매출\n\n"
                        "> Comment(박철수, cell B2): 잠정치")

    def test_trailing_repeats_of_empty_rows_and_cells_cost_nothing(self):
        """What LibreOffice writes at the end of every sheet: rows of 16,384 repeated empty cells and a row repeated 1,048,564
        times. Only the two real rows come out, and no row or cell is materialized for the repeats (the test would not finish
        otherwise)."""
        tail = '<table:table-cell table:number-columns-repeated="16382"/>'
        result = _convert("spreadsheet", _sheet(
            "S", _row(_cell("이름"), _cell("값"), tail), _row(_cell("가"), _cell("1"), tail),
            '<table:table-row table:number-rows-repeated="1048564"><table:table-cell table:number-columns-repeated="16384"/>'
            '</table:table-row>', _row('<table:table-cell table:number-columns-repeated="16384"/>')))
        assert result.sections[0].text == "| 이름 | 값 |\n| --- | --- |\n| 가 | 1 |"
        assert not result.warnings


    def test_column_limit_counts_from_the_first_used_column(self):
        """A three-column table that starts at column GW (204 empty columns to its left, written as one repeated cell), with a
        comment on its first cell. Counting `max_sheet_cols` (100) from column A dropped the whole table with a 'columns excluded'
        warning while the same workbook as XLSX was read in full. The limit counts from the first used column, the empty columns
        are not written, and the comment keeps the real cell address."""
        skip = '<table:table-cell table:number-columns-repeated="204"/>'
        note = '<table:table-cell><office:annotation><text:p>확인 필요</text:p></office:annotation><text:p>부위</text:p></table:table-cell>'
        result = _convert("spreadsheet", _sheet("Sizes", _row(skip, note, _cell("A"), _cell("G")),
                                                _row('<table:table-cell table:number-columns-repeated="205"/>', _cell("52"), _cell("54"))))
        assert result.sections[0].text == "| 부위 | A | G |\n| --- | --- | --- |\n| | 52 | 54 |\n\n> Comment(cell GW1): 확인 필요"
        assert result.warnings == []
        wide = _convert("spreadsheet", _sheet("Wide", _row(skip, *[_cell(f"c{index}") for index in range(5)])), ConvertOptions(max_sheet_cols=3))
        assert wide.sections[0].text == "| c0 | c1 | c2 |\n| --- | --- | --- |"
        assert any("columns beyond" in warning for warning in wide.warnings)

    def test_text_in_a_drawn_shape_follows_the_cells(self):
        """A text box drawn in Excel is a `draw:custom-shape` (not a frame) once the workbook is saved as ODS, anchored to a cell
        or — when it is anchored to the page — listed in `table:shapes`. A sheet whose only content is such a shape came out
        empty; the XLSX extractor reads the same text, so both forms are written after the cells."""
        in_cell = '<table:table-cell><draw:custom-shape draw:name="Box 1"><text:p><text:span>결재 대기</text:span></text:p></draw:custom-shape></table:table-cell>'
        on_page = ('<table:shapes><draw:rect><text:p>Approved by finance</text:p></draw:rect>'
                   '<draw:g><draw:custom-shape><text:p>Grouped note</text:p></draw:custom-shape></draw:g></table:shapes>')
        result = _convert("spreadsheet", _sheet("Budget", on_page, _row(_cell("Item"), _cell("Cost")), _row(_cell("Desk"), in_cell))
                          + _sheet("Shapes only", _row(in_cell)))
        assert result.sections[0].text == ("| Item | Cost |\n| --- | --- |\n| Desk | |\n\n결재 대기\n\nApproved by finance\n\nGrouped note")
        assert result.sections[1].text == "결재 대기"

class TestPresentation:
    TITLE = 'presentation:class="title"'

    def test_one_slide_section_per_page_with_the_title_from_the_title_placeholder(self):
        """Two slides: title placeholder + subtitle, and title + a free text box. The title is the section title and is not
        repeated in the slide text."""
        result = _convert("presentation",
                          _slide(_text_frame("<text:p>출시 계획</text:p>", self.TITLE),
                                 _text_frame("<text:p>아틀라스 2.0</text:p>", 'presentation:class="subtitle"'))
                          + _slide(_text_frame("<text:p>목표</text:p>", self.TITLE), _text_frame("<text:p>4월 말 출시</text:p>")))
        assert result.format is DocumentFormat.ODP
        assert [(section.kind, section.title, section.text) for section in result.sections] == [
            (SectionKind.SLIDE, "출시 계획", "아틀라스 2.0"), (SectionKind.SLIDE, "목표", "4월 말 출시")]
        assert result.text == "## Slide 1: 출시 계획\n\n아틀라스 2.0\n\n## Slide 2: 목표\n\n4월 말 출시"
        assert result.metadata["slide_count"] == "2"

    def test_bullet_levels_are_nested_lists(self):
        """An outline placeholder the way Impress writes it: the second level is a list inside a list item that has no paragraph
        of its own."""
        outline = ('<text:list><text:list-item><text:p>전 지역 출시</text:p></text:list-item></text:list>'
                   '<text:list><text:list-item><text:list><text:list-item><text:p>단계적 배포</text:p></text:list-item></text:list>'
                   '</text:list-item><text:list-item><text:p>안정성 유지</text:p><text:list><text:list-item>'
                   '<text:p>주간 측정</text:p></text:list-item></text:list></text:list-item></text:list>')
        result = _convert("presentation", _slide(_text_frame(outline, 'presentation:class="outline"')))
        assert result.sections[0].text == "- 전 지역 출시\n  - 단계적 배포\n- 안정성 유지\n  - 주간 측정"

    def test_list_level_without_a_label_is_a_paragraph_and_does_not_indent_the_level_below(self):
        """A text box whose list style gives level 1 no label (`num-format=""`, how Impress writes unbulleted text) and level 2 a
        bullet: the first level is a plain paragraph and the bullets under it start at the margin."""
        automatic = ('<text:list-style style:name="L1"><text:list-level-style-number text:level="1" style:num-format=""/>'
                     '<text:list-level-style-bullet text:level="2" text:bullet-char="●"/></text:list-style>')
        box = ('<text:list text:style-name="L1"><text:list-item><text:p>일정 안내</text:p><text:list><text:list-item>'
               '<text:p>3월 기능 동결</text:p></text:list-item></text:list></text:list-item></text:list>')
        result = _convert("presentation", _slide(_text_frame(box)), automatic=automatic)
        assert result.sections[0].text == "일정 안내\n\n- 3월 기능 동결"

    def test_table_and_picture_on_a_slide(self):
        """A table frame that also carries a preview picture (alternative content: only the table is used) and a picture
        frame."""
        table = ("<draw:frame><table:table>" + _row(_cell("단계"), _cell("담당")) + _row(_cell("검토"), _cell("플랫폼"))
                 + '</table:table><draw:image xlink:href="Pictures/TablePreview1.svm"/></draw:frame>')
        result = _convert("presentation", _slide(table, '<draw:frame><draw:image xlink:href="Pictures/p.png"/></draw:frame>'),
                          parts={"Pictures/p.png": builder.png_bytes(), "Pictures/TablePreview1.svm": b"VCLMTF"})
        assert result.sections[0].text.startswith("| 단계 | 담당 |\n| --- | --- |\n| 검토 | 플랫폼 |")
        assert len(result.images) == 1

    def test_speaker_notes_use_the_pptx_form_and_follow_the_option(self):
        """A notes page with the slide thumbnail, the notes placeholder (two paragraphs) and a page-number placeholder: the notes
        are a `> Speaker notes:` block after the slide text, without the page number."""
        notes = ('<presentation:notes><draw:page-thumbnail presentation:class="page"/>'
                 + _text_frame("<text:p>보안 검토 일정에 달려 있음을 강조</text:p><text:p>질문은 마지막에</text:p>", 'presentation:class="notes"')
                 + _text_frame("<text:p><text:page-number>2</text:page-number></text:p>", 'presentation:class="page-number"')
                 + '</presentation:notes>')
        body = _slide(_text_frame("<text:p>목표</text:p>", self.TITLE), _text_frame("<text:p>4월 말 출시</text:p>"), notes)
        assert _convert("presentation", body).sections[0].text == (
            "4월 말 출시\n\n> Speaker notes: 보안 검토 일정에 달려 있음을 강조\n> 질문은 마지막에")
        assert _convert("presentation", body, ConvertOptions(include_speaker_notes=False)).sections[0].text == "4월 말 출시"

    def test_empty_layout_placeholders_are_left_out(self):
        """`presentation:placeholder="true"` frames are the 'Click to add text' prompts of the layout."""
        result = _convert("presentation", _slide(
            _text_frame("<text:p>제목</text:p>", self.TITLE),
            _text_frame("", 'presentation:class="outline" presentation:placeholder="true"'),
            '<presentation:notes>' + _text_frame("", 'presentation:class="notes" presentation:placeholder="true"')
            + '</presentation:notes>'))
        assert result.sections[0].text == ""
        assert result.sections[0].title == "제목"

    def test_hidden_slide_is_marked_in_its_title(self):
        automatic = ('<style:style style:name="dp1" style:family="drawing-page">'
                     '<style:drawing-page-properties presentation:visibility="hidden"/></style:style>')
        result = _convert("presentation", _slide(_text_frame("<text:p>부록</text:p>", self.TITLE),
                                                 attributes='draw:style-name="dp1"'), automatic=automatic)
        assert result.sections[0].title == "부록 (hidden)"


class TestObjects:
    def test_chart_gives_its_title_and_value_table(self):
        """A chart object on a slide: the sub-document `Object 1/content.xml` holds the title and the chart's copy of its values
        (header row with an empty corner). The output has the PPTX shape: `[Chart] title` and a table headed 'Category'. The
        replacement picture of the frame is not used."""
        chart = (f'<office:document-content {_NAMESPACES}><office:body><office:chart><chart:chart>'
                 '<chart:title><text:p>예상 가입자</text:p></chart:title><table:table table:name="local-table">'
                 f'<table:table-header-rows>{_row(_cell(""), _cell("활성"), _cell("유료"))}</table:table-header-rows>'
                 f'<table:table-rows>{_row(_cell("1주"), _cell("1200"), _cell("150"))}{_row(_cell("2주"), _cell("2900"), _cell("420"))}'
                 '</table:table-rows></table:table></chart:chart></office:chart></office:body></office:document-content>').encode()
        result = _convert("presentation", _slide(
            '<draw:frame><draw:object xlink:href="./Object 1"/><draw:image xlink:href="./ObjectReplacements/Object 1"/></draw:frame>'),
            parts={"Object 1/content.xml": chart, "ObjectReplacements/Object 1": b"VCLMTF"})
        assert result.sections[0].text == ("[Chart] 예상 가입자\n\n| Category | 활성 | 유료 |\n| --- | --- | --- |\n"
                                           "| 1주 | 1200 | 150 |\n| 2주 | 2900 | 420 |")
        assert not result.images and not result.warnings

    FORMULA = ('<math:math {namespaces}><math:semantics>{mathml}'
               '<math:annotation math:encoding="StarMath 5.0">g = {{a - b}} over b</math:annotation></math:semantics></math:math>')
    BODY = '<text:p>성장률은 <draw:frame><draw:object xlink:href="./Object 1"/></draw:frame> 이다.</text:p>'

    def test_formula_becomes_latex(self):
        """A LibreOffice Math object anchored in a sentence: the sub-document is presentation MathML (a fraction with
        subscripts). It becomes `$LaTeX$` inside the sentence, the same text the DOCX original gives for its equation."""
        mathml = ('<math:mrow><math:mi>g</math:mi><math:mo>=</math:mo><math:mfrac><math:mrow><math:msub><math:mi>R</math:mi>'
                  '<math:mi>t</math:mi></math:msub><math:mo>-</math:mo><math:msub><math:mi>R</math:mi><math:mrow><math:mi>t</math:mi>'
                  '<math:mo>-</math:mo><math:mn>1</math:mn></math:mrow></math:msub></math:mrow><math:msqrt><math:mi>n</math:mi>'
                  '</math:msqrt></math:mfrac></math:mrow>')
        text = _odt(self.BODY, parts={"Object 1/content.xml": self.FORMULA.format(namespaces=_NAMESPACES, mathml=mathml).encode()})
        assert text == r"성장률은 $g=\frac{R_t-R_{t-1}}{\sqrt{n}}$ 이다."

    def test_formula_without_readable_mathml_keeps_its_starmath_text(self):
        """A formula whose MathML part is empty: the StarMath annotation is kept between `$` (not LaTeX — a recorded gap), which
        beats losing the formula."""
        text = _odt(self.BODY, parts={"Object 1/content.xml": self.FORMULA.format(namespaces=_NAMESPACES, mathml="").encode()})
        assert text == "성장률은 $g = {a - b} over b$ 이다."


class TestCharacterLimit:
    def test_text_document_stops_reading_once_the_limit_is_exceeded(self):
        """2,000 paragraphs of 50 characters against a limit of 1,000: `exceeded` is set and the last paragraph was never
        rendered into the section."""
        body = "".join(f"<text:p>{'가' * 46}{index:04d}</text:p>" for index in range(2000))
        result = _convert("text", body, ConvertOptions(max_chars=1000))
        assert result.exceeded
        assert "1999" not in result.sections[0].template
        assert len(result.text) <= 1000

    def test_document_at_the_limit_is_not_exceeded(self):
        body = "<text:p>가나다라마</text:p><text:p>바사아자차</text:p>"
        whole = _convert("text", body)
        assert not _convert("text", body, ConvertOptions(max_chars=whole.char_count)).exceeded
        assert _convert("text", body, ConvertOptions(max_chars=whole.char_count - 1)).exceeded

    def test_large_table_stops_reading_rows_once_the_limit_is_exceeded(self):
        """One top-level table of 3,000 rows: the rows after the limit are not placed on the grid (`TableBudget`)."""
        rows = "".join(_row(_cell(f"행{index:04d}"), _cell("나" * 40)) for index in range(3000))
        result = _convert("text", f"<table:table>{rows}</table:table>", ConvertOptions(max_chars=2000))
        assert result.exceeded
        assert "행2999" not in result.sections[0].template

    def test_spreadsheet_stops_at_the_row_and_the_sheet_where_the_limit_is_exceeded(self):
        rows = "".join(_row(_cell(f"행{index:04d}"), _cell("다" * 40)) for index in range(3000))
        result = _convert("spreadsheet", _sheet("첫째", rows) + _sheet("둘째", _row(_cell("읽지 않음"))), ConvertOptions(max_chars=2000))
        assert result.exceeded
        assert [section.title for section in result.sections] == ["첫째"]
        assert "행2999" not in result.sections[0].template

    def test_presentation_stops_at_the_slide_where_the_limit_is_exceeded(self):
        slides = "".join(_slide(_text_frame(f"<text:p>{'라' * 300}</text:p>")) for _ in range(20))
        result = _convert("presentation", slides, ConvertOptions(max_chars=1000))
        assert result.exceeded
        assert len(result.sections) < 20


class TestBrokenParts:
    def test_encrypted_package_is_reported_as_password_protected(self):
        """A password-protected ODF file: the manifest carries `manifest:encryption-data` for each part and content.xml is
        ciphertext."""
        manifest = ('<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
                    '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml" manifest:size="100">'
                    '<manifest:encryption-data manifest:checksum-type="SHA1/1K" manifest:checksum="x"/></manifest:file-entry>'
                    '</manifest:manifest>')
        with pytest.raises(EncryptedDocumentError) as raised:
            convert(_package("text", "", manifest=manifest, content=b"\x8f\x02\x99 not xml \x00\x01"), "secret.odt")
        assert "password" in str(raised.value)

    @pytest.mark.parametrize("content", [b"<office:document-content", b"\xff\xfe\x00garbage", b"<a/>"],
                             ids=["cut-off", "not-xml", "no-body"])
    def test_broken_content_part_is_a_corrupted_document_with_a_fixed_message(self, content):
        with pytest.raises(CorruptedDocumentError) as raised:
            convert(_package("text", "", content=content), "broken.odt")
        assert str(raised.value) == "The content of the OpenDocument file (content.xml) is missing or corrupted."

    def test_broken_styles_part_keeps_the_body_with_a_warning(self):
        """styles.xml is cut off in the middle. The body is still converted; the list falls back to bullets and a warning says
        the style information was lost."""
        data = _package("text", '<text:list text:style-name="Numbers"><text:list-item><text:p>항목</text:p></text:list-item>'
                                '</text:list>', parts={"styles.xml": b"<office:document-styles"})
        result = convert(data, "a.odt")
        assert result.text == "- 항목"
        assert any("styles.xml" in warning for warning in result.warnings)

    def test_broken_meta_and_missing_picture_cost_only_themselves(self):
        """meta.xml is not XML and the picture a frame points to is not in the package: no title, no picture, the text stays."""
        data = _package("text", '<text:p>본문<draw:frame><draw:image xlink:href="Pictures/none.png"/></draw:frame></text:p>',
                        parts={"meta.xml": b"\x00\x01"})
        result = convert(data, "a.odt")
        assert result.text == "본문"
        assert "title" not in result.metadata

    def test_content_in_another_encoding_is_read_by_its_declaration(self):
        """content.xml declared and encoded as UTF-16: the XML declaration decides, the Korean text survives."""
        content = (f'<?xml version="1.0" encoding="UTF-16"?><office:document-content {_NAMESPACES}><office:body><office:text>'
                   '<text:p>한글 본문</text:p></office:text></office:body></office:document-content>').encode("utf-16")
        assert convert(_package("text", "", content=content), "a.odt").text == "한글 본문"

    def test_body_decides_when_the_mimetype_names_another_kind(self):
        """A package whose mimetype says spreadsheet but whose body is a text document is read as text."""
        buffer = BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
            archive.writestr("content.xml", f'<office:document-content {_NAMESPACES}><office:body><office:text>'
                                            '<text:p>문서 본문</text:p></office:text></office:body></office:document-content>')
        assert convert(buffer.getvalue(), "a.ods").text == "문서 본문"


class TestHostileInput:
    def test_huge_repeat_counts_on_empty_cells_and_rows_in_a_text_table_are_ignored(self):
        """A text table with `number-columns-repeated` / `number-rows-repeated` of 10^9 on empty cells and rows and numbers too
        long for `int()`: the table is its two real rows."""
        text = _odt("<table:table>" + _row(_cell("이름"), _cell("값"), '<table:table-cell table:number-columns-repeated="1000000000"/>')
                    + '<table:table-row table:number-rows-repeated="1000000000"><table:table-cell/></table:table-row>'
                    + _row(_cell("가", f'table:number-rows-spanned="{"9" * 5000}"'), _cell("1", 'table:number-columns-spanned="²"'))
                    + "</table:table>")
        assert text == "| 이름 | 값 |\n| --- | --- |\n| 가 | 1 |"

    def test_repeated_row_of_many_cells_in_a_text_table_is_bounded_by_cell_count(self):
        """A text-document table whose one row holds a letter and 500 empty cell elements, repeated 10,000 times: within the
        character budget (10,000 characters) but 5 million cells of the grid — 13 seconds and 2.7 GB from a few hundred bytes.
        Copies are bounded by the number of cells they add as well (200,000 per document), and the cut is reported. A second
        table in the same document has no budget left, so its repeated rows are written once."""
        row = f'<table:table-row table:number-rows-repeated="10000">{_cell("가")}{"<table:table-cell/>" * 500}</table:table-row>'
        result = _convert("text", f"<table:table>{row}</table:table><text:p>사이</text:p><table:table>{row}</table:table>",
                          ConvertOptions(max_chars=None))
        first, second = result.text.split("사이")
        assert first.count("가") == 1 + 200_000 // 501
        assert second.count("가") == 1
        assert any("was cut short" in warning for warning in result.warnings)

    def test_forged_nested_table_placeholders_do_not_multiply_the_table(self):
        """Seven tables nested in each other, each cell also holding nine copies of the text a nested-table placeholder is made
        of (`U+E020 0 U+E021`). Every copy used to draw the inner table again, at every level: 736 bytes took 26 seconds and
        1.4 GB. The innermost text appears once."""
        table = "<text:p>가장 안쪽</text:p>"
        for _ in range(7):
            table = ("<table:table><table:table-row><table:table-cell><text:p>" + "&#xE020;0&#xE021;" * 9 + "</text:p>" + table
                     + "</table:table-cell>" + _cell("y") + "</table:table-row></table:table>")
        assert _odt(table).count("가장 안쪽") == 1

    def test_object_referenced_many_times_is_parsed_once_and_written_once(self):
        """300 frames in one paragraph, all pointing at the same chart sub-document (2,000 value rows). Each frame used to read,
        parse and render the chart again (100 frames on a 20,000-row chart: 17 seconds from a 10 KB file). The values are written
        at the first frame; the others give the title only."""
        rows = "".join(_row(_cell(f"r{index}"), _cell("1")) for index in range(2000))
        chart = (f'<office:document-content {_NAMESPACES}><office:body><office:chart><chart:chart><chart:title><text:p>매출</text:p>'
                 f'</chart:title><table:table>{rows}</table:table></chart:chart></office:chart></office:body></office:document-content>')
        text = _odt('<text:p>' + '<draw:frame><draw:object xlink:href="./Object 1"/></draw:frame>' * 300 + '</text:p>',
                    parts={"Object 1/content.xml": chart.encode()})
        assert text.count("[Chart] 매출") == 300
        assert text.count("| r1999 | 1 |") == 1

    def test_repeated_sheet_rows_are_bounded_by_cell_count(self, monkeypatch: pytest.MonkeyPatch):
        """A sheet of rows that hold one character in the 99th column and are each repeated 10,000 times: little text, a
        million cells per row element. 500 of them (2 KB) ran for a minute. The copies of a document add at most
        `_REPEAT_SHEET_CELLS` cells; rows are still written once after that, and the cut is reported."""
        row = ('<table:table-row table:number-rows-repeated="10000"><table:table-cell table:number-columns-repeated="98"/>'
               + _cell("x") + "</table:table-row><table:table-row/>")
        monkeypatch.setattr(odf_module, "_REPEAT_SHEET_CELLS", 19_800)
        result = _convert("spreadsheet", _sheet("S", _row(_cell("a")), row * 40), ConvertOptions(max_chars=None))
        assert result.text.count("x") == 201 + 39   # 19,800 cells / 99 per row are 200 copies; then one row per row element
        assert result.warnings == ["Sheet 'S': a row repeated 10,000 times was read 201 times only."]   # once, not per row
        # The cells a vertical merge adds to every copy count too: a value in column 100 merged down over all the rows.
        merged = _row(_cell("h"), '<table:table-cell table:number-columns-repeated="98"/>', _cell("y", 'table:number-rows-spanned="1000000"'))
        narrow = f'<table:table-row table:number-rows-repeated="10000">{_cell("x")}</table:table-row>{_row(_cell("z"))}'
        result = _convert("spreadsheet", _sheet("S", merged, narrow * 40), ConvertOptions(max_chars=None))
        assert result.text.count("| x |") + result.text.count("| x ") <= 2 * (198 + 40)

    def test_repeated_rows_with_text_are_cut_short_with_a_warning(self):
        """A sheet row with text repeated a billion times and no character limit: a few bytes of input must not become gigabytes
        of output. The row is written out a bounded number of times and a warning says it was cut."""
        result = _convert("spreadsheet", _sheet("S", _row(_cell("이름"), _cell("값")),
                                                f'<table:table-row table:number-rows-repeated="1000000000">{_cell("가")}{_cell("1")}'
                                                '</table:table-row>'), ConvertOptions(max_chars=None))
        assert result.sections[0].text.count("| 가 | 1 |") == 10_000
        assert "Sheet 'S': a row repeated 1,000,000,000 times was read 10,000 times only." in result.warnings

    def test_repeated_cell_with_text_stops_at_the_column_limit(self):
        result = _convert("spreadsheet", _sheet("S", _row(_cell("머리", 'table:number-columns-repeated="1000000000"')),
                                                _row(_cell("값", 'table:number-columns-repeated="1000000000"'))))
        assert result.sections[0].text.splitlines()[0].count("머리") == 100
        assert "Sheet 'S': columns beyond column 100 were excluded." in result.warnings

    def test_cell_far_to_the_right_and_far_down_does_not_build_an_empty_grid(self):
        """One value after a billion empty columns and a billion empty rows, in a sheet and in a text table."""
        far = ('<table:table-row table:number-rows-repeated="1000000000"><table:table-cell/></table:table-row>'
               + _row('<table:table-cell table:number-columns-repeated="1000000000"/>', _cell("멀리")))
        sheet = _convert("spreadsheet", _sheet("S", _row(_cell("가까이")), far))
        assert "가까이" in sheet.text and "멀리" not in sheet.text
        assert "Sheet 'S': columns beyond column 100 were excluded." in sheet.warnings
        assert _odt(f"<table:table>{_row(_cell('가까이'), _cell('옆'))}{far}</table:table>").count("멀리") == 1

    def test_deeply_nested_elements_keep_their_text_without_recursing(self):
        """Spans and sections nested 150 deep, lists 100 deep and tables 60 deep (each just below the XML parser's own limit of 256
        elements): rendering recurses only to a fixed depth and keeps the innermost text as plain text."""
        depth = 150
        spans = "<text:p>" + "<text:span>" * depth + "깊은 글자" + "</text:span>" * depth + "</text:p>"
        lists = "<text:list><text:list-item>" * 100 + "<text:p>깊은 항목</text:p>" + "</text:list-item></text:list>" * 100
        sections = "<text:section>" * depth + "<text:p>깊은 구역</text:p>" + "</text:section>" * depth
        tables = ("<table:table><table:table-row><table:table-cell>" * 60 + "<text:p>깊은 표</text:p>"
                  + "</table:table-cell></table:table-row></table:table>" * 60)
        text = _odt(spans + lists + sections + tables)
        for expected in ("깊은 글자", "깊은 항목", "깊은 구역", "깊은 표"):
            assert expected in text

    def test_nesting_beyond_the_parser_limit_fails_cleanly(self):
        """100,000 nested spans: either the XML parser refuses the depth (a corrupted document) or the text is kept — never a
        RecursionError or a crash."""
        depth = 100_000
        body = "<text:p>" + "<text:span>" * depth + "끝" + "</text:span>" * depth + "</text:p>"
        try:
            assert "끝" in _odt(body)
        except CorruptedDocumentError as error:
            assert str(error) == "The content of the OpenDocument file (content.xml) is missing or corrupted."

    def test_entities_are_not_expanded(self):
        """An internal entity bomb in content.xml: entities are not resolved, so nothing is expanded."""
        content = ('<?xml version="1.0"?><!DOCTYPE d [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
                   '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]>'
                   f'<office:document-content {_NAMESPACES}><office:body><office:text><text:p>값 &c;&c;&c; 끝</text:p>'
                   '</office:text></office:body></office:document-content>').encode()
        text = convert(_package("text", "", content=content), "a.odt").text
        assert "aaaa" not in text and "끝" in text

    def test_style_that_inherits_from_itself_and_absurd_numbers_do_not_hang(self):
        """A text style whose parent is itself, a list level of 10^9, a `text:s` count of 10^9 and a start value too long for
        `int()`."""
        automatic = ('<style:style style:name="T" style:family="text" style:parent-style-name="T"/>'
                     '<text:list-style style:name="L"><text:list-level-style-number text:level="1000000000" style:num-format="1"/>'
                     '</text:list-style>')
        text = _odt('<text:p><text:span text:style-name="T">가</text:span><text:s text:c="1000000000"/>나</text:p>'
                    f'<text:list text:style-name="L"><text:list-item text:start-value="{"9" * 5000}"><text:p>항목</text:p>'
                    '</text:list-item></text:list>', automatic=automatic)
        assert text == "가 나\n\n- 항목"

    def test_picture_shown_many_times_is_read_once(self):
        """One picture referenced by 300 frames: the part is decompressed once and every registered image is the same bytes
        object, so the converter decodes it once as well."""
        frames = '<draw:frame><draw:image xlink:href="Pictures/a.png"/></draw:frame>' * 300
        data = _package("text", f"<text:p>{frames}</text:p>", parts={"Pictures/a.png": builder.png_bytes()})
        extraction = extract_odt(data, ConvertOptions())
        assert len(extraction.images) == 300
        assert len({id(image.data) for image in extraction.images}) == 1

    def test_picture_link_out_of_the_package_is_not_followed(self):
        """Links to the network, to an absolute path and up the tree are not read (and never fetched)."""
        frames = "".join(f'<draw:frame><draw:image xlink:href="{href}"/></draw:frame>'
                         for href in ("https://example.com/a.png", "/etc/passwd", "../outside.png"))
        result = _convert("text", f"<text:p>본문{frames}</text:p>")
        assert result.text == "본문" and not result.images

    @pytest.mark.timing
    def test_large_sheet_converts_in_linear_time(self):
        """4× the rows take well under 16× the time (a quadratic pass over merges or rows would show)."""
        def elapsed(count: int) -> float:
            rows = "".join(_row(_cell(f"이름{index}"), _cell(str(index)), _cell("비고", 'table:number-rows-spanned="2"'))
                           for index in range(count))
            data = _package("spreadsheet", _sheet("S", rows))
            started = time.perf_counter()
            convert(data, "a.ods", ConvertOptions(max_chars=None))
            return time.perf_counter() - started

        small = max(elapsed(5_000), 0.01)
        assert elapsed(20_000) < small * 10
