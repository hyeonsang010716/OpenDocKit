"""Hyperlinks and bold / italic kept as Markdown in DOCX, PPTX and HTML (`ConvertOptions.keep_links`, `keep_emphasis`).

Both options are on by default. The decisions pinned here are recorded in docs/decisions/inline-formatting.md. Documents are built
in memory: DOCX and PPTX with python-docx / python-pptx plus the XML those libraries cannot write, HTML as text.
"""

from io import BytesIO
import time

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE
from lxml import etree
from pptx import Presentation
from pptx.util import Inches
import pytest

from opendockit import ConvertOptions, convert
from opendockit.extractors import html as html_extractor
from opendockit.extractors._inline import (
    BLOCK_INSIDE,
    BOLD_OFF,
    BOLD_ON,
    LINK_ON,
    LINK_OPEN,
    link_close,
    link_off,
    link_target,
    mark_emphasis,
    merge_emphasis_marks,
    plain_inline,
    resolve_inline,
)
from tests import builder


_W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PLAIN = ConvertOptions(keep_links=False, keep_emphasis=False)


def _docx(body: str, *, links: dict[str, str] | None = None, styles: str = "") -> bytes:
    """A DOCX whose body is given as WordprocessingML. `links` maps a name to an address: an external hyperlink relationship is
    made for each and `{name}` in the body is replaced by its relationship id. `styles` is appended to the style definitions."""
    def fragment(xml: str) -> list:
        return list(etree.fromstring(f'<root xmlns:w="{_W}" xmlns:r="{_R}">{xml}</root>'))

    document = Document()
    for name, address in (links or {}).items():
        body = body.replace(f"{{{name}}}", document.part.relate_to(address, RELATIONSHIP_TYPE.HYPERLINK, is_external=True))
    section = document.element.body[-1]
    for element in fragment(body):
        section.addprevious(element)
    for element in fragment(styles):
        document.styles.element.append(element)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _run(text: str, properties: str = "") -> str:
    return f'<w:r><w:rPr>{properties}</w:rPr><w:t xml:space="preserve">{text}</w:t></w:r>'


def _bold(text: str) -> str:
    return _run(text, "<w:b/>")


def _italic(text: str) -> str:
    return _run(text, "<w:i/>")


def _paragraph(*runs: str, style: str = "") -> str:
    properties = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{properties}{''.join(runs)}</w:p>"


def _link(name: str, *runs: str) -> str:
    return f'<w:hyperlink r:id="{{{name}}}">{"".join(runs)}</w:hyperlink>'


def _table(*rows: list[str]) -> str:
    """A table whose cells are given as paragraph XML."""
    return "<w:tbl>" + "".join("<w:tr>" + "".join(f"<w:tc>{cell}</w:tc>" for cell in row) + "</w:tr>" for row in rows) + "</w:tbl>"


def _docx_text(body: str, options: ConvertOptions | None = None, **parts: object) -> str:
    return convert(_docx(body, **parts), "a.docx", options).text


def _html_text(html: str, options: ConvertOptions | None = None) -> str:
    return convert(html.encode(), "a.html", options).text


def _deck(build) -> bytes:
    """A one-slide deck; `build(slide)` adds the shapes."""
    presentation = Presentation()
    build(presentation.slides.add_slide(presentation.slide_layouts[5]))
    buffer = BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def _text_box(slide: object, *runs: tuple[str, dict]) -> object:
    """A text box with one paragraph; each run is (text, {"bold": …, "italic": …, "link": address})."""
    paragraph = slide.shapes.add_textbox(Inches(1), Inches(2), Inches(6), Inches(1)).text_frame.paragraphs[0]
    _add_runs(paragraph, runs)
    return paragraph


def _add_runs(paragraph: object, runs: tuple[tuple[str, dict], ...]) -> None:
    for text, look in runs:
        run = paragraph.add_run()
        run.text = text
        if "bold" in look:
            run.font.bold = look["bold"]
        if "italic" in look:
            run.font.italic = look["italic"]
        if "link" in look:
            run.hyperlink.address = look["link"]


def _seconds(action) -> float:
    """Fastest of three runs — a brief stall of the machine must not look like slow code."""
    best = float("inf")
    for _ in range(3):
        started = time.perf_counter()
        action()
        best = min(best, time.perf_counter() - started)
    return best


class TestLinkTargets:
    """Which addresses become a link, and how an address is written so that it cannot end the link early."""

    @pytest.mark.parametrize("address", ["https://example.com/a", "http://example.com", "HTTPS://Example.com/Path",
                                          "mailto:team@example.com", "  https://example.com/a  "])
    def test_http_https_and_mailto_addresses_are_kept(self, address):
        assert link_target(address) == address.strip()

    @pytest.mark.parametrize("address", ["", None, "   ", "/docs/guide.html", "guide.html", "../up", "#section", "//example.com/x",
                                          "javascript:alert(1)", "JavaScript:void(0)", "file:///etc/passwd", "data:text/html,x",
                                          "ftp://example.com/f", "tel:+82-2-000-0000", "https://", "mailto:", "vbscript:x"])
    def test_other_addresses_are_not_links(self, address):
        """A relative address or a fragment points nowhere outside the original page, and a script, file or data address must not
        become something the reader of the Markdown can follow."""
        assert link_target(address) is None

    def test_characters_that_would_break_the_markdown_are_percent_encoded(self):
        """`)` ends the link, a space ends the address, `<` `>` open an autolink, `|` splits a table row and a backslash escapes
        the character after it. Whitespace of any kind (a line break, a non-breaking space) is encoded too — the cleanup step
        would turn it into a plain space."""
        assert (link_target("https://example.com/a (b)/<c>|d\\e\tf\ng\u00a0h")
                == "https://example.com/a%20%28b%29/%3Cc%3E%7Cd%5Ce%09f%0Ag%C2%A0h")

    def test_korean_address_is_kept_readable(self):
        assert link_target("https://ko.example.com/문서/보고서?연도=2024") == "https://ko.example.com/문서/보고서?연도=2024"

    def test_text_that_closes_the_link_cannot_point_it_somewhere_else(self):
        """Link text `여기](https://evil.example) [x` would end the link early and start a second one with an address the
        document's author chose as text. Brackets that are unbalanced or followed by `(` or `[` are escaped."""
        marked = f"{LINK_OPEN}여기](https://evil.example) [x{link_close('https://good.example/')}"

        assert resolve_inline(marked) == "[여기\\](https://evil.example) \\[x](https://good.example/)"

    def test_a_link_written_out_inside_link_text_is_escaped(self):
        marked = f"{LINK_OPEN}보기 [원문](https://evil.example) 참고{link_close('https://good.example/')}"

        assert resolve_inline(marked) == "[보기 \\[원문\\](https://evil.example) 참고](https://good.example/)"

    def test_balanced_brackets_in_link_text_stay(self):
        marked = f"{LINK_OPEN}보고서 [1] 보기{link_close('https://good.example/')}"

        assert resolve_inline(marked) == "[보고서 [1] 보기](https://good.example/)"

    def test_backslash_at_the_end_of_link_text_does_not_escape_the_bracket(self):
        marked = f"{LINK_OPEN}C:\\문서\\{link_close('https://good.example/')}"

        assert resolve_inline(marked) == "[C:\\문서\\\\](https://good.example/)"

    def test_marks_can_be_taken_out_again(self):
        """`plain_inline` gives the text the paragraph had before the options existed — the extractors decide what a paragraph is
        (empty, a heading) from it."""
        marked = merge_emphasis_marks(f"{mark_emphasis('굵게', True, False)} {LINK_OPEN}링크{link_close('https://a.example/')}")

        assert plain_inline(marked) == "굵게 링크"
        assert resolve_inline("표시 없는 문장") == "표시 없는 문장"


class TestDocxLinks:
    def test_hyperlink_with_an_external_relationship_is_a_link(self):
        body = _paragraph(_run("배포 전에 "), _link("guide", _run("배포 안내서")), _run("를 읽습니다."))

        text = _docx_text(body, links={"guide": "https://example.com/release-guide"})

        assert text == "배포 전에 [배포 안내서](https://example.com/release-guide)를 읽습니다."

    def test_link_split_into_several_runs_is_one_link(self):
        """Word splits link text into runs (spelling marks, a change of font). Each run must not become its own link."""
        body = _paragraph(_link("guide", _run("배포 "), _run("안내"), _run("서")), _run(" 참고"))

        assert _docx_text(body, links={"guide": "https://example.com/guide"}) == "[배포 안내서](https://example.com/guide) 참고"

    def test_bookmark_is_appended_as_the_fragment(self):
        body = _paragraph(f'<w:hyperlink r:id="{{guide}}" w:anchor="설치">{_run("설치 절")}</w:hyperlink>')

        assert _docx_text(body, links={"guide": "https://example.com/guide"}) == "[설치 절](https://example.com/guide#설치)"

    def test_jump_inside_the_document_and_a_broken_relationship_stay_text(self):
        """A w:hyperlink with only a bookmark (w:anchor) jumps inside the document; a relationship id that does not exist is a
        damaged file. Neither has an address to keep."""
        body = _paragraph(f'<w:hyperlink w:anchor="chapter2">{_run("2장")}</w:hyperlink>', _run(" / "),
                          f'<w:hyperlink r:id="rId999">{_run("끊긴 링크")}</w:hyperlink>')

        assert _docx_text(body) == "2장 / 끊긴 링크"

    @pytest.mark.parametrize("address", ["guide.docx", "file:///C:/docs/guide.docx", "javascript:alert(1)", "#top"])
    def test_external_relationship_to_something_other_than_a_web_address_stays_text(self, address):
        body = _paragraph(_link("target", _run("안내서")))

        assert _docx_text(body, links={"target": address}) == "안내서"

    def test_hyperlink_field_code_is_a_link(self):
        """Older documents and some generators write a link as a field: `HYPERLINK "address"` between fldChar begin and separate,
        the visible text up to fldChar end."""
        field = ('<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> HYPERLINK '
                 '"https://example.com/notice" \\o "공지" </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r>'
                 f'{_run("공지 ")}{_run("사항")}<w:r><w:fldChar w:fldCharType="end"/></w:r>')

        assert _docx_text(_paragraph(_run("자세한 내용은 "), field, _run(" 참고"))) == "자세한 내용은 [공지 사항](https://example.com/notice) 참고"

    def test_simple_hyperlink_field_is_a_link(self):
        body = _paragraph(f'<w:fldSimple w:instr=" HYPERLINK &quot;https://example.com/faq&quot; ">{_run("자주 묻는 질문")}</w:fldSimple>')

        assert _docx_text(body) == "[자주 묻는 질문](https://example.com/faq)"

    def test_hyperlink_field_to_a_bookmark_or_without_an_end_stays_text(self):
        """`HYPERLINK \\l "bookmark"` jumps inside the document. A field that never ends (a damaged file) must not swallow the
        rest of the paragraph or leave half a link."""
        jump = ('<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> HYPERLINK \\l "chapter2" </w:instrText></w:r>'
                f'<w:r><w:fldChar w:fldCharType="separate"/></w:r>{_run("2장")}<w:r><w:fldChar w:fldCharType="end"/></w:r>')
        open_field = ('<w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText> HYPERLINK "https://example.com/x" '
                      f'</w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r>{_run("끝나지 않는 필드")}')

        assert _docx_text(_paragraph(jump) + _paragraph(open_field)) == "2장\n\n끝나지 않는 필드"

    def test_link_whose_text_is_its_address_stays_the_bare_address(self):
        """`[https://a.example](https://a.example)` says the same thing twice. The same for a mail address linked to itself."""
        body = (_paragraph(_link("site", _run("https://example.com/docs")), _run(" 와 "), _link("mail", _run("team@example.com")))
                + _paragraph(_link("slash", _run("https://example.com"))))

        text = _docx_text(body, links={"site": "https://example.com/docs", "mail": "mailto:team@example.com",
                                       "slash": "https://example.com/"})

        assert text == "https://example.com/docs 와 team@example.com\n\nhttps://example.com"

    def test_link_around_a_picture_keeps_the_picture_and_writes_no_link(self):
        """A picture placeholder becomes a block below its line, so `[picture](url)` would be left as `[](url)`."""
        document = Document()
        document.add_picture(BytesIO(builder.png_bytes()))
        rel_id = document.part.relate_to("https://example.com/chart", RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
        run = document.paragraphs[0].runs[0]._r
        hyperlink = etree.SubElement(run.getparent(), f"{{{_W}}}hyperlink")
        hyperlink.set(f"{{{_R}}}id", rel_id)
        hyperlink.append(run)
        buffer = BytesIO()
        document.save(buffer)

        result = convert(buffer.getvalue(), "a.docx")

        assert result.text == "[Image]"
        assert len(result.images) == 1

    def test_picture_at_the_edge_of_a_link_stays_outside_the_brackets(self):
        marked = f"{LINK_OPEN} ⟦IMG:abc:1⟧ 차트 보기{link_close('https://example.com/chart')}"

        assert resolve_inline(marked) == " ⟦IMG:abc:1⟧ [차트 보기](https://example.com/chart)"

    def test_link_in_a_heading_a_list_item_and_a_table_cell(self):
        body = (_paragraph(_link("a", _run("개요")), style="Heading1")
                + _paragraph(_link("a", _run("목록 링크")), style="ListBullet")
                + _table([_paragraph(_run("항목")), _paragraph(_run("출처"))],
                         [_paragraph(_run("매출")), _paragraph(_link("b", _run("통계청")))]))

        text = _docx_text(body, links={"a": "https://example.com/a", "b": "https://example.com/q?a=1|2"})

        assert text == ("# [개요](https://example.com/a)\n\n- [목록 링크](https://example.com/a)\n\n"
                        "| 항목 | 출처 |\n| --- | --- |\n| 매출 | [통계청](https://example.com/q?a=1%7C2) |")

    def test_link_in_a_footnote_uses_the_relationships_of_the_footnote_part(self):
        """Sources are usually cited with a link in a footnote. The relationship id belongs to footnotes.xml, not to the body."""
        notes = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:footnotes xmlns:w="{_W}" xmlns:r="{_R}">'
                 f'<w:footnote w:id="2"><w:p>{_run("출처: ")}<w:hyperlink r:id="rId7">{_run("통계청")}</w:hyperlink></w:p></w:footnote>'
                 "</w:footnotes>")
        note_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                     '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                     f'<Relationship Id="rId7" Type="{RELATIONSHIP_TYPE.HYPERLINK}" Target="https://kostat.example/2024" '
                     'TargetMode="External"/></Relationships>')
        data = builder.zip_with(
            _docx(_paragraph(_run("매출이 늘었다"), '<w:r><w:footnoteReference w:id="2"/></w:r>')),
            edit={"word/_rels/document.xml.rels": lambda xml: xml.replace(
                      "</Relationships>", f'<Relationship Id="rId70" Type="{_R}/footnotes" Target="footnotes.xml"/></Relationships>'),
                  "[Content_Types].xml": lambda xml: xml.replace(
                      "</Types>", '<Override PartName="/word/footnotes.xml" ContentType="application/vnd.openxmlformats-'
                                  'officedocument.wordprocessingml.footnotes+xml"/></Types>')},
            add={"word/footnotes.xml": notes, "word/_rels/footnotes.xml.rels": note_rels})

        assert convert(data, "a.docx").text == "매출이 늘었다[^1]\n\n[^1]: 출처: [통계청](https://kostat.example/2024)"

    def test_links_off_gives_the_text_only(self):
        body = _paragraph(_run("배포 전에 "), _link("guide", _bold("배포 안내서")), _run("를 읽습니다."))
        data = _docx(body, links={"guide": "https://example.com/release-guide"})

        assert convert(data, "a.docx", ConvertOptions(keep_links=False)).text == "배포 전에 **배포 안내서**를 읽습니다."
        assert convert(data, "a.docx", _PLAIN).text == "배포 전에 배포 안내서를 읽습니다."


class TestDocxEmphasis:
    def test_bold_italic_and_both(self):
        body = _paragraph(_run("변경 사항 중 "), _bold("호환성 파괴"), _run("는 "), _italic("이전 안내"), _run("가 필요하고 "),
                          _run("필수", "<w:b/><w:i/>"), _run("입니다."))

        assert _docx_text(body) == "변경 사항 중 **호환성 파괴**는 *이전 안내*가 필요하고 ***필수***입니다."

    def test_value_zero_or_false_switches_the_emphasis_off(self):
        body = _paragraph(_run("보통 ", '<w:b w:val="0"/>'), _run("글자 ", '<w:i w:val="false"/>'), _run("굵게", '<w:b w:val="1"/>'),
                          _run(" 기울임", '<w:i w:val="true"/>'), _run(" 끝"))

        assert _docx_text(body) == "보통 글자 **굵게** *기울임* 끝"

    def test_strong_and_emphasis_character_styles_are_emphasis(self):
        """Word's 'Strong' and 'Emphasis' are character styles: the run only names the style. A direct `w:b w:val="0"` on the run
        still switches it off."""
        styles = ('<w:style w:type="character" w:styleId="a5"><w:name w:val="Strong"/><w:rPr><w:b/></w:rPr></w:style>'
                  '<w:style w:type="character" w:styleId="a6"><w:name w:val="Emphasis"/><w:rPr><w:i/></w:rPr></w:style>'
                  '<w:style w:type="character" w:styleId="a7"><w:name w:val="Book Title"/><w:rPr><w:b/></w:rPr></w:style>')
        body = _paragraph(_run("결과는 "), _run("중요", '<w:rStyle w:val="a5"/>'), _run("하고 "), _run("주의", '<w:rStyle w:val="a6"/>'),
                          _run("가 필요하며 "), _run("꺼짐", '<w:rStyle w:val="a5"/><w:b w:val="0"/>'), _run(" 과 "),
                          _run("다른 스타일", '<w:rStyle w:val="a7"/>'), _run(" 은 그대로."))

        assert _docx_text(body, styles=styles) == "결과는 **중요**하고 *주의*가 필요하며 꺼짐 과 다른 스타일 은 그대로."

    def test_boldness_of_a_paragraph_style_is_not_emphasis(self):
        """A paragraph style that is bold (a heading, a title, a custom label style) says what the paragraph is. Its runs carry
        no w:b of their own and must not be marked."""
        styles = ('<w:style w:type="paragraph" w:styleId="Label"><w:name w:val="Label"/><w:rPr><w:b/><w:i/></w:rPr></w:style>')
        body = _paragraph(_run("굵은 문단 스타일의 "), _run("문단"), style="Label") + _paragraph(_run("보통 문단"))

        assert _docx_text(body, styles=styles) == "굵은 문단 스타일의 문단\n\n보통 문단"

    def test_heading_is_not_emphasized(self):
        """Headings are bold by style, and a title typed in bold is still a heading. `# **제목**` is noise."""
        body = _paragraph(_bold("1장 "), _run("개요", "<w:b/><w:i/>"), style="Heading1") + _paragraph(_run("본문 "), _bold("강조"))

        assert _docx_text(body) == "# 1장 개요\n\n본문 **강조**"

    def test_runs_with_the_same_formatting_are_one_span(self):
        """Word splits a sentence into runs at every spelling mark and revision. `**매**​**출**` for one bold word is noise, and
        the space run between two bold words is usually not bold itself."""
        body = _paragraph(_bold("매"), _bold("출 "), _run(" "), _bold("증가"), _run(" 그리고 "), _italic("주의"), _run(" "), _italic("필요"))

        assert _docx_text(body) == "**매출 증가** 그리고 *주의 필요*"

    def test_whitespace_stays_outside_the_marks(self):
        """`** 굵게 **` is not emphasis in Markdown."""
        body = _paragraph(_run("앞"), _bold("  굵게  "), _run("뒤 "), _bold("   "), _italic(" 기울임 "), _run("끝"))

        assert _docx_text(body) == "앞 **굵게** 뒤 *기울임* 끝"

    def test_bold_inside_italic_and_the_other_way_round_nest(self):
        body = _paragraph(_bold("굵게 "), _run("둘 다", "<w:b/><w:i/>"), _bold(" 다시 굵게"), _run(" / "), _italic("기울임 "),
                          _run("둘 다", "<w:b/><w:i/>"), _run(" 끝"))

        assert _docx_text(body) == "**굵게 *둘 다* 다시 굵게** / *기울임* ***둘 다*** 끝"

    def test_line_break_closes_and_reopens_the_span_and_a_bullet_stays_in_front(self):
        """A span must not run across a line break into the next line's bullet: `**- 항목**` is not a list item."""
        body = _paragraph(_run("요약 "), '<w:r><w:rPr><w:b/></w:rPr><w:t>첫 줄</w:t><w:br/><w:t>- 둘째 줄</w:t></w:r>', _run(" 끝"))

        assert _docx_text(body) == "요약 **첫 줄**\n- **둘째 줄** 끝"

    def test_strikethrough_superscript_footnote_and_picture_inside_bold_text(self):
        """Strikethrough nests inside the emphasis, a script stays part of the bold span (`**H_2O**`, not `**H**_2**O**`), and
        a footnote reference is not emphasized."""
        body = _paragraph(_run("값 "), _run("1,000", "<w:b/><w:strike/>"), _run(" "), _bold("H"),
                          _run("2", '<w:b/><w:vertAlign w:val="subscript"/>'), _bold("O 농도"),
                          '<w:r><w:rPr><w:b/></w:rPr><w:footnoteReference w:id="2"/></w:r>', _bold(" 기준"), _run(" 입니다."))

        text = convert(_docx(body), "a.docx", ConvertOptions(include_footnotes=True)).text

        assert text.split("\n")[0] == "값 **~~1,000~~ H_2O 농도**[^1] **기준** 입니다."

    def test_literal_asterisk_or_backslash_at_the_edge_of_a_span_is_escaped(self):
        """`**주의***` would be read as a longer mark, and `**C:\\dir\\**` as an escaped asterisk."""
        body = (_paragraph(_run("표시 "), _bold("주의*"), _run(" 와 "), _bold("*각주"), _run(" 및 "), _italic("*"), _run(" 끝"))
                + _paragraph(_run("경로 "), _bold("C:\\dir\\"), _run(" 확인")))

        assert _docx_text(body) == "표시 **주의\\*** 와 **\\*각주** 및 *\\** 끝\n\n경로 **C:\\dir\\\\** 확인"

    def test_emphasis_off_gives_the_text_only(self):
        body = _paragraph(_run("변경 사항 중 "), _bold("호환성 파괴"), _run("는 "), _link("a", _italic("이전 안내")), _run("가 필요"))
        data = _docx(body, links={"a": "https://example.com/a"})

        assert convert(data, "a.docx", ConvertOptions(keep_emphasis=False)).text == (
            "변경 사항 중 호환성 파괴는 [이전 안내](https://example.com/a)가 필요")
        assert convert(data, "a.docx", _PLAIN).text == "변경 사항 중 호환성 파괴는 이전 안내가 필요"


class TestWholeBlocks:
    """Bold (or italic) that covers a whole paragraph, list item or table-cell line is how that block is styled — a label, a header
    cell, a slide caption — not a stress inside it. Marked, a quarter of all lines of real slide decks came out as `**…**`."""

    def test_paragraph_that_is_bold_throughout_is_not_marked(self):
        body = (_paragraph(_bold("□ 추진 "), _bold("배경")) + _paragraph(_run("본문에서는 "), _bold("핵심"), _run("만 표시합니다."))
                + _paragraph(_italic("※ 전체가 기울임인 주석")) + _paragraph(_run("전체", "<w:b/><w:i/>"), _bold(" 굵고 일부만 기울임")))

        assert _docx_text(body) == "□ 추진 배경\n\n본문에서는 **핵심**만 표시합니다.\n\n※ 전체가 기울임인 주석\n\n*전체* 굵고 일부만 기울임"

    def test_header_cells_and_label_cells_are_not_marked_and_rows_stay_whole(self):
        """Header cells are bold by convention; marking every one is noise. A cell with some bold words keeps them, and a `|` in
        bold text does not split the row."""
        body = _table([_paragraph(_bold("구분")), _paragraph(_bold("내용"))],
                      [_paragraph(_bold("합계")), _paragraph(_run("매출 "), _bold("1,200|900"), _run(" 억원"))],
                      [_paragraph(_bold("비고")) + _paragraph(_run("둘째 줄은 "), _italic("참고")), _paragraph(_run("-"))])

        assert _docx_text(body) == ("| 구분 | 내용 |\n| --- | --- |\n| 합계 | 매출 **1,200\\|900** 억원 |\n"
                                    "| 비고<br>둘째 줄은 *참고* | - |")

    def test_list_item_and_numbered_line_keep_their_marker_in_front(self):
        body = (_paragraph(_bold("전체가 굵은 항목"), style="ListBullet") + _paragraph(_bold("용어"), _run(": 설명"), style="ListBullet")
                + _paragraph(_bold("1. 손으로 쓴 번호 "), _run("뒤의 설명")))

        assert _docx_text(body) == "- 전체가 굵은 항목\n- **용어**: 설명\n1. **손으로 쓴 번호** 뒤의 설명"


class TestPartOfAWord:
    """Bold on part of a word is styling (a digit in another weight, a decorated initial), and marks inside a word are noise that
    Markdown readers do not agree on. A stressed word followed by its particle is the normal shape of Korean emphasis and stays."""

    def test_word_followed_by_its_particle_is_emphasized(self):
        body = _paragraph(_bold("매출"), _run("은 늘고 "), _bold("신규 요금제"), _run("를 도입했으며 "), _italic("배출"), _run("량도 줄었다."))

        assert _docx_text(body) == "**매출**은 늘고 **신규 요금제**를 도입했으며 *배출*량도 줄었다."

    def test_span_that_starts_inside_a_word_is_not_marked(self):
        body = _paragraph(_run("매출 1"), _bold("2"), _run(",345원, 인사"), _italic("발령"), _run(" 공지, 비"), _bold("공개"), _run(" 문서"))

        assert _docx_text(body) == "매출 12,345원, 인사발령 공지, 비공개 문서"
        assert _html_text("<p>매출 1<b>2</b>,345원, 인사<em>발령</em> 공지</p>") == "매출 12,345원, 인사발령 공지"

    def test_single_character_glued_to_the_rest_of_its_word_is_not_marked(self):
        body = _paragraph(_bold("부"), _run("분 강조와 "), _bold("S"), _run("trength, 그리고 "), _bold("값"), _run(" 하나"))

        assert _docx_text(body) == "부분 강조와 Strength, 그리고 **값** 하나"
        assert _html_text("<p><b>한</b>국어와 <b>한국</b>어</p>") == "한국어와 **한국**어"


class TestPptx:
    def test_link_and_emphasis_in_a_text_box(self):
        data = _deck(lambda slide: _text_box(slide, ("출시 전에 ", {}), ("배포 안내서", {"link": "https://example.com/guide"}),
                                             ("를 읽고 ", {}), ("필수", {"bold": True}), (" 항목과 ", {}), ("참고", {"italic": True}),
                                             (" 항목을 확인", {})))

        assert convert(data, "a.pptx").sections[0].text == (
            "출시 전에 [배포 안내서](https://example.com/guide)를 읽고 **필수** 항목과 *참고* 항목을 확인")

    def test_runs_of_one_link_are_one_link_and_bold_inside_it_is_kept(self):
        """PowerPoint puts the link on every run (a:hlinkClick in a:rPr). Runs that follow each other with the same address are
        one link."""
        link = {"link": "https://example.com/plan"}
        data = _deck(lambda slide: _text_box(slide, ("2026 ", link), ("출시", {**link, "bold": True}), (" 계획", link), (" 참고", {})))

        assert convert(data, "a.pptx").sections[0].text == "[2026 **출시** 계획](https://example.com/plan) 참고"

    def test_caption_that_is_bold_throughout_is_not_marked(self):
        """Slide captions and labels are whole lines in bold."""
        def build(slide):
            _text_box(slide, ("핵심 지표", {"bold": True}))
            _text_box(slide, ("매출 ", {}), ("12%", {"bold": True}), (" 증가", {}))

        assert convert(_deck(build), "a.pptx").sections[0].text == "핵심 지표\n\n매출 **12%** 증가"

    def test_bold_set_to_false_and_a_jump_to_another_slide_are_plain(self):
        def build(slide):
            paragraph = _text_box(slide, ("꺼진 굵게 ", {"bold": False}), ("다음 슬라이드", {}), (" 보기", {}))
            properties = paragraph.runs[1]._r.get_or_add_rPr()
            jump = etree.SubElement(properties, "{http://schemas.openxmlformats.org/drawingml/2006/main}hlinkClick")
            jump.set(f"{{{_R}}}id", "")
            jump.set("action", "ppaction://hlinkshowjump?jump=nextslide")

        assert convert(_deck(build), "a.pptx").sections[0].text == "꺼진 굵게 다음 슬라이드 보기"

    def test_table_cell(self):
        def build(slide):
            table = slide.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(6), Inches(1)).table
            for column, (text, look) in enumerate([("구분", {"bold": True}), ("출처", {"bold": True})]):
                _add_runs(table.cell(0, column).text_frame.paragraphs[0], ((text, look),))
            _add_runs(table.cell(1, 0).text_frame.paragraphs[0], (("매출 ", {}), ("최대", {"bold": True})))
            _add_runs(table.cell(1, 1).text_frame.paragraphs[0], (("통계청", {"link": "https://kostat.example/a|b"}),))

        assert convert(_deck(build), "a.pptx").sections[0].text == (
            "| 구분 | 출처 |\n| --- | --- |\n| 매출 **최대** | [통계청](https://kostat.example/a%7Cb) |")

    def test_slide_title_stays_plain(self):
        """The title is the heading of the slide section."""
        def build(slide):
            _add_runs(slide.shapes.title.text_frame.paragraphs[0], (("출시 ", {}), ("계획", {"bold": True, "link": "https://example.com/"})))
            _text_box(slide, ("본문", {}))

        result = convert(_deck(build), "a.pptx")

        assert result.sections[0].title == "출시 계획"
        assert result.text == "## Slide 1: 출시 계획\n\n본문"

    def test_options_off_give_the_text_only(self):
        data = _deck(lambda slide: _text_box(slide, ("출시 전에 ", {}), ("안내서", {"link": "https://example.com/guide", "bold": True}),
                                             (" 확인", {})))

        assert convert(data, "a.pptx", _PLAIN).sections[0].text == "출시 전에 안내서 확인"
        assert convert(data, "a.pptx", ConvertOptions(keep_links=False)).sections[0].text == "출시 전에 **안내서** 확인"
        assert convert(data, "a.pptx", ConvertOptions(keep_emphasis=False)).sections[0].text == (
            "출시 전에 [안내서](https://example.com/guide) 확인")


class TestHtmlLinks:
    def test_absolute_link_is_kept_and_other_addresses_stay_text(self):
        """Nothing is resolved against a page address (a saved file has none) and nothing is fetched."""
        html = ('<p><a href="https://example.com/guide">배포 안내서</a>, <a href="/docs/faq">상대 주소</a>, <a href="#top">맨 위</a>, '
                '<a href="javascript:open()">스크립트</a>, <a href="">빈 주소</a>, <a>주소 없음</a>, '
                '<a href="mailto:team@example.com?subject=문의 사항">메일 보내기</a></p>')

        assert _html_text(html) == ("[배포 안내서](https://example.com/guide), 상대 주소, 맨 위, 스크립트, 빈 주소, 주소 없음, "
                                    "[메일 보내기](mailto:team@example.com?subject=문의%20사항)")

    def test_link_whose_text_is_its_address_stays_the_bare_address(self):
        html = '<p><a href="https://example.com/docs">https://example.com/docs</a> / <a href="mailto:a@example.com">a@example.com</a></p>'

        assert _html_text(html) == "https://example.com/docs / a@example.com"

    def test_address_cannot_close_the_link_or_split_a_table_row(self):
        html = ('<table><tr><th>이름</th><th>링크</th></tr><tr><td>보고서</td>'
                '<td><a href="https://example.com/a) [x](https://evil.example/|b c">열기</a></td></tr></table>')

        assert _html_text(html) == ("| 이름 | 링크 |\n| --- | --- |\n"
                                    "| 보고서 | [열기](https://example.com/a%29%20[x]%28https://evil.example/%7Cb%20c) |")

    def test_link_around_a_picture_keeps_the_placeholder(self):
        """An external picture is not fetched and keeps its alt text; the link around it has no text to carry."""
        html = ('<p><a href="https://example.com/"><img src="logo.png" alt="회사 로고"></a> '
                '<a href="https://example.com/chart"><img src="chart.png" alt="차트"> 차트 보기</a></p>')

        assert _html_text(html) == "[Image: 회사 로고] [Image: 차트] [차트 보기](https://example.com/chart)"

    def test_link_around_one_heading_is_written_inside_the_heading(self):
        """Card layouts wrap the heading in the link (`<a><h3>…</h3></a>`). The mark goes after the `#`."""
        html = '<a href="https://example.com/post/1"><h3>첫 번째 글</h3></a><p>요약</p>'

        assert _html_text(html) == "### [첫 번째 글](https://example.com/post/1)\n\n요약"

    def test_link_around_several_blocks_or_a_table_stays_text(self):
        """A Markdown link cannot hold a blank line, a table or a list. The text is kept; the address is not written."""
        html = ('<a href="https://example.com/card"><div>제목</div><p>요약 문장</p></a>'
                '<a href="https://example.com/t"><table><tr><td>가</td><td>나</td></tr><tr><td>1</td><td>2</td></tr></table></a>'
                '<a href="https://example.com/l"><ul><li>항목</li></ul></a>')

        assert _html_text(html) == "제목\n\n요약 문장\n\n| 가 | 나 |\n| --- | --- |\n| 1 | 2 |\n\n- 항목"

    def test_link_in_a_heading_a_list_item_and_a_quote(self):
        html = ('<h2><a href="https://example.com/h">공지</a> 사항</h2><ul><li><a href="https://example.com/1">첫째</a> 항목</li></ul>'
                '<blockquote><p>인용 <a href="https://example.com/q">출처</a></p></blockquote>')

        assert _html_text(html) == ("## [공지](https://example.com/h) 사항\n\n- [첫째](https://example.com/1) 항목\n\n"
                                    "> 인용 [출처](https://example.com/q)")

    def test_control_characters_in_the_page_cannot_forge_a_link(self):
        """DOCX and PPTX announce a link with control characters inside the text, which XML cannot hold. HTML can (`&#18;`), so
        the HTML extractor never uses them: the characters are removed like any other control character."""
        html = "<p>보기 &#18;여기&#19;https://evil.example/&#20; 끝 &#14;굵게&#15;</p>"

        assert _html_text(html) == "보기 여기https://evil.example/ 끝 굵게"

    def test_links_off_gives_the_text_only(self):
        html = '<p><a href="https://example.com/guide">배포 <b>안내서</b></a> 참고</p>'

        assert _html_text(html, ConvertOptions(keep_links=False)) == "배포 **안내서** 참고"
        assert _html_text(html, _PLAIN) == "배포 안내서 참고"


class TestHtmlEmphasis:
    def test_bold_and_italic_elements(self):
        html = "<p><b>굵게</b> 와 <strong>강조</strong>, <i>기울임</i> 과 <em>강세</em>, <b><i>둘 다</i></b> 입니다.</p>"

        assert _html_text(html) == "**굵게** 와 **강조**, *기울임* 과 *강세*, ***둘 다*** 입니다."

    def test_cite_var_and_css_weight_are_not_emphasis(self):
        """Deliberately only the four emphasis elements. CSS weight and the italic of `<cite>` / `<var>` are how a page is styled."""
        html = ('<p><cite>보고서 제목</cite> 의 <var>x</var> 값과 <span style="font-weight:bold">CSS 굵게</span>, '
                '<span style="font-style:italic">CSS 기울임</span></p>')

        assert _html_text(html) == "보고서 제목 의 x 값과 CSS 굵게, CSS 기울임"

    def test_code_is_not_emphasized(self):
        """`*` is a character of the code there."""
        html = "<pre>a = <b>b</b> * c</pre><p>명령 <code>run <b>--force</b></code> 과 <kbd><i>Ctrl</i>+C</kbd> 입력</p>"

        assert _html_text(html) == "```\na = b * c\n```\n\n명령 run --force 과 Ctrl+C 입력"

    def test_heading_and_caption_are_not_emphasized(self):
        html = ("<h2><b>1장</b> <em>개요</em></h2><table><caption>표 1. <b>매출</b> 현황</caption>"
                "<tr><th>연도</th><th>값</th></tr><tr><td>2024</td><td>1</td></tr></table>")

        assert _html_text(html) == "## 1장 개요\n\n표 1. 매출 현황\n\n| 연도 | 값 |\n| --- | --- |\n| 2024 | 1 |"

    def test_cell_that_is_bold_throughout_is_not_marked_and_rows_stay_whole(self):
        html = ("<table><tr><td><b>구분</b></td><td><strong>내용</strong></td></tr>"
                "<tr><td><b>합계</b></td><td>매출 <b>1,200|900</b> 억원<br><i>잠정</i> 수치</td></tr></table>")

        assert _html_text(html) == "| 구분 | 내용 |\n| --- | --- |\n| 합계 | 매출 **1,200\\|900** 억원<br>*잠정* 수치 |"

    def test_paragraph_and_list_item_that_are_bold_throughout_are_not_marked(self):
        html = "<p><strong>공지 사항</strong></p><ul><li><b>전체가 굵은 항목</b></li><li><b>용어</b>: 설명</li></ul>"

        assert _html_text(html) == "공지 사항\n\n- 전체가 굵은 항목\n- **용어**: 설명"

    def test_unclosed_bold_element_does_not_mark_the_rest_of_the_page(self):
        """A `<b>` that is never closed wraps everything after it. Emphasis does not cross paragraphs, so nothing is marked — but
        emphasis inside those paragraphs still is."""
        html = "<p>첫 문단 <b>굵게 시작</p><p>둘째 문단 <i>기울임</i> 포함</p><ul><li>항목</li></ul><p>끝 문단</p>"

        assert _html_text(html) == "첫 문단 **굵게 시작**\n\n둘째 문단 *기울임* 포함\n\n- 항목\n\n끝 문단"

    def test_emphasis_around_a_single_paragraph_marks_nothing_but_words_inside_it(self):
        html = "<b><p>문단 전체가 굵은 요소 안</p></b><i><div>기울임 안의 <b>굵은 단어</b> 하나</div></i>"

        assert _html_text(html) == "문단 전체가 굵은 요소 안\n\n기울임 안의 **굵은 단어** 하나"

    def test_strikethrough_and_emphasis_nest(self):
        """The marks must close in the order they opened: `~~구 **판~~**` is neither."""
        html = "<p><s>이전 <b>가격</b></s> 과 <b><del>삭제</del> 후 유지</b>, <s><b>굵게</b> 시작</s> 끝</p>"

        assert _html_text(html) == "~~이전 **가격**~~ 과 **~~삭제~~ 후 유지**, ~~**굵게** 시작~~ 끝"

    def test_line_break_closes_and_reopens_the_span(self):
        html = "<p>요약: <b>첫 줄<br>둘째 줄</b> 끝</p>"

        assert _html_text(html) == "요약: **첫 줄**\n**둘째 줄** 끝"

    def test_title_is_found_in_a_first_line_that_carries_marks(self):
        """`<title>` becomes a heading only when the body does not already start with it. The marks must not hide it."""
        html = "<html><head><title>주간 보고</title></head><body><p><b>주간</b> 보고 (3월)</p><p>본문</p></body></html>"

        assert _html_text(html) == "**주간** 보고 (3월)\n\n본문"

    def test_emphasis_off_gives_the_text_only(self):
        html = '<p><b>굵게</b> 와 <i>기울임</i>, <a href="https://example.com/"><b>링크</b> 글</a></p>'

        assert _html_text(html, ConvertOptions(keep_emphasis=False)) == "굵게 와 기울임, [링크 글](https://example.com/)"
        assert _html_text(html, _PLAIN) == "굵게 와 기울임, 링크 글"


class TestResolver:
    """`_inline.resolve_inline` on its own: the shapes the extractors hand over."""

    def test_span_with_words_in_two_blocks_is_not_written(self):
        tokens = ["앞 ", BOLD_ON, "첫 블록", "\n\n", "둘째 블록", BOLD_OFF, " ", LINK_ON, "한 블록", link_off("https://a.example/"), " ",
                  LINK_ON, "링크\n\n둘", link_off("https://b.example/")]

        assert resolve_inline(tokens) == "앞 첫 블록\n\n둘째 블록 [한 블록](https://a.example/) 링크\n\n둘"

    def test_block_rendered_on_its_own_breaks_the_span_around_it(self):
        tokens = [BOLD_ON, "앞", BLOCK_INSIDE, "\n\n| 가 | 나 |\n\n", "뒤", BOLD_OFF, " 그리고 ", BOLD_ON, "굵게", BOLD_OFF]

        assert resolve_inline(tokens) == "앞\n\n| 가 | 나 |\n\n뒤 그리고 **굵게**"

    def test_link_that_is_never_closed_is_not_written(self):
        assert resolve_inline(f"앞 {LINK_OPEN}열린 채 끝나는 링크") == "앞 열린 채 끝나는 링크"

    def test_inner_link_wins_over_the_link_around_it(self):
        marked = f"{LINK_OPEN}바깥 {LINK_OPEN}안쪽{link_close('https://in.example/')} 글{link_close('https://out.example/')}"

        assert resolve_inline(marked) == "[바깥](https://out.example/) [안쪽](https://in.example/) [글](https://out.example/)"

    def test_emphasis_stops_at_the_edge_of_a_link(self):
        """`**앞 [링크**](url)` would cross the two notations."""
        marked = merge_emphasis_marks(f"{mark_emphasis('굵은 앞 ', True, False)}{LINK_OPEN}{mark_emphasis('링크', True, False)}"
                                      f"{link_close('https://a.example/')}{mark_emphasis(' 굵은 뒤', True, False)} 보통")

        assert resolve_inline(marked) == "**굵은 앞** [**링크**](https://a.example/) **굵은 뒤** 보통"

    def test_placeholders_are_not_emphasized(self):
        marked = mark_emphasis("⟦IMG:abc:1⟧", True, False) + " " + mark_emphasis("[^1]", True, False) + " 본문 " + mark_emphasis("굵게", True, False)

        assert resolve_inline(marked) == "⟦IMG:abc:1⟧ [^1] 본문 **굵게**"


class TestCharacterLimit:
    """`exceeded` must equal "the full conversion is longer than `max_chars`". The marks and addresses are part of the text, so
    they count — but nothing may be counted while reading that is not in the final text."""

    @staticmethod
    def _check(data: bytes, name: str) -> tuple[int, int]:
        full = convert(data, name, ConvertOptions(max_chars=None))
        plain = convert(data, name, ConvertOptions(max_chars=None, keep_links=False, keep_emphasis=False))
        assert full.char_count == len(full.text) > plain.char_count
        for limit in (plain.char_count - 1, plain.char_count, (plain.char_count + full.char_count) // 2, full.char_count - 1,
                      full.char_count, full.char_count + 1):
            result = convert(data, name, ConvertOptions(max_chars=limit))
            assert result.exceeded == (full.char_count > limit), limit
        return full.char_count, plain.char_count

    def test_docx_paragraphs_with_links_and_emphasis(self):
        body = "".join(_paragraph(_run(f"{index}번 문단의 "), _bold("강조"), _run(" 와 "), _link("a", _run("링크")), _run(" 입니다."))
                       for index in range(200))

        self._check(_docx(body, links={"a": "https://example.com/a-long-address-for-the-link"}), "a.docx")

    def test_docx_table_whose_cells_are_bold_throughout_is_not_counted_with_marks(self):
        """A table is counted cell by cell while it is read (`TableBudget`). The cells here are bold from start to end and come
        out unmarked; counting the marks that were never written stopped a document that is within the limit."""
        rows = [[_paragraph(_bold(f"항목 {row}")), _paragraph(_bold("값")), _paragraph(_run("보통 "), _bold("일부"))] for row in range(300)]
        data = _docx(_table(*rows))

        full, plain = self._check(data, "a.docx")

        assert full - plain == 300 * 4  # only the partly bold cell of every row is marked
        assert not convert(data, "a.docx", ConvertOptions(max_chars=full)).exceeded

    def test_pptx_slides(self):
        def build(slide):
            for index in range(40):
                _text_box(slide, (f"{index}번 ", {}), ("강조", {"bold": True}), (" 와 ", {}), ("링크", {"link": "https://example.com/a"}))

        self._check(_deck(build), "a.pptx")

    def test_html_page_with_links_and_emphasis(self):
        html = "".join(f'<p>{index}번 문단의 <b>강조</b> 와 <a href="https://example.com/a-long-address/{index}">링크</a></p>'
                       for index in range(2000))

        self._check(html.encode(), "a.html")

    def test_front_of_a_large_page_does_not_count_link_addresses(self, monkeypatch):
        """A large page is first judged from its front part alone. A heading cut at the end of the front part holds links there;
        in the whole page the same heading turns out to hold a list and is read as text only, without the addresses. Counting
        the addresses in the front part stopped a page that is within the limit."""
        links = "".join(f'<a href="https://example.com/a/very/long/address/for/item/{index}">글{index}</a> ' for index in range(80))
        html = f"<p>머리말</p><h2>{links}<ul><li>목록</li></ul></h2>" + "<!-- 주석 -->" * 2000 + "<p>끝</p>"
        full = convert(html.encode(), "a.html", ConvertOptions(max_chars=None))
        limit = 2000
        assert full.char_count < limit < len(html) // 8 and "https://" not in full.text

        assert not convert(html.encode(), "a.html", ConvertOptions(max_chars=limit)).exceeded

        # The same page is stopped early when the addresses are counted — the case really goes through the front part
        monkeypatch.setattr(html_extractor, "_LINK_ADDRESS", html_extractor.re.compile(r"(?!)"))
        assert convert(html.encode(), "a.html", ConvertOptions(max_chars=limit)).exceeded

    def test_link_cut_at_the_end_of_the_front_part_stays_text_there(self):
        """The front part of a large page ends inside a link whose text goes on. With the address written, the front part would
        hold a shorter link text than the whole page — text that is not in the whole conversion."""
        words = "<span>긴 링크 글</span> " * 2000
        html = f'<p><a href="https://example.com/long">{words}</a></p>' + "<p>본문 문단</p>" * 4000

        full = convert(html.encode(), "a.html", ConvertOptions(max_chars=None))
        stopped = convert(html.encode(), "a.html", ConvertOptions(max_chars=1000))

        assert full.text.startswith("[긴 링크 글") and stopped.exceeded
        assert stopped.text.startswith("긴 링크 글")


class TestLinearTime:
    """Tens of thousands of runs, links or nested elements must take time proportional to their number."""

    @pytest.mark.timing
    def test_docx_paragraph_with_many_formatted_runs(self):
        def document(count: int) -> bytes:
            runs = (_run(f"단어{index} ", ("<w:b/>", "<w:i/>", "")[index % 3]) for index in range(count))
            return _docx(_paragraph(*runs))

        small, large = document(5_000), document(20_000)
        assert convert(large, "a.docx").text.startswith("**단어0** *단어1* 단어2 **단어3**")

        assert _seconds(lambda: convert(large, "a.docx")) < 8 * _seconds(lambda: convert(small, "a.docx"))

    @pytest.mark.timing
    def test_docx_paragraph_with_many_links(self):
        def document(count: int) -> bytes:
            return _docx(_paragraph(*(_link("a", _run(f"링크{index}")) + _run(" ") for index in range(count))),
                         links={"a": "https://example.com/a"})

        small, large = document(5_000), document(20_000)

        assert _seconds(lambda: convert(large, "a.docx")) < 8 * _seconds(lambda: convert(small, "a.docx"))

    @pytest.mark.timing
    def test_html_page_with_many_links(self):
        def page(count: int) -> bytes:
            return ("<p>" + " ".join(f'<a href="https://example.com/p/{index}?q=(x)">[{index}] 링크</a>' for index in range(count))
                    + "</p>").encode()

        options = ConvertOptions(max_chars=None)
        small, large = page(10_000), page(40_000)
        assert convert(large, "a.html", options).text.startswith("[[0] 링크](https://example.com/p/0?q=%28x%29) [[1] 링크]")

        # Four times the links: 4x when linear, 16x when quadratic. The bound is 12, not 8: on a Linux virtual machine short of
        # memory the large page measured 8.3x in two runs of five while the time per link was flat from 10,000 to 80,000 links.
        assert _seconds(lambda: convert(large, "a.html", options)) < 12 * _seconds(lambda: convert(small, "a.html", options))

    @pytest.mark.timing
    def test_html_unclosed_emphasis_elements_nested_once_per_paragraph(self):
        """The parser nests an unclosed `<b>` one level deeper every time it appears. Each of them holds every block after it."""
        def page(count: int) -> bytes:
            return "".join(f'<b>굵게{index}<p>문단 <i>기울임</i> <a href="https://example.com/{index}">링크</a></p>'
                           for index in range(count)).encode()

        options = ConvertOptions(max_chars=None)
        small, large = page(5_000), page(20_000)

        assert _seconds(lambda: convert(large, "a.html", options)) < 8 * _seconds(lambda: convert(small, "a.html", options))

    @pytest.mark.timing
    def test_resolver_with_many_lines_placeholders_and_brackets(self):
        def text(count: int) -> str:
            line = (mark_emphasis("- 항목", True, False) + " " + LINK_OPEN + "[" * 3 + " ⟦IMG:ab:1⟧ 글 ](" + link_close("https://a.example/")
                    + mark_emphasis(" 기울임*", False, True))
            return "\n".join([line] * count)

        small, large = text(10_000), text(40_000)

        assert _seconds(lambda: resolve_inline(large)) < 8 * _seconds(lambda: resolve_inline(small))
