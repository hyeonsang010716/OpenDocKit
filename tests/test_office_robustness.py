"""When one part of a DOCX/PPTX is odd, only that part is skipped: span counts past the table range, speaker notes, document
properties (core.xml), an odd slide root, very long numeric attributes. Text boxes and pictures inside footnotes."""

from io import BytesIO

import pytest

from opendockit import convert
from tests import builder
from tests.test_office import _docx_with_body, _pptx_with_text_boxes, _slide_body


_HUGE = "9" * 5000  # more digits than int() accepts (over 4,300)
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"


def _run(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'


def _cell(text: str, properties: str = "") -> str:
    return f"<w:tc><w:tcPr>{properties}</w:tcPr><w:p>{_run(text)}</w:p></w:tc>"


def _deck(build) -> bytes:
    from pptx import Presentation

    presentation = Presentation()
    build(presentation)
    buffer = BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def _pptx_table(build_cells) -> bytes:
    """Slide with one 2×2 table. `build_cells(table)` fills in the cell text and merge attributes."""
    from pptx.util import Inches

    def build(presentation):
        slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        slide.shapes.title.text = "표"
        build_cells(slide.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(6), Inches(2)).table)

    return _deck(build)


def _deck_with_notes(change_second_note=None) -> bytes:
    """Three slides with a title, body and notes. `change_second_note(notes_slide)` alters the notes of the second slide."""
    def build(presentation):
        for index in range(3):
            slide = presentation.slides.add_slide(presentation.slide_layouts[1])
            slide.shapes.title.text = f"제목{index}"
            slide.placeholders[1].text = f"본문{index}"
            slide.notes_slide.notes_text_frame.text = f"노트{index}"
            if index == 1 and change_second_note is not None:
                change_second_note(slide.notes_slide)

    return _deck(build)


def _docx_with_footnote(note_body: str, *, body: str = "", add: dict | None = None, note_relationships: str = "") -> bytes:
    notes = f'<w:footnotes {_NOTE_NAMESPACES}><w:footnote w:id="1">{note_body}</w:footnote></w:footnotes>'
    parts = {"word/footnotes.xml": notes, **(add or {})}
    if note_relationships:
        parts["word/_rels/footnotes.xml.rels"] = (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{note_relationships}</Relationships>")
    return builder.zip_with(_docx_with_body("<w:p>" + _run("본문") + '<w:r><w:footnoteReference w:id="1"/></w:r></w:p>' + body),
                            edit={
        "word/_rels/document.xml.rels": lambda xml: xml.replace(
            "</Relationships>", f'<Relationship Id="rIdNotes" Type="{_RELATIONSHIPS_NS}/footnotes" Target="footnotes.xml"/>'
                                "</Relationships>"),
        "[Content_Types].xml": lambda xml: xml.replace(
            "</Types>", '<Default Extension="png" ContentType="image/png"/><Override PartName="/word/footnotes.xml" '
                        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/></Types>'),
    }, add=parts)


_NOTE_NAMESPACES = (
    f'xmlns:w="{_WORD_NS}" xmlns:r="{_RELATIONSHIPS_NS}" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" xmlns:v="urn:schemas-microsoft-com:vml"')
# Text box: Word puts the same text in the new form (mc:Choice, wps) and in the VML copy kept for compatibility (mc:Fallback).
_TEXT_BOX = (
    "<w:r><mc:AlternateContent><mc:Choice Requires='wps'><w:drawing><wp:anchor><wp:docPr id='1' name='t'/><a:graphic>"
    "<a:graphicData uri='x'><wps:wsp><wps:txbx><w:txbxContent><w:p><w:r><w:t>상자글</w:t></w:r></w:p></w:txbxContent>"
    "</wps:txbx></wps:wsp></a:graphicData></a:graphic></wp:anchor></w:drawing></mc:Choice><mc:Fallback><w:pict><v:shape>"
    "<v:textbox><w:txbxContent><w:p><w:r><w:t>상자글</w:t></w:r></w:p></w:txbxContent></v:textbox></v:shape></w:pict>"
    "</mc:Fallback></mc:AlternateContent></w:r>")
_PICTURE = (
    "<w:r><w:drawing><wp:inline><wp:docPr id='3' name='p' descr='각주 그래프'/><a:graphic><a:graphicData "
    "uri='http://schemas.openxmlformats.org/drawingml/2006/picture'><pic:pic xmlns:pic='http://schemas.openxmlformats.org/"
    "drawingml/2006/picture'><pic:blipFill><a:blip r:embed='rIdImg'/></pic:blipFill></pic:pic></a:graphicData></a:graphic>"
    "</wp:inline></w:drawing></w:r>")


def _wrong_core_namespace(xml: str) -> str:
    import re

    return re.sub(r"<cp:coreProperties[^>]*>.*</cp:coreProperties>",
                  '<cp:coreProperties xmlns:cp="urn:wrong-core" xmlns:dc="http://purl.org/dc/elements/1.1/">'
                  "<dc:title>제목</dc:title></cp:coreProperties>", xml, flags=re.S)


class TestSpansPastTheTable:
    """When the span count written in the file is larger than the table, that many fake rows/columns appeared (`markdown.TableGrid`
    cuts it to the real range)."""

    def test_pptx_row_span_past_the_last_row_adds_no_rows(self):
        def cells(table):
            for row, values in enumerate([["구분", "값"], ["매출", "10"]]):
                for col, value in enumerate(values):
                    table.cell(row, col).text = value
            table.cell(1, 0)._tc.set("rowSpan", "5000")

        result = convert(_pptx_table(cells), "a.pptx")

        assert result.text == "## Slide 1: 표\n\n| 구분 | 값 |\n| --- | --- |\n| 매출 | 10 |"

    def test_pptx_last_row_covered_by_vertical_merges_is_kept(self):
        """Table whose last-row cells are all covered (vMerge) by the vertical merge of the cells above. Covered cells are still
        inside the table, so that row is not removed."""
        def cells(table):
            table.cell(0, 0).text, table.cell(0, 1).text = "가", "나"
            for col in range(2):
                table.cell(0, col)._tc.set("rowSpan", "2")
                table.cell(1, col)._tc.set("vMerge", "1")

        result = convert(_pptx_table(cells), "a.pptx")

        assert result.text == "## Slide 1: 표\n\n| 가 | 나 |\n| --- | --- |\n| 가 | 나 |"

    def test_docx_column_span_past_the_table_keeps_every_row(self):
        """gridSpan=10000 on the top cell + 600 rows: the columns grew to 10,000, hit the grid cell limit and the lower rows vanished
        without a warning."""
        rows = "".join(f"<w:tr>{_cell(f'행{row}')}{_cell(str(row))}</w:tr>" for row in range(1, 601))
        body = f"<w:tbl><w:tr>{_cell('제목', '<w:gridSpan w:val=\"10000\"/>')}</w:tr>{rows}</w:tbl>"

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text.count("<tr>") == 601  # title row (colspan 2) + 600 rows
        assert "<tr><td>행600</td><td>600</td></tr>" in result.text
        assert result.warnings == []

    def test_docx_last_row_covered_by_vertical_merges_is_kept(self):
        restart, cont = '<w:vMerge w:val="restart"/>', "<w:vMerge/>"
        body = (f"<w:tbl><w:tr>{_cell('가', restart)}{_cell('나', restart)}</w:tr>"
                f"<w:tr>{_cell('', cont)}{_cell('', cont)}</w:tr></w:tbl>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "| 가 | 나 |\n| --- | --- |\n| 가 | 나 |"

    def test_docx_rows_left_out_by_the_grid_limit_are_reported(self, monkeypatch):
        """A table scattered diagonally, with a different leading-cell skip (gridBefore) in every row, drops its lower rows at the
        grid cell limit. There used to be no warning. The limit (5 million cells) is lowered to 100 cells and checked with
        20 rows × 20 columns."""
        monkeypatch.setattr("opendockit.markdown._MAX_GRID_CELLS", 100)
        rows = "".join(f'<w:tr><w:trPr><w:gridBefore w:val="{row}"/></w:trPr>{_cell("x")}</w:tr>' for row in range(20))

        result = convert(_docx_with_body(f"<w:tbl>{rows}</w:tbl>"), "a.docx")

        assert result.warnings == ["The table is too large; the bottom 15 row(s) could not be converted."]


class TestPptxSpeakerNotesAreIsolated:
    """Notes are outside the protection that skips one shape at a time, so a single level value or placeholder type of a note
    paragraph got the whole document rejected."""

    @pytest.mark.parametrize("level", ["x", "²", _HUGE], ids=["letter", "superscript-two", "5000-digits"])
    def test_odd_note_paragraph_level_is_level_zero(self, level):
        data = _deck_with_notes(lambda notes: notes.notes_text_frame.paragraphs[0]._p.get_or_add_pPr().set("lvl", level))

        result = convert(data, "a.pptx")

        assert "## Slide 2: 제목1\n\n- 본문1\n\n> Speaker notes: 노트1" in result.text

    def test_note_with_an_unknown_placeholder_type_is_read(self):
        def odd_type(notes):
            for placeholder in notes._element.iter(f"{{{_PRESENTATION_NS}}}ph"):
                if placeholder.get("type") in ("sldNum", "sldImg"):
                    placeholder.set("type", "weird")

        result = convert(_deck_with_notes(odd_type), "a.pptx")

        assert "> Speaker notes: 노트1" in result.text
        assert "> Speaker notes: 노트2" in result.text

    def test_unreadable_note_is_skipped_with_a_warning(self):
        data = builder.zip_with(_deck_with_notes(), edit={
            "ppt/notesSlides/notesSlide2.xml": lambda xml: xml.replace("<p:notes ", "<p:notesX ", 1).replace("</p:notes>", "</p:notesX>")})

        result = convert(data, "a.pptx")

        assert "## Slide 2: 제목1\n\n- 본문1\n\n## Slide 3" in result.text
        assert "> Speaker notes: 노트2" in result.text
        assert result.warnings == ["Slide 2: skipped the speaker notes because they could not be read."]


class TestDocumentPropertiesAndSlideRoots:
    """A wrong namespace in the document properties (core.xml) raised AttributeError, and a single slide whose root is not p:sld
    got the whole document rejected."""

    def test_docx_core_properties_in_a_wrong_namespace_are_left_out(self):
        data = builder.zip_with(builder.docx_bytes(), edit={"docProps/core.xml": _wrong_core_namespace})

        result = convert(data, "a.docx")

        assert result.text.startswith("# 1장 개요")
        assert "title" not in result.metadata

    def test_pptx_core_properties_in_a_wrong_namespace_are_left_out(self):
        data = builder.zip_with(builder.pptx_bytes(), edit={"docProps/core.xml": _wrong_core_namespace})

        result = convert(data, "a.pptx")

        assert result.text.startswith("## Slide 1: 사업 계획")
        assert "title" not in result.metadata

    def test_slide_with_an_odd_root_is_skipped_with_a_warning(self):
        data = builder.zip_with(builder.pptx_bytes(), edit={
            "ppt/slides/slide2.xml": lambda xml: xml.replace("<p:sld ", "<p:sldX ", 1).replace("</p:sld>", "</p:sldX>")})

        result = convert(data, "a.pptx")

        assert result.text.startswith("## Slide 1: 사업 계획")
        assert "## Slide 3: 실적" in result.text
        assert "핵심 목표" not in result.text
        assert result.warnings == ["Slide 2: skipped because the slide could not be read."]


class TestHugeNumbersInOfficeDocuments:
    """A number of more than 4,300 digits that passed `isdecimal()` made `int()` raise ValueError and the document (for PPTX, the
    shape) was rejected. It is treated as the default value."""

    def test_docx_grid_span_with_too_many_digits_is_one_column(self):
        body = "<w:p>" + _run("앞 문단") + f"</w:p><w:tbl><w:tr>{_cell('x', f'<w:gridSpan w:val=\"{_HUGE}\"/>')}</w:tr></w:tbl>"

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "앞 문단\n\nx"  # a one-cell table is a box, so it is unwrapped into a paragraph

    def test_docx_indent_with_too_many_digits_is_ignored(self):
        body = (f'<w:p><w:pPr><w:ind w:left="{_HUGE}"/></w:pPr>' + _run("들여쓴 문단") + "</w:p>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "들여쓴 문단"

    def test_pptx_paragraph_level_with_too_many_digits_is_level_zero(self):
        data = builder.zip_with(_pptx_with_text_boxes("본문"), edit={
            "ppt/slides/slide1.xml": lambda xml: xml.replace("<a:p><a:r>", f'<a:p><a:pPr lvl="{_HUGE}"/><a:r>', 1)})

        result = convert(data, "a.pptx")

        assert _slide_body(data) == "본문"
        assert result.warnings == []

    def test_pptx_start_number_and_baseline_with_too_many_digits_are_the_defaults(self):
        data = builder.zip_with(_pptx_with_text_boxes("항목"), edit={
            "ppt/slides/slide1.xml": lambda xml: xml.replace(
                "<a:p><a:r>", f'<a:p><a:pPr><a:buAutoNum type="arabicPeriod" startAt="{_HUGE}"/></a:pPr>'
                              f'<a:r><a:rPr baseline="{_HUGE}"/>', 1)})

        assert _slide_body(data) == "1. 항목"


class TestDocxFootnoteBoxesAndPictures:
    def test_text_box_in_a_footnote_is_read_once(self):
        """Footnote paragraphs were collected with `iter(w:p)`, so even the compatibility copy of a text box (the VML in mc:Fallback)
        was read, giving '상자글 상자글' (the body reads it once)."""
        data = _docx_with_footnote(f"<w:p>{_run('각주')}{_TEXT_BOX}</w:p>"
                                   f"<w:tbl><w:tr><w:tc><w:p>{_run('칸')}{_TEXT_BOX}</w:p></w:tc></w:tr></w:tbl>")

        result = convert(data, "a.docx")

        assert result.text == "본문[^1]\n\n[^1]: 각주 상자글 칸 상자글"

    def test_picture_in_a_footnote_is_registered_from_the_footnote_part(self):
        """A picture in a footnote was dropped without a placeholder (HWPX footnotes keep pictures). The relationship id is looked up
        in the footnote part — the same id in the body is a different picture."""
        relationship = f'<Relationship Id="rIdImg" Type="{_RELATIONSHIPS_NS}/image" Target="media/note.png"/>'
        data = _docx_with_footnote(f"<w:p>{_run('출처 그림:')}{_PICTURE}</w:p>", note_relationships=relationship,
                                   add={"word/media/note.png": builder.png_bytes(300, 200, seed=3)})

        result = convert(data, "a.docx")

        assert [image.alt for image in result.images] == ["각주 그래프"]
        assert result.text == "본문[^1]\n\n[^1]: 출처 그림:\n\n[Image: 각주 그래프]"  # same shape as an HWPX footnote picture
