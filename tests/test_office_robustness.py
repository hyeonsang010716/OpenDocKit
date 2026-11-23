"""When one part of a DOCX/PPTX is odd, only that part is skipped: span counts past the table range, speaker notes, document
properties (core.xml), an odd slide root, very long numeric attributes. Text boxes and pictures inside footnotes."""

from io import BytesIO

import pytest

from opendockit import ConvertOptions, convert
from tests import builder
from tests.test_office import _docx_with_body, _pptx_with_text_boxes, _slide_body, _with_parts_replaced


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


def _with_part_changed(data: bytes, name: str, change) -> bytes:
    """The package with the XML of one part rewritten by `change(text) -> text`."""
    import zipfile

    with zipfile.ZipFile(BytesIO(data)) as archive:
        text = archive.read(name).decode("utf-8")
    return _with_parts_replaced(data, {name: change(text).encode("utf-8")})


class TestSlideListedMoreThanOnce:
    """PowerPoint writes each slide once in the slide list. A list naming one slide 50 times converted it 50 times (68 s for a 98 KB
    deck whose slide held 20,000 shapes without text, which the character limit never sees). A slide part is read once, with a
    warning, as a sheet named twice by a workbook is."""

    _WARNING = "The presentation lists the same slide more than once, so it was read only once."

    @staticmethod
    def _two_slides() -> bytes:
        def build(presentation):
            for title in ("First", "Second"):
                presentation.slides.add_slide(presentation.slide_layouts[5]).shapes.title.text = title

        return _deck(build)

    @staticmethod
    def _first_entry(presentation_xml: str) -> str:
        start = presentation_xml.index("<p:sldId ")
        return presentation_xml[start:presentation_xml.index("/>", start) + 2]

    def test_entry_repeated_with_the_same_relationship_is_read_once(self):
        def repeat(text: str) -> str:
            return text.replace("</p:sldIdLst>", self._first_entry(text) * 40 + "</p:sldIdLst>")

        result = convert(_with_part_changed(self._two_slides(), "ppt/presentation.xml", repeat), "a.pptx")

        assert (result.text.count("First"), result.text.count("Second")) == (1, 1)
        assert len(result.sections) == 2
        assert result.metadata["slide_count"] == "2"
        assert result.warnings == [self._WARNING]

    def test_slide_named_by_a_second_relationship_is_read_once(self):
        import re

        def add_relationship(text: str) -> str:
            first = re.search(r'<Relationship [^>]*Target="slides/slide1.xml"[^>]*/>', text).group(0)
            kind = re.search(r'Type="([^"]+)"', first).group(1)
            again = f'<Relationship Id="rIdAgain" Type="{kind}" Target="slides/slide1.xml"/>'
            return text.replace("</Relationships>", again + "</Relationships>")

        def list_it(text: str) -> str:
            entry = re.sub(r'r:id="[^"]+"', 'r:id="rIdAgain"', self._first_entry(text))
            return text.replace("</p:sldIdLst>", entry + "</p:sldIdLst>")

        deck = _with_part_changed(self._two_slides(), "ppt/_rels/presentation.xml.rels", add_relationship)
        result = convert(_with_part_changed(deck, "ppt/presentation.xml", list_it), "a.pptx")

        assert result.text.count("First") == 1
        assert result.metadata["slide_count"] == "2"
        assert result.warnings == [self._WARNING]

    def test_deck_of_distinct_slides_has_no_warning(self):
        result = convert(self._two_slides(), "a.pptx")

        assert result.metadata["slide_count"] == "2"
        assert result.warnings == []


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


    # One number of exactly 4,300 digits passes `decimal_int` (Python's limit for `int()`), and the next number (one digit more)
    # could not be written with `str()`: the document was rejected as corrupted. 4,299 digits gained no digit and passed.
    @pytest.mark.parametrize("length", [4299, 4300])
    @pytest.mark.parametrize("where", ["start", "startOverride"])
    def test_docx_start_number_of_thousands_of_digits_is_the_default(self, length, where):
        big = "9" * length
        start = big if where == "start" else "1"
        override = f'<w:lvlOverride w:ilvl="0"><w:startOverride w:val="{big}"/></w:lvlOverride>' if where == "startOverride" else ""
        numbering = (f'<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="{start}"/><w:numFmt w:val="decimal"/>'
                     f'<w:lvlText w:val="%1."/></w:lvl></w:abstractNum>'
                     f'<w:num w:numId="1"><w:abstractNumId w:val="0"/>{override}</w:num>')
        item = '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>' + _run("항목") + "</w:p>"

        result = convert(_docx_with_body(item * 2, numbering=numbering), "a.docx")

        assert result.text == "1. 항목\n2. 항목"

    @pytest.mark.parametrize("length", [4299, 4300])
    def test_pptx_start_number_of_thousands_of_digits_is_the_default(self, length):
        data = builder.zip_with(_pptx_with_text_boxes("항목"), edit={
            "ppt/slides/slide1.xml": lambda xml: xml.replace(
                "<a:p><a:r>", f'<a:p><a:pPr><a:buAutoNum type="arabicPeriod" startAt="{"9" * length}"/></a:pPr><a:r>', 1)})

        assert _slide_body(data) == "1. 항목"

    @pytest.mark.parametrize(("start", "first", "second"), [
        ("32767", "32767", "32768"), ("40000", "40000", "40001"), ("2147483647", "2147483647", "2147483648"),
        ("2147483648", "1", "2"), ("99999999999", "1", "2"),
    ])
    @pytest.mark.parametrize("where", ["start", "startOverride"])
    def test_start_number_past_what_word_lets_a_user_type_is_kept(self, start, first, second, where):
        """The bound that keeps a number of thousands of digits out is on its width (ten digits, a signed 32-bit integer), not
        Word's 32,767: a list written to start at 40000 read '1. 2.'."""
        level_start = start if where == "start" else "1"
        override = f'<w:lvlOverride w:ilvl="0"><w:startOverride w:val="{start}"/></w:lvlOverride>' if where == "startOverride" else ""
        numbering = (f'<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="{level_start}"/>'
                     '<w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl></w:abstractNum>'
                     f'<w:num w:numId="1"><w:abstractNumId w:val="0"/>{override}</w:num>')
        item = '<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>' + _run("항목") + "</w:p>"

        assert convert(_docx_with_body(item * 2, numbering=numbering), "a.docx").text == f"{first}. 항목\n{second}. 항목"

    @pytest.mark.parametrize(("start", "shown"), [("40000", "40000"), ("2147483648", "1")])
    def test_pptx_start_number_past_what_powerpoint_lets_a_user_type_is_kept(self, start, shown):
        data = builder.zip_with(_pptx_with_text_boxes("항목"), edit={
            "ppt/slides/slide1.xml": lambda xml: xml.replace(
                "<a:p><a:r>", f'<a:p><a:pPr><a:buAutoNum type="arabicPeriod" startAt="{start}"/></a:pPr><a:r>', 1)})

        assert _slide_body(data) == f"{shown}. 항목"

    @pytest.mark.parametrize("length", [4299, 4300])
    def test_chart_point_index_past_the_unsigned_range_is_left_out(self, length):
        from tests.test_office import _chart_xml, _pptx_with_chart

        points = f'<c:pt idx="{"9" * length}"><c:v>5</c:v></c:pt><c:pt idx="4294967295"><c:v>7</c:v></c:pt>'
        series = ('<c:ser><c:idx val="0"/><c:order val="0"/><c:tx><c:v>S</c:v></c:tx><c:val><c:numRef><c:numCache>'
                  f"{points}</c:numCache></c:numRef></c:val></c:ser>")
        data = builder.zip_with(_pptx_with_chart(), edit={
            "ppt/charts/chart1.xml": lambda xml: _chart_xml(f"<c:barChart>{series}</c:barChart>", "T")})

        result = convert(data, "a.pptx")

        assert "| 4294967296 | 7 |" in result.text and "| 5 |" not in result.text and result.warnings == []

    @pytest.mark.parametrize("shape", ["DIGIT", "HANGUL_DIGIT", "ZERO_PADDED_DIGIT", "FULLWIDTH_DIGIT", "ROMAN_CAPITAL"])
    def test_number_one_digit_past_the_string_limit_is_written(self, shape):
        """HWPX list start numbers and other callers reach `format_number` with whatever `decimal_int` accepted."""
        from opendockit.extractors._lists import format_number

        assert len(format_number(int("9" * 4300) + 1, shape)) == 4301


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


def _rebuilt(data: bytes, *, corrupt: tuple[str, ...] = (), rename: dict[str, str] | None = None,
             edit: dict[str, object] | None = None, add: dict[str, str] | None = None) -> bytes:
    """A copy of a ZIP-based document. The members in `corrupt` are stored uncompressed with one byte of their data flipped (so
    reading them fails their CRC check); `rename` moves members, `edit` (name → function of the text) changes them, `add` adds members."""
    import zipfile

    rename, edit = rename or {}, edit or {}
    buffer = BytesIO()
    with zipfile.ZipFile(BytesIO(data)) as source, zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for name in source.namelist():
            content = source.read(name)
            if name in edit:
                content = edit[name](content.decode("utf-8")).encode("utf-8")
            method = zipfile.ZIP_STORED if name in corrupt else zipfile.ZIP_DEFLATED
            target.writestr(rename.get(name, name), content, compress_type=method)
        for name, content in (add or {}).items():
            target.writestr(name, content, compress_type=zipfile.ZIP_STORED if name in corrupt else zipfile.ZIP_DEFLATED)
    raw = bytearray(buffer.getvalue())
    with zipfile.ZipFile(BytesIO(bytes(raw))) as archive:
        infos = [archive.getinfo(rename.get(name, name)) for name in corrupt]
    for info in infos:
        start = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
        raw[start + info.compress_size // 2] ^= 0xFF
    return bytes(raw)


class TestDamagedZipMember:
    """python-docx / python-pptx read every part when opening, so one picture whose bytes could not be read out of the archive (a
    broken compressed stream or a failed CRC check) rejected the whole document. Such a member is detached like a missing part."""

    def test_docx_picture_that_cannot_be_read_is_detached_and_the_body_kept(self):
        data = _rebuilt(builder.docx_with_images(), corrupt=("word/media/image1.png",))

        result = convert(data, "a.docx")

        assert result.text.startswith("차트 앞 문단입니다.\n\n아이콘")
        assert any("detaching 1 missing or broken part(s) (image1.png)" in warning for warning in result.warnings)

    def test_pptx_picture_that_cannot_be_read_is_detached_and_the_slides_kept(self):
        data = _rebuilt(builder.pptx_with_image(), corrupt=("ppt/media/image1.png",))

        result = convert(data, "a.pptx")

        assert result.sections
        assert any("detaching 1 missing or broken part(s) (image1.png)" in warning for warning in result.warnings)

    def test_member_nothing_points_at_that_cannot_be_read_is_left_out_of_the_copy(self):
        data = _rebuilt(builder.docx_with_images(), corrupt=("word/media/image1.png", "word/media/orphan.bin"),
                        add={"word/media/orphan.bin": "x" * 5000})

        result = convert(data, "a.docx")

        assert any("detaching 1 missing or broken part(s)" in warning for warning in result.warnings)

    def test_relationships_of_a_part_in_a_folder_ending_in_rels_resolve_against_that_folder(self):
        """The folder of a part was taken as the text before the first `_rels/` of its relationships file, so for
        `word/hdr_rels/_rels/header1.xml.rels` it was `word/hdr`, the header's picture counted as missing and was detached when
        the package was repaired for another reason."""
        from docx import Document

        document = Document()
        document.add_paragraph("본문")
        document.sections[0].header.paragraphs[0].add_run().add_picture(BytesIO(builder.png_bytes(seed=3)))
        buffer = BytesIO()
        document.save(buffer)
        relationship = f'<Relationship Id="rIdGone" Type="{_RELATIONSHIPS_NS}/image" Target="media/gone.png"/></Relationships>'
        data = _rebuilt(buffer.getvalue(), rename={
            "word/header1.xml": "word/hdr_rels/header1.xml",
            "word/_rels/header1.xml.rels": "word/hdr_rels/_rels/header1.xml.rels",
            "word/media/image1.png": "word/hdr_rels/logo.png",
        }, edit={
            "word/_rels/header1.xml.rels": lambda xml: xml.replace('Target="media/image1.png"', 'Target="logo.png"'),
            "word/_rels/document.xml.rels": lambda xml: xml.replace('Target="header1.xml"', 'Target="hdr_rels/header1.xml"')
                                                           .replace("</Relationships>", relationship),
            "[Content_Types].xml": lambda xml: xml.replace("/word/header1.xml", "/word/hdr_rels/header1.xml"),
        })

        result = convert(data, "a.docx", ConvertOptions(include_headers_footers=True))

        assert any("detaching 1 missing or broken part(s) (gone.png)" in warning for warning in result.warnings)
        assert len(result.images) == 1

    def test_errors_of_a_damaged_member_are_listed_for_other_extractors(self):
        import lzma
        import zipfile
        import zlib

        from opendockit.extractors._package import ZIP_MEMBER_ERRORS

        for error in (zipfile.BadZipFile, zlib.error, lzma.LZMAError, EOFError, OSError, RuntimeError, NotImplementedError):
            assert issubclass(error, ZIP_MEMBER_ERRORS)


class TestBrokenRelationshipsPart:
    """One relationships part (`.rels`) that is not XML was skipped when the package was repaired, so nothing was detached and the
    whole document was rejected as corrupted. It is now left out of the copy: the part that owns it opens without relationships."""

    def test_docx_header_relationships_that_are_not_xml_keep_the_body(self):
        from docx import Document

        document = Document()
        document.add_paragraph("본문")
        document.sections[0].header.paragraphs[0].text = "머리글"
        buffer = BytesIO()
        document.save(buffer)
        data = builder.zip_with(buffer.getvalue(), add={"word/_rels/header1.xml.rels": "<Relationships"})

        result = convert(data, "a.docx", ConvertOptions(include_headers_footers=True))

        assert result.text == "머리글\n\n본문"
        assert any("detaching 1 missing or broken part(s)" in warning for warning in result.warnings)

    def test_pptx_slide_relationships_that_are_not_xml_keep_every_slide(self):
        data = builder.zip_with(builder.pptx_bytes(), edit={"ppt/slides/_rels/slide2.xml.rels": lambda xml: xml[:-5]})

        result = convert(data, "a.pptx")

        assert "사업 계획" in result.text and "매출 성장" in result.text and "서울" in result.text
        assert any("detaching 1 missing or broken part(s)" in warning for warning in result.warnings)

    def test_main_part_relationships_that_are_not_xml_keep_the_body(self):
        data = builder.zip_with(builder.docx_bytes(), edit={"word/_rels/document.xml.rels": lambda xml: xml[:-5]})

        result = convert(data, "a.docx")

        assert "본문 첫 문단입니다." in result.text and "끝 | 파이프" in result.text
        assert any("detaching 1 missing or broken part(s)" in warning for warning in result.warnings)

    @pytest.mark.parametrize(("name", "make"), [("a.docx", builder.docx_bytes), ("a.pptx", builder.pptx_bytes)])
    def test_package_relationships_that_are_not_xml_find_the_main_part_by_its_content_type(self, name, make):
        data = make()  # built here: as a parameter its bytes were the test's name, which differed between test workers
        expected = convert(data, name).text

        result = convert(builder.zip_with(data, edit={"_rels/.rels": lambda xml: xml[:-5]}), name)

        assert result.text == expected
        assert any("detaching 1 missing or broken part(s)" in warning for warning in result.warnings)

    def test_two_parts_of_a_main_document_type_name_no_main_part(self):
        """With the package relationships gone, the content types name the main part only when one part has a main-document
        type. With two, one of them was not picked: which one the document shows is not known."""
        from opendockit.exceptions import CorruptedDocumentError

        other = ('<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r>'
                 "<w:t>another document</w:t></w:r></w:p></w:body></w:document>")
        override = ('<Override PartName="/word/another.xml" ContentType="application/vnd.openxmlformats-officedocument.'
                    'wordprocessingml.document.main+xml"/></Types>')
        data = builder.zip_with(builder.docx_bytes(), add={"word/another.xml": other}, edit={
            "_rels/.rels": lambda xml: xml[:-5], "[Content_Types].xml": lambda xml: xml.replace("</Types>", override)})

        with pytest.raises(CorruptedDocumentError):
            convert(data, "a.docx")

    def test_percent_encoded_target_is_decoded(self):
        """`sty%6Ces.xml` is the URI form of `styles.xml`. It was looked up as written, counted as missing and the styles were
        detached with a warning."""
        data = builder.docx_bytes()
        encoded = builder.zip_with(data, edit={
            "word/_rels/document.xml.rels": lambda xml: xml.replace('Target="styles.xml"', 'Target="sty%6Ces.xml"')})

        result = convert(encoded, "a.docx")

        assert result.text == convert(data, "a.docx").text and result.warnings == []

    def test_literal_name_with_a_percent_sign_is_kept_when_only_it_exists(self):
        data = _rebuilt(builder.docx_bytes(), rename={"word/styles.xml": "word/sty%6Ces.xml"}, edit={
            "word/_rels/document.xml.rels": lambda xml: xml.replace('Target="styles.xml"', 'Target="sty%6Ces.xml"'),
            "[Content_Types].xml": lambda xml: xml.replace("/word/styles.xml", "/word/sty%6Ces.xml")})

        result = convert(data, "a.docx")

        assert result.text == convert(builder.docx_bytes(), "a.docx").text and result.warnings == []


_MISSING_PICTURE = f'<Relationship Id="rIdGone" Type="{_RELATIONSHIPS_NS}/image" Target="media/gone.png"/></Relationships>'
_SHEET_NAMED_AS_WRITTEN = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1">'
                           '<c r="A1" t="inlineStr"><is><t>named as written</t></is></c></row></sheetData></worksheet>')


def _xlsx_with_parts() -> bytes:
    """Two sheets; the first has a comment, a chart and a picture."""
    import openpyxl
    from openpyxl.chart import BarChart, Reference
    from openpyxl.comments import Comment
    from openpyxl.drawing.image import Image

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "One"
    for row in range(1, 5):
        sheet.append([f"a{row}", row])
    sheet["A1"].comment = Comment("remark", "me")
    chart = BarChart()
    chart.title = "Chart T"
    chart.add_data(Reference(sheet, min_col=2, min_row=1, max_row=4))
    sheet.add_chart(chart, "D2")
    sheet.add_image(Image(BytesIO(builder.png_bytes())), "D20")
    workbook.create_sheet("Two").append(["second sheet"])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


class TestTargetInAnotherCase:
    """Part names are case-insensitive in a package (OPC), and Office opens a part whose name differs in case from the target that
    points at it. The libraries read the archive by the name as written: a DOCX lost the part to the repair as missing, with a
    warning, and a PPTX lost a picture without a word and reported a slide as unreadable. A renamed part keeps its relationships
    when they kept the old case."""

    _NOTE = "<w:p>" + _run("출처 그림:") + _PICTURE + "</w:p>"

    def _docx(self) -> bytes:
        return _docx_with_footnote(self._NOTE, note_relationships=_NOTE_PICTURE_RELATIONSHIP,
                                   add={"word/media/note.png": builder.png_bytes(300, 200, seed=3)})

    @pytest.mark.parametrize("rename", [
        pytest.param({"word/media/note.png": "word/Media/Note.png"}, id="picture"),
        pytest.param({"word/footnotes.xml": "word/Footnotes.xml"}, id="part with relationships of the old case"),
        pytest.param({"word/footnotes.xml": "word/Footnotes.xml", "word/_rels/footnotes.xml.rels": "word/_rels/FOOTNOTES.xml.rels"},
                     id="part and relationships each in another case"),
    ])
    def test_docx_part_in_another_case_is_read(self, rename):
        expected = convert(self._docx(), "a.docx")
        assert [image.alt for image in expected.images] == ["각주 그래프"]

        result = convert(_rebuilt(self._docx(), rename=rename), "a.docx")

        assert (result.text, [image.alt for image in result.images], result.warnings) == (expected.text, ["각주 그래프"], [])

    _CUSTOM_XML = {"customXml/item1.xml": "CustomXml/item1.xml"}  # its relationships and itemProps1.xml stay in `customXml/`

    def test_relationships_carried_to_another_folder_case_keep_their_targets(self):
        """The relationships of a part found in another folder case are carried to that folder: their relative target
        `itemProps1.xml` then named `CustomXml/itemProps1.xml`, which is not there, and the whole document was rejected."""
        expected = convert(builder.docx_bytes(), "a.docx")

        result = convert(_rebuilt(builder.docx_bytes(), rename=self._CUSTOM_XML), "a.docx")

        assert (result.text, result.warnings) == (expected.text, [])

    def test_document_that_fails_with_the_part_found_opens_without_it(self, monkeypatch):
        """A copy reading the parts found in another case that still fails to open is made again without them: the part is
        detached with the warning, as it was before parts were found in another case, instead of the document being rejected."""
        from opendockit.extractors import _package

        repair = _package.repair_package

        def failing(data, *, in_another_case=True):
            repaired = repair(data, in_another_case=in_another_case)
            if repaired is None or not in_another_case:
                return repaired
            return _rebuilt(repaired[0], edit={"CustomXml/_rels/item1.xml.rels": lambda xml: xml.replace(
                'Target="/customXml/itemProps1.xml"', 'Target="itemProps1.xml"')}), repaired[1]

        monkeypatch.setattr(_package, "repair_package", failing)
        expected = convert(builder.docx_bytes(), "a.docx")

        result = convert(_rebuilt(builder.docx_bytes(), rename=self._CUSTOM_XML), "a.docx")

        assert result.text == expected.text
        assert result.warnings == ["Read after detaching 1 missing or broken part(s) (item1.xml). The contents of those parts "
                                   "(pictures, numbering, styles, etc.) may be missing."]

    def test_two_members_that_differ_only_in_case_are_not_guessed_between(self):
        data = _rebuilt(self._docx(), rename={"word/media/note.png": "word/media/Note.png"},
                        add={"word/media/NOTE.png": builder.png_bytes(10, 10)})

        result = convert(data, "a.docx")

        assert result.images == [] and "detaching 1 missing or broken part(s) (note.png)" in result.warnings[0]

    @pytest.mark.parametrize("rename", [
        pytest.param({"ppt/media/image1.png": "ppt/Media/Image1.png"}, id="picture"),
        pytest.param({"ppt/slides/slide1.xml": "ppt/slides/Slide1.xml"}, id="slide with relationships of the old case"),
    ])
    def test_pptx_part_in_another_case_is_read(self, rename):
        expected = convert(builder.pptx_with_image(), "a.pptx")
        assert len(expected.images) == 1

        result = convert(_rebuilt(builder.pptx_with_image(), rename=rename), "a.pptx")

        assert (result.text, len(result.images), result.warnings) == (expected.text, 1, [])


    @pytest.mark.parametrize(("make", "name", "rename"), [
        pytest.param(builder.docx_bytes, "a.docx", {"word/document.xml": "word/Document.xml"}, id="docx"),
        pytest.param(builder.docx_bytes, "a.docx", {"word/document.xml": "word/Document.xml",
                                                    "word/_rels/document.xml.rels": "word/_rels/Document.xml.rels"},
                     id="docx with its relationships"),
        pytest.param(builder.pptx_bytes, "a.pptx", {"ppt/presentation.xml": "ppt/Presentation.xml"}, id="pptx"),
        pytest.param(builder.xlsx_bytes, "a.xlsx", {"xl/workbook.xml": "xl/Workbook.xml"}, id="xlsx"),
        pytest.param(builder.xlsx_bytes, "a.xlsx", {"xl/workbook.xml": "xl/Workbook.xml",
                                                    "xl/_rels/workbook.xml.rels": "xl/_rels/Workbook.xml.rels"},
                     id="xlsx with its relationships"),
    ])
    def test_main_part_in_another_case(self, make, name, rename):
        """Detection rejected the archive as unsupported, and past it the DOCX repair did not look at the main part."""
        expected = convert(make(), name)

        result = convert(_rebuilt(make(), rename=rename), name)

        assert (result.text, result.warnings) == (expected.text, expected.warnings)

    @pytest.mark.parametrize("rename", [
        pytest.param({"xl/worksheets/sheet1.xml": "xl/worksheets/Sheet1.xml"}, id="sheet"),
        pytest.param({"xl/worksheets/sheet1.xml": "xl/Worksheets/SHEET1.xml"}, id="sheet in a folder of another case"),
        pytest.param({"xl/styles.xml": "xl/Styles.xml"}, id="styles"),
        pytest.param({"xl/workbook.xml": "xl/Workbook.xml", "xl/worksheets/sheet1.xml": "xl/worksheets/Sheet1.xml",
                      "xl/styles.xml": "xl/STYLES.xml"}, id="several parts"),
    ])
    def test_xlsx_parts_in_another_case(self, rename):
        """openpyxl passed over a sheet it did not find under the target's name without a word, and read the styles only under
        `xl/styles.xml`: the dates came out as serial numbers."""
        expected = convert(builder.xlsx_bytes(), "a.xlsx")
        assert "2026-01-31" in expected.text

        result = convert(_rebuilt(builder.xlsx_bytes(), rename=rename), "a.xlsx")

        assert (result.text, result.warnings) == (expected.text, expected.warnings)

    def test_xlsx_target_written_in_another_case(self):
        data = builder.zip_with(builder.xlsx_bytes(), edit={
            "xl/_rels/workbook.xml.rels": lambda xml: xml.replace("worksheets/sheet1.xml", "Worksheets/Sheet1.XML"),
            "[Content_Types].xml": lambda xml: xml.replace("/xl/worksheets/sheet1.xml", "/xl/Worksheets/Sheet1.XML")})
        expected = convert(builder.xlsx_bytes(), "a.xlsx")

        result = convert(data, "a.xlsx")

        assert (result.text, result.warnings) == (expected.text, expected.warnings)

    def test_xlsx_picture_of_a_sheet_in_another_case(self):
        expected = convert(builder.xlsx_with_image(), "a.xlsx")
        assert len(expected.images) == 1

        result = convert(_rebuilt(builder.xlsx_with_image(), rename={"xl/media/image1.png": "xl/Media/Image1.PNG",
                                                                     "xl/drawings/drawing1.xml": "xl/Drawings/Drawing1.xml"}),
                         "a.xlsx")

        assert (result.text, len(result.images), result.warnings) == (expected.text, 1, [])

    def test_xlsx_member_also_named_as_it_is_keeps_its_name(self):
        """A content-type override in another case than the member and its relationship: renamed after the override, the
        sheet would no longer be found behind the relationship."""
        data = builder.zip_with(builder.xlsx_bytes(), edit={"[Content_Types].xml": lambda xml: xml.replace(
            "/xl/worksheets/sheet1.xml", "/xl/worksheets/Sheet1.xml")})
        expected = convert(builder.xlsx_bytes(), "a.xlsx")

        result = convert(data, "a.xlsx")

        assert (result.text, result.warnings) == (expected.text, expected.warnings)

    def test_xlsx_two_members_that_differ_only_in_case_are_not_guessed_between(self):
        import zipfile

        with zipfile.ZipFile(BytesIO(builder.xlsx_bytes())) as archive:
            xml = archive.read("xl/worksheets/sheet1.xml")
        data = _rebuilt(builder.xlsx_bytes(), rename={"xl/worksheets/sheet1.xml": "xl/worksheets/Sheet1.xml"},
                        add={"xl/worksheets/SHEET1.xml": xml})

        result = convert(data, "a.xlsx")

        assert "## Sheet: 매출" not in result.text
        assert "Sheet '매출' was left out because its part is missing from the file." in result.warnings


class TestIncompleteRelationship:
    """One relationship without its Id, Type or Target, in any `.rels` (even one nothing uses), rejected the whole document: the
    libraries fail on it with a TypeError, an AttributeError or an InvalidXmlError, which the repair did not take. Nothing tells
    what such a relationship points at; it is left out, with a warning when the library could not open the package."""

    @pytest.mark.parametrize("missing", ["Id", "Type", "Target"])
    @pytest.mark.parametrize(("data", "name", "relationships", "target"), [
        pytest.param(builder.docx_bytes(), "a.docx", "word/_rels/document.xml.rels", "styles.xml", id="docx"),
        pytest.param(builder.pptx_bytes(), "a.pptx", "ppt/slideLayouts/_rels/slideLayout1.xml.rels",
                     "../slideMasters/slideMaster1.xml", id="pptx"),
    ])
    def test_document_is_read(self, data, name, relationships, target, missing):
        attributes = {"Id": "rId99", "Type": f"{_RELATIONSHIPS_NS}/customXml", "Target": target}
        del attributes[missing]
        extra = "<Relationship " + " ".join(f'{key}="{value}"' for key, value in attributes.items()) + "/>"
        expected = convert(data, name).text

        result = convert(builder.zip_with(data, edit={relationships: lambda xml: xml.replace("</Relationships>",
                                                                                           extra + "</Relationships>")}), name)

        assert result.text == expected
        assert result.warnings in ([], ["Read after leaving out 1 relationship(s) with no Id, Type or Target. "
                                        "Whatever they pointed at may be missing."])

    @pytest.mark.parametrize("kind", ["numbering", "styles"])
    def test_docx_relationship_without_a_type_takes_the_type_of_its_part(self, kind):
        """python-docx opens a package with a relationship without a Type and keeps it, so the repair never runs, and a part
        looked up by type was not found behind it: the list items lost `- ` and `1.` (numbering), the headings their `#`
        (styles), without a word. It takes the type of the part it points at, and the document reads as it is."""
        import re

        data = builder.zip_with(builder.docx_bytes(), edit={"word/_rels/document.xml.rels": lambda xml: re.sub(
            f'Type="[^"]*/{kind}"', "", xml)})
        expected = convert(builder.docx_bytes(), "a.docx")

        result = convert(data, "a.docx")

        assert (result.text, result.warnings) == (expected.text, [])
        assert "1. 첫째" in result.text and "# 1장 개요" in result.text


class TestPercentEncodedTargets:
    """A relationship target is a URI: `im%61ge1.png` names the part `image1.png` (OPC). Only a package that failed to open had
    its targets decoded (`_package.repair_package`), and that is a DOCX: python-pptx and openpyxl leave out a part they do not
    find under the name as written, so a slide was reported as unreadable, a sheet was left out without a word, and pictures,
    notes, comments and charts were gone. A part named as written wins when it exists (a file name may hold a percent sign)."""

    @pytest.mark.parametrize(("relationships", "written", "encoded"), [
        pytest.param("ppt/_rels/presentation.xml.rels", "slides/slide1.xml", "slides/slide%31.xml", id="slide"),
        pytest.param("ppt/slides/_rels/slide1.xml.rels", "../media/image1.png", "../media/im%61ge1.png", id="picture"),
        pytest.param("ppt/slides/_rels/slide2.xml.rels", "../notesSlides/notesSlide1.xml", "../notesSlides/notesSl%69de1.xml",
                     id="notes"),
        pytest.param("ppt/slides/_rels/slide3.xml.rels", "../charts/chart1.xml", "../charts/ch%61rt1.xml", id="chart"),
        pytest.param("_rels/.rels", "ppt/presentation.xml", "ppt/present%61tion.xml", id="main part"),
    ])
    def test_pptx_part_is_read(self, relationships, written, encoded):
        from pptx import Presentation
        from pptx.util import Inches

        presentation = Presentation(BytesIO(builder.pptx_bytes()))
        presentation.slides[0].shapes.add_picture(BytesIO(builder.png_bytes()), Inches(1), Inches(1))
        buffer = BytesIO()
        presentation.save(buffer)
        data = buffer.getvalue()
        expected = convert(data, "a.pptx")
        assert len(expected.images) == 1 and "[Chart]" in expected.text and "여기서 수치를 강조" in expected.text

        result = convert(builder.zip_with(data, edit={relationships: lambda xml: xml.replace(written, encoded)}), "a.pptx")

        assert (result.text, len(result.images), result.warnings) == (expected.text, 1, [])

    def test_pptx_part_named_as_written_wins_over_the_decoded_name(self):
        data = builder.zip_with(
            builder.pptx_with_image(),
            edit={"ppt/slides/_rels/slide1.xml.rels": lambda xml: xml.replace("../media/image1.png", "../media/im%61ge1.png")},
            add={"ppt/media/im%61ge1.png": builder.png_bytes(120, 200, seed=7)})

        result = convert(data, "a.pptx")

        assert [(image.width, image.height) for image in result.images] == [(120, 200)]

    def test_docx_literal_name_is_kept_in_a_repaired_package_when_only_it_exists(self):
        """A missing picture sends the package through the repair; the styles part exists only under the name with the percent
        sign. Decoded without looking, the relationship pointed at no part and the document was rejected."""
        data = _rebuilt(builder.docx_bytes(), rename={"word/styles.xml": "word/sty%6Ces.xml"}, edit={
            "word/_rels/document.xml.rels": lambda xml: (xml.replace('Target="styles.xml"', 'Target="sty%6Ces.xml"')
                                                         .replace("</Relationships>", _MISSING_PICTURE)),
            "[Content_Types].xml": lambda xml: xml.replace("/word/styles.xml", "/word/sty%6Ces.xml")})

        result = convert(data, "a.docx")

        assert result.text == convert(builder.docx_bytes(), "a.docx").text
        assert len(result.warnings) == 1 and "detaching 1 missing or broken part(s) (gone.png)" in result.warnings[0]

    def test_docx_part_named_as_written_wins_in_a_repaired_package(self):
        data = _rebuilt(
            builder.docx_with_images(),
            edit={"word/_rels/document.xml.rels": lambda xml: (xml.replace("media/image1.png", "media/im%61ge1.png")
                                                               .replace("</Relationships>", _MISSING_PICTURE))},
            add={"word/media/im%61ge1.png": builder.png_bytes(120, 200, seed=7)})

        result = convert(data, "a.docx")

        assert (result.images[0].width, result.images[0].height) == (120, 200)
        assert len(result.warnings) == 1 and "detaching 1 missing or broken part(s) (gone.png)" in result.warnings[0]

    @pytest.mark.parametrize(("relationships", "written", "encoded"), [
        pytest.param("xl/_rels/workbook.xml.rels", "worksheets/sheet1.xml", "worksheets/sh%65et1.xml", id="first sheet"),
        pytest.param("xl/_rels/workbook.xml.rels", "worksheets/sheet2.xml", "worksheets/sh%65et2.xml", id="second sheet"),
        pytest.param("xl/worksheets/_rels/sheet1.xml.rels", "comment1.xml", "comm%65nt1.xml", id="comments"),
        pytest.param("xl/worksheets/_rels/sheet1.xml.rels", "drawing1.xml", "dr%61wing1.xml", id="drawing"),
        pytest.param("xl/drawings/_rels/drawing1.xml.rels", "chart1.xml", "ch%61rt1.xml", id="chart"),
        pytest.param("xl/drawings/_rels/drawing1.xml.rels", "image1.png", "im%61ge1.png", id="picture"),
    ])
    def test_xlsx_part_is_read(self, relationships, written, encoded):
        data = _xlsx_with_parts()
        expected = convert(data, "a.xlsx")
        assert len(expected.images) == 1 and len(expected.sections) == 2
        assert "remark" in expected.text and "Chart T" in expected.text and "second sheet" in expected.text

        result = convert(builder.zip_with(data, edit={relationships: lambda xml: xml.replace(written, encoded)}), "a.xlsx")

        assert (result.text, len(result.images), result.warnings) == (expected.text, 1, [])

    def test_xlsx_part_named_as_written_wins_over_the_decoded_name(self):
        data = builder.zip_with(
            _xlsx_with_parts(),
            edit={"xl/_rels/workbook.xml.rels": lambda xml: xml.replace("worksheets/sheet2.xml", "worksheets/sh%65et2.xml"),
                  "[Content_Types].xml": lambda xml: xml.replace(
                      "</Types>", '<Override PartName="/xl/worksheets/sh%65et2.xml" ContentType="application/vnd.openxmlformats-'
                                  'officedocument.spreadsheetml.worksheet+xml"/></Types>')},
            add={"xl/worksheets/sh%65et2.xml": _SHEET_NAMED_AS_WRITTEN})

        result = convert(data, "a.xlsx")

        assert "named as written" in result.sections[1].text and "second sheet" not in result.text

    def test_xlsx_workbook_behind_an_encoded_target_keeps_what_its_relationships_name(self):
        """The parts only the converter reads are found from the workbook the package relationships name: with
        `xl/workb%6Fok.xml` taken as written no workbook was found, and the person list with it (the author of a threaded
        comment)."""
        from tests.test_spreadsheet_notes_pictures_and_times import _report_with_comments, _with_threaded_comments

        data = _with_threaded_comments(
            _report_with_comments({"B2": ("legacy", "tc={C1}")}),
            '<threadedComment ref="B2" personId="{P1}" id="{C1}"><text>check the price</text></threadedComment>',
            '<person displayName="Reviewer Kim" id="{P1}"/>')
        expected = convert(data, "a.xlsx")
        assert expected.text.endswith('> Comment(Reviewer Kim, cell B2, on "1월: 100"): check the price')

        result = convert(builder.zip_with(data, edit={"_rels/.rels": lambda xml: xml.replace("xl/workbook.xml", "xl/workb%6Fok.xml")}),
                         "a.xlsx")

        assert (result.text, result.warnings) == (expected.text, [])


class TestExternalTargetsAreNotParts:
    """A relationship marked `TargetMode="External"` holds an address or a file outside the package: it is not
    percent-decoded into the name of a part."""

    def test_xlsx_part_named_as_written_by_a_relationship_marked_external_is_still_read(self):
        """A producer that marks the relationship of a part of the package External by mistake: the part is there under the
        name as written, and leaving it out lost a picture (a chart, the comments of a sheet) that was read before."""
        data = _xlsx_with_parts()
        expected = convert(data, "a.xlsx")

        result = convert(builder.zip_with(data, edit={"xl/drawings/_rels/drawing1.xml.rels": lambda xml: xml.replace(
            'Target="/xl/media/image1.png"', 'Target="/xl/media/image1.png" TargetMode="External"')}), "a.xlsx")

        assert len(result.images) == 1 and result.text == expected.text and result.warnings == expected.warnings

    @pytest.mark.parametrize("target", ["../media/im%61ge1.png", "/xl/media/im%61ge1.png"])
    def test_xlsx_linked_picture_is_not_the_part_its_address_decodes_to(self, target):
        """The address of a linked picture was percent-decoded like the target of a part, and the picture was read from the
        package."""
        data = _xlsx_with_parts()
        expected = convert(data, "a.xlsx")
        assert len(expected.images) == 1 and "[Image]" in expected.text

        result = convert(builder.zip_with(data, edit={"xl/drawings/_rels/drawing1.xml.rels": lambda xml: xml.replace(
            'Target="/xl/media/image1.png"', f'Target="{target}" TargetMode="External"')}), "a.xlsx")

        assert result.images == [] and "[Image]" not in result.text
        assert result.text == expected.text.replace("[Image]\n\n", "")
        assert result.warnings == ["Pictures and charts of a sheet that have no relationship information were left out."]

    def test_xlsx_external_relationship_is_not_decoded_into_a_sheet(self):
        data = builder.zip_with(_xlsx_with_parts(), edit={"xl/_rels/workbook.xml.rels": lambda xml: xml.replace(
            'Target="/xl/worksheets/sheet2.xml"', 'Target="xl/worksheets/sh%65et2.xml" TargetMode="External"')})

        result = convert(data, "a.xlsx")

        assert len(result.sections) == 1 and "second sheet" not in result.text

    def test_pptx_link_address_is_kept_as_written(self):
        """The address of a link out of the deck, joined to the folder of the slide and decoded, is the name of a member of
        the archive: decoded like the target of a part, the link went to `…/page`."""
        from pptx import Presentation
        from pptx.util import Inches

        presentation = Presentation(BytesIO(builder.pptx_with_image()))
        box = presentation.slides[0].shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1))
        run = box.text_frame.paragraphs[0].add_run()
        run.text = "linked words"
        run.hyperlink.address = "https://example.com/p%61ge"
        buffer = BytesIO()
        presentation.save(buffer)
        data = builder.zip_with(buffer.getvalue(), add={"ppt/slides/https:/example.com/page": "not a part"})

        result = convert(data, "a.pptx", ConvertOptions(keep_links=True))

        assert "[linked words](https://example.com/p%61ge)" in result.text
        assert len(result.images) == 1 and result.warnings == []


_NOTE_PICTURE_RELATIONSHIP =f'<Relationship Id="rIdImg" Type="{_RELATIONSHIPS_NS}/image" Target="media/note.png"/>'
_OLE = ('<w:r><w:object xmlns:o="urn:schemas-microsoft-com:office:office">'
        '<o:OLEObject Type="Embed" ProgID="Excel.Sheet.12" ShapeID="s1"/></w:object></w:r>')


class TestDocxNoteWithOnlyUnwrittenContent:
    """Whether a footnote has content was decided by searching it for pictures and objects, while the footnote itself is written
    by the paragraph walk, which leaves out deleted, moved-away and hidden content, compatibility copies, linked pictures and
    objects that are not OLE objects. Such a footnote got a body mark `[^1]` and no definition. The decision now uses the same
    walk (`_DocxRenderer._written_notes`)."""

    @pytest.mark.parametrize("note", [
        pytest.param(f'<w:p><w:del w:id="5" w:author="a">{_PICTURE}</w:del></w:p>', id="deleted picture"),
        pytest.param(f'<w:p><w:moveFrom w:id="6" w:author="a">{_PICTURE}</w:moveFrom></w:p>', id="moved-away picture"),
        pytest.param("<w:p>" + _PICTURE.replace("<w:r>", "<w:r><w:rPr><w:vanish/></w:rPr>", 1) + "</w:p>", id="hidden picture"),
        pytest.param("<w:p><w:r><mc:AlternateContent><mc:Choice Requires='x'/><mc:Fallback>" + _PICTURE[5:-6]
                     + "</mc:Fallback></mc:AlternateContent></w:r></w:p>", id="picture only in the compatibility copy"),
        pytest.param("<w:p>" + _PICTURE.replace("r:embed='rIdImg'", "r:link='rIdImg'") + "</w:p>", id="linked picture"),
        pytest.param('<w:p><w:r><w:object><w:control r:id="rIdImg" w:name="x"/></w:object></w:r></w:p>', id="ActiveX control"),
        pytest.param(f'<w:p><w:del w:id="5" w:author="a">{_OLE}</w:del></w:p>', id="deleted OLE object"),
    ])
    @pytest.mark.parametrize("extract_images", [True, False])
    def test_footnote_with_nothing_that_is_written_writes_no_mark(self, note, extract_images):
        data = _docx_with_footnote(note, note_relationships=_NOTE_PICTURE_RELATIONSHIP,
                                   add={"word/media/note.png": builder.png_bytes(300, 200, seed=3)})

        assert convert(data, "a.docx", ConvertOptions(extract_images=extract_images)).text == "본문"

    def test_footnote_whose_picture_is_written_keeps_its_mark(self):
        data = _docx_with_footnote(f"<w:p>{_PICTURE}</w:p>", note_relationships=_NOTE_PICTURE_RELATIONSHIP,
                                   add={"word/media/note.png": builder.png_bytes(300, 200, seed=3)})

        result = convert(data, "a.docx")

        assert result.text.startswith("본문[^1]\n\n[^1]:") and len(result.images) == 1

    @pytest.mark.parametrize("extract_images", [True, False])
    def test_footnote_with_an_ole_object_keeps_its_mark_and_placeholder(self, extract_images):
        data = _docx_with_footnote(f"<w:p>{_OLE}</w:p>")

        result = convert(data, "a.docx", ConvertOptions(extract_images=extract_images))

        assert result.text == "본문[^1]\n\n[^1]: [OLE object]"

    def test_endnotes_follow_the_same_rule(self):
        body = ("<w:p>" + _run("본문") + '<w:r><w:endnoteReference w:id="1"/></w:r><w:r><w:endnoteReference w:id="2"/></w:r>'
                + '<w:r><w:endnoteReference w:id="3"/></w:r></w:p>')
        notes = (f'<w:endnotes {_NOTE_NAMESPACES}><w:endnote w:id="1"><w:p>'
                 f'<w:del w:id="5" w:author="a"><w:r><w:delText>지움</w:delText></w:r></w:del></w:p></w:endnote>'
                 f"<w:endnote w:id=\"2\"><w:p>{_run('미주')}</w:p></w:endnote></w:endnotes>")
        data = builder.zip_with(_docx_with_body(body), edit={
            "word/_rels/document.xml.rels": lambda xml: xml.replace(
                "</Relationships>", f'<Relationship Id="rIdEnd" Type="{_RELATIONSHIPS_NS}/endnotes" Target="endnotes.xml"/>'
                                    "</Relationships>"),
            "[Content_Types].xml": lambda xml: xml.replace(
                "</Types>", '<Override PartName="/word/endnotes.xml" '
                            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml"/></Types>'),
        }, add={"word/endnotes.xml": notes})

        assert convert(data, "a.docx").text == "본문[^endnote1]\n\n[^endnote1]: 미주"
