"""DOCX numbering restart (w:lvlRestart), numbered paragraphs wrapped by an empty content control, number references without a
definition; text boxes and shape text inside tracked changes and hidden runs; Strict Open XML DOCX/PPTX."""

from io import BytesIO
import re
import zipfile

import pytest

from opendockit import UnsupportedFormatError, convert
from tests import builder
from tests.test_office import _docx_with_body, _number_list, _paragraph, _without_parts


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


class TestStrictOpenXmlOfficeDocuments:
    """A DOCX/PPTX saved as a 'Strict Open XML document' differs in every relationship type and namespace, so the main part was
    not found and it was rejected as 'document structure could not be read' (corrupted). It is not corruption but an unsupported
    format, so the message advises saving it again (same direction as XLSX Strict)."""

    @pytest.mark.parametrize(("data", "filename", "advice"), [
        pytest.param(builder.docx_bytes(), "a.docx", "Word Document (.docx)", id="docx"),
        pytest.param(builder.pptx_bytes(), "a.pptx", "PowerPoint Presentation (.pptx)", id="pptx"),
    ])
    def test_strict_document_is_reported_as_unsupported_with_a_resave_advice(self, data, filename, advice):
        strict = _strict(data)

        with pytest.raises(UnsupportedFormatError) as raised:
            convert(strict, filename)

        assert "Strict Open XML" in str(raised.value)
        assert advice in str(raised.value)


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
