"""Charts and SmartArt inside DOCX table cells, and paragraph levels of PPTX speaker notes."""

from io import BytesIO

from opendockit import ConvertOptions, convert
from tests import builder
from tests.test_office import (
    _CHART_NS,
    _DIAGRAM_NS,
    _RELATIONSHIPS_NS,
    _chart_series,
    _chart_xml,
    _docx_inline,
    _docx_with_table,
    _smartart_data,
)


_CHART_INLINE = _docx_inline(f'<c:chart xmlns:c="{_CHART_NS}" xmlns:r="{_RELATIONSHIPS_NS}" r:id="rIdChart"/>', _CHART_NS)
_SMARTART_INLINE = _docx_inline(f'<dgm:relIds xmlns:dgm="{_DIAGRAM_NS}" xmlns:r="{_RELATIONSHIPS_NS}" r:dm="rIdDgm" '
                                'r:lo="rIdLo" r:qs="rIdQs" r:cs="rIdCs"/>', _DIAGRAM_NS)


def _docx_with_objects_in_a_table(*rows: list[str]) -> bytes:
    """DOCX with an appended table whose cells in `rows` each hold text or a chart/SmartArt placeholder
    (`_CHART_INLINE`, `_SMARTART_INLINE`)."""
    def cell(content: str) -> str:
        run = content if content.startswith("<w:drawing") else f"<w:t>{content}</w:t>"
        return f"<w:tc><w:p><w:r>{run}</w:r></w:p></w:tc>"

    table = "<w:tbl>" + "".join("<w:tr>" + "".join(cell(content) for content in row) + "</w:tr>" for row in rows) + "</w:tbl>"
    chart = _chart_xml(f"<c:barChart>{_chart_series(0, '매출', ['10', '20'], ['1분기', '2분기'])}"
                       f"{_chart_series(1, '이익', ['1', '2'], ['1분기', '2분기'])}</c:barChart>", title="분기 실적")
    relationships = (f'<Relationship Id="rIdChart" Type="{_RELATIONSHIPS_NS}/chart" Target="charts/chart1.xml"/>'
                     f'<Relationship Id="rIdDgm" Type="{_RELATIONSHIPS_NS}/diagramData" Target="diagrams/data1.xml"/>'
                     "</Relationships>")
    return builder.zip_with(_docx_with_table(), edit={
        "word/document.xml": lambda xml: xml.replace("<w:sectPr", table + "<w:sectPr", 1),
        "word/_rels/document.xml.rels": lambda xml: xml.replace("</Relationships>", relationships),
    }, add={"word/charts/chart1.xml": chart, "word/diagrams/data1.xml": _smartart_data()})


class TestDocxObjectsInsideTableCells:
    """A chart in a cell had its Markdown table text put into the cell and became a mess of `\\|` and `<br>`, and SmartArt levels
    were flattened because leading spaces inside a cell are removed. The chart value table is handed to the outer table like a
    table in a table (`markdown.TableGrid.embed`), and SmartArt levels are marked with `└ `."""

    def test_chart_keeps_its_rows_and_columns_and_smartart_keeps_its_levels(self):
        data = _docx_with_objects_in_a_table(["그래프", "설명"], [_CHART_INLINE, "매출이 2배 증가"],
                                             [_SMARTART_INLINE, "추진 절차"])

        text = convert(data, "a.docx", ConvertOptions(max_chars=None)).text

        assert ("<tr><td>[Chart] 분기 실적<br><table><tr><th>Category</th><th>매출</th><th>이익</th></tr>"
                "<tr><td>1분기</td><td>10</td><td>1</td></tr><tr><td>2분기</td><td>20</td><td>2</td></tr></table></td>"
                "<td>매출이 2배 증가</td></tr>") in text
        assert "<tr><td>- 과제 발의<br>└ - 재단 승인<br>- 설비 구축</td><td>추진 절차</td></tr>" in text
        assert "\\|" not in text

    def test_chart_in_a_one_column_box_stays_a_table_between_paragraphs(self):
        data = _docx_with_objects_in_a_table(["그래프"], [_CHART_INLINE])

        text = convert(data, "a.docx", ConvertOptions(max_chars=None)).text

        assert text.endswith("그래프\n\n[Chart] 분기 실적\n\n| Category | 매출 | 이익 |\n| --- | --- | --- |\n| 1분기 | 10 | 1 |\n"
                             "| 2분기 | 20 | 2 |")


class TestPptxSpeakerNoteLevels:
    """Speaker notes were read with `text.splitlines()`, so paragraph levels were lost. Leading spaces of a quote-block line are
    removed, so levels are marked with `└ ` as in a list inside a cell (no numbers are added — docs/decisions/office-lists.md,
    automatic numbers row)."""

    def test_lower_level_note_paragraphs_are_marked(self):
        from pptx import Presentation

        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        slide.shapes.title.text = "추이"
        notes = slide.notes_slide.notes_text_frame
        notes.text = "핵심 메시지"
        for text, level in (("세부 1", 1), ("근거", 2), ("맺음", 0)):
            paragraph = notes.add_paragraph()
            paragraph.text, paragraph.level = text, level
        buffer = BytesIO()
        presentation.save(buffer)

        result = convert(buffer.getvalue(), "a.pptx")

        assert "> Speaker notes: 핵심 메시지\n> └ 세부 1\n> └─ 근거\n> 맺음" in result.text

    def test_text_box_on_the_notes_page_is_part_of_the_notes(self):
        """Only the notes body placeholder was read, so a text box placed separately on the notes page (the shape in which
        LibreOffice exports notes) was dropped without a warning."""
        from copy import deepcopy

        from pptx import Presentation
        from pptx.util import Cm

        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        slide.shapes.title.text = "추이"
        slide.notes_slide.notes_text_frame.text = "핵심 메시지"
        box = slide.shapes.add_textbox(Cm(1), Cm(1), Cm(5), Cm(2))
        box.text_frame.text = "글상자 메모"
        slide.notes_slide.shapes._spTree.append(deepcopy(box._element))
        slide.shapes._spTree.remove(box._element)
        buffer = BytesIO()
        presentation.save(buffer)

        result = convert(buffer.getvalue(), "a.pptx")

        assert "> Speaker notes: 핵심 메시지\n> 글상자 메모" in result.text
        assert result.text.count("글상자 메모") == 1

    def test_field_only_text_box_on_the_notes_page_is_not_notes(self):
        """A deck that went through LibreOffice (a converted .ppt) has the notes-page number as an ordinary text box holding one
        slide-number field. It was read as notes text, so every such deck's notes ended with a `<number>` line."""
        from copy import deepcopy

        from lxml import etree
        from pptx import Presentation
        from pptx.util import Cm

        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        slide.shapes.title.text = "추이"
        slide.notes_slide.notes_text_frame.text = "핵심 메시지"
        box = slide.shapes.add_textbox(Cm(1), Cm(1), Cm(5), Cm(2))
        box.text_frame.text = "x"
        run = box._element.find(".//{http://schemas.openxmlformats.org/drawingml/2006/main}r")
        field = etree.fromstring(
            '<a:fld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" id="{B6F15528-21DE-4FAA-801E-634DDDAF4B2B}" '
            'type="slidenum"><a:rPr lang="en-US"/><a:t>&lt;number&gt;</a:t></a:fld>')
        run.getparent().replace(run, field)
        slide.notes_slide.shapes._spTree.append(deepcopy(box._element))
        slide.shapes._spTree.remove(box._element)
        buffer = BytesIO()
        presentation.save(buffer)

        result = convert(buffer.getvalue(), "a.pptx")

        assert "> Speaker notes: 핵심 메시지" in result.text
        assert "number" not in result.text


class TestPptxTableCellLevels:
    """Paragraphs of a PPTX table cell were joined without their level (a:pPr lvl), so sub-items were flattened. They are marked
    with `└ ` like DOCX/HWP cell lists."""

    def test_lower_level_paragraphs_in_a_cell_are_marked_like_docx_and_hwp(self):
        from pptx import Presentation
        from pptx.util import Cm

        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        slide.shapes.title.text = "과제별 추진 내용"
        table = slide.shapes.add_table(2, 2, Cm(1), Cm(4), Cm(20), Cm(6)).table
        table.cell(0, 0).text, table.cell(0, 1).text, table.cell(1, 0).text = "과제", "주요 내용", "A 과제"
        frame = table.cell(1, 1).text_frame
        for index, (level, text) in enumerate([(0, "설비 개선"), (1, "1호기 교체"), (2, "부품 발주"), (0, "인력 충원")]):
            paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
            paragraph.text, paragraph.level = text, level
        buffer = BytesIO()
        presentation.save(buffer)

        result = convert(buffer.getvalue(), "a.pptx")

        assert "| A 과제 | 설비 개선<br>└ 1호기 교체<br>└─ 부품 발주<br>인력 충원 |" in result.text
