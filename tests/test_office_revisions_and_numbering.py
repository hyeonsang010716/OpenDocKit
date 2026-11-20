"""DOCX numbering restart (w:lvlRestart), numbered paragraphs wrapped by an empty content control, number references without a
definition; text boxes and shape text inside tracked changes and hidden runs; deleted table rows; Strict Open XML DOCX/PPTX."""

from io import BytesIO
import re
import zipfile

import pytest

from opendockit import ConvertOptions, UnsupportedFormatError, convert
from opendockit.extractors._package import transitional_package
from tests import builder
from tests.test_office import _docx_with_body, _number_list, _paragraph, _without_parts
from tests.timing import fastest


_WPS_NS = ('xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
           'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
           'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"')
_VML_NS = 'xmlns:v="urn:schemas-microsoft-com:vml"'
_STRICT_NAMESPACES = {
    "http://schemas.openxmlformats.org/wordprocessingml/2006/main": "http://purl.oclc.org/ooxml/wordprocessingml/main",
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships": "http://purl.oclc.org/ooxml/officeDocument/relationships",
    "http://schemas.openxmlformats.org/presentationml/2006/main": "http://purl.oclc.org/ooxml/presentationml/main",
    "http://schemas.openxmlformats.org/drawingml/2006/main": "http://purl.oclc.org/ooxml/drawingml/main",
}


def _level(level: int, text: str, restart: str = "") -> str:
    restart_element = f'<w:lvlRestart w:val="{restart}"/>' if restart else ""
    return (f'<w:lvl w:ilvl="{level}"><w:start w:val="1"/><w:numFmt w:val="decimal"/>{restart_element}'
            f'<w:lvlText w:val="{text}"/></w:lvl>')


def _text_box(text: str) -> str:
    return (f'<w:drawing><wp:anchor {_WPS_NS}><wp:docPr id="1" name="t"/><a:graphic><a:graphicData '
            'uri="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"><wps:wsp><wps:txbx><w:txbxContent>'
            f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:txbxContent></wps:txbx></wps:wsp></a:graphicData></a:graphic>'
            "</wp:anchor></w:drawing>")


def _shape_text(text: str) -> str:
    return (f"<w:drawing><a:graphic {_WPS_NS}><a:graphicData><a:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></a:txBody>"
            "</a:graphicData></a:graphic></w:drawing>")


def _word_art(text: str) -> str:
    return f'<w:pict><v:shape {_VML_NS}><v:textpath string="{text}"/></v:shape></w:pict>'


def _strict(data: bytes) -> bytes:
    """Copy with namespaces and relationship types replaced by the Strict Open XML (purl.oclc.org) ones. The same synthesis as the
    Excel Strict tests."""
    buffer = BytesIO()
    with zipfile.ZipFile(BytesIO(data)) as source, zipfile.ZipFile(buffer, "w") as target:
        for name in source.namelist():
            content = source.read(name)
            if name.endswith((".xml", ".rels")):
                text = content.decode("utf-8")
                for transitional, strict in _STRICT_NAMESPACES.items():
                    text = text.replace(transitional, strict)
                content = text.encode("utf-8")
            target.writestr(name, content)
    return buffer.getvalue()


class TestDocxLevelRestart:
    """w:lvlRestart was not read, so the lower level was counted again from 1 every time an upper level appeared. '제N조'
    (Article N), which continues across chapters, went back to '제1조' in every chapter."""

    def test_level_that_never_restarts_keeps_counting_across_chapters(self):
        numbering = ('<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="multilevel"/>'
                     + _level(0, "제%1장") + _level(1, "제%2조", restart="0") + "</w:abstractNum>" + _number_list(1))
        body = "".join(_paragraph(text, 1, level) for text, level in [
            ("총칙", 0), ("목적", 1), ("정의", 1), ("계약", 0), ("계약의 성립", 1), ("계약의 해지", 1)])

        result = convert(_docx_with_body(body, numbering=numbering), "a.docx")

        assert result.text == ("제1장 총칙\n\n  - 제1조 목적\n  - 제2조 정의\n\n제2장 계약\n\n"
                               "  - 제3조 계약의 성립\n  - 제4조 계약의 해지")

    def test_level_restarts_only_after_the_level_it_names(self):
        """lvlRestart N restarts the count only after level N (1-based) or a shallower level has appeared — articles (조) continue
        when the section (절) changes, and go back to 1 when the chapter (장) changes."""
        numbering = ('<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="multilevel"/>'
                     + _level(0, "제%1장") + _level(1, "제%2절") + _level(2, "제%3조", restart="1")
                     + "</w:abstractNum>" + _number_list(1))
        body = "".join(_paragraph(text, 1, level) for text, level in [
            ("총칙", 0), ("통칙", 1), ("목적", 2), ("정의", 2), ("용어", 1), ("해석", 2), ("계약", 0), ("성립", 1), ("청약", 2)])

        result = convert(_docx_with_body(body, numbering=numbering), "a.docx")

        assert result.text == ("제1장 총칙\n\n  - 제1절 통칙\n    - 제1조 목적\n    - 제2조 정의\n  - 제2절 용어\n    - 제3조 해석\n\n"
                               "제2장 계약\n\n  - 제1절 성립\n    - 제1조 청약")


class TestDocxPlaceholderNumberedParagraph:
    def test_numbered_paragraph_of_an_empty_content_control_still_counts(self):
        """A numbered paragraph wrapped by an empty content control (showingPlcHdr) was discarded before its number was counted, so
        later numbers moved up ('3. 신청 사유' became '2.'). The prompt text is still left out and only the number is counted (the
        same as a numbered paragraph with empty text)."""
        placeholder = ("<w:sdt><w:sdtPr><w:showingPlcHdr/></w:sdtPr><w:sdtContent>" + _paragraph("여기를 클릭하여 입력", 1)
                       + "</w:sdtContent></w:sdt>")
        numbering = ('<w:abstractNum w:abstractNumId="0">' + _level(0, "%1.") + "</w:abstractNum>" + _number_list(1))
        body = _paragraph("신청인", 1) + placeholder + _paragraph("신청 사유", 1) + _paragraph("첨부 서류", 1)

        result = convert(_docx_with_body(body, numbering=numbering), "a.docx")

        assert result.text == "1. 신청인\n3. 신청 사유\n4. 첨부 서류"


class TestDocxNumberReferenceWithoutADefinition:
    """When there is no numbering definition (numbering.xml), no w:num matching the paragraph's numId, or no such level in the
    numbering definition, Word draws no number, but a bullet `- ` was added and an ordinary paragraph read as a list item."""

    _NUMBERING = '<w:abstractNum w:abstractNumId="0">' + _level(0, "%1.") + "</w:abstractNum>" + _number_list(1)

    def test_paragraph_pointing_to_an_undefined_list_or_level_has_no_mark(self):
        body = (_paragraph("정의 없는 목록", 7) + _paragraph("첫 항목", 1) + _paragraph("정의 없는 수준", 1, level=3)
                + _paragraph("둘째 항목", 1))

        result = convert(_docx_with_body(body, numbering=self._NUMBERING), "a.docx")

        assert result.text == "정의 없는 목록\n\n1. 첫 항목\n\n정의 없는 수준\n\n2. 둘째 항목"

    def test_document_without_a_numbering_part_has_no_list_marks(self):
        unlinked = builder.zip_with(_docx_with_body(_paragraph("번호 문단", 1) + _paragraph("일반 문단")), edit={
            "word/_rels/document.xml.rels": lambda xml: re.sub(r'<Relationship [^>]*Target="numbering.xml"/>', "", xml)})
        data = _without_parts(unlinked, "word/numbering.xml")

        result = convert(data, "a.docx")

        assert result.text == "번호 문단\n\n일반 문단" and result.warnings == []


class TestDocxBoxesOutsideTheFinalVersion:
    """Text boxes, pasted shape text and legacy WordArt are found and read separately from paragraph text, so those inside a tracked
    deletion (w:del), the original place of moved text (w:moveFrom) or a hidden run (w:vanish) — all skipped by body text — came
    out as if they were content of the final version."""

    @pytest.mark.parametrize("inner", [
        pytest.param(f'<w:del w:id="1" w:author="a"><w:r>{_text_box("삭제된 상자: 가격 1,000원")}</w:r></w:del>', id="deleted-box"),
        pytest.param(f"<w:r><w:rPr><w:vanish/></w:rPr>{_text_box('숨긴 상자')}</w:r>", id="hidden-box"),
        pytest.param(f'<w:del w:id="1" w:author="a"><w:r>{_shape_text("삭제된 도형 글")}</w:r></w:del>', id="deleted-shape-text"),
        pytest.param(f'<w:del w:id="1" w:author="a"><w:r>{_word_art("삭제된 워드아트")}</w:r></w:del>', id="deleted-word-art"),
        pytest.param(f"<w:r><w:rPr><w:vanish/></w:rPr>{_word_art('숨긴 워드아트')}</w:r>", id="hidden-word-art"),
    ])
    def test_box_that_is_not_in_the_final_version_is_left_out(self, inner):
        body = f"<w:p><w:r><w:t>본문</w:t></w:r>{inner}</w:p>"

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "본문"

    def test_moved_text_box_is_read_once_at_its_new_place(self):
        body = (f'<w:p><w:r><w:t>앞</w:t></w:r><w:moveFrom w:id="1" w:author="a"><w:r>{_text_box("옮긴 상자")}</w:r></w:moveFrom></w:p>'
                f'<w:p><w:r><w:t>뒤</w:t></w:r><w:moveTo w:id="2" w:author="a"><w:r>{_text_box("옮긴 상자")}</w:r></w:moveTo></w:p>')

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "앞\n\n뒤\n\n옮긴 상자"

    def test_text_box_of_a_visible_run_is_still_read(self):
        body = f'<w:p><w:r><w:t>본문</w:t></w:r><w:r><w:rPr><w:vanish w:val="0"/></w:rPr>{_text_box("보이는 상자")}</w:r></w:p>'

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "본문\n\n보이는 상자"


def _row(*cells: str, deleted: bool = False) -> str:
    """A table row whose cells are (text, tcPr inside) pairs written as 'text' or 'text|<tcPr children>'."""
    properties = '<w:trPr><w:del w:id="9" w:author="a" w:date="2024-01-01T00:00:00Z"/></w:trPr>' if deleted else ""
    written = []
    for cell in cells:
        text, _, cell_properties = cell.partition("|")
        written.append(f"<w:tc>{f'<w:tcPr>{cell_properties}</w:tcPr>' if cell_properties else ''}"
                       f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>")
    return f"<w:tr>{properties}{''.join(written)}</w:tr>"


class TestDocxDeletedTableRow:
    """A table row deleted with Track Changes on (w:trPr/w:del) is not in the final version, as no other tracked deletion is: it
    came out as a row of the table, so a struck-out clause or price read as if it were still agreed."""

    def test_deleted_row_is_left_out_of_the_table(self):
        body = "<w:tbl>" + _row("품목", "단가") + _row("철근", "1,000원", deleted=True) + _row("시멘트", "500원") + "</w:tbl>"

        result = convert(_docx_with_body(body), "a.docx")

        assert "철근" not in result.text and "1,000원" not in result.text
        assert result.text == "| 품목 | 단가 |\n| --- | --- |\n| 시멘트 | 500원 |"

    def test_vertical_merge_across_a_deleted_row_stays_tied(self):
        body = ("<w:tbl>" + _row("구분", "값") + _row('본사|<w:vMerge w:val="restart"/>', "1")
                + _row("|<w:vMerge/>", "2", deleted=True) + _row("|<w:vMerge/>", "3") + "</w:tbl>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "| 구분 | 값 |\n| --- | --- |\n| 본사 | 1 |\n| 본사 | 3 |"

    def test_merge_starting_in_a_deleted_row_keeps_its_text(self):
        """Its continuation cells were tied to the unrelated cell above the deleted row and wrote that cell's text."""
        body = ("<w:tbl>" + _row("H1", "H2") + _row("Seoul", "10") + _row('Busan|<w:vMerge w:val="restart"/>', "20", deleted=True)
                + _row("|<w:vMerge/>", "30") + _row("|<w:vMerge/>", "40") + "</w:tbl>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "| H1 | H2 |\n| --- | --- |\n| Seoul | 10 |\n| Busan | 30 |\n| Busan | 40 |"

    def test_merge_starting_in_a_deleted_first_row_keeps_its_text(self):
        body = ("<w:tbl>" + _row('Busan|<w:vMerge w:val="restart"/>', "20", deleted=True) + _row("|<w:vMerge/>", "30")
                + _row("Seoul", "10") + "</w:tbl>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "| Busan | 30 |\n| --- | --- |\n| Seoul | 10 |"

    def test_merge_whose_first_rows_are_all_deleted_keeps_its_text(self):
        body = ("<w:tbl>" + _row("H1", "H2") + _row("Seoul", "10") + _row('Busan|<w:vMerge w:val="restart"/>', "20", deleted=True)
                + _row("|<w:vMerge/>", "25", deleted=True) + _row("|<w:vMerge/>", "30") + _row("Daegu", "50") + "</w:tbl>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "| H1 | H2 |\n| --- | --- |\n| Seoul | 10 |\n| Busan | 30 |\n| Daegu | 50 |"

    def test_merge_ending_in_a_deleted_row_does_not_reach_the_row_after(self):
        body = ("<w:tbl>" + _row("H1", "H2") + _row('Seoul|<w:vMerge w:val="restart"/>', "10")
                + _row("|<w:vMerge/>", "20", deleted=True) + _row("Busan", "30") + "</w:tbl>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "| H1 | H2 |\n| --- | --- |\n| Seoul | 10 |\n| Busan | 30 |"

    def test_merge_starting_in_a_deleted_row_of_a_nested_table_keeps_its_text(self):
        inner = ("<w:tbl>" + _row("Seoul", "10") + _row('Busan|<w:vMerge w:val="restart"/>', "20", deleted=True)
                 + _row("|<w:vMerge/>", "30") + "</w:tbl>")
        body = f"<w:tbl>{_row('A', 'B')}<w:tr><w:tc>{inner}<w:p/></w:tc><w:tc><w:p><w:r><w:t>o</w:t></w:r></w:p></w:tc></w:tr></w:tbl>"

        result = convert(_docx_with_body(body), "a.docx")

        assert "<tr><th>Seoul</th><th>10</th></tr><tr><td>Busan</td><td>30</td></tr>" in result.text

    def test_table_whose_rows_are_all_deleted_leaves_nothing(self):
        body = "<w:p><w:r><w:t>본문</w:t></w:r></w:p><w:tbl>" + _row("가", "나", deleted=True) + "</w:tbl>"

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "본문"

    def test_deleted_row_of_a_table_in_a_footnote_is_left_out(self):
        from tests.test_office_symbols_fields_notes import _docx_with_footnote

        note = "<w:tbl>" + _row("구분", "값") + _row("삭제", "9", deleted=True) + _row("유지", "1") + "</w:tbl>"

        result = convert(_docx_with_footnote(note), "a.docx")

        assert "삭제" not in result.text
        assert "유지" in result.text

    def test_text_of_a_deleted_row_is_not_quoted_as_a_commented_range(self):
        from tests.test_office_comments import _comment, _docx_with_comments

        body = ('<w:tbl><w:tr><w:tc><w:p><w:commentRangeStart w:id="1"/><w:r><w:t>남은 칸</w:t></w:r></w:p></w:tc></w:tr>'
                '<w:tr><w:trPr><w:del w:id="9" w:author="a"/></w:trPr><w:tc><w:p><w:r><w:t>지운 칸</w:t></w:r>'
                '<w:commentRangeEnd w:id="1"/><w:r><w:commentReference w:id="1"/></w:r></w:p></w:tc></w:tr></w:tbl>')

        result = convert(_docx_with_comments(body, _comment("1", "검토자", "확인")), "a.docx")

        assert "지운 칸" not in result.text
        assert '> Comment(검토자, on "남은 칸"): 확인' in result.text


class TestDocxDeletedParagraphMark:
    """Two paragraphs joined with Track Changes on keep both paragraphs, the mark of the first one deleted (`w:pPr/w:rPr/w:del`).
    Its text is one paragraph with the next one in the final version, and it was written as a paragraph of its own, the sentence
    cut in two. The two are joined as written, and the joined one takes the properties of the mark that is left."""

    _DELETED = '<w:pPr><w:rPr><w:del w:id="1" w:author="a" w:date="2026-01-01T00:00:00Z"/></w:rPr></w:pPr>'

    def test_paragraph_whose_mark_was_deleted_is_joined_to_the_next(self):
        body = (f'<w:p>{self._DELETED}<w:r><w:t>The contract amount is</w:t></w:r></w:p>'
                '<w:p><w:r><w:t xml:space="preserve"> 1,200,000 won including tax.</w:t></w:r></w:p>'
                f'<w:p>{self._DELETED}<w:r><w:t xml:space="preserve">Pay </w:t></w:r></w:p>'
                f'<w:p>{self._DELETED}<w:r><w:t xml:space="preserve">within </w:t></w:r></w:p>'
                "<w:p><w:r><w:t>30 days.</w:t></w:r></w:p>")

        assert convert(_docx_with_body(body), "a.docx").text == "The contract amount is 1,200,000 won including tax.\n\nPay within 30 days."

    def test_range_marker_between_does_not_keep_them_apart(self):
        """A bookmark or comment range written between the two paragraphs holds no content: the sentence stayed cut in two."""
        body = (f'<w:p>{self._DELETED}<w:r><w:t>The amount is</w:t></w:r></w:p>'
                '<w:bookmarkStart w:id="0" w:name="amount"/><w:commentRangeStart w:id="1"/>'
                '<w:p><w:r><w:t xml:space="preserve"> 1,200 won.</w:t></w:r></w:p><w:bookmarkEnd w:id="0"/>'
                "<w:p><w:r><w:t>Next.</w:t></w:r></w:p>")

        assert convert(_docx_with_body(body), "a.docx").text == "The amount is 1,200 won.\n\nNext."

    def test_numbered_paragraph_whose_mark_was_deleted_takes_no_number(self):
        numbering = ('<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/>'
                     '<w:lvlText w:val="%1."/></w:lvl></w:abstractNum>' + _number_list(1))
        first = _paragraph("first and", 1).replace("</w:pPr>", '<w:rPr><w:del w:id="1" w:author="a"/></w:rPr></w:pPr>', 1)
        body = first + _paragraph(" second", 1) + _paragraph("third", 1)

        assert convert(_docx_with_body(body, numbering=numbering), "a.docx").text == "1. first and second\n2. third"

    def test_text_box_goes_after_the_joined_paragraph(self):
        """A text box anchored in the paragraph whose mark was deleted came before the joined paragraph."""
        body = (f'<w:p>{self._DELETED}<w:r><w:t xml:space="preserve">The amount </w:t></w:r><w:r>{_text_box("Box")}</w:r></w:p>'
                "<w:p><w:r><w:t>is due.</w:t></w:r></w:p>")

        assert convert(_docx_with_body(body), "a.docx").text == "The amount is due.\n\nBox"


def _deleted(text: str) -> str:
    """A paragraph whose mark was deleted by tracked changes."""
    return _paragraph(text).replace("</w:pPr>", '<w:rPr><w:del w:id="1" w:author="a"/></w:rPr></w:pPr>', 1)


class TestDocxDeletedParagraphMarkChain:
    """A chain of paragraphs whose mark was deleted was joined again at every link and counted toward `max_chars` only at its end:
    40,000 of them (160 KB) took 104 s, and the limit did not stop them."""

    @staticmethod
    def _chain(count: int, deleted: bool = True) -> bytes:
        mark = TestDocxDeletedParagraphMark._DELETED if deleted else ""
        body = "".join(f'<w:p>{mark}<w:r><w:t xml:space="preserve">word{index} some text here </w:t></w:r></w:p>'
                       for index in range(count))
        return _docx_with_body(body + "<w:p><w:r><w:t>end</w:t></w:r></w:p>")

    def test_chain_is_one_paragraph(self):
        text = convert(self._chain(300), "a.docx").text

        assert text == " ".join(f"word{index} some text here" for index in range(300)) + " end"

    def test_chain_past_the_limit_stops_reading(self, monkeypatch):
        """Read on past the limit to the end of the chain, it stops `_FAR_PAST_THE_LIMIT` further (made small here)."""
        from opendockit.extractors import docx

        read = []
        original = docx._paragraph_text
        monkeypatch.setattr(docx, "_paragraph_text", lambda paragraph, *rest: read.append(1) or original(paragraph, *rest))
        monkeypatch.setattr(docx, "_FAR_PAST_THE_LIMIT", 1_000)
        full = convert(self._chain(3000), "a.docx").text
        read.clear()

        result = convert(self._chain(3000), "a.docx", ConvertOptions(max_chars=2_000))

        assert result.exceeded
        assert len(read) < 200
        assert full.startswith(result.text)

    def test_text_boxes_of_a_chain_stop_it_past_the_limit(self, monkeypatch):
        """The text boxes of a chain were not counted at all, so a chain past the limit was read to its end with every box built
        and held: 30,000 links of `w ` with a text box of 4,000 characters each read 120 million characters (7.4 s, 942 MB). The
        boxes now stop the chain `_FAR_PAST_THE_LIMIT` past the limit (made small here) once its text is too long for a heading;
        what is written is still the front of the full text, and the limit is reported."""
        from opendockit.extractors import docx

        mark = TestDocxDeletedParagraphMark._DELETED
        link = f'<w:p>{mark}<w:r><w:t xml:space="preserve">w </w:t></w:r><w:r>{_text_box("y" * 2000)}</w:r></w:p>'
        data = _docx_with_body(_paragraph("Intro") + link * 400 + _paragraph("end"))
        full = convert(data, "a.docx").text
        monkeypatch.setattr(docx, "_FAR_PAST_THE_LIMIT", 10_000)
        built = []
        render = docx._DocxRenderer.render_container
        monkeypatch.setattr(docx._DocxRenderer, "render_container", lambda self, box: built.append(1) or render(self, box))

        result = convert(data, "a.docx", ConvertOptions(max_chars=1_000))

        assert result.exceeded and result.warnings == ["The conversion was stopped because the text exceeded 1,000 characters."]
        assert len(built) < 20
        assert full.startswith(result.text) and result.text.startswith("Intro\n\nw w w")

    def test_cut_never_skips_the_links_between(self):
        """Stopped as soon as the text and the text box of the first link passed the limit, the paragraph ending the chain was
        joined straight onto the first: "Do sign the contract" for "Do not sign the contract."; and a joined text short of the
        heading length became a heading the whole one is not."""
        mark = TestDocxDeletedParagraphMark._DELETED
        first = f'<w:p>{mark}<w:r><w:t xml:space="preserve">Do </w:t></w:r><w:r>{_text_box("Figure 1: signature box")}</w:r></w:p>'
        contract = _docx_with_body(first + _deleted("not ") + _paragraph("sign the contract."))
        long_box = (f'<w:p>{mark}<w:r><w:t xml:space="preserve">{"x" * 60} </w:t></w:r>'
                    f'<w:r>{_text_box("box" * 10)}</w:r></w:p>')
        heading = _docx_with_body(long_box + _deleted("y" * 80 + " ") + _paragraph("end", style="Heading1"))

        assert convert(contract, "a.docx").text == "Do not sign the contract.\n\nFigure 1: signature box"
        assert convert(contract, "a.docx", ConvertOptions(max_chars=20)).text == "Do not sign the cont"
        assert convert(heading, "a.docx", ConvertOptions(max_chars=70)).text == "x" * 60 + " " + "y" * 9

    @pytest.mark.parametrize("far", [None, 130])
    def test_cut_text_is_the_front_of_the_full_text_at_every_limit(self, monkeypatch, far):
        """Random chains of paragraphs whose mark was deleted, with headings, numbers, text boxes and empty links, cut at many
        limits: the cut text is always the front of the full one. With `_FAR_PAST_THE_LIMIT` made small the body stops
        inside the chains too (`finish_joined`); bold is left out there, whole-paragraph emphasis being the gap it records."""
        import random

        from opendockit.extractors import docx

        if far is not None:
            monkeypatch.setattr(docx, "_FAR_PAST_THE_LIMIT", far)
        numbering = ('<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/>'
                     '<w:lvlText w:val="%1."/></w:lvl></w:abstractNum>' + _number_list(1))
        words = ["alpha ", "beta", " gamma ", "한글 ", "가", "x" * 40 + " ", "​", "", " ", "#", "1."]
        rng = random.Random(7)
        for _ in range(20):
            body = ""
            for index in range(rng.randint(2, 14)):
                text = "".join(rng.choice(words) for _ in range(rng.randint(0, 4)))
                paragraph = _paragraph(text, 1 if rng.random() < 0.3 else None, style=rng.choice([None, None, "Heading1"]))
                if rng.random() < 0.15:
                    paragraph = paragraph.replace("</w:p>", f"<w:r>{_text_box(f'box{index}')}</w:r></w:p>")
                if far is None and rng.random() < 0.15:
                    paragraph = paragraph.replace("<w:r>", "<w:r><w:rPr><w:b/></w:rPr>", 1)
                if rng.random() < 0.6:
                    paragraph = paragraph.replace("</w:pPr>", '<w:rPr><w:del w:id="1" w:author="a"/></w:rPr></w:pPr>', 1)
                body += paragraph
            data = _docx_with_body(body, numbering=numbering)
            full = convert(data, "a.docx")
            for limit in sorted({*range(1, 40, 4), *range(40, full.char_count + 2, max(full.char_count // 6, 1))}):
                cut = convert(data, "a.docx", ConvertOptions(max_chars=limit)).text
                assert full.text.startswith(cut.rstrip()), (body, limit, cut)

    @pytest.mark.timing
    def test_chain_takes_the_time_of_as_many_paragraphs(self):
        """10,000 links took 7 s against 0.4 s for 10,000 paragraphs."""
        chain, plain = self._chain(10_000), self._chain(10_000, deleted=False)

        assert fastest(lambda: convert(chain, "a.docx"), runs=2) < fastest(lambda: convert(plain, "a.docx"), runs=2) * 2 + 0.3


_TRANSITIONAL_NAME = re.compile(r"http://schemas\.openxmlformats\.org/(wordprocessingml|drawingml|presentationml|spreadsheetml|"
                                r"officeDocument)/2006/([A-Za-z-]+)(/[A-Za-z-]+)?")
_STRICT_SPELLING = {"extended-properties": "extendedProperties", "custom-properties": "customProperties"}


def _strict_everywhere(data: bytes, edit: dict[str, tuple[str, str]] | None = None) -> bytes:
    """Copy as Word saves a 'Strict Open XML Document': every Transitional namespace and relationship type of the five
    vocabularies in its purl.oclc.org form, the two property parts spelled as Strict spells them; `edit` replaces text in a part
    after that (part name → (old, new)) — the measures Word writes with a unit."""
    def strict(match: re.Match[str]) -> str:
        vocabulary, name, tail = match.group(1), match.group(2), match.group(3) or ""
        if tail:
            tail = "/" + _STRICT_SPELLING.get(tail[1:], tail[1:])
        return f"http://purl.oclc.org/ooxml/{vocabulary}/{_STRICT_SPELLING.get(name, name)}{tail}"

    buffer = BytesIO()
    with zipfile.ZipFile(BytesIO(data)) as source, zipfile.ZipFile(buffer, "w") as target:
        for name in source.namelist():
            content = source.read(name)
            if name.endswith((".xml", ".rels")):
                text = _TRANSITIONAL_NAME.sub(strict, content.decode("utf-8"))
                for part, (old, new) in (edit or {}).items():
                    if part == name:
                        text = text.replace(old, new)
                content = text.encode("utf-8")
            target.writestr(name, content)
    return buffer.getvalue()


class TestStrictOpenXmlOfficeDocuments:
    """A DOCX/PPTX saved as a 'Strict Open XML document' differs in every relationship type and namespace, so the main part was
    not found and it was rejected as 'document structure could not be read' (corrupted). A DOCX is read from a copy that uses the
    Transitional names (`_package.transitional_package`) — a document Word saved that way (Word 2016 and later) reads in full
    then, so refusing it lost the whole document over its names. A PPTX is still refused, with the advice to save it again."""

    def test_strict_pptx_is_reported_as_unsupported_with_a_resave_advice(self):
        strict = _strict(builder.pptx_bytes())

        with pytest.raises(UnsupportedFormatError) as raised:
            convert(strict, "a.pptx")

        assert "Strict Open XML" in str(raised.value)
        assert "PowerPoint Presentation (.pptx)" in str(raised.value)

    def test_strict_docx_reads_as_its_transitional_form(self):
        data = builder.docx_bytes()
        strict = _strict_everywhere(data)
        assert b"purl.oclc.org/ooxml/officeDocument/relationships/extendedProperties" in zipfile.ZipFile(BytesIO(strict)).read(
            "_rels/.rels")

        result, expected = convert(strict, "a.docx"), convert(data, "a.docx")

        assert result.text == expected.text
        assert "# 1장 개요" in result.text and "$\\frac{a}{b}x^2$" in result.text
        assert result.metadata == expected.metadata
        assert result.warnings == expected.warnings == []

    def test_pictures_of_a_strict_docx_are_read(self):
        data = builder.docx_with_images()

        result, expected = convert(_strict_everywhere(data), "a.docx"), convert(data, "a.docx")

        assert result.text == expected.text
        assert len(result.images) == len(expected.images) > 0

    def test_measures_written_with_a_unit_are_read(self):
        """Word writes twips as points in Strict (`w:ind w:left="40pt"` for 800): the indent that nests a list under the item it is
        indented from was read as none. Font sizes and raises written with a unit are read too (half-points)."""
        from tests.test_office import _indented_level

        numbering = ('<w:abstractNum w:abstractNumId="0">' + _indented_level(0, "%1.", 800)
                     + '</w:abstractNum><w:abstractNum w:abstractNumId="1">' + _indented_level(0, "-", 1600, "bullet")
                     + "</w:abstractNum>" + _number_list(1, 0) + _number_list(2, 1))
        styles = '<w:style w:type="paragraph" w:styleId="BodyText"><w:name w:val="Body Text"/><w:rPr><w:sz w:val="24"/></w:rPr></w:style>'
        body = (_paragraph("개요", 1, 0) + _paragraph("세부 항목", 2, 0) + _paragraph("결론", 1, 0)
                + '<w:p><w:pPr><w:pStyle w:val="BodyText"/></w:pPr><w:r><w:t xml:space="preserve">H</w:t></w:r>'
                '<w:r><w:rPr><w:position w:val="-2"/><w:sz w:val="19"/></w:rPr><w:t>2</w:t></w:r><w:r><w:t>O</w:t></w:r></w:p>')
        data = _docx_with_body(body, numbering=numbering, styles=styles)
        in_points = {"word/numbering.xml": ('w:left="800"', 'w:left="40pt"'), "word/document.xml": ('w:val="19"', 'w:val="9.5pt"')}
        strict = _strict_everywhere(data, edit=in_points)
        strict = _strict_everywhere(strict, edit={"word/numbering.xml": ('w:left="1600"', 'w:left="80pt"'),
                                                 "word/styles.xml": ('w:sz w:val="24"', 'w:sz w:val="12pt"'),
                                                 "word/document.xml": ('w:val="-2"', 'w:val="-1pt"')})

        result, expected = convert(strict, "a.docx"), convert(data, "a.docx")

        assert expected.text == "1. 개요\n  - 세부 항목\n2. 결론\n\nH_2O"
        assert result.text == expected.text

    def test_strict_part_stored_in_utf16_is_renamed_too(self):
        data = builder.docx_bytes()
        buffer = BytesIO()
        with zipfile.ZipFile(BytesIO(_strict_everywhere(data))) as source, zipfile.ZipFile(buffer, "w") as target:
            for name in source.namelist():
                content = source.read(name)
                if name == "word/document.xml":
                    declared = re.sub(r"encoding=['\"][^'\"]*['\"]", 'encoding="UTF-16"', content.decode("utf-8"), count=1)
                    assert 'encoding="UTF-16"' in declared
                    content = declared.encode("utf-16")
                target.writestr(name, content)

        assert convert(buffer.getvalue(), "a.docx").text == convert(data, "a.docx").text

    def test_strict_names_in_text_and_link_targets_stay_as_written(self):
        """Only the names are renamed: a paragraph that quotes a Strict namespace (even as a declaration), a link to one, a
        property and a comment kept their Strict spelling in the Transitional copy."""
        namespace, relationship = ("http://purl.oclc.org/ooxml/wordprocessingml/main",
                                   "http://purl.oclc.org/ooxml/officeDocument/relationships/extendedProperties")
        body = (f'<w:p><w:r><w:t xml:space="preserve">Strict namespace is {namespace} and xmlns:w="{namespace}", '
                f'Type="{relationship}"</w:t></w:r></w:p><!-- <w:p xmlns:x="{namespace}"> -->'
                '<w:p><w:hyperlink r:id="rIdX"><w:r><w:t>schema link</w:t></w:r></w:hyperlink></w:p>')
        link = ('<Relationship Id="rIdX" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" '
                'Target="http://purl.oclc.org/ooxml/drawingml/main" TargetMode="External"/></Relationships>')
        data = builder.zip_with(_docx_with_body(body), edit={
            "word/_rels/document.xml.rels": lambda xml: xml.replace("</Relationships>", link),
            "docProps/core.xml": lambda xml: xml.replace("<dc:title/>", f"<dc:title>{namespace}</dc:title>")})
        strict = _strict_everywhere(data)
        with zipfile.ZipFile(BytesIO(strict)) as archive:
            assert archive.read("word/document.xml").count(namespace.encode()) > 3

        result, expected = convert(strict, "a.docx"), convert(data, "a.docx")

        assert f"Strict namespace is {namespace}" in expected.text and "(http://purl.oclc.org/ooxml/drawingml/main)" in expected.text
        assert result.text == expected.text
        assert result.metadata == expected.metadata
        with zipfile.ZipFile(BytesIO(transitional_package(strict))) as archive:
            document = archive.read("word/document.xml")
        assert f'<!-- <w:p xmlns:x="{namespace}"> -->'.encode() in document
        assert b'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"' in document

    @pytest.mark.parametrize("relationships", [False, True])
    def test_only_declarations_and_relationship_types_are_renamed(self, relationships):
        from opendockit.extractors._package import _transitional_names

        strict, transitional = ("http://purl.oclc.org/ooxml/drawingml/main", "http://schemas.openxmlformats.org/drawingml/2006/main")
        kept = (f'<?pi {strict}?><!-- <a xmlns="{strict}"> --><b c="{strict}" d=\'xmlns="{strict}"\' Target="{strict}">'
                f'<![CDATA[<a xmlns="{strict}">]]>xmlns="{strict}" Type="{strict}"</b>')
        unknown = 'xmlns:a="http://purl.oclc.org/ooxml/other/main"'  # not one of the five vocabularies
        part = f"<r xmlns='{strict}' {unknown} Type=\"{strict}\">{kept}</r>"

        renamed = _transitional_names(part.encode(), relationships=relationships)

        type_value = transitional if relationships else strict
        assert renamed == f"<r xmlns='{transitional}' {unknown} Type=\"{type_value}\">{kept}</r>".encode()
        assert _transitional_names(part.encode("utf-16"), relationships=relationships).decode("utf-16") == renamed.decode()

    def test_picture_of_a_strict_docx_that_cannot_be_read_is_detached_and_the_body_kept(self):
        """A member that does not read is left out of the copy, and opening detaches it as for a Transitional document."""
        from tests.test_office_robustness import _rebuilt

        data = builder.docx_with_images()
        transitional = convert(_rebuilt(data, corrupt=("word/media/image1.png",)), "a.docx")

        result = convert(_rebuilt(_strict_everywhere(data), corrupt=("word/media/image1.png",)), "a.docx")

        assert result.text == transitional.text
        assert result.warnings == transitional.warnings
        assert any("detaching 1 missing or broken part(s) (image1.png)" in warning for warning in result.warnings)

    def test_strict_docx_whose_copy_holds_no_document_gets_the_resave_advice(self):
        strict = builder.zip_with(_strict_everywhere(builder.docx_bytes()),
                                  edit={"word/document.xml": lambda xml: re.sub(r"<w:document\b", "<w:other", xml, count=1)
                                        .replace("</w:document>", "</w:other>")})

        with pytest.raises(UnsupportedFormatError) as raised:
            convert(strict, "a.docx")

        assert "Strict Open XML" in str(raised.value)
        assert "Word Document (.docx)" in str(raised.value)

    def test_transitional_document_is_not_copied(self, monkeypatch):
        from opendockit.extractors import docx

        def never(data: bytes) -> bytes:
            raise AssertionError("a healthy document was copied")

        monkeypatch.setattr(docx, "transitional_package", never)

        assert "# 1장 개요" in convert(builder.docx_bytes(), "a.docx").text


_GLOSSARY_RELATIONSHIP = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/glossaryDocument"


def _docx_with_glossary(body: str, parts: dict[str, str]) -> bytes:
    """Document whose glossary document (word/glossary/document.xml) holds prompt-text docParts (name → text)."""
    glossary = ('<?xml version="1.0" encoding="UTF-8"?><w:glossaryDocument '
                'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:docParts>'
                + "".join(f'<w:docPart><w:docPartPr><w:name w:val="{name}"/><w:types><w:type w:val="bbPlcHdr"/></w:types>'
                          f'</w:docPartPr><w:docPartBody><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:docPartBody></w:docPart>'
                          for name, text in parts.items())
                + "</w:docParts></w:glossaryDocument>")
    return builder.zip_with(_docx_with_body(body), edit={
        "word/_rels/document.xml.rels": lambda xml: xml.replace(
            "</Relationships>", f'<Relationship Id="rIdGlossary" Type="{_GLOSSARY_RELATIONSHIP}" '
                                'Target="glossary/document.xml"/></Relationships>'),
        "[Content_Types].xml": lambda xml: xml.replace(
            "</Types>", '<Override PartName="/word/glossary/document.xml" ContentType="application/vnd.openxmlformats-'
                        'officedocument.wordprocessingml.document.glossary+xml"/></Types>')},
        add={"word/glossary/document.xml": glossary})


def _placeholder_control(content: str, part: str | None = "Guide") -> str:
    reference = f'<w:placeholder><w:docPart w:val="{part}"/></w:placeholder>' if part else ""
    return f"<w:sdt><w:sdtPr>{reference}<w:showingPlcHdr/></w:sdtPr><w:sdtContent>{content}</w:sdtContent></w:sdt>"


class TestDocxPlaceholderFlagOnRealInput:
    """A content control that still has showingPlcHdr but holds a real input value (a document whose text was filled in by a
    program) keeps its content. Whether it is prompt text is decided by whether it equals the docPart text of the glossary
    document. If it is equal, or there is no docPart to compare with, it is left out as before."""

    _RUN = '<w:r><w:t xml:space="preserve">{}</w:t></w:r>'

    def test_content_that_differs_from_the_placeholder_text_is_kept(self):
        run = self._RUN.format
        body = ("<w:p>" + run("신청인: ") + _placeholder_control(run("홍길동")) + "</w:p>"
                + _placeholder_control("<w:p>" + run("문단 전체가 입력값") + "</w:p>"))

        result = convert(_docx_with_glossary(body, {"Guide": "여기를 클릭하세요."}), "a.docx")

        assert result.text == "신청인: 홍길동\n\n문단 전체가 입력값"

    def test_content_equal_to_the_placeholder_text_is_left_out(self):
        run = self._RUN.format
        body = ("<w:p>" + run("신청인: ") + _placeholder_control(run("여기를 ") + run("클릭하세요.")) + "</w:p>"
                + _placeholder_control("<w:p>" + run("여기를 클릭하세요.") + "</w:p>") + "<w:p>" + run("끝") + "</w:p>")

        result = convert(_docx_with_glossary(body, {"Guide": "여기를 클릭하세요."}), "a.docx")

        assert result.text == "신청인:\n\n끝"

    def test_control_without_a_matching_glossary_entry_is_left_out_as_before(self):
        run = self._RUN.format
        body = ("<w:p>" + run("주소: ") + _placeholder_control(run("[Address]"), part=None) + "</w:p>"
                + "<w:p>" + run("직함: ") + _placeholder_control(run("[Job Title]"), part="Missing") + "</w:p>")

        result = convert(_docx_with_glossary(body, {"Guide": "여기를 클릭하세요."}), "a.docx")

        assert result.text == "주소:\n\n직함:"
