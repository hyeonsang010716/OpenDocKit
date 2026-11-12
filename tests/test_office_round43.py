"""Office fixes of review round 43: long PPTX tables, XLSX parts not used in the result, workbooks with many sheets, LibreOffice XLS
chart sheets."""

from io import BytesIO
import struct
from time import perf_counter

from openpyxl import Workbook
from openpyxl.chart import BarChart, Reference
from openpyxl.comments import Comment
from openpyxl.drawing.image import Image
import pytest

from opendockit import ConvertOptions, convert
from tests import builder


_SLIDE = "ppt/slides/slide1.xml"


def _pptx_with_table(rows: int) -> bytes:
    cells = "".join(
        "<a:tr h=\"10\">"
        + "".join(f"<a:tc><a:txBody><a:bodyPr/><a:p><a:r><a:t>{row}-{col}</a:t></a:r></a:p></a:txBody></a:tc>" for col in range(3))
        + "</a:tr>"
        for row in range(rows)
    )
    frame = (
        "<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id=\"50\" name=\"tb\"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>"
        "<p:xfrm><a:off x=\"0\" y=\"0\"/><a:ext cx=\"10\" cy=\"10\"/></p:xfrm><a:graphic>"
        "<a:graphicData uri=\"http://schemas.openxmlformats.org/drawingml/2006/table\"><a:tbl><a:tblGrid>"
        + "<a:gridCol w=\"10\"/>" * 3
        + f"</a:tblGrid>{cells}</a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
    )
    return builder.zip_with(builder.pptx_bytes(), {_SLIDE: lambda xml: xml.replace("</p:spTree>", frame + "</p:spTree>")})


class TestPptxTableRows:
    def test_long_table_keeps_every_row(self):
        data = _pptx_with_table(300)

        text = convert(data, "a.pptx").text

        assert "| 0-0 | 0-1 | 0-2 |" in text
        assert "| 299-0 | 299-1 | 299-2 |" in text

    @pytest.mark.timing
    def test_many_rows_take_linear_time(self):
        """python-pptx's `table.rows` rebuilds `tr_lst` from the start for every row, which took time quadratic in the row count
        (21 s for 8,000 rows). With 8 times the rows the time should be a little over 8 times (over 40 times if quadratic)."""
        def seconds(rows: int) -> float:
            data = _pptx_with_table(rows)
            started = perf_counter()
            convert(data, "a.pptx")
            return perf_counter() - started

        small, large = seconds(500), seconds(4_000)

        assert large < small * 20


def _xlsx_with_chart_and_note(*, chart_sheet: bool = False) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "실적"
    for row in (["분기", "매출"], ["1분기", 120], ["2분기", 150]):
        sheet.append(row)
    sheet["B3"].comment = Comment("신규 2명 포함", "홍길동")
    chart = BarChart()
    chart.title = "분기 매출"
    chart.add_data(Reference(sheet, min_col=2, min_row=1, max_row=3), titles_from_data=True)
    chart.set_categories(Reference(sheet, min_col=1, min_row=2, max_row=3))
    if chart_sheet:
        workbook.create_chartsheet("차트").add_chart(chart)
    else:
        sheet.add_chart(chart, "D2")
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


class TestPartsTheConverterReadsItself:
    """Charts, drawings and comments are read by the converter directly from the ZIP. When openpyxl parsed them separately and
    rejected a single value, the whole workbook vanished as 'corrupted' (chart legendPos val="right", comment authorId="x")."""

    @pytest.mark.parametrize("chart_sheet", [False, True])
    def test_chart_with_an_odd_enumerated_value_does_not_reject_the_workbook(self, chart_sheet):
        data = builder.zip_with(_xlsx_with_chart_and_note(chart_sheet=chart_sheet),
                                {"xl/charts/chart1.xml": lambda xml: xml.replace('legendPos val="r"', 'legendPos val="right"')})

        result = convert(data, "a.xlsx")

        assert "| 2분기 | 150 |" in result.text
        assert "분기 매출" in result.text

    def test_note_with_an_odd_author_number_does_not_reject_the_workbook(self):
        data = builder.zip_with(_xlsx_with_chart_and_note(),
                                {"xl/comments/comment1.xml": lambda xml: xml.replace('authorId="0"', 'authorId="x"')})

        result = convert(data, "a.xlsx")

        assert "| 2분기 | 150 |" in result.text
        assert "신규 2명 포함" in result.text

    def test_picture_with_an_odd_shape_value_does_not_reject_the_workbook(self):
        workbook = Workbook()
        workbook.active.append(["분기", "매출"])
        workbook.active.add_image(Image(BytesIO(builder.png_bytes(seed=3))), "C2")
        buffer = BytesIO()
        workbook.save(buffer)
        data = builder.zip_with(buffer.getvalue(),
                                {"xl/drawings/drawing1.xml": lambda xml: xml.replace('prst="rect"', 'prst="odd"')})

        result = convert(data, "a.xlsx")

        assert "| 분기 | 매출 |" in result.text
        assert len(result.images) == 1

    def test_sound_workbook_reads_as_before(self):
        result = convert(_xlsx_with_chart_and_note(), "a.xlsx")

        assert "| 2분기 | 150 |" in result.text
        assert "분기 매출" in result.text
        assert "신규 2명 포함" in result.text
        assert not result.warnings


class TestManySheets:
    def test_workbook_relationships_are_not_read_again_for_every_sheet(self, monkeypatch):
        """To find comment authors (persons) the workbook relationships file (one entry per sheet) was parsed again for every sheet,
        taking time quadratic in the sheet count (8.9 s for 3,200 sheets). Timing fluctuates because openpyxl's own share is mixed
        in, so the number of re-reads is counted — it must not depend on the sheet count."""
        from opendockit.extractors import spreadsheet

        def reads(sheets: int) -> int:
            workbook = Workbook()
            for index in range(sheets):
                workbook.create_sheet(f"s{index}").append(["a", index])
            buffer = BytesIO()
            workbook.save(buffer)
            sources: list[str] = []
            related = spreadsheet._related
            monkeypatch.setattr(spreadsheet, "_related",
                                lambda archive, names, source, kind=None: sources.append(source) or related(archive, names, source, kind))
            convert(buffer.getvalue(), "a.xlsx", ConvertOptions(max_chars=None))
            monkeypatch.setattr(spreadsheet, "_related", related)
            return sources.count("xl/workbook.xml")

        few, many = reads(3), reads(300)

        assert many == few


def _embedded_chart() -> bytes:
    """Chart placed on a sheet: OBJ (object type 5) followed by a chart substream (BOF type 0x20 … EOF)."""
    chart_object = builder.xls_record(0x005D, struct.pack("<HHHHI", 0x15, 18, 5, 1, 0) + bytes(10))
    begin = builder.xls_record(0x0809, struct.pack("<HHHHII", 0x0600, 0x0020, 0x0DBB, 0x07CC, 0, 6))
    return chart_object + begin + builder.xls_record(0x000A)


class TestXlsChartSheetSavedAsAWorksheet:
    """LibreOffice saves an XLS chart sheet as BOUNDSHEET type 0 (ordinary sheet) + a chart placed on the sheet. Only type 2 was
    treated as a chart sheet, so that sheet was dropped without a warning."""

    def test_empty_sheet_holding_only_a_chart_is_reported_like_a_chart_sheet(self):
        from opendockit.extractors import spreadsheet

        data = builder.xls_bytes([("데이터", [["월", "매출"]], b""), ("차트1", [[]], _embedded_chart())])

        extraction = spreadsheet.extract_xls(data, ConvertOptions())

        assert extraction.warnings == ["Chart sheet '차트1' was not read "
                                       "(XLS charts are not converted to value tables — saving as XLSX converts them)."]
        assert [section.title for section in extraction.sections if section.text] == ["데이터"]

    def test_chart_placed_next_to_a_table_is_left_as_before(self):
        """An XLS chart on a sheet that has a table gives no warning, as before (Known accepted gap: XLS charts are not
        converted)."""
        from opendockit.extractors import spreadsheet

        data = builder.xls_bytes([("데이터", [["월", "매출"]], _embedded_chart())])

        extraction = spreadsheet.extract_xls(data, ConvertOptions())

        assert extraction.warnings == []
