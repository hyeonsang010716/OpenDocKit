"""Defects found by an audit of the DOCX, PPTX and ODF extractors with hand-built hostile documents: one odd structure that
rejected the whole document, values that came out wrong, and work that was repeated for every reference to the same part."""

from io import BytesIO
import re

from docx import Document
from docx.shared import Inches
import pytest

from opendockit import ConvertOptions, CorruptedDocumentError, convert
from tests import builder
from tests.test_office import _docx_with_body


_SUPERSCRIPT = '<w:rPr><w:vertAlign w:val="superscript"/></w:rPr>'
_ENCLOSED_21 = r"<w:instrText> eq \o\ac(○,21) </w:instrText>"


def _field_char(kind: str) -> str:
    return f'<w:fldChar w:fldCharType="{kind}"/>'


def _docx(body: str, options: ConvertOptions | None = None) -> str:
    return convert(_docx_with_body(body), "sample.docx", options).text


class TestDocxFieldStartedInsideASwappedRun:
    """A complex field remembers the index of the piece where its result starts. A superscript run and ruby swap their pieces for
    one piece afterwards, so the index of a field that began inside pointed past the end (IndexError — the whole document was
    rejected as corrupted) or at another piece (the wrong 'this field has no result' verdict)."""

    def test_field_begun_in_a_superscript_run_and_ended_in_the_next_run_is_read(self):
        text = _docx(f"<w:p><w:r>{_SUPERSCRIPT}<w:t>x</w:t><w:t>y</w:t>{_field_char('begin')}<w:instrText> REF a </w:instrText>"
                     f"{_field_char('separate')}</w:r><w:r>{_field_char('end')}</w:r><w:r><w:t> tail</w:t></w:r></w:p>")

        assert "xy" in text
        assert "tail" in text

    def test_field_begun_in_a_ruby_annotation_and_ended_after_it_is_read(self):
        text = _docx(f"<w:p><w:r><w:ruby><w:rt><w:r><w:t>a</w:t><w:t>b</w:t>{_field_char('begin')}{_field_char('separate')}</w:r>"
                     f"</w:rt><w:rubyBase><w:r><w:t>c</w:t></w:r></w:rubyBase></w:ruby></w:r><w:r>{_field_char('end')}</w:r>"
                     "<w:r><w:t> tail</w:t></w:r></w:p>")

        assert text == "c(ab) tail"

    def test_result_text_inside_the_superscript_run_still_counts_as_the_result(self):
        """The field has the result 'R', so its instruction (an EQ field that would draw ㉑) is not written."""
        text = _docx(f"<w:p><w:r>{_SUPERSCRIPT}<w:t>x</w:t>{_field_char('begin')}{_ENCLOSED_21}{_field_char('separate')}"
                     f"<w:t>R</w:t></w:r><w:r>{_field_char('end')}</w:r></w:p>")

        assert "㉑" not in text
        assert "xR" in text

    def test_text_before_the_field_in_the_same_superscript_run_is_not_its_result(self):
        """The field has no result of its own, so the EQ instruction is read (Word 'Enclose Characters')."""
        text = _docx(f"<w:p><w:r>{_SUPERSCRIPT}<w:t>x</w:t><w:t>y</w:t>{_field_char('begin')}{_ENCLOSED_21}"
                     f"{_field_char('separate')}</w:r><w:r>{_field_char('end')}</w:r></w:p>")

        assert "㉑" in text

    def test_text_left_out_of_a_field_that_never_ends_stays_after_the_superscript_run(self):
        text = _docx(f"<w:p><w:r>{_SUPERSCRIPT}<w:t>x</w:t><w:t>y</w:t>{_field_char('begin')}<w:t>h</w:t></w:r>"
                     "<w:r><w:t>z</w:t></w:r></w:p>")

        assert text.index("h") < text.index("z")


class TestDocxMainPartWithoutABody:
    """`w:body` is optional in the schema. Without it the body walk raised TypeError and the document was rejected as corrupted;
    it is an empty document, as a deck without a slide list is an empty deck. A main part whose root is not `w:document` is not
    a Word document: it gets the fixed structure error instead of an AttributeError from inside the library."""

    def test_document_without_a_body_is_an_empty_document(self):
        data = builder.zip_with(_docx_with_body("<w:p><w:r><w:t>gone</w:t></w:r></w:p>"), edit={
            "word/document.xml": lambda xml: re.sub(r"<w:body>.*</w:body>", "", xml, flags=re.DOTALL)})

        result = convert(data, "sample.docx", ConvertOptions(include_headers_footers=True))

        assert result.text == ""

    def test_root_that_is_not_a_document_gets_the_structure_error(self):
        data = builder.zip_with(_docx_with_body("<w:p><w:r><w:t>text</w:t></w:r></w:p>"), edit={
            "word/document.xml": lambda xml: xml.replace("<w:document ", "<w:documentX ").replace("</w:document>", "</w:documentX>")})

        with pytest.raises(CorruptedDocumentError) as error:
            convert(data, "sample.docx")

        assert str(error.value) == "The DOCX document structure could not be read."


class TestDocxStylesAndNumberingWithAGenericContentType:
    """A styles or numbering part declared as `application/xml` comes back from python-docx as a plain part without a parsed
    tree; asking it for its styles / element raised AttributeError and the document was rejected as corrupted. It is parsed from
    its bytes, as the footnotes and comments parts are."""

    _BODY = ('<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Chapter</w:t></w:r></w:p>'
             '<w:p><w:pPr><w:pStyle w:val="ListNumber"/></w:pPr><w:r><w:t>first</w:t></w:r></w:p>'
             '<w:p><w:pPr><w:pStyle w:val="ListNumber"/></w:pPr><w:r><w:t>second</w:t></w:r></w:p>')

    @pytest.mark.parametrize("kind", ["styles", "numbering"])
    def test_document_is_read_as_with_the_proper_content_type(self, kind):
        proper = _docx_with_body(self._BODY)
        declared = f"application/vnd.openxmlformats-officedocument.wordprocessingml.{kind}+xml"
        generic = builder.zip_with(proper, edit={"[Content_Types].xml": lambda xml: xml.replace(declared, "application/xml")})

        text = convert(generic, "sample.docx").text

        assert text == convert(proper, "sample.docx").text
        assert text == "# Chapter\n\n1. first\n2. second"

    @pytest.mark.parametrize("kind", ["styles", "numbering"])
    def test_part_that_is_not_xml_only_loses_its_own_information(self, kind):
        declared = f"application/vnd.openxmlformats-officedocument.wordprocessingml.{kind}+xml"
        data = builder.zip_with(_docx_with_body(self._BODY), edit={
            "[Content_Types].xml": lambda xml: xml.replace(declared, "application/octet-stream"),
            f"word/{kind}.xml": lambda xml: "not xml"})

        text = convert(data, "sample.docx").text

        assert [line for line in text.split("\n") if line] == {
            "styles": ["Chapter", "first", "second"], "numbering": ["# Chapter", "first", "second"]}[kind]


def _docx_saved(document) -> bytes:
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class TestDocxHeaderReferenceToAPartThatIsNotAHeader:
    """A header reference whose relationship points at another kind of part (a picture) raised AttributeError when asked for
    its content, and the document was rejected as corrupted. Only that header is skipped, as one whose part is missing is."""

    def test_only_that_header_is_skipped(self):
        document = Document()
        document.add_paragraph("body text")
        document.add_picture(BytesIO(builder.png_bytes()), width=Inches(1))
        document.sections[0].header.paragraphs[0].text = "header text"
        document.sections[0].footer.paragraphs[0].text = "footer text"
        data = builder.zip_with(_docx_saved(document), edit={
            "word/_rels/document.xml.rels": lambda xml: xml.replace('Target="header1.xml"', 'Target="media/image1.png"')})

        result = convert(data, "sample.docx", ConvertOptions(include_headers_footers=True, extract_images=False))

        assert result.text == "body text\n\nfooter text"
