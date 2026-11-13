"""Defects found by an audit of the DOCX, PPTX and ODF extractors with hand-built hostile documents: one odd structure that
rejected the whole document, values that came out wrong, and work that was repeated for every reference to the same part."""

import base64
from copy import deepcopy
from io import BytesIO
import re
import time

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Inches
import pytest

from opendockit import ConvertOptions, CorruptedDocumentError, convert
from opendockit.extractors import docx as docx_module
from opendockit.extractors import pptx as pptx_module
from opendockit.extractors.docx import extract_docx
from tests import builder
from tests.test_office import _CHART_NS, _chart_series, _chart_xml, _docx_inline, _docx_with_body, _docx_with_chunks
from tests.test_odf import _package as _odf_package


_SUPERSCRIPT = '<w:rPr><w:vertAlign w:val="superscript"/></w:rPr>'
_ENCLOSED_21 = r"<w:instrText> eq \o\ac(○,21) </w:instrText>"


_WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_RELATIONSHIPS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


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


class TestDocxHeaderSharedBySections:
    """A header part was rendered once for every section that references it and the copies were dropped afterwards by comparing
    their text. Rendering has side effects: a numbered paragraph counted on ('1. Company', '2. Company' … — four different
    texts, all kept) and a picture was registered once per section."""

    @staticmethod
    def _four_sections_sharing_the_header(document) -> bytes:
        for number in range(2, 5):
            document.add_section()
            document.add_paragraph(f"section {number}")
        sections = list(document.sections)
        reference = sections[0]._sectPr.find(qn("w:headerReference"))
        assert reference is not None
        for section in sections[1:]:
            section._sectPr.insert(0, deepcopy(reference))
        return _docx_saved(document)

    def test_numbered_paragraph_in_a_shared_header_is_written_once(self):
        document = Document()
        document.add_paragraph("section 1")
        document.sections[0].header.paragraphs[0].text = "Company"
        document.sections[0].header.paragraphs[0].style = document.styles["List Number"]
        data = self._four_sections_sharing_the_header(document)

        result = convert(data, "sample.docx", ConvertOptions(include_headers_footers=True))

        assert result.text == "1. Company\n\nsection 1\n\nsection 2\n\nsection 3\n\nsection 4"

    def test_picture_in_a_shared_header_is_registered_once(self):
        document = Document()
        document.add_paragraph("section 1")
        document.sections[0].header.paragraphs[0].add_run("Logo ").add_picture(BytesIO(builder.png_bytes()), width=Inches(1))
        data = self._four_sections_sharing_the_header(document)

        extraction = extract_docx(data, ConvertOptions(include_headers_footers=True))

        assert len(extraction.images) == 1


class TestDocxFootnoteNobodyReferences:
    """Every note of the footnotes part was rendered, referenced or not, and only the referenced ones were written. A picture in
    a note the body never references was registered as a picture of the document with no placeholder anywhere."""

    @staticmethod
    def _document_referencing(note_id: int) -> bytes:
        """Footnotes 2 (text) and 7 (text and a picture); the body references `note_id` only."""
        def note(number: int, extra: str = "") -> str:
            return f'<w:footnote w:id="{number}"><w:p><w:r><w:t>note {number}</w:t></w:r>{extra}</w:p></w:footnote>'

        picture = '<w:r><w:drawing><a:blip r:embed="rIdPicture"/></w:drawing></w:r>'
        footnotes = (f'<w:footnotes xmlns:w="{_WORD}" xmlns:r="{_RELATIONSHIPS}" '
                     f'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">{note(2)}{note(7, picture)}</w:footnotes>')
        body = f'<w:p><w:r><w:t>body</w:t></w:r><w:r><w:footnoteReference w:id="{note_id}"/></w:r></w:p>'
        return builder.zip_with(_docx_with_body(body), edit={
            "word/_rels/document.xml.rels": lambda xml: xml.replace(
                "</Relationships>", f'<Relationship Id="rIdNotes" Type="{_RELATIONSHIPS}/footnotes" Target="footnotes.xml"/>'
                                    "</Relationships>"),
            "[Content_Types].xml": lambda xml: xml.replace(
                "</Types>", '<Override PartName="/word/footnotes.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                            'wordprocessingml.footnotes+xml"/><Default Extension="png" ContentType="image/png"/></Types>'),
        }, add={
            "word/footnotes.xml": footnotes,
            "word/_rels/footnotes.xml.rels": (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                f'<Relationship Id="rIdPicture" Type="{_RELATIONSHIPS}/image" Target="media/note.png"/></Relationships>'),
            "word/media/note.png": builder.png_bytes(400, 300),
        })

    def test_picture_in_an_unreferenced_footnote_is_not_registered(self):
        extraction = extract_docx(self._document_referencing(2), ConvertOptions())

        assert extraction.sections[0].text == "body[^1]\n\n[^1]: note 2"
        assert extraction.images == []

    def test_picture_in_the_referenced_footnote_is_still_registered(self):
        extraction = extract_docx(self._document_referencing(7), ConvertOptions())

        assert extraction.sections[0].text.startswith("body[^1]\n\n[^1]: note 7")
        assert len(extraction.images) == 1


class TestDocxGridSpanBelowOne:
    """`w:gridSpan` and `w:gridBefore` were read as signed numbers and used as they were: a span of 0 put the next cell on the
    same column (its text was lost) and a negative one moved the next cell to the left of its row."""

    @staticmethod
    def _cell(text: str, properties: str = "") -> str:
        return f"<w:tc><w:tcPr>{properties}</w:tcPr><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>"

    @pytest.mark.parametrize("span", ["0", "-5"])
    def test_span_below_one_is_one_column(self, span):
        table = (f'<w:tbl><w:tr>{self._cell("a", f"""<w:gridSpan w:val="{span}"/>""")}{self._cell("b")}</w:tr>'
                 f'<w:tr>{self._cell("c")}{self._cell("d")}</w:tr></w:tbl>')

        assert _docx(table) == "| a | b |\n| --- | --- |\n| c | d |"

    def test_negative_grid_before_skips_no_column(self):
        table = (f'<w:tbl><w:tr><w:trPr><w:gridBefore w:val="-1"/></w:trPr>{self._cell("a")}{self._cell("b")}</w:tr>'
                 f'<w:tr>{self._cell("c")}{self._cell("d")}</w:tr></w:tbl>')

        assert _docx(table) == "| a | b |\n| --- | --- |\n| c | d |"

    def test_grid_before_still_skips_columns(self):
        table = (f'<w:tbl><w:tr>{self._cell("a")}{self._cell("b")}</w:tr>'
                 f'<w:tr><w:trPr><w:gridBefore w:val="1"/></w:trPr>{self._cell("d")}</w:tr></w:tbl>')

        assert _docx(table) == "| a | b |\n| --- | --- |\n| | d |"


class TestOdsPercentageBeyondTheLargestNumber:
    """A percentage cell without displayed text is written as its stored value times 100. The 'not a finite number' test looked
    at the stored value, so 1e308 passed it and came out as 'inf%'."""

    def test_stored_percentage_that_overflows_is_left_empty_like_an_infinite_value(self):
        def cell(kind: str, value: str) -> str:
            return f'<table:table-cell office:value-type="{kind}" office:value="{value}"/>'

        sheet = ('<table:table table:name="S"><table:table-row>'
                 f'{cell("float", "7")}{cell("percentage", "1e308")}{cell("percentage", "-1e308")}{cell("percentage", "0.253")}'
                 f'{cell("float", "1e308")}</table:table-row></table:table>')

        text = convert(_odf_package("spreadsheet", sheet), "sample.ods").text

        assert "inf" not in text
        assert "| 7 | 25.3% | 1e+308 |" in text   # the two empty columns are dropped by the sheet rendering


def _slide_shape(number: int, left: int, top: int, width: int, height: int, text: str) -> str:
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="{number + 100}" name="s"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr><a:xfrm>'
            f'<a:off x="{left}" y="{top}"/><a:ext cx="{width}" cy="{height}"/></a:xfrm></p:spPr>'
            f"<p:txBody><a:bodyPr/><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>")


def _deck_with_side_labels(count: int) -> bytes:
    """One tall box and `count` zero-width labels left of it, each lower than the one before and all vertically centred on the
    box: every label joins the box's row as a side label."""
    step, below = 100_000, 20_000_000   # below the shapes the slide already has
    height = 2 * step * count + 10_000_000
    shapes = _slide_shape(0, 5_000_000, below, 1_000_000, height, "big")
    shapes += "".join(_slide_shape(index, 1000, below + index * step, 0, height - 2 * index * step, f"label{index}")
                      for index in range(1, count + 1))
    return builder.zip_with(builder.pptx_bytes(), edit={
        "ppt/slides/slide1.xml": lambda xml: xml.replace("</p:spTree>", shapes + "</p:spTree>")})


class TestPptxRowOfManySideLabels:
    """Each shape that joined a row as a side label was compared with every shape already in the row, and the position of each
    of those was resolved again (eight look-ups per shape of the row): 2,000 labels beside one box (a 79 KB deck) took 36 s.
    The box of a row member is resolved once."""

    def test_position_of_a_row_member_is_resolved_once(self, monkeypatch: pytest.MonkeyPatch):
        calls = 0
        resolve = pptx_module._offset

        def counted(shape: object, axis: str) -> int:
            nonlocal calls
            calls += 1
            return resolve(shape, axis)

        monkeypatch.setattr(pptx_module, "_offset", counted)
        count = 200

        result = convert(_deck_with_side_labels(count), "sample.pptx", ConvertOptions(max_chars=None))

        blocks = result.sections[0].text.split("\n\n")
        assert blocks[-count - 1:] ==[f"label{index}" for index in range(1, count + 1)] + ["big"]
        assert calls < 20 * (count + 10)   # it was about 4 × count²

    @pytest.mark.timing
    def test_many_side_labels_do_not_take_many_seconds(self):
        data = _deck_with_side_labels(2000)

        started = time.perf_counter()
        convert(data, "sample.pptx", ConvertOptions(max_chars=None))

        assert time.perf_counter() - started < 10.0


def _count_chart_parses(monkeypatch: pytest.MonkeyPatch, module: object) -> list[int]:
    """Count how many times `module` parses a chart part. The count is the single item of the returned list."""
    parses = [0]
    parse = module.parse_part

    def counted(data: bytes) -> object:
        parses[0] += b"chartSpace" in data
        return parse(data)

    monkeypatch.setattr(module, "parse_part", counted)
    return parses


class TestChartPartReferencedByManyFrames:
    """A chart part was parsed and rendered again for every frame that points at it: 100 frames on one chart of 20,000 rows (a
    255 KB deck) took 25 s and 590 MB and wrote 36 million characters. As for ODF objects, the part is read once and its values
    are written at the first frame; the later frames give `[Chart] title`."""

    def test_pptx_chart_is_read_once_and_its_values_written_once(self, monkeypatch: pytest.MonkeyPatch):
        deck = builder.zip_with(builder.pptx_bytes(), edit={"ppt/charts/chart1.xml": lambda xml: xml.replace(
            "<c:plotArea>", "<c:title><c:tx><c:rich><a:bodyPr/><a:p><a:r><a:t>Sales</a:t></a:r></a:p></c:rich></c:tx></c:title>"
                            "<c:plotArea>", 1)})
        once = convert(deck, "sample.pptx").sections[2].text
        assert once.endswith("[Chart] Sales\n\n| Category | 매출 |\n| --- | --- |\n| 1분기 | 10.5 |\n| 2분기 | 20 |")

        def repeat_frame(xml: str) -> str:
            frame = re.search(r"<p:graphicFrame>(?:(?!</p:graphicFrame>).)*<c:chart (?:(?!</p:graphicFrame>).)*</p:graphicFrame>",
                              xml, flags=re.DOTALL).group(0)
            return xml.replace("</p:spTree>", frame * 30 + "</p:spTree>")

        parses = _count_chart_parses(monkeypatch, pptx_module)

        text = convert(builder.zip_with(deck, edit={"ppt/slides/slide3.xml": repeat_frame}), "sample.pptx").sections[2].text

        assert text == once + "\n\n[Chart] Sales" * 30
        assert parses == [1]

    @staticmethod
    def _docx_with_chart_frames(body: str) -> bytes:
        chart = _chart_xml(f"<c:barChart>{_chart_series(0, 'Sales', ['10', '20'], ['Q1', 'Q2'])}</c:barChart>", title="Quarters")
        relationship = f'<Relationship Id="rIdChart" Type="{_RELATIONSHIPS}/chart" Target="charts/chart1.xml"/></Relationships>'
        return builder.zip_with(_docx_with_body(body), edit={
            "word/_rels/document.xml.rels": lambda xml: xml.replace("</Relationships>", relationship)},
            add={"word/charts/chart1.xml": chart})

    _FRAME = _docx_inline(f'<c:chart xmlns:c="{_CHART_NS}" xmlns:r="{_RELATIONSHIPS}" r:id="rIdChart"/>', _CHART_NS)
    _VALUES = "| Category | Sales |\n| --- | --- |\n| Q1 | 10 |\n| Q2 | 20 |"

    def test_docx_chart_is_read_once_and_its_values_written_once(self, monkeypatch: pytest.MonkeyPatch):
        parses = _count_chart_parses(monkeypatch, docx_module)

        text = convert(self._docx_with_chart_frames(f"<w:p><w:r>{self._FRAME}</w:r></w:p>" * 31), "sample.docx").text

        assert text == f"[Chart] Quarters\n\n{self._VALUES}" + "\n\n[Chart] Quarters" * 30
        assert parses == [1]

    def test_docx_chart_in_two_table_cells_gives_its_values_to_the_first(self):
        def cell(content: str) -> str:
            return f"<w:tc><w:p><w:r>{content}</w:r></w:p></w:tc>"

        table = f"<w:tbl><w:tr>{cell('<w:t>a</w:t>')}{cell(self._FRAME)}</w:tr><w:tr>{cell('<w:t>b</w:t>')}{cell(self._FRAME)}</w:tr></w:tbl>"

        text = convert(self._docx_with_chart_frames(table), "sample.docx").text

        assert text.count("[Chart] Quarters") == 2
        assert text.count("Q2") == 1


class TestDocxChunkPartReferencedManyTimes:
    """The part of imported content (w:altChunk) was converted again for every reference to it, and a chunk that gives no text
    never moves the character count: 20 references to 2 MB of empty `<div>` (a 41 KB document) took 60 s. The part is converted
    once per document; what each reference writes is unchanged."""

    @staticmethod
    def _count_html_conversions(monkeypatch: pytest.MonkeyPatch) -> list[int]:
        conversions = [0]
        extract = docx_module.extract_html

        def counted(data: bytes, options: ConvertOptions) -> object:
            conversions[0] += 1
            return extract(data, options)

        monkeypatch.setattr(docx_module, "extract_html", counted)
        return conversions

    def test_chunk_referenced_many_times_is_converted_once(self, monkeypatch: pytest.MonkeyPatch):
        picture = base64.b64encode(builder.png_bytes()).decode()
        html = f'<html><body><p>imported text</p><p><img src="data:image/png;base64,{picture}" alt="figure"></p></body></html>'
        chunks = {"rIdChunk": ("chunk1.html", "text/html", html.encode())}
        conversions = self._count_html_conversions(monkeypatch)

        extraction = extract_docx(_docx_with_chunks('<w:altChunk r:id="rIdChunk"/>' * 20, chunks), ConvertOptions())

        assert conversions == [1]
        assert extraction.sections[0].text.count("imported text") == 20
        assert len(extraction.images) == 20   # every reference still registers the chunk's picture, with its own placeholder
        assert all(f":{number}⟧" in extraction.sections[0].text for number in range(1, 21))

    def test_chunk_without_text_referenced_many_times_is_converted_once(self, monkeypatch: pytest.MonkeyPatch):
        html = "<html><body>" + "<div><span></span></div>" * 50 + "</body></html>"
        conversions = self._count_html_conversions(monkeypatch)

        result = convert(_docx_with_chunks('<w:altChunk r:id="rIdChunk"/>' * 20 + "<w:p><w:r><w:t>tail</w:t></w:r></w:p>",
                                           {"rIdChunk": ("chunk1.html", "text/html", html.encode())}), "sample.docx")

        assert conversions == [1]
        assert result.text == "tail"


class TestOdfObjectThatIsNotAChart:
    """An embedded sub-document is parsed once, but one that is neither a chart nor a formula was searched for a chart again for
    every frame that points at it: a 2-million-element sub-document and 3,000 frames (a 36 KB file) took 15 s. Whether the
    sub-document holds a chart is remembered with it."""

    @staticmethod
    def _document(elements: int, frames: int) -> bytes:
        other = ('<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
                 'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"><office:body><office:text>'
                 + "<text:p/>" * elements + "</office:text></office:body></office:document-content>").encode()
        body = "<text:p>before" + '<draw:frame><draw:object xlink:href="./Obj1"/></draw:frame>' * frames + "after</text:p>"
        return _odf_package("text", body, parts={"Obj1/content.xml": other})

    def test_object_without_a_chart_gives_nothing_at_every_frame(self):
        assert convert(self._document(10, 30), "sample.odt").text == "beforeafter"

    @pytest.mark.timing
    def test_many_frames_on_a_large_object_do_not_search_it_again(self):
        data = self._document(200_000, 20_000)   # 4 × 10⁹ element visits before: about 10 s

        started = time.perf_counter()
        convert(data, "sample.odt")

        assert time.perf_counter() - started < 4.0


class TestPptxSlideListEntryWithoutARelationship:
    """A slide list entry without `r:id` raised InvalidXmlError, which was not among the errors that skip one slide, so the whole
    deck was rejected. A main part whose root is not `p:presentation` gets the fixed structure error."""

    def test_entry_without_a_relationship_id_skips_only_that_slide(self):
        deck = builder.pptx_bytes()
        whole = convert(deck, "sample.pptx")
        data = builder.zip_with(deck, edit={
            "ppt/presentation.xml": lambda xml: re.sub(r"<p:sldId [^>]*/>", '<p:sldId id="999"/>', xml, count=1)})

        result = convert(data, "sample.pptx")

        assert [section.text for section in result.sections] == [section.text for section in whole.sections[1:]]
        assert result.warnings == ["Slide 1: skipped because its part could not be read."]

    def test_root_that_is_not_a_presentation_gets_the_structure_error(self):
        data = builder.zip_with(builder.pptx_bytes(), edit={
            "ppt/presentation.xml": lambda xml: xml.replace("<p:presentation ", "<p:presentationX ")
                                                   .replace("</p:presentation>", "</p:presentationX>")})

        with pytest.raises(CorruptedDocumentError) as error:
            convert(data, "sample.pptx")

        assert str(error.value) == "The PPTX document structure could not be read."
