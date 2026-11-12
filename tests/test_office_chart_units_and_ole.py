"""Units of OOXML charts (text boxes inside the chart, c:userShapes; literal units in the value format) and placeholders for
DOCX/PPTX OLE objects."""

import pytest

from opendockit import ConvertOptions, convert
from tests import builder
from tests.test_hwp import _CHART_XML
from tests.test_office import (
    _CHART_NS,
    _OLE_FRAME,
    _RELATIONSHIPS_NS,
    _chart_series,
    _chart_xml,
    _convert_chart,
    _docx_inline,
    _docx_with_table,
    _pptx_with_chart,
    _replace_picture,
)


_CHART_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/chartDrawing"
_USER_SHAPES_TYPE = f"{_RELATIONSHIPS_NS}/chartUserShapes"


def _user_shapes(*boxes: list[str]) -> str:
    """Chart drawing part (c:userShapes). Takes a list of paragraphs per text box."""
    def box(paragraphs: list[str]) -> str:
        body = "".join(f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>" for text in paragraphs)
        return ("<cdr:relSizeAnchor><cdr:from><cdr:x>0.75</cdr:x><cdr:y>0.01</cdr:y></cdr:from><cdr:to><cdr:x>0.99</cdr:x>"
                '<cdr:y>0.08</cdr:y></cdr:to><cdr:sp macro="" textlink=""><cdr:nvSpPr><cdr:cNvPr id="2" name="TextBox 1"/>'
                '<cdr:cNvSpPr txBox="1"/></cdr:nvSpPr><cdr:spPr/>'
                f"<cdr:txBody><a:bodyPr/><a:lstStyle/>{body}</cdr:txBody></cdr:sp></cdr:relSizeAnchor>")

    return (f'<?xml version="1.0" encoding="UTF-8"?><c:userShapes xmlns:c="{_CHART_NS}" xmlns:cdr="{_CHART_DRAWING_NS}" '
            f'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">{"".join(box(item) for item in boxes)}'
            "</c:userShapes>")


def _with_user_shapes_reference(chart: str) -> str:
    return chart.replace("</c:chartSpace>", f'<c:userShapes xmlns:r="{_RELATIONSHIPS_NS}" r:id="rIdU1"/></c:chartSpace>')


def _chart_relationships(target: str) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
            f'relationships"><Relationship Id="rIdU1" Type="{_USER_SHAPES_TYPE}" Target="{target}"/></Relationships>')


_SALES_CHART = _chart_xml(f"<c:barChart>{_chart_series(0, '2024', ['120', '80'], ['서울', '부산'])}</c:barChart>",
                          title="지역별 매출")


class TestChartUserShapes:
    """Text boxes laid over a chart (c:userShapes → chart drawing part) were not read, so units such as '(단위: 억원)' (unit: 100
    million won) were lost. They are put one per line under the title and axis titles. The relationship is one of the chart part
    (charts/_rels/chartN.xml.rels), so each format looks it up from the chart part."""

    def test_pptx_chart_text_box_is_kept_under_the_title(self):
        rels = "ppt/charts/_rels/chart1.xml.rels"
        data = builder.zip_with(_pptx_with_chart(), edit={
            "ppt/charts/chart1.xml": lambda _: _with_user_shapes_reference(_SALES_CHART),
            rels: lambda xml: xml.replace("</Relationships>", f'<Relationship Id="rIdU1" Type="{_USER_SHAPES_TYPE}" '
                                                              'Target="../drawings/drawing1.xml"/></Relationships>'),
            "[Content_Types].xml": lambda xml: xml.replace("</Types>", '<Override PartName="/ppt/drawings/drawing1.xml" '
                                                           'ContentType="application/vnd.openxmlformats-officedocument.'
                                                           'drawingml.chartshapes+xml"/></Types>'),
        }, add={"ppt/drawings/drawing1.xml": _user_shapes(["(단위: 억원)"], ["출처: 통계청", "2024년 기준"])})

        text = convert(data, "a.pptx").sections[0].text

        assert text == ("[Chart] 지역별 매출\n(단위: 억원)\n출처: 통계청\n2024년 기준\n\n| Category | 2024 |\n| --- | --- |\n"
                        "| 서울 | 120 |\n| 부산 | 80 |")

    def test_docx_chart_text_box_is_kept(self):
        objects = (f'<w:p><w:r>{_docx_inline(f'<c:chart xmlns:c="{_CHART_NS}" xmlns:r="{_RELATIONSHIPS_NS}" r:id="rIdChart"/>',
                                             _CHART_NS)}</w:r></w:p>')
        relationship = f'<Relationship Id="rIdChart" Type="{_RELATIONSHIPS_NS}/chart" Target="charts/chart1.xml"/></Relationships>'
        data = builder.zip_with(
            _docx_with_table(),
            edit={"word/document.xml": lambda xml: xml.replace("<w:sectPr", objects + "<w:sectPr", 1),
                  "word/_rels/document.xml.rels": lambda xml: xml.replace("</Relationships>", relationship)},
            add={"word/charts/chart1.xml": _with_user_shapes_reference(_SALES_CHART),
                 "word/charts/_rels/chart1.xml.rels": _chart_relationships("../drawings/drawing1.xml"),
                 "word/drawings/drawing1.xml": _user_shapes(["(단위: 억원)"])})

        text = convert(data, "a.docx").text

        assert "[Chart] 지역별 매출\n(단위: 억원)\n\n| Category | 2024 |" in text

    def test_hwpx_chart_text_box_is_kept(self):
        body = ('<hp:p><hp:run><hp:switch><hp:case hp:required-namespace="http://www.hancom.co.kr/hwpml/2016/ooxmlchart">'
                '<hp:chart chartIDRef="Chart/chart1.xml"/></hp:case></hp:switch></hp:run></hp:p>')
        data = builder.zip_with(builder.hwpx_bytes(body), add={
            "Chart/chart1.xml": _with_user_shapes_reference(_CHART_XML),
            "Chart/_rels/chart1.xml.rels": _chart_relationships("../Chart/drawing1.xml"),
            "Chart/drawing1.xml": _user_shapes(["(단위: 대)"])})

        result = convert(data, "a.hwpx")

        assert result.text.startswith("[Chart] 판매\n(단위: 대)\n\n| Category | 판매량 |")

    def test_broken_user_shapes_relationship_keeps_the_chart(self):
        """With no relationship or a broken drawing part, only the text box is dropped and the chart stays as it is."""
        text = _convert_chart(_with_user_shapes_reference(_SALES_CHART))

        assert text.startswith("[Chart] 지역별 매출\n\n| Category | 2024 |")


class TestChartValueUnits:
    """The literal unit in the display format of the value cache (`#,##0"억원"`) was not read, so 1200 came out without its unit.
    Only simple number formats made of a unit, digit grouping, thousands scaling (`#,##0,`) and decimal places are followed; a
    format without a literal unit, and other formats such as conditions and dates, leave the value as is."""

    @pytest.mark.parametrize(("format_code", "value", "expected"), [
        ('#,##0"억원"', "1200", "1,200억원"),
        ('0"명"', "35", "35명"),
        ('0.00"배"', "1.5", "1.50배"),
        ('#,##0.##"톤"', "1234.5", "1,234.5톤"),
        ('#,##0.0"%p"', "1.25", "1.3%p"),
        ('"₩"#,##0', "1500", "₩1,500"),
        ("[$₩-412]#,##0", "1500", "₩1,500"),
        ('#,##0,"천원"', "1234567", "1,235천원"),
        ('_-* #,##0"원"_-', "1000", "1,000원"),
        ('#,##0"억원"', "-300", "-300억원"),
        ('#,##0"억원";"△"#,##0"억원"', "-300", "△300억원"),
        ("#,##0\\명", "12", "12명"),
        ("#,##0", "1200", "1200"),
        ("General", "1200", "1200"),
        ('[>=1000]#,##0"천";0', "1200", "1200"),
        ('yyyy"년"', "46023", "46023"),
        ('#,##0"억원"', "해당 없음", "해당 없음"),
    ])
    def test_literal_units_of_the_value_format_are_kept(self, format_code, value, expected):
        series = _chart_series(0, "매출", [value], ["서울"]).replace(
            "<c:numCache>", f"<c:numCache><c:formatCode>{format_code}</c:formatCode>")

        text = _convert_chart(_chart_xml(f"<c:barChart>{series}</c:barChart>"))

        assert text.endswith(f"| 서울 | {expected} |")

    def test_point_level_format_with_a_unit_applies_to_that_point_only(self):
        series = _chart_series(0, "값", ["5", "5"], ["A", "B"]).replace(
            '<c:numCache><c:ptCount val="2"/><c:pt idx="0">',
            '<c:numCache><c:ptCount val="2"/><c:pt idx="0" formatCode="0&quot;개&quot;">')

        text = _convert_chart(_chart_xml(f"<c:barChart>{series}</c:barChart>"))

        assert text.endswith("| A | 5개 |\n| B | 5 |")


class TestChartNegativeSectionWithoutSign:
    """With a format whose negative section has no character showing that the value is negative (`#,##0"억원";[Red]#,##0"억원"` —
    only the red colour marks a negative), -300 came out as `300억원` and a loss read as a profit. `-` is added, as for Excel
    cells (`spreadsheet._format_section`)."""

    @pytest.mark.parametrize(("format_code", "expected"), [
        ('#,##0"억원";[Red]#,##0"억원"', "-300억원"),
        ('#,##0"억원";#,##0"억원"', "-300억원"),
        ('#,##0"억원";[Red]#,##0 "억원"', "-300 억원"),
        ('#,##0"억원";', "-300억원"),
        ('#,##0"억원";[Red]-#,##0"억원"', "-300억원"),
        ('#,##0"억원";(#,##0"억원")', "(300억원)"),
        ('#,##0"억원";"△"#,##0"억원"', "△300억원"),
    ])
    def test_negative_value_keeps_its_sign(self, format_code, expected):
        series = _chart_series(0, "영업이익", ["-300"], ["2023"]).replace(
            "<c:numCache>", f"<c:numCache><c:formatCode>{format_code}</c:formatCode>")

        text = _convert_chart(_chart_xml(f"<c:barChart>{series}</c:barChart>"))

        assert text.endswith(f"| 2023 | {expected} |")


def _labelled_chart(series_labels: str = "", plot_labels: str = "", value_axis: str = "", cache_format: str = "General") -> str:
    """Bar chart whose value cache format is `cache_format`. Data labels of the series/chart (c:dLbls) and a value axis (c:valAx)
    can be added."""
    series = _chart_series(0, "매출", ["1200"], ["2024"]).replace(
        "<c:numCache>", f"<c:numCache><c:formatCode>{cache_format}</c:formatCode>").replace("<c:cat>", f"{series_labels}<c:cat>")
    axes = ('<c:catAx><c:axId val="1"/><c:scaling/><c:delete val="0"/><c:axPos val="b"/><c:crossAx val="2"/></c:catAx>'
            f'<c:valAx><c:axId val="2"/><c:scaling/>{value_axis}<c:axPos val="l"/><c:crossAx val="1"/></c:valAx>')
    plot = f'<c:barChart><c:barDir val="col"/>{series}{plot_labels}<c:axId val="1"/><c:axId val="2"/></c:barChart>'
    return _chart_xml(plot + axes)


def _data_labels(format_code: str, *, linked: str = "0", show_value: str = "1") -> str:
    return (f'<c:dLbls><c:numFmt formatCode="{format_code}" sourceLinked="{linked}"/><c:showLegendKey val="0"/>'
            f'<c:showVal val="{show_value}"/><c:showCatName val="0"/><c:showSerName val="0"/><c:showPercent val="0"/>'
            '<c:showBubbleSize val="0"/></c:dLbls>')


_UNIT = "#,##0&quot;억원&quot;"


class TestChartUnitsOnLabelsAndValueAxis:
    """In a chart whose unit is not in the value cache format (General) but only in the data labels (c:dLbls/c:numFmt) or the value
    axis (c:valAx/c:numFmt), 1200 came out without its unit. Only when the value cache format is General, the format of visible
    labels not linked to the source (sourceLinked=0) is used, then that of a visible value axis. The value axis format of a 100%
    stacked chart (`0%`) is the scale of the stacked ratio and is not used."""

    @pytest.mark.parametrize(("chart", "expected"), [
        (_labelled_chart(plot_labels=_data_labels(_UNIT)), "1,200억원"),
        (_labelled_chart(series_labels=_data_labels(_UNIT), plot_labels=_data_labels("0&quot;원&quot;")), "1,200억원"),
        (_labelled_chart(value_axis=f'<c:delete val="0"/><c:numFmt formatCode="{_UNIT}" sourceLinked="0"/>'), "1,200억원"),
        (_labelled_chart(plot_labels=_data_labels("0.0&quot;조&quot;"),
                         value_axis=f'<c:delete val="0"/><c:numFmt formatCode="{_UNIT}" sourceLinked="0"/>'), "1200.0조"),
    ], ids=["plot-labels", "series-labels-first", "value-axis", "labels-before-axis"])
    def test_unit_of_labels_or_value_axis_is_used(self, chart, expected):
        text = _convert_chart(chart)

        assert text.endswith(f"| 2024 | {expected} |")

    @pytest.mark.parametrize("chart", [
        _labelled_chart(plot_labels=_data_labels(_UNIT, linked="1")),
        _labelled_chart(plot_labels=_data_labels(_UNIT, show_value="0")),
        _labelled_chart(value_axis=f'<c:delete val="0"/><c:numFmt formatCode="{_UNIT}" sourceLinked="1"/>'),
        _labelled_chart(value_axis=f'<c:delete val="1"/><c:numFmt formatCode="{_UNIT}" sourceLinked="0"/>'),
        _labelled_chart(value_axis='<c:delete val="0"/><c:numFmt formatCode="0%" sourceLinked="0"/>').replace(
            '<c:barDir val="col"/>', '<c:barDir val="col"/><c:grouping val="percentStacked"/>'),
    ], ids=["labels-linked-to-source", "labels-without-values", "axis-linked-to-source", "deleted-axis",
            "percent-stacked-axis"])
    def test_linked_or_hidden_formats_are_ignored(self, chart):
        text = _convert_chart(chart)

        assert text.endswith("| 2024 | 1200 |")

    def test_value_format_of_the_cache_wins(self):
        """Deliberate: when the cache format is not General it is the format of the source cell, so it comes before label and axis
        formats."""
        chart = _labelled_chart(plot_labels=_data_labels(_UNIT), cache_format='#,##0"톤"')

        text = _convert_chart(chart)

        assert text.endswith("| 2024 | 1,200톤 |")


_DOCX_OLE = ('<w:p><w:r><w:t>붙여 넣은 표:</w:t></w:r></w:p><w:p><w:r><w:object xmlns:v="urn:schemas-microsoft-com:vml" '
             'xmlns:o="urn:schemas-microsoft-com:office:office" w:dxaOrig="4000" w:dyaOrig="2000">'
             '<v:shape id="_x0000_i1025" style="width:200pt;height:100pt">{preview}</v:shape>'
             '<o:OLEObject Type="Embed" ProgID="Excel.Sheet.12" ShapeID="_x0000_i1025" DrawAspect="Content" ObjectID="_1" '
             'r:id="rIdOle"/></w:object></w:r></w:p><w:p><w:r><w:t>합계는 100이다.</w:t></w:r></w:p>')


def _docx_with_ole(*, preview: bool) -> bytes:
    """w:object of a pasted Excel sheet (preview v:imagedata + o:OLEObject). The object part is empty (it is not read)."""
    imagedata = '<v:imagedata r:id="rIdPreview" o:title=""/>' if preview else ""
    relationships = (f'<Relationship Id="rIdPreview" Type="{_RELATIONSHIPS_NS}/image" Target="media/preview.png"/>'
                     f'<Relationship Id="rIdOle" Type="{_RELATIONSHIPS_NS}/package" Target="embeddings/sheet.xlsx"/>'
                     "</Relationships>")
    return builder.zip_with(
        _docx_with_table(),
        edit={"word/document.xml": lambda xml: xml.replace("<w:sectPr", _DOCX_OLE.format(preview=imagedata) + "<w:sectPr", 1),
              "word/_rels/document.xml.rels": lambda xml: xml.replace("</Relationships>", relationships),
              "[Content_Types].xml": lambda xml: xml.replace("</Types>", '<Default Extension="png" ContentType="image/png"/>'
                                                             '<Default Extension="xlsx" ContentType="application/vnd.'
                                                             'openxmlformats-officedocument.spreadsheetml.sheet"/></Types>')},
        add={"word/media/preview.png": builder.png_bytes(), "word/embeddings/sheet.xlsx": "x"})


def _pptx_with_ole(*, preview: bool) -> bytes:
    def frame(picture: str) -> str:
        return _OLE_FRAME.format(ole='<p:oleObj name="Worksheet" r:id="rId9" progId="Excel.Sheet.12"><p:embed/>'
                                     f"{picture if preview else ''}</p:oleObj>")

    return builder.zip_with(builder.pptx_with_image(), edit={"ppt/slides/slide1.xml": _replace_picture(frame)})


_OLE_WARNING = "The contents of 1 OLE object(s) (pasted tables, charts, etc.) could not be converted."


class TestOfficeOleObjectPlaceholders:
    """With picture extraction off (or with no preview picture), OLE objects of DOCX/PPTX vanished with neither a placeholder nor a
    warning. As for HWP/HWPX, `[OLE object]` and a count warning are left. An object whose preview picture was registered stays
    a picture (docs/decisions/office.md, OLE row)."""

    def test_docx_ole_object_leaves_a_mark_when_images_are_off(self):
        result = convert(_docx_with_ole(preview=True), "a.docx", ConvertOptions(extract_images=False))

        assert "붙여 넣은 표:\n\n[OLE object]\n\n합계는 100이다." in result.text
        assert _OLE_WARNING in result.warnings

    def test_docx_ole_object_without_a_preview_leaves_a_mark(self):
        result = convert(_docx_with_ole(preview=False), "a.docx")

        assert "붙여 넣은 표:\n\n[OLE object]\n\n합계는 100이다." in result.text
        assert _OLE_WARNING in result.warnings

    def test_docx_ole_preview_is_still_a_picture_when_images_are_on(self):
        result = convert(_docx_with_ole(preview=True), "a.docx")

        assert "붙여 넣은 표:\n\n[Image]\n\n합계는 100이다." in result.text
        assert len(result.images) == 1
        assert _OLE_WARNING not in result.warnings

    def test_pptx_ole_object_leaves_a_mark_when_images_are_off(self):
        result = convert(_pptx_with_ole(preview=True), "a.pptx", ConvertOptions(extract_images=False))

        assert result.text == "## Slide 1: 구성도\n\n[OLE object]"
        assert result.warnings == [_OLE_WARNING]

    def test_pptx_ole_object_without_a_preview_leaves_a_mark(self):
        result = convert(_pptx_with_ole(preview=False), "a.pptx")

        assert result.text == "## Slide 1: 구성도\n\n[OLE object]"
        assert result.warnings == [_OLE_WARNING]
