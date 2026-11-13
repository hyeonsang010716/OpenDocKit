"""Line breaks inside DrawingML paragraphs (a:br, Shift+Enter): SmartArt items, text boxes inside a chart (c:userShapes), titles of
new charts (chartex), and text of shapes pasted into DOCX. Only a:t was joined, so '2023' + '1,500' ran together as '20231,500'."""

from lxml import etree

from opendockit import convert
from opendockit.extractors._charts import render_chart, render_chartex, render_smartart
from tests import builder
from tests.test_office import (
    _CHART_NS,
    _chart_series,
    _chart_xml,
    _chartex_xml,
    _docx_with_body,
    _pptx_with_smartart,
)


_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_DIAGRAM_NS = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
_CHART_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/chartDrawing"
_BROKEN = "<a:p><a:r><a:t>2023</a:t></a:r><a:br/><a:r><a:t>1,500</a:t></a:r></a:p>"


def _smartart(*bodies: str) -> str:
    points = "".join(f'<dgm:pt modelId="{index}"><dgm:t><a:bodyPr/>{body}</dgm:t></dgm:pt>'
                     for index, body in enumerate(bodies, start=1))
    links = "".join(f'<dgm:cxn modelId="{10 + index}" srcId="0" destId="{index}" srcOrd="{index}"/>'
                    for index in range(1, len(bodies) + 1))
    return (f'<?xml version="1.0" encoding="UTF-8"?><dgm:dataModel xmlns:dgm="{_DIAGRAM_NS}" xmlns:a="{_A_NS}"><dgm:ptLst>'
            f'<dgm:pt modelId="0" type="doc"><dgm:t><a:bodyPr/><a:p/></dgm:t></dgm:pt>{points}</dgm:ptLst>'
            f"<dgm:cxnLst>{links}</dgm:cxnLst></dgm:dataModel>")


class TestLineBreaksInsideDrawingParagraphs:
    """a:br is a line break. Where the text is written on one line (SmartArt items, chart titles) it becomes a space; where each line
    becomes a block (text boxes inside a chart, DOCX shape text) the line is split — the same rule as XLSX text boxes
    (`spreadsheet._text_body`) and PPTX text boxes."""

    def test_smartart_item_with_a_line_break_keeps_its_words_apart(self):
        data = _smartart("<a:p><a:r><a:t>1단계</a:t></a:r><a:br/><a:r><a:t>기반 구축</a:t></a:r></a:p>", _BROKEN)

        text = render_smartart(etree.fromstring(data.encode()))

        assert text == "- 1단계 기반 구축\n- 2023 1,500"

    def test_smartart_line_break_in_a_presentation(self):
        data = builder.zip_with(_pptx_with_smartart(), edit={"ppt/diagrams/data1.xml": lambda _: _smartart(_BROKEN)})

        result = convert(data, "a.pptx")

        assert "- 2023 1,500" in result.text
        assert "20231,500" not in result.text

    def test_chart_text_box_line_break_splits_the_line(self):
        shapes = (f'<c:userShapes xmlns:c="{_CHART_NS}" xmlns:cdr="{_CHART_DRAWING_NS}" xmlns:a="{_A_NS}">'
                  f"<cdr:relSizeAnchor><cdr:sp><cdr:txBody><a:bodyPr/>{_BROKEN}</cdr:txBody></cdr:sp></cdr:relSizeAnchor>"
                  "</c:userShapes>")
        chart = _chart_xml(f"<c:barChart>{_chart_series(0, '값', ['1'], ['가'])}</c:barChart>", title="매출").replace(
            "</c:chartSpace>", '<c:userShapes xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
                               'r:id="rIdU1"/></c:chartSpace>')

        text = render_chart(etree.fromstring(chart.encode()), load_related=lambda _: shapes.encode())

        assert text.startswith("[Chart] 매출\n2023\n1,500\n\n")

    def test_chartex_title_line_break_is_a_space(self):
        chart = _chartex_xml("x", ["가"], ["1"]).replace(
            "<cx:tx><cx:txData><cx:v>x</cx:v></cx:txData></cx:tx>",
            f'<cx:tx><cx:rich xmlns:a="{_A_NS}"><a:bodyPr/>{_BROKEN}</cx:rich></cx:tx>')

        text = render_chartex(etree.fromstring(chart.encode()))

        assert text.startswith("[Chart] 2023 1,500\n")

    def test_docx_pasted_drawing_shape_line_break_splits_the_line(self):
        canvas_uri = "http://schemas.openxmlformats.org/drawingml/2006/lockedCanvas"
        body = ('<w:p><w:r><w:drawing><wp:inline><wp:extent cx="1" cy="1"/><wp:docPr id="1" name="x"/>'
                f'<a:graphic><a:graphicData uri="{canvas_uri}"><lc:lockedCanvas><a:sp><a:txBody><a:bodyPr/>{_BROKEN}'
                "</a:txBody></a:sp></lc:lockedCanvas></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "2023\n\n1,500"
