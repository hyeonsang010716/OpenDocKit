"""DOCX numbering restart (w:lvlRestart), numbered paragraphs wrapped by an empty content control, number references without a
definition; text boxes and shape text inside tracked changes and hidden runs; Strict Open XML DOCX/PPTX."""

from io import BytesIO
import re
import zipfile

import pytest

from opendockit import ConvertOptions, UnsupportedFormatError, convert
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
