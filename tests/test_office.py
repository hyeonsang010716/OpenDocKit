"""DOCX, PPTX, XLSX and CSV conversion."""

import base64
from collections.abc import Callable
from io import BytesIO
import re
import zipfile

import pytest

from opendockit import ConvertOptions, DocumentFormat, SectionKind, convert
from tests import builder


class TestDocx:
    @pytest.fixture(scope="class")
    @classmethod
    def result(cls):
        return convert(builder.docx_bytes(), "보고서.docx")

    def test_maps_heading_styles_to_markdown_headings(self, result):
        assert "# 1장 개요" in result.text
        assert "## 1.1 세부" in result.text

    def test_renders_bullet_and_numbered_lists(self, result):
        assert "- 사과\n- 배" in result.text
        assert "1. 첫째\n2. 둘째" in result.text

    def test_repeats_vertically_merged_cell_in_table(self, result):
        assert "| 과일 | 사과 | 10 |\n| 과일 | 배 | 20 |" in result.text

    def test_converts_omml_equation_to_latex(self, result):
        """Word equations are m:t, not w:t, so plain text extraction drops them entirely."""
        assert "공식: $\\frac{a}{b}x^2$" in result.text

    def test_collapses_spaces_and_keeps_title_metadata(self, result):
        assert "본문 첫 문단입니다." in result.text
        assert result.metadata["title"] == "분기 보고서"
        assert result.format is DocumentFormat.DOCX


class TestPptx:
    @pytest.fixture(scope="class")
    @classmethod
    def result(cls):
        return convert(builder.pptx_bytes(), "발표.pptx")

    def test_one_section_per_slide_with_title_heading(self, result):
        assert [section.kind for section in result.sections] == [SectionKind.SLIDE] * 3
        assert "## Slide 1: 사업 계획\n\n2026년 상반기" in result.text

    def test_renders_body_placeholder_as_nested_bullets_and_notes(self, result):
        assert "- 매출 성장\n  - 신규 고객 확보" in result.text
        assert "> Speaker notes: 여기서 수치를 강조" in result.text

    def test_renders_table_and_chart_data(self, result):
        assert "| 지역 | 매출 |\n| --- | --- |\n| 서울 | 100 |" in result.text
        assert "| 1분기 | 10.5 |" in result.text

    def test_picture_shape_without_image_data_does_not_reject_the_deck(self):
        """In a real file whose picture shape had no picture information (blipFill), python-pptx raised InvalidXmlError, which was
        outside the list of caught exceptions, so the whole document was rejected as a 'corrupted document'. Failing to read one
        picture is a problem of that picture only."""
        from io import BytesIO

        from pptx import Presentation

        presentation = Presentation(BytesIO(builder.pptx_with_image()))
        picture = next(shape for shape in presentation.slides[0].shapes if shape.shape_type == 13)
        picture._element.remove(picture._element.blipFill)
        buffer = BytesIO()
        presentation.save(buffer)

        result = convert(buffer.getvalue(), "a.pptx")

        assert result.text == "## Slide 1: 구성도"

    def test_shape_of_unrecognized_type_does_not_reject_the_deck(self):
        """For a shape with no geometry information (prstGeom, custGeom, txBox), python-pptx's `shape_type` raised NotImplementedError
        and the whole document was rejected as a 'corrupted document'. Whether a shape is a group is decided by its class, without
        asking `shape_type`."""
        import re

        def strip_geometry(xml):
            xml = xml.replace('<p:cNvSpPr txBox="1"/>', "<p:cNvSpPr/>", 1)
            return re.sub(r'<a:prstGeom prst="rect">.*?</a:prstGeom>', "", xml, count=1, flags=re.DOTALL)

        data = builder.zip_with(_pptx_with_text_boxes("모양 정보 없는 도형의 글", "보통 글상자"),
                                edit={"ppt/slides/slide1.xml": strip_geometry})

        result = convert(data, "a.pptx")

        assert result.text == "## Slide 1\n\n모양 정보 없는 도형의 글\n\n보통 글상자"

    def test_table_repeats_vertically_merged_cell(self):
        """The same merge rule as the other formats (repeat vertically, first cell horizontally). Cells covered by a merge used to be
        left empty, which lost the category."""
        from io import BytesIO

        from pptx import Presentation
        from pptx.util import Inches

        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        table = slide.shapes.add_table(3, 3, Inches(1), Inches(1), Inches(6), Inches(2)).table
        for row, values in enumerate([["분류", "항목", "값"], ["과일", "사과", "1"], ["", "배", "2"]]):
            for col, value in enumerate(values):
                table.cell(row, col).text = value
        table.cell(1, 0).merge(table.cell(2, 0))
        buffer = BytesIO()
        presentation.save(buffer)

        result = convert(buffer.getvalue(), "a.pptx")

        assert "| 과일 | 사과 | 1 |\n| 과일 | 배 | 2 |" in result.text

    def test_slide_show_deck_is_read_like_a_plain_one(self):
        """A show (.ppsx) and a template (.potx) differ only in the content type of the main part; the structure is the same.
        python-pptx rejected them by the content type name, which made them a 'corrupted document'."""
        data = builder.zip_with(_pptx_with_text_boxes("쇼 문서의 글"), edit={"[Content_Types].xml": lambda xml: xml.replace(
            "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
            "application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml")})

        result = convert(data, "a.ppsx")

        assert result.text == "## Slide 1\n\n쇼 문서의 글"

    def test_smartart_text_is_extracted_as_nested_list(self):
        """SmartArt text is not in the slide XML but in a separate part (ppt/diagrams/data*.xml), so it was dropped entirely
        (144 of 242 SmartArt texts missing in a public PPTX collection). It is rendered as a nested list following the parent-child
        connections."""
        result = convert(_pptx_with_smartart(), "a.pptx")

        assert "- 과제 발의\n  - 재단 승인\n- 설비 구축" in result.sections[0].text

    def test_chart_values_keep_all_their_digits(self):
        """Chart values were written with `:g`, which cut them at 6 significant digits (1234567890 → 1.23457e+09). Amount charts
        were read wrong."""
        from io import BytesIO

        from pptx import Presentation
        from pptx.chart.data import CategoryChartData
        from pptx.enum.chart import XL_CHART_TYPE
        from pptx.util import Inches

        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        chart_data = CategoryChartData()
        chart_data.categories = ["2025년", "2026년"]
        chart_data.add_series("매출", (1234567890, 15000000.5))
        slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(1), Inches(4), Inches(3), chart_data)
        buffer = BytesIO()
        presentation.save(buffer)

        result = convert(buffer.getvalue(), "a.pptx")

        assert "| 2025년 | 1234567890 |" in result.text
        assert "| 2026년 | 15000000.5 |" in result.text

    def test_scatter_chart_values_are_kept(self):
        """A scatter chart uses X/Y values (c:xVal, c:yVal) instead of categories (c:cat), so not a single value row came out."""
        from io import BytesIO

        from pptx import Presentation
        from pptx.chart.data import XyChartData
        from pptx.enum.chart import XL_CHART_TYPE
        from pptx.util import Inches

        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        chart_data = XyChartData()
        series = chart_data.add_series("온도-수율")
        for x, y in [(10, 55.5), (20, 61.2)]:
            series.add_data_point(x, y)
        slide.shapes.add_chart(XL_CHART_TYPE.XY_SCATTER, Inches(1), Inches(1), Inches(4), Inches(3), chart_data)
        buffer = BytesIO()
        presentation.save(buffer)

        result = convert(buffer.getvalue(), "a.pptx")

        assert "| Series | X | Y |\n| --- | --- | --- |\n| 온도-수율 | 10 | 55.5 |\n| 온도-수율 | 20 | 61.2 |" in result.text

    def test_combo_chart_keeps_series_of_every_plot(self):
        """A combo chart (bar + line) has two charts in the plot area. Only the first was read, so the line series (이익률) was dropped."""
        months = ["1월", "2월"]
        combo = _chart_xml(
            f"<c:barChart>{_chart_series(0, '매출', ['100', '200'], months)}</c:barChart>"
            f"<c:lineChart>{_chart_series(1, '이익률', ['0.1', '0.2'], months)}</c:lineChart>", title="월별 실적")
        data = builder.zip_with(_pptx_with_chart(), edit={"ppt/charts/chart1.xml": lambda _: combo})

        result = convert(data, "a.pptx")

        assert "[Chart] 월별 실적\n\n| Category | 매출 | 이익률 |\n| --- | --- | --- |\n| 1월 | 100 | 0.1 |\n| 2월 | 200 | 0.2 |" \
            in result.text

    def test_absurd_paragraph_level_does_not_build_a_huge_indent(self):
        """The paragraph level (lvl) was used as the indent as is, so a 28KB file with lvl=100000000 took 26 seconds and 1.4GB and was
        rejected for exceeding the limit. PowerPoint levels are 0–8. It is clamped to 8 as in DOCX."""
        from io import BytesIO

        from pptx import Presentation

        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.placeholders[1].text_frame.text = "본문"
        buffer = BytesIO()
        presentation.save(buffer)
        data = builder.zip_with(buffer.getvalue(), edit={
            "ppt/slides/slide1.xml": lambda xml: xml.replace("<a:p><a:r>", '<a:p><a:pPr lvl="100000"/><a:r>', 1)})

        result = convert(data, "a.pptx")

        assert f"{'  ' * 8}- 본문" in result.text
        assert len(result.text) < 200

    def test_notes_can_be_excluded(self):
        result = convert(builder.pptx_bytes(), options=ConvertOptions(include_speaker_notes=False))

        assert "Speaker notes" not in result.text


class TestXlsx:
    @pytest.fixture(scope="class")
    @classmethod
    def result(cls):
        return convert(builder.xlsx_bytes(), "매출.xlsx")

    def test_lifts_merged_title_row_and_formats_values(self, result):
        assert result.text.startswith("## Sheet: 매출\n\n2026년 매출 현황\n\n| 지역 | 지점 | 금액 | 마감일 |")
        assert "| 서울 | 강남 | 1200 | 2026-01-31 00:00:00 |" in result.text  # the openpyxl default format shows the time too (as Excel does)
        assert "| 서울 | 종로 | 0.3 | 2026-02-01 09:30:00 |" in result.text
        assert "| 부산 | 서면 | TRUE | |" in result.text

    def test_skips_hidden_sheet_with_warning(self, result):
        assert "숨김" not in result.text
        assert any("비밀" in warning for warning in result.warnings)

    def test_has_no_row_cap(self):
        """Rows are not cut by count. The only limit is the character count (a 2,390-row sheet was cut at 2,000 rows and lost 390)."""
        result = convert(_long_sheet(3_000), "a.xlsx")

        assert "| 3000 | 항목 3000 |" in result.text
        assert not result.exceeded

    def test_stops_reading_rows_once_the_character_limit_is_exceeded(self):
        """Past the limit, the remaining rows are not read. Even read to the end, a 130,000-row sheet would be rejected anyway."""
        result = convert(_long_sheet(20_000), "a.xlsx", ConvertOptions(max_chars=2_000))

        assert result.exceeded
        assert result.char_count < 10_000, "reading everything would exceed 300,000 characters"

    def test_formulas_without_stored_results_are_shown_as_formulas(self):
        """A file made by a library (never opened in Excel) has no stored formula results. Reading values only left those cells blank
        without a warning. The formula is shown instead, with a warning."""
        from io import BytesIO

        import openpyxl

        workbook = openpyxl.Workbook()
        workbook.active.append(["단가", "수량", "금액"])
        workbook.active.append([1000, 3, "=A2*B2"])
        buffer = BytesIO()
        workbook.save(buffer)

        result = convert(buffer.getvalue(), "a.xlsx")

        assert "| 1000 | 3 | =A2*B2 |" in result.text
        assert any("Formula results are not stored" in warning for warning in result.warnings)

    def test_formulas_with_stored_results_show_the_results(self):
        """A file saved by Excel holds the computed results. Then the value is written, not the formula."""
        from io import BytesIO

        import openpyxl

        workbook = openpyxl.Workbook()
        workbook.active.append(["단가", "수량", "금액"])
        workbook.active.append([1000, 3, "=A2*B2"])
        buffer = BytesIO()
        workbook.save(buffer)
        data = builder.zip_with(buffer.getvalue(), edit={
            "xl/worksheets/sheet1.xml": lambda xml: xml.replace("<f>A2*B2</f><v></v>", "<f>A2*B2</f><v>3000</v>")})

        result = convert(data, "a.xlsx")

        assert "| 1000 | 3 | 3000 |" in result.text
        assert not result.warnings

    def test_streamed_sheet_ignores_a_wrong_dimension(self, monkeypatch):
        """Large sheets are read by streaming, and openpyxl then trusts the <dimension> at the head of the sheet and reads only up to
        that range. In a file from a generator that writes the range wrong (A1:A1), only the first cell remained and the rest
        vanished without a warning."""
        from opendockit.extractors import spreadsheet

        monkeypatch.setattr(spreadsheet, "_FULL_LOAD_MAX_SHEET_BYTES", 0)
        data = builder.zip_with(_long_sheet(3), edit={
            "xl/worksheets/sheet1.xml": lambda xml: xml.replace('<dimension ref="A1:B4"/>', '<dimension ref="A1:A1"/>')})

        result = convert(data, "a.xlsx")

        assert "| 3 | 항목 3 |" in result.text

    @pytest.mark.parametrize("streamed", [False, True], ids=["full-load", "streamed"])
    def test_rows_are_never_wider_than_the_column_limit(self, monkeypatch, streamed):
        """One far column with formatting only (CV1) made every row be built out to that column (a 4.9KB file took 17 seconds and
        3.3GB at 2000 columns). Anything past the column limit (`max_sheet_cols`) is discarded anyway, so rows are built only up to
        limit + 1 cells (the extra one is for the exceeded warning)."""
        from io import BytesIO

        import openpyxl
        from openpyxl.styles import Font

        from opendockit.extractors import spreadsheet

        if streamed:
            monkeypatch.setattr(spreadsheet, "_FULL_LOAD_MAX_SHEET_BYTES", 0)
        widths = []
        original_collect = spreadsheet._collect_rows

        def recording_collect(rows, *args):
            def recorded():
                for row in rows:
                    widths.append(len(row))
                    yield row
            return original_collect(recorded(), *args)

        monkeypatch.setattr(spreadsheet, "_collect_rows", recording_collect)
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["이름", "값"])
        sheet.append(["a", 1])
        sheet["CV1"].font = Font(bold=True)
        sheet["ZZ3"] = "먼 값"
        buffer = BytesIO()
        workbook.save(buffer)

        result = convert(buffer.getvalue(), "a.xlsx", ConvertOptions(max_sheet_cols=10))

        assert "| a | 1 |" in result.text
        assert any("column 10" in warning for warning in result.warnings)
        assert max(widths) <= 11

    def test_streamed_row_keeps_cells_written_out_of_order(self, monkeypatch):
        """When the cells of a row are not in column order (A1 after C1), streaming took the row width only up to the last cell written
        and lost C1."""
        from opendockit.extractors import spreadsheet

        monkeypatch.setattr(spreadsheet, "_FULL_LOAD_MAX_SHEET_BYTES", 0)
        row = ('<row r="1"><c r="C1" t="inlineStr"><is><t>셋</t></is></c><c r="A1" t="inlineStr"><is><t>하나</t></is></c></row>'
               '<row r="2"><c r="A2" t="inlineStr"><is><t>a</t></is></c><c r="C2" t="inlineStr"><is><t>c</t></is></c></row>')
        data = builder.zip_with(_long_sheet(1), edit={
            "xl/worksheets/sheet1.xml": lambda xml: re.sub(r"<sheetData>.*</sheetData>", f"<sheetData>{row}</sheetData>",
                                                           xml, flags=re.S)})

        result = convert(data, "a.xlsx")

        assert "| 하나 | 셋 |\n| --- | --- |\n| a | c |" in result.text

    def test_huge_merged_range_is_read_without_expanding_it(self, monkeypatch):
        """In a 4.8KB file with just one merged range (A1:CV20000), openpyxl created a cell object for every covered position and took
        8.8 seconds and 800MB. When a merge covers too many cells, the sheet is read by streaming, which does not read merge
        information."""
        import openpyxl

        from opendockit.extractors import spreadsheet

        modes = []
        original_load = openpyxl.load_workbook

        def recording_load(*args, **kwargs):
            modes.append(kwargs.get("read_only"))
            return original_load(*args, **kwargs)

        monkeypatch.setattr(spreadsheet.openpyxl, "load_workbook", recording_load)
        data = builder.zip_with(_long_sheet(1), edit={
            "xl/worksheets/sheet1.xml": lambda xml: xml.replace(
                "</sheetData>", "</sheetData><mergeCells count='1'><mergeCell ref = 'A&#49;:CV20000'/></mergeCells>")})

        result = convert(data, "a.xlsx")

        assert "| 1 | 항목 1 |" in result.text
        assert modes == [True]

    def test_huge_hyperlink_range_is_read_without_expanding_it(self, monkeypatch):
        """openpyxl also creates a cell for every position a hyperlink range covers (one `A1:XFD1048576` took 20 seconds and 5GB).
        It is counted together with merges."""
        import openpyxl

        from opendockit.extractors import spreadsheet

        modes = []
        original_load = openpyxl.load_workbook

        def recording_load(*args, **kwargs):
            modes.append(kwargs.get("read_only"))
            return original_load(*args, **kwargs)

        monkeypatch.setattr(spreadsheet.openpyxl, "load_workbook", recording_load)
        data = builder.zip_with(_long_sheet(1), edit={
            "xl/worksheets/sheet1.xml": lambda xml: xml.replace(
                "</sheetData>", '</sheetData><hyperlinks><hyperlink ref="A1:Z30000" location="Sheet!A1"/></hyperlinks>')})

        result = convert(data, "a.xlsx")

        assert "| 1 | 항목 1 |" in result.text
        assert modes == [True]

    def test_sheet_listed_many_times_is_read_once(self):
        """When workbook.xml points to the same sheet several times, openpyxl read it again every time (a 44KB file took 23 seconds
        and 2.6GB)."""
        data = builder.zip_with(_long_sheet(2), edit={
            "xl/workbook.xml": lambda xml: re.sub(r"(<sheet [^>]*/>)", lambda match: match.group(1) * 50, xml)})

        result = convert(data, "a.xlsx")

        assert len(result.sections) == 1
        assert result.text.count("| 2 | 항목 2 |") == 1
        assert any("more than once" in warning for warning in result.warnings)

    def test_formula_check_still_runs_when_pictures_force_streaming(self, monkeypatch):
        """Even when the size of picture references forces streaming, the sheet is small, so the formula result check must not be
        skipped. Formula cells became blank without a warning."""
        from io import BytesIO

        import openpyxl

        from opendockit.extractors import spreadsheet

        monkeypatch.setattr(spreadsheet, "_picture_reference_bytes", lambda archive: 10**12)
        workbook = openpyxl.Workbook()
        workbook.active.append(["단가", "수량", "금액"])
        workbook.active.append([1000, 3, "=A2*B2"])
        buffer = BytesIO()
        workbook.save(buffer)

        result = convert(buffer.getvalue(), "a.xlsx")

        assert "| 1000 | 3 | =A2*B2 |" in result.text
        assert any("Formula results are not stored" in warning for warning in result.warnings)

    def test_full_load_does_not_create_cells_for_empty_positions(self, monkeypatch):
        """Walking a fully loaded sheet with iter_rows makes openpyxl create a new cell object for every empty position and attach it
        to the sheet. One far row or column with formatting only produced rows × columns cells. Only the cells in the file are read."""
        from io import BytesIO

        import openpyxl
        from openpyxl.styles import Font
        from openpyxl.worksheet.worksheet import Worksheet

        created = []
        original_cell = Worksheet.cell

        def counting_cell(self, *args, **kwargs):
            created.append(1)
            return original_cell(self, *args, **kwargs)

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["이름", "값"])
        sheet.append(["a", 1])
        sheet["CV1"].font = Font(bold=True)
        sheet["A3000"].font = Font(bold=True)
        buffer = BytesIO()
        workbook.save(buffer)
        monkeypatch.setattr(Worksheet, "cell", counting_cell)

        result = convert(buffer.getvalue(), "a.xlsx")

        assert "| a | 1 |" in result.text
        assert len(created) < 100


def _long_sheet(rows: int) -> bytes:
    from io import BytesIO

    import openpyxl

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["번호", "이름"])
    for number in range(1, rows + 1):
        sheet.append([number, f"항목 {number}"])
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


class TestCsv:
    def test_decodes_cp949_and_sniffs_delimiter(self):
        data = "이름;나이\n홍길동;30\n".encode("cp949")

        result = convert(data, "명단.csv")

        assert result.text == "| 이름 | 나이 |\n| --- | --- |\n| 홍길동 | 30 |"
        assert result.metadata["encoding"] == "cp949"

    def test_reads_tab_separated(self):
        result = convert(b"a\tb\n1\t2\n", "x.tsv")

        assert result.text == "| a | b |\n| --- | --- |\n| 1 | 2 |"


def _pptx_with_text_boxes(*texts: str) -> bytes:
    from io import BytesIO

    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    for index, text in enumerate(texts):
        slide.shapes.add_textbox(Inches(1), Inches(1 + index * 2), Inches(4), Inches(1)).text_frame.text = text
    buffer = BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


_DIAGRAM_NS = "http://schemas.openxmlformats.org/drawingml/2006/diagram"
_RELATIONSHIPS_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_CHART_NS = "http://schemas.openxmlformats.org/drawingml/2006/chart"


def _smartart_data() -> str:
    """SmartArt data part: document root → 과제 발의 (→ 재단 승인), 설비 구축. The layout-only point (pres) must be left out even though
    it has text."""
    main = "http://schemas.openxmlformats.org/drawingml/2006/main"

    def point(model_id: int, text: str = "", kind: str = "") -> str:
        body = f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>" if text else "<a:p/>"
        return f'<dgm:pt modelId="{model_id}"{kind}><dgm:t><a:bodyPr/>{body}</dgm:t></dgm:pt>'

    return (f'<?xml version="1.0" encoding="UTF-8"?><dgm:dataModel xmlns:dgm="{_DIAGRAM_NS}" xmlns:a="{main}"><dgm:ptLst>'
            + point(0, kind=' type="doc"') + point(1, "과제 발의") + point(2, "재단 승인") + point(3, "설비 구축")
            + point(9, "표시용", ' type="pres"') + "</dgm:ptLst><dgm:cxnLst>"
            '<dgm:cxn modelId="10" srcId="0" destId="1" srcOrd="0"/><dgm:cxn modelId="11" srcId="1" destId="2" srcOrd="0"/>'
            '<dgm:cxn modelId="12" srcId="0" destId="3" srcOrd="1"/><dgm:cxn modelId="13" type="presOf" srcId="1" destId="9"/>'
            "</dgm:cxnLst></dgm:dataModel>")


def _chart_xml(plots: str, title: str = "") -> str:
    """One c:chartSpace. `plots` is the c:barChart etc. that goes inside c:plotArea."""
    title_xml = (f"<c:title><c:tx><c:rich><a:bodyPr/><a:p><a:r><a:t>{title}</a:t></a:r></a:p></c:rich></c:tx></c:title>"
                 if title else "")
    return (f'<?xml version="1.0" encoding="UTF-8"?><c:chartSpace xmlns:c="{_CHART_NS}" '
            'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            f"<c:chart>{title_xml}<c:plotArea><c:layout/>{plots}</c:plotArea></c:chart></c:chartSpace>")


def _chart_series(order: int, name: str, values: list[str], categories: list[str] | None = None) -> str:
    def cache(tag: str, items: list[str]) -> str:
        points = "".join(f'<c:pt idx="{index}"><c:v>{item}</c:v></c:pt>' for index, item in enumerate(items))
        return f'<c:{tag}><c:ptCount val="{len(items)}"/>{points}</c:{tag}>'

    category_xml = f"<c:cat><c:strRef>{cache('strCache', categories)}</c:strRef></c:cat>" if categories else ""
    return (f'<c:ser><c:idx val="{order}"/><c:order val="{order}"/><c:tx><c:v>{name}</c:v></c:tx>{category_xml}'
            f"<c:val><c:numRef>{cache('numCache', values)}</c:numRef></c:val></c:ser>")


def _pptx_with_chart() -> bytes:
    """A slide with one bar chart. The chart XML is swapped in with `zip_with` (ppt/charts/chart1.xml)."""
    from io import BytesIO

    from pptx import Presentation
    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    chart_data = CategoryChartData()
    chart_data.categories = ["가"]
    chart_data.add_series("값", (1,))
    slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(1), Inches(4), Inches(3), chart_data)
    buffer = BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def _pptx_with_smartart() -> bytes:
    """python-pptx cannot create SmartArt. The diagram frame and the data part are inserted into the slide directly."""
    from io import BytesIO
    import zipfile

    diagram = _DIAGRAM_NS
    relationships = _RELATIONSHIPS_NS
    data = _smartart_data()
    frame = ('<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="99" name="다이어그램"/><p:cNvGraphicFramePr/><p:nvPr/>'
             '</p:nvGraphicFramePr><p:xfrm><a:off x="914400" y="1828800"/><a:ext cx="5486400" cy="2743200"/></p:xfrm>'
             f'<a:graphic><a:graphicData uri="{diagram}"><dgm:relIds xmlns:dgm="{diagram}" xmlns:r="{relationships}" '
             'r:dm="rIdDgm" r:lo="rIdLo" r:qs="rIdQs" r:cs="rIdCs"/></a:graphicData></a:graphic></p:graphicFrame>')

    source = zipfile.ZipFile(BytesIO(builder.pptx_with_image()))
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in source.namelist():
            content = source.read(name).decode("utf-8") if name.endswith((".xml", ".rels")) else source.read(name)
            if name == "ppt/slides/slide1.xml":
                content = content.replace("</p:spTree>", frame + "</p:spTree>")
            elif name == "ppt/slides/_rels/slide1.xml.rels":
                content = content.replace("</Relationships>", f'<Relationship Id="rIdDgm" Type="{relationships}/diagramData" '
                                          'Target="../diagrams/data1.xml"/></Relationships>')
            elif name == "[Content_Types].xml":
                content = content.replace("</Types>", '<Override PartName="/ppt/diagrams/data1.xml" ContentType='
                                          '"application/vnd.openxmlformats-officedocument.drawingml.diagramData+xml"/></Types>')
            archive.writestr(name, content)
        archive.writestr("ppt/diagrams/data1.xml", data)
    return buffer.getvalue()


class TestCsvWithBrokenQuotes:
    """One unclosed quote swallowed everything after it into a single cell, and when that cell exceeded the csv module's field size
    limit (131,072 characters) the whole file was rejected as a 'corrupted document'. From the point where the quote structure
    breaks, the rest is split by the delimiter only."""

    def test_unclosed_quote_keeps_the_following_rows(self):
        rows = ["이름,설명,값", '부품,"지름 5 규격,1', *(f"항목{number},설명 {number},{number}" for number in range(9_000))]

        result = convert("\n".join(rows).encode(), "data.csv", ConvertOptions(max_chars=None))

        assert '| 부품 | "지름 5 규격 | 1 |\n| 항목0 | 설명 0 | 0 |' in result.text
        assert result.text.endswith("| 항목8999 | 설명 8999 | 8999 |")
        assert any("quoting" in warning for warning in result.warnings)

    def test_quoted_field_longer_than_the_csv_module_limit_is_kept_whole(self):
        body = "쉼표, 포함\n본문 " * 20_000  # 240,000 characters — over the csv module's field limit (131,072 characters)
        rows = ["번호,제목,본문,작성자", f'1,공지,"{body}""따옴표"" 끝",홍길동', "2,안내,짧은 글,김철수"]

        result = convert("\n".join(rows).encode(), "data.csv", ConvertOptions(max_chars=None))

        assert result.warnings == []
        assert result.text.endswith("| 홍길동 |\n| 2 | 안내 | 짧은 글 | 김철수 |")
        assert result.text.count("쉼표, 포함") == 20_000
        assert '"따옴표" 끝 | 홍길동 |' in result.text

    def test_well_formed_quotes_are_still_parsed(self):
        result = convert('이름,설명\n부품,"쉼표, 포함"\n'.encode(), "data.csv")

        assert result.text == "| 이름 | 설명 |\n| --- | --- |\n| 부품 | 쉼표, 포함 |"
        assert not result.warnings


class TestDocxStructure:
    def test_table_row_wrapped_in_content_control_is_kept(self):
        """Repeating-section and form documents wrap a table row (or cell) in a content control (w:sdt). Only the direct children of
        the table were looked at, so that row vanished without a warning."""
        import re

        def wrap_rows(xml):
            rows = re.findall(r"<w:tr>.*?</w:tr>", xml, flags=re.DOTALL)
            xml = xml.replace(rows[1], f"<w:sdt><w:sdtPr/><w:sdtContent>{rows[1]}</w:sdtContent></w:sdt>")
            cell = re.search(r"<w:tc>.*?</w:tc>", rows[2], flags=re.DOTALL).group()
            return xml.replace(cell, f"<w:sdt><w:sdtPr/><w:sdtContent>{cell}</w:sdtContent></w:sdt>")

        data = builder.zip_with(_docx_with_table(), edit={"word/document.xml": wrap_rows})

        result = convert(data, "a.docx")

        assert result.text == "| r0c0 | r0c1 |\n| --- | --- |\n| r1c0 | r1c1 |\n| r2c0 | r2c1 |"

    def test_endnote_is_kept_like_a_footnote(self):
        """An endnote (w:endnoteReference) was dropped entirely: neither the reference nor the content was read."""
        namespace = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        endnotes = (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:endnotes {namespace}>'
                    '<w:endnote w:id="2"><w:p><w:r><w:t>미주 내용</w:t></w:r></w:p></w:endnote></w:endnotes>')
        relationship = ('<Relationship Id="rIdEndnotes" Target="endnotes.xml" Type="http://schemas.openxmlformats.org/'
                        'officeDocument/2006/relationships/endnotes"/></Relationships>')
        data = builder.zip_with(
            _docx_with_table(),
            edit={"word/document.xml": lambda xml: xml.replace("</w:body>", '<w:p><w:r><w:t>본문</w:t></w:r><w:r>'
                                                               '<w:endnoteReference w:id="2"/></w:r></w:p></w:body>'),
                  "word/_rels/document.xml.rels": lambda xml: xml.replace("</Relationships>", relationship)},
            add={"word/endnotes.xml": endnotes},
        )

        result = convert(data, "a.docx")

        assert result.text.endswith("본문[^endnote1]\n\n[^endnote1]: 미주 내용")

    def test_text_moved_away_by_tracked_change_is_not_included(self):
        """Text moved under track changes stays both at the original place (w:moveFrom) and at the new place (w:moveTo). The original
        place is deleted text."""
        moved = ('<w:moveFrom w:id="1" w:author="a"><w:r><w:t>옮겨 간 글</w:t></w:r></w:moveFrom>'
                 '<w:moveTo w:id="2" w:author="a"><w:r><w:t>옮겨 온 글</w:t></w:r></w:moveTo>')
        data = builder.zip_with(_docx_with_table(), edit={
            "word/document.xml": lambda xml: xml.replace("</w:body>", f"<w:p>{moved}</w:p></w:body>")})

        result = convert(data, "a.docx")

        assert "옮겨 온 글" in result.text
        assert "옮겨 간 글" not in result.text

    def test_macro_enabled_document_is_read_like_a_plain_one(self):
        """.docm and .dotx differ only in the content type of the main part. python-docx rejected them by the content type name, which
        made them a 'corrupted document'."""
        data = builder.zip_with(_docx_with_table(), edit={"[Content_Types].xml": lambda xml: xml.replace(
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
            "application/vnd.ms-word.document.macroEnabled.main+xml")})

        result = convert(data, "a.docm")

        assert "| r1c0 | r1c1 |" in result.text

    def test_stops_reading_paragraphs_once_the_character_limit_is_exceeded(self):
        from io import BytesIO

        from docx import Document

        document = Document()
        for number in range(2_000):
            document.add_paragraph(f"{number}번째 문단입니다. " * 5)
        buffer = BytesIO()
        document.save(buffer)

        result = convert(buffer.getvalue(), "a.docx", ConvertOptions(max_chars=2_000))

        assert result.exceeded
        assert result.char_count < 10_000, "reading everything would exceed 100,000 characters"


class TestDocxObjects:
    def test_chart_and_smartart_are_kept(self):
        """Only pictures (a:blip) in a paragraph were read, so charts (c:chart) and SmartArt (dgm:relIds) vanished without a warning.
        They are rendered with the same renderer as PPTX."""
        chart = _chart_xml(f"<c:barChart>{_chart_series(0, '매출', ['10', '20'], ['1분기', '2분기'])}</c:barChart>",
                           title="분기 매출")
        objects = (f'<w:p><w:r><w:t>매출 추이:</w:t></w:r><w:r>{_docx_inline(f'<c:chart xmlns:c="{_CHART_NS}" '
                   f'xmlns:r="{_RELATIONSHIPS_NS}" r:id="rIdChart"/>', _CHART_NS)}</w:r></w:p>'
                   f'<w:p><w:r>{_docx_inline(f'<dgm:relIds xmlns:dgm="{_DIAGRAM_NS}" xmlns:r="{_RELATIONSHIPS_NS}" '
                   'r:dm="rIdDgm" r:lo="rIdLo" r:qs="rIdQs" r:cs="rIdCs"/>', _DIAGRAM_NS)}</w:r></w:p>')
        relationships = (f'<Relationship Id="rIdChart" Type="{_RELATIONSHIPS_NS}/chart" Target="charts/chart1.xml"/>'
                         f'<Relationship Id="rIdDgm" Type="{_RELATIONSHIPS_NS}/diagramData" Target="diagrams/data1.xml"/>'
                         "</Relationships>")
        data = builder.zip_with(
            _docx_with_table(),
            edit={"word/document.xml": lambda xml: xml.replace("<w:sectPr", objects + "<w:sectPr", 1),
                  "word/_rels/document.xml.rels": lambda xml: xml.replace("</Relationships>", relationships)},
            add={"word/charts/chart1.xml": chart, "word/diagrams/data1.xml": _smartart_data()},
        )

        result = convert(data, "a.docx")

        assert ("매출 추이:\n\n[Chart] 분기 매출\n\n| Category | 매출 |\n| --- | --- |\n| 1분기 | 10 |\n| 2분기 | 20 |\n\n"
                "- 과제 발의\n  - 재단 승인\n- 설비 구축") in result.text

    def test_first_page_and_even_page_headers_are_read(self):
        """Only the default header was read, so the first-page and even-page headers were dropped. The same text is kept only once."""
        from io import BytesIO

        from docx import Document

        document = Document()
        document.settings.odd_and_even_pages_header_footer = True
        section = document.sections[0]
        section.different_first_page_header_footer = True
        section.header.paragraphs[0].text = "기본 머리말"
        section.first_page_header.paragraphs[0].text = "표지 머리말"
        section.even_page_header.paragraphs[0].text = "기본 머리말"
        section.first_page_footer.paragraphs[0].text = "표지 꼬리말"
        document.add_paragraph("본문")
        buffer = BytesIO()
        document.save(buffer)

        result = convert(buffer.getvalue(), "a.docx", ConvertOptions(include_headers_footers=True))

        assert result.text == "기본 머리말\n\n표지 머리말\n\n본문\n\n표지 꼬리말"

    def test_header_picture_comes_from_the_header_part(self):
        """The relationship id of a header picture was looked up in the body part, so another part with the same id (customXml etc.)
        was registered as the picture."""
        from io import BytesIO

        from docx import Document
        from docx.shared import Inches

        from opendockit.extractors.docx import extract_docx

        body_picture, header_picture = builder.png_bytes(seed=1), builder.png_bytes(seed=2)
        document = Document()
        document.add_paragraph("본문 그림:")
        document.add_picture(BytesIO(body_picture), width=Inches(2))
        document.sections[0].header.paragraphs[0].add_run("로고 ").add_picture(BytesIO(header_picture), width=Inches(1))
        buffer = BytesIO()
        document.save(buffer)

        extraction = extract_docx(buffer.getvalue(), ConvertOptions(include_headers_footers=True))

        assert {image.data for image in extraction.images} == {header_picture, body_picture}

    def test_numbering_uses_start_and_level_text(self):
        """The start number (w:start) and number text (w:lvlText) of the numbering definition were ignored, so "제3조" and "제4조"
        came out as "1." and "2."."""
        from io import BytesIO

        from docx import Document

        document = Document()
        for text in ("목적", "정의"):
            document.add_paragraph(text, style="List Number")
        buffer = BytesIO()
        document.save(buffer)
        num_id = re.search(r'w:numId w:val="(\d+)"', Document(BytesIO(buffer.getvalue())).styles["List Number"].element.xml)

        def first_level_as_articles(xml):
            abstract_id = re.search(rf'<w:num w:numId="{num_id.group(1)}"[^>]*>\s*<w:abstractNumId w:val="(\d+)"', xml).group(1)
            start = xml.index(f'w:abstractNumId="{abstract_id}"')
            end = xml.index("</w:lvl>", start)
            level = re.sub(r'<w:start w:val="\d+"/>', '<w:start w:val="3"/>', xml[start:end], count=1)
            level = re.sub(r'<w:lvlText w:val="[^"]*"/>', '<w:lvlText w:val="제%1조"/>', level, count=1)
            return xml[:start] + level + xml[end:]

        data = builder.zip_with(buffer.getvalue(), edit={"word/numbering.xml": first_level_as_articles})

        result = convert(data, "a.docx")

        assert result.text == "제3조 목적\n\n제4조 정의"

    def test_symbol_font_check_boxes_are_kept(self):
        """A Wingdings symbol (w:sym) is a character-code attribute, not w:t, so check boxes were dropped entirely."""
        symbols = ('<w:p><w:r><w:t xml:space="preserve">동의 </w:t></w:r><w:r><w:sym w:font="Wingdings" w:char="F0FE"/>'
                   '</w:r><w:r><w:t xml:space="preserve"> 미동의 </w:t></w:r><w:r><w:sym w:font="Wingdings" w:char="00A8"/>'
                   "</w:r></w:p>")
        data = builder.zip_with(_docx_with_table(), edit={
            "word/document.xml": lambda xml: xml.replace("<w:sectPr", symbols + "<w:sectPr", 1)})

        result = convert(data, "a.docx")

        assert result.text.endswith("동의 ☑ 미동의 □")


def _docx_inline(graphic_data: str, uri: str) -> str:
    """A w:drawing (wp:inline) to insert into the body. `graphic_data` is the c:chart or dgm:relIds inside a:graphicData."""
    return ('<w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">'
            '<wp:extent cx="5486400" cy="3200400"/><wp:docPr id="50" name="개체"/>'
            '<a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">'
            f'<a:graphicData uri="{uri}">{graphic_data}</a:graphicData></a:graphic></wp:inline></w:drawing>')


def _docx_with_table() -> bytes:
    from io import BytesIO

    from docx import Document

    document = Document()
    table = document.add_table(rows=3, cols=2)
    for row in range(3):
        for col in range(2):
            table.cell(row, col).text = f"r{row}c{col}"
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


_XML_HEAD = ('<?xml version="1.0"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
             'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">')
_XML_SPREADSHEET = """<?xml version="1.0" encoding="UTF-8"?>
<?mso-application progid="Excel.Sheet"?>
<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"
 xmlns:x="urn:schemas-microsoft-com:office:excel">
 <Styles><Style ss:ID="pct"><NumberFormat ss:Format="0.0%"/></Style></Styles>
 <Worksheet ss:Name="매출">
  <Table>
   <Row><Cell ss:MergeAcross="2"><Data ss:Type="String">2026년 매출 현황</Data></Cell></Row>
   <Row><Cell><Data ss:Type="String">지역</Data></Cell><Cell><Data ss:Type="String">지점</Data></Cell>
    <Cell><Data ss:Type="String">달성률</Data></Cell></Row>
   <Row><Cell ss:MergeDown="1"><Data ss:Type="String">서울</Data></Cell><Cell><Data ss:Type="String">강남</Data></Cell>
    <Cell ss:StyleID="pct"><Data ss:Type="Number">1.082</Data></Cell></Row>
   <Row><Cell ss:Index="2"><Data ss:Type="String">종로</Data></Cell><Cell ss:StyleID="pct"><Data ss:Type="Number">0.9</Data></Cell></Row>
   <Row ss:Index="6"><Cell><Data ss:Type="String">합계</Data></Cell><Cell ss:Index="3"><Data ss:Type="DateTime">2026-03-31T00:00:00.000</Data></Cell></Row>
  </Table>
 </Worksheet>
 <Worksheet ss:Name="비밀">
  <Table><Row><Cell><Data ss:Type="String">숨김 내용</Data></Cell></Row></Table>
  <x:WorksheetOptions><x:Visible>SheetHidden</x:Visible></x:WorksheetOptions>
 </Worksheet>
</Workbook>"""


class TestXmlSpreadsheet:
    """Excel 2003 XML spreadsheet (common for the 'Excel download' of Korean sites). `<Table>` was detected as an HTML table and the
    HTML extractor reads only tr/td, so the result was an empty string."""

    @pytest.fixture(scope="class")
    @classmethod
    def result(cls):
        return convert(_XML_SPREADSHEET.encode(), "다운로드.xls")

    def test_is_detected_as_an_xml_spreadsheet(self, result):
        assert result.format is DocumentFormat.XML_SPREADSHEET

    def test_renders_cells_with_merges_skipped_columns_and_formats(self, result):
        assert result.text.startswith("## Sheet: 매출\n\n2026년 매출 현황\n\n| 지역 | 지점 | 달성률 |")
        assert "| 서울 | 강남 | 108.2% |\n| 서울 | 종로 | 90.0% |" in result.text
        assert "| 합계 | | 2026-03-31 |" in result.text

    def test_skips_hidden_sheet_with_warning(self, result):
        assert "숨김 내용" not in result.text
        assert any("비밀" in warning for warning in result.warnings)

    @pytest.mark.timing
    def test_merges_and_cells_beyond_the_column_limit_stay_cheap(self):
        """MergeDown of cells beyond the column limit was remembered too and scanned on every row, so a 2.4MB file took 44 seconds.
        Cells beyond the limit only produce a warning."""
        import time

        cells = '<Cell ss:Index="200" ss:MergeDown="999999999"><Data ss:Type="String">a</Data></Cell>'
        cells += '<Cell ss:MergeDown="999999999"><Data ss:Type="String">a</Data></Cell>' * 4_000
        data = (_XML_HEAD + '<Worksheet ss:Name="S"><Table><Row><Cell><Data ss:Type="String">값</Data></Cell>'
                + cells + "</Row>" + "<Row/>" * 4_000 + "</Table></Worksheet></Workbook>").encode()

        started = time.perf_counter()
        result = convert(data, "a.xml", ConvertOptions(max_sheet_cols=10))

        assert time.perf_counter() - started < 3, "it took tens of seconds before the fix"
        assert result.text == "## Sheet: S\n\n값"
        assert any("column 10" in warning for warning in result.warnings)

    @pytest.mark.parametrize("prefix", [b"\r\n  ", b"\xef\xbb\xbf\n"], ids=["whitespace", "bom-and-newline"])
    def test_leading_whitespace_before_declaration_is_tolerated(self, prefix):
        """Files made by JSP/ASP often have blank lines before `<?xml`. The XML parser rejected them and the whole document became
        'corrupted'."""
        result = convert(prefix + _XML_SPREADSHEET.encode(), "a.xls")

        assert "| 서울 | 강남 | 108.2% |" in result.text

    @pytest.mark.parametrize("declaration", ['<?xml version="1.0"?>', '<?xml version="1.0" encoding="UTF-8"?>',
                                             '<?xml version="1.0" encoding="ks_c_5601-1987"?>'],
                             ids=["undeclared", "mislabeled-utf8", "ks_c_5601"])
    def test_korean_encodings_that_xml_parsers_reject_are_decoded(self, declaration):
        """CP949 with no declaration, CP949 labelled as UTF-8, and the `ks_c_5601-1987` of Korean ASP sites were rejected by the XML
        parser."""
        text = _XML_SPREADSHEET.replace('<?xml version="1.0" encoding="UTF-8"?>', declaration)

        result = convert(text.encode("cp949"), "a.xls")

        assert "| 서울 | 강남 | 108.2% |" in result.text

    def test_number_format_comes_from_row_column_default_or_parent_style(self):
        """The format comes not only from the cell's StyleID but also from the row's or column's StyleID, the Default style and
        ss:Parent inheritance."""
        data = (_XML_HEAD + '<Styles><Style ss:ID="Default"><NumberFormat ss:Format="#,##0"/></Style>'
                '<Style ss:ID="pct"><NumberFormat ss:Format="0%"/></Style><Style ss:ID="child" ss:Parent="pct"/></Styles>'
                '<Worksheet ss:Name="S"><Table><Column ss:Width="50"/><Column ss:StyleID="pct"/>'
                '<Row><Cell><Data ss:Type="Number">1234567</Data></Cell><Cell><Data ss:Type="Number">0.5</Data></Cell></Row>'
                '<Row ss:StyleID="pct"><Cell><Data ss:Type="Number">0.25</Data></Cell>'
                '<Cell ss:StyleID="child"><Data ss:Type="Number">0.75</Data></Cell></Row>'
                "</Table></Worksheet></Workbook>").encode()

        result = convert(data, "a.xml")

        assert "| 1,234,567 | 50% |\n| --- | --- |\n| 25% | 75% |" in result.text

    def test_time_only_value_is_shown_as_a_time(self):
        """A time-only value is stored together with the date 1899-12-31. Attaching the date makes it read as an unrelated day."""
        data = (_XML_HEAD + '<Worksheet ss:Name="S"><Table><Row><Cell><Data ss:Type="String">시작</Data></Cell>'
                '<Cell><Data ss:Type="DateTime">1899-12-31T09:30:00.000</Data></Cell></Row>'
                "</Table></Worksheet></Workbook>").encode()

        result = convert(data, "a.xml")

        assert "| 시작 | 09:30:00 |" in result.text

    def test_entities_are_not_expanded(self):
        data = _XML_SPREADSHEET.replace('<?mso-application progid="Excel.Sheet"?>',
                                        '<!DOCTYPE Workbook [<!ENTITY x SYSTEM "file:///etc/passwd">]>')
        data = data.replace("2026년 매출 현황", "&x;")

        result = convert(data.encode(), "a.xml")

        assert "root:" not in result.text


class TestReportedGaps:
    def test_pptx_shape_wrapped_in_alternate_content_is_read(self):
        """A shape wrapped in `mc:AlternateContent`, like a text box holding an equation, is not counted as a shape by python-pptx, so
        its text was dropped entirely. The shape of the new version (mc:Choice) is read and the fallback (mc:Fallback) is not."""
        from io import BytesIO

        from pptx import Presentation
        from pptx.util import Inches

        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        slide.shapes.title.text = "제목"
        slide.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1)).text_frame.text = "수식이 든 글상자"
        slide.shapes.add_textbox(Inches(1), Inches(4), Inches(4), Inches(1)).text_frame.text = "보통 글상자"
        buffer = BytesIO()
        presentation.save(buffer)

        def wrap(xml):
            shape = re.search(r"<p:sp>(?:(?!<p:sp>).)*?수식이 든 글상자.*?</p:sp>", xml, re.DOTALL).group(0)
            wrapped = ('<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
                       f'<mc:Choice xmlns:a14="http://schemas.microsoft.com/office/drawing/2010/main" Requires="a14">{shape}'
                       f'</mc:Choice><mc:Fallback>{shape.replace("수식이 든", "대체용")}</mc:Fallback></mc:AlternateContent>')
            return xml.replace(shape, wrapped)

        data = builder.zip_with(buffer.getvalue(), edit={"ppt/slides/slide1.xml": wrap})

        result = convert(data, "a.pptx")

        assert result.text == "## Slide 1: 제목\n\n수식이 든 글상자\n\n보통 글상자"

    def test_docx_numbered_heading_keeps_its_number(self):
        """For a heading with automatic numbering, heading detection finished first and the number was dropped ('see section 3.2'
        could not be followed)."""
        from io import BytesIO

        from docx import Document
        from docx.oxml import parse_xml

        document = Document()
        num_id = re.search(r'w:numId w:val="(\d+)"', document.styles["List Number"].element.xml).group(1)
        for text in ("개요", "범위"):
            heading = document.add_heading(text, level=1)
            heading._p.get_or_add_pPr().append(parse_xml(
                '<w:numPr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f'<w:ilvl w:val="0"/><w:numId w:val="{num_id}"/></w:numPr>'))
            document.add_paragraph(f"{text} 본문")
        buffer = BytesIO()
        document.save(buffer)

        result = convert(buffer.getvalue(), "a.docx")

        assert result.text == "# 1. 개요\n\n개요 본문\n\n# 2. 범위\n\n범위 본문"

    def test_docx_picture_in_a_vertically_merged_cell_is_placed_once(self):
        """Text of a vertically merged cell is repeated on every row, but the picture placeholder was repeated too, so the description
        went in once per row (the other formats put it only in the top cell)."""
        from io import BytesIO

        from docx import Document
        from docx.shared import Inches

        document = Document()
        table = document.add_table(rows=3, cols=2)
        table.cell(0, 0).merge(table.cell(2, 0))
        table.cell(0, 0).paragraphs[0].add_run("분류").add_picture(BytesIO(builder.png_bytes(seed=3)), width=Inches(1))
        for row in range(3):
            table.cell(row, 1).text = f"값{row}"
        buffer = BytesIO()
        document.save(buffer)

        result = convert(buffer.getvalue(), "a.docx")

        assert result.text == "| 분류 [Image] | 값0 |\n| --- | --- |\n| 분류 | 값1 |\n| 분류 | 값2 |"

    def test_docx_table_with_horizontal_and_vertical_merges_keeps_both_spans(self):
        """A table with horizontal merges goes out as an HTML table. DOCX writes a vertical merge as continuing cells (vMerge), which
        must go out as a rowspan, not as a value repeated on every row."""
        from io import BytesIO

        from docx import Document

        document = Document()
        table = document.add_table(rows=4, cols=3)
        for col, text in enumerate(["구분", "과제", "기간"]):
            table.cell(0, col).text = text
        table.cell(1, 0).merge(table.cell(2, 0)).text = "상반기"
        table.cell(1, 1).text, table.cell(1, 2).text = "정비", "1~3월"
        table.cell(2, 1).text, table.cell(2, 2).text = "교체", "4~6월"
        table.cell(3, 0).merge(table.cell(3, 1)).text = "합계"
        table.cell(3, 2).text = "2개"
        buffer = BytesIO()
        document.save(buffer)

        result = convert(buffer.getvalue(), "a.docx")

        assert result.text == ("<table>\n<tr><th>구분</th><th>과제</th><th>기간</th></tr>\n"
                               '<tr><td rowspan="2">상반기</td><td>정비</td><td>1~3월</td></tr>\n'
                               "<tr><td>교체</td><td>4~6월</td></tr>\n"
                               '<tr><td colspan="2">합계</td><td>2개</td></tr>\n</table>')

    def test_docx_vertical_merge_whose_width_changes_keeps_the_neighbour_cell(self):
        """When the continuing cell of a vertical merge that started two cells wide is one cell wide, the row span was carried down two
        cells wide and hid the neighbour cell ('X'). Only continuing cells of the same width are joined into the merge; others repeat
        the text above (the previous behaviour)."""
        def cell(text, span=1, merge=None):
            properties = (f'<w:gridSpan w:val="{span}"/>' if span > 1 else "") + (
                f'<w:vMerge w:val="{merge}"/>' if merge == "restart" else "<w:vMerge/>" if merge else "")
            return f"<w:tc><w:tcPr>{properties}</w:tcPr><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:tc>"

        rows = [[cell("H1"), cell("H2"), cell("H3")], [cell("A", 2, "restart"), cell("B")],
                [cell("", 1, "continue"), cell("X"), cell("Y")]]
        body = "<w:tbl>" + "".join("<w:tr>" + "".join(row) + "</w:tr>" for row in rows) + "</w:tbl>"

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == ("<table>\n<tr><th>H1</th><th>H2</th><th>H3</th></tr>\n"
                               '<tr><td colspan="2">A</td><td>B</td></tr>\n'
                               "<tr><td>A</td><td>X</td><td>Y</td></tr>\n</table>")

    def test_xlsx_with_a_large_pivot_cache_is_streamed(self, monkeypatch):
        """A full openpyxl load turns the pivot cache, which is never used, into objects in its entirety (a 2.8MB file took 14 seconds
        and 1.5GB). The criterion that looked only at the sheet XML size did not catch it."""
        from opendockit.extractors import spreadsheet

        modes = []
        original_load = spreadsheet.openpyxl.load_workbook

        def recording_load(*args, **kwargs):
            modes.append(kwargs.get("read_only"))
            return original_load(*args, **kwargs)

        monkeypatch.setattr(spreadsheet.openpyxl, "load_workbook", recording_load)
        monkeypatch.setattr(spreadsheet, "_MAX_PIVOT_CACHE_BYTES", 100)
        relationship = ('<Relationship Id="rIdPivot" Target="pivotCache/pivotCacheDefinition1.xml" Type="http://schemas.'
                        'openxmlformats.org/officeDocument/2006/relationships/pivotCacheDefinition"/>')
        records = ('<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship '
                   'Id="rId1" Target="pivotCacheRecords1.xml" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                   'relationships/pivotCacheRecords"/></Relationships>')
        data = builder.zip_with(
            _long_sheet(1),
            edit={"xl/_rels/workbook.xml.rels": lambda xml: xml.replace("</Relationships>", f"{relationship}</Relationships>")},
            add={"xl/pivotCache/pivotCacheDefinition1.xml": "<pivotCacheDefinition/>",
                 "xl/pivotCache/_rels/pivotCacheDefinition1.xml.rels": records,
                 "xl/pivotCache/pivotCacheRecords1.xml": "<pivotCacheRecords>" + "<r/>" * 50 + "</pivotCacheRecords>"})

        result = convert(data, "a.xlsx")

        assert "| 1 | 항목 1 |" in result.text
        assert modes == [True]


class TestSuperscriptAndTextBoxes:
    def test_docx_superscript_and_subscript_runs_are_marked(self):
        """Superscript and subscript characters were appended as plain text, so 10⁶ read as 106. Consecutive superscript runs ('-' and
        '3') are grouped into one."""
        from io import BytesIO

        from docx import Document

        document = Document()
        paragraph = document.add_paragraph("면적 10")
        paragraph.add_run("6").font.superscript = True
        paragraph.add_run(" m")
        paragraph.add_run("2").font.superscript = True
        paragraph.add_run(", H")
        paragraph.add_run("2").font.subscript = True
        paragraph.add_run("O, 10")
        paragraph.add_run("-").font.superscript = True
        paragraph.add_run("3").font.superscript = True
        buffer = BytesIO()
        document.save(buffer)

        result = convert(buffer.getvalue(), "a.docx")

        assert result.text == "면적 10^6 m^2, H_2O, 10^{-3}"

    def test_docx_raised_and_lowered_small_runs_from_libreoffice_are_marked(self):
        """LibreOffice writes super/subscripts not with w:vertAlign but with raise/lower (w:position, half-points) and a small font
        size (w:sz). The marks were missing, giving '각주 대신 1 위첨자와 H2O'. Only a run that is at most 0.8× the paragraph font size
        (style → document default), and raised by at least 0.2× or lowered by at least 0.1× the font size, counts as a
        super/subscript (the same criterion as the position check for PDF)."""
        def run(text: str, properties: str = "") -> str:
            return f'<w:r><w:rPr>{properties}</w:rPr><w:t xml:space="preserve">{text}</w:t></w:r>'

        styles = ('<w:style w:type="paragraph" w:styleId="BodyText"><w:name w:val="Body Text"/>'
                  '<w:rPr><w:sz w:val="24"/></w:rPr></w:style>')
        body = ('<w:p><w:pPr><w:pStyle w:val="BodyText"/></w:pPr>' + run("각주 대신 ")
                + run("1", '<w:position w:val="8"/><w:sz w:val="19"/>') + run(" 위첨자와 H")
                + run("2", '<w:position w:val="-2"/><w:sz w:val="19"/>') + run("O") + "</w:p>")

        result = convert(_docx_with_body(body, styles=styles), "a.docx")

        assert result.text == "각주 대신 ^1 위첨자와 H_2O"

    def test_docx_raised_run_of_ordinary_size_is_not_marked(self):
        """A run raised at the same size (no font size given, or the same size as the paragraph) or raised only slightly is not a
        superscript (text lifted for decoration)."""
        def run(text: str, properties: str = "") -> str:
            return f'<w:r><w:rPr>{properties}</w:rPr><w:t xml:space="preserve">{text}</w:t></w:r>'

        body = ("<w:p>" + run("가격 ") + run("100", '<w:position w:val="6"/>') + run(" 원 ")
                + run("200", '<w:position w:val="6"/><w:sz w:val="22"/>') + run(" 원 ")
                + run("300", '<w:position w:val="1"/><w:sz w:val="16"/>') + "</w:p>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "가격 100 원 200 원 300"

    def test_docx_superscript_with_hangul_is_left_as_plain_text(self):
        """A raised unit or footnote mark ('원', '주1)') is not an exponent. It became '1,000^원' (the same rule as HTML and PDF)."""
        from io import BytesIO

        from docx import Document

        document = Document()
        paragraph = document.add_paragraph("가격 1,000")
        paragraph.add_run("원").font.superscript = True
        paragraph.add_run(", 각주")
        paragraph.add_run("주1)").font.superscript = True
        paragraph.add_run(", 10")
        paragraph.add_run("6").font.superscript = True
        buffer = BytesIO()
        document.save(buffer)

        result = convert(buffer.getvalue(), "a.docx")

        assert result.text == "가격 1,000원, 각주주1), 10^6"

    def test_pptx_superscript_runs_are_marked_in_text_boxes_and_table_cells(self):
        from io import BytesIO

        from pptx import Presentation
        from pptx.util import Inches

        def exponent(paragraph):
            paragraph.add_run().text = "10"
            run = paragraph.add_run()
            run.text = "6"
            run.font._element.set("baseline", "30000")
            paragraph.add_run().text = " 개"

        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        slide.shapes.title.text = "지수"
        exponent(slide.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1)).text_frame.paragraphs[0])
        table = slide.shapes.add_table(2, 2, Inches(1), Inches(4), Inches(4), Inches(1)).table
        for index, text in enumerate(["항목", "값", "수량"]):
            table.cell(index // 2, index % 2).text = text
        exponent(table.cell(1, 1).text_frame.paragraphs[0])
        buffer = BytesIO()
        presentation.save(buffer)

        result = convert(buffer.getvalue(), "a.pptx")

        assert result.text == "## Slide 1: 지수\n\n10^6 개\n\n| 항목 | 값 |\n| --- | --- |\n| 수량 | 10^6 개 |"

    def test_docx_text_box_inside_a_text_box_is_read_once(self):
        """A text box inside a text box was read once from the outer paragraph and once more from the outer text box's paragraph, so
        it came out twice."""
        from io import BytesIO

        from docx import Document
        from docx.oxml import parse_xml

        namespace = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
        document = Document()
        paragraph = document.add_paragraph("바깥 문단")
        paragraph._p.append(parse_xml(
            f"<w:r {namespace}><w:pict><w:txbxContent><w:p><w:r><w:t>바깥 글상자</w:t></w:r><w:r><w:pict><w:txbxContent>"
            "<w:p><w:r><w:t>안쪽 글상자</w:t></w:r></w:p></w:txbxContent></w:pict></w:r></w:p></w:txbxContent></w:pict></w:r>"))
        buffer = BytesIO()
        document.save(buffer)

        result = convert(buffer.getvalue(), "a.docx")

        assert result.text == "바깥 문단\n\n바깥 글상자\n\n안쪽 글상자"


def test_pptx_line_break_inside_a_table_cell_is_kept():
    """A line break (Shift+Enter) inside a table cell came out as a vertical tab, which the cleanup step erased, joining the words."""
    from io import BytesIO

    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "표"
    table = slide.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(4), Inches(1)).table
    for index, text in enumerate(["항목", "값", "주소", "서울시\v강남구"]):
        table.cell(index // 2, index % 2).text = text
    buffer = BytesIO()
    presentation.save(buffer)

    result = convert(buffer.getvalue(), "a.pptx")

    assert "| 주소 | 서울시<br>강남구 |" in result.text


def test_docx_ordinal_suffix_after_a_number_is_not_marked_as_superscript():
    from io import BytesIO

    from docx import Document

    document = Document()
    paragraph = document.add_paragraph("CPU: 13")
    paragraph.add_run("th").font.superscript = True
    paragraph.add_run(" Gen, 2")
    paragraph.add_run("n").font.superscript = True
    buffer = BytesIO()
    document.save(buffer)

    result = convert(buffer.getvalue(), "a.docx")

    assert result.text == "CPU: 13th Gen, 2^n"


_CHARTEX_NS = "http://schemas.microsoft.com/office/drawing/2014/chartex"
_MC_NS = "http://schemas.openxmlformats.org/markup-compatibility/2006"
_OLE_FRAME = ('<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="9" name="개체"/><p:cNvGraphicFramePr/><p:nvPr/>'
              '</p:nvGraphicFramePr><p:xfrm><a:off x="914400" y="1828800"/><a:ext cx="2540000" cy="1524000"/></p:xfrm>'
              '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/presentationml/2006/ole">{ole}'
              "</a:graphicData></a:graphic></p:graphicFrame>")


def _replace_picture(replacement: Callable[[str], str]) -> Callable[[str], str]:
    """Edit function that replaces the picture shape (p:pic) of the `builder.pptx_with_image()` slide with `replacement(picture XML)`."""
    def edit(xml: str) -> str:
        picture = re.search(r"<p:pic>.*?</p:pic>", xml, re.DOTALL).group(0)
        return xml.replace(picture, replacement(picture))

    return edit


def _pptx_with_chartex(*, fallback: bool, chart_part: str | None = None) -> bytes:
    """A slide with a new chart type (chartex). PowerPoint puts the chart frame in mc:Choice and a pre-rendered picture in mc:Fallback."""
    frame = ('<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="9" name="차트"/><p:cNvGraphicFramePr/><p:nvPr/>'
             '</p:nvGraphicFramePr><p:xfrm><a:off x="914400" y="1828800"/><a:ext cx="2540000" cy="1524000"/></p:xfrm>'
             f'<a:graphic><a:graphicData uri="{_CHARTEX_NS}"><cx:chart xmlns:cx="{_CHARTEX_NS}" r:id="rIdCx"/>'
             "</a:graphicData></a:graphic></p:graphicFrame>")
    relationship = ('<Relationship Id="rIdCx" Type="http://schemas.microsoft.com/office/2014/relationships/chartEx" '
                    'Target="../charts/chartEx1.xml"/></Relationships>')
    edit = {"ppt/slides/slide1.xml": _replace_picture(
        lambda picture: f'<mc:AlternateContent xmlns:mc="{_MC_NS}"><mc:Choice Requires="cx1">{frame}</mc:Choice>'
                        f'{f"<mc:Fallback>{picture}</mc:Fallback>" if fallback else ""}</mc:AlternateContent>')}
    if chart_part is not None:
        edit["ppt/slides/_rels/slide1.xml.rels"] = lambda xml: xml.replace("</Relationships>", relationship)
    return builder.zip_with(builder.pptx_with_image(), edit=edit,
                            add={"ppt/charts/chartEx1.xml": chart_part} if chart_part is not None else None)


def _chartex_xml(title: str, categories: list[str], values: list[str]) -> str:
    def points(items: list[str]) -> str:
        return "".join(f'<cx:pt idx="{index}">{item}</cx:pt>' for index, item in enumerate(items))

    return (f'<?xml version="1.0" encoding="UTF-8"?><cx:chartSpace xmlns:cx="{_CHARTEX_NS}"><cx:chartData><cx:data id="0">'
            f'<cx:strDim type="cat"><cx:f>Sheet1!$A$2:$A$3</cx:f><cx:lvl ptCount="{len(categories)}">{points(categories)}'
            f'</cx:lvl></cx:strDim><cx:numDim type="val"><cx:f>Sheet1!$B$2:$B$3</cx:f><cx:lvl ptCount="{len(values)}" '
            f'formatCode="General">{points(values)}</cx:lvl></cx:numDim></cx:data></cx:chartData><cx:chart><cx:title>'
            f"<cx:tx><cx:txData><cx:v>{title}</cx:v></cx:txData></cx:tx></cx:title><cx:plotArea><cx:plotAreaRegion>"
            '<cx:series layoutId="waterfall"><cx:tx><cx:txData><cx:f>Sheet1!$B$1</cx:f><cx:v>금액</cx:v></cx:txData></cx:tx>'
            '<cx:dataId val="0"/></cx:series></cx:plotAreaRegion></cx:plotArea></cx:chart></cx:chartSpace>')


class TestPptxLayoutAndEmbeddedObjects:
    def test_shapes_misaligned_within_the_tolerance_are_read_left_to_right(self):
        """Vertical positions were sorted on a 72,000 EMU grid, so even shapes misaligned within the tolerance had the right one read
        first when they straddled a grid boundary ("2단계 → 1단계 → 3단계", the value before its name)."""
        from io import BytesIO

        from pptx import Presentation
        from pptx.util import Inches

        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        for text, left, top in [("1단계: 접수", 0.5, 2.00), ("2단계: 심사", 3.5, 1.96), ("3단계: 승인", 6.5, 2.02),
                                ("매출", 0.5, 4.0), ("1,200억원", 3.5, 3.93)]:
            slide.shapes.add_textbox(Inches(left), Inches(top), Inches(2.5), Inches(0.5)).text_frame.text = text
        buffer = BytesIO()
        presentation.save(buffer)

        result = convert(buffer.getvalue(), "a.pptx")

        assert result.text == "## Slide 1\n\n1단계: 접수\n\n2단계: 심사\n\n3단계: 승인\n\n매출\n\n1,200억원"

    def test_chartex_without_readable_data_falls_back_to_its_picture(self):
        """For new chart types (waterfall, treemap …) the frame in mc:Choice was taken as a 'known shape' and the picture in
        mc:Fallback was discarded, so the chart vanished with no data, no picture and no warning."""
        result = convert(_pptx_with_chartex(fallback=True), "a.pptx")

        assert result.text == "## Slide 1: 구성도\n\n[Image]"
        assert len(result.images) == 1

    def test_chartex_without_data_or_fallback_leaves_a_placeholder(self):
        result = convert(_pptx_with_chartex(fallback=False), "a.pptx")

        assert result.text == "## Slide 1: 구성도\n\n[Chart]"

    def test_chartex_data_is_rendered_as_a_table_instead_of_the_picture(self):
        chart = _chartex_xml("손익 분석", ["매출", "원가"], ["100", "-60.0"])

        result = convert(_pptx_with_chartex(fallback=True, chart_part=chart), "a.pptx")

        assert result.text == ("## Slide 1: 구성도\n\n[Chart] 손익 분석\n\n| Category | 금액 |\n| --- | --- |\n| 매출 | 100 |\n"
                               "| 원가 | -60 |")
        assert not result.images

    def test_ole_object_preview_picture_is_registered(self):
        """An Excel sheet or Word document pasted onto a slide (p:oleObj) vanished with no placeholder and no warning. Its preview
        picture is registered."""
        data = builder.zip_with(builder.pptx_with_image(), edit={"ppt/slides/slide1.xml": _replace_picture(
            lambda picture: _OLE_FRAME.format(ole=f'<p:oleObj name="Worksheet" r:id="rId9" progId="Excel.Sheet.12">'
                                                  f"<p:embed/>{picture}</p:oleObj>"))})

        result = convert(data, "a.pptx")

        assert result.text == "## Slide 1: 구성도\n\n[Image]"
        assert len(result.images) == 1

    def test_ole_object_preview_in_the_vml_drawing_is_registered(self):
        """For a legacy OLE object the preview picture is not in the slide but in the VML drawing part (linked by spid)."""
        relationships = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        vml = ('<xml xmlns:v="urn:schemas-microsoft-com:vml" xmlns:o="urn:schemas-microsoft-com:office:office">'
               '<v:shape id="_x0000_s1026"><v:imagedata o:relid="rId1" o:title=""/></v:shape></xml>')
        vml_relationships = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/'
                             f'package/2006/relationships"><Relationship Id="rId1" Type="{relationships}/image" '
                             'Target="../media/image1.png"/></Relationships>')
        data = builder.zip_with(
            builder.pptx_with_image(),
            edit={"ppt/slides/slide1.xml": _replace_picture(lambda _: _OLE_FRAME.format(
                      ole='<p:oleObj spid="_x0000_s1026" name="Worksheet" r:id="rId9" progId="Excel.Sheet.12"><p:embed/></p:oleObj>')),
                  "ppt/slides/_rels/slide1.xml.rels": lambda xml: xml.replace(
                      "</Relationships>", f'<Relationship Id="rIdVml" Type="{relationships}/vmlDrawing" '
                                          'Target="../drawings/vmlDrawing1.vml"/></Relationships>'),
                  "[Content_Types].xml": lambda xml: xml.replace("</Types>", '<Default Extension="vml" ContentType="application/'
                                                                 'vnd.openxmlformats-officedocument.vmlDrawing"/></Types>')},
            add={"ppt/drawings/vmlDrawing1.vml": vml, "ppt/drawings/_rels/vmlDrawing1.vml.rels": vml_relationships})

        result = convert(data, "a.pptx")

        assert len(result.images) == 1


def _axis(kind: str, position: str, title: str, *, deleted: bool = False) -> str:
    return (f'<c:{kind}><c:delete val="{int(deleted)}"/><c:axPos val="{position}"/><c:title><c:tx><c:rich><a:bodyPr/><a:p>'
            f"<a:r><a:t>{title}</a:t></a:r></a:p></c:rich></c:tx></c:title></c:{kind}>")


def _convert_chart(chart: str) -> str:
    """The body (without the slide heading) of one chart XML inserted into a slide and converted."""
    data = builder.zip_with(_pptx_with_chart(), edit={"ppt/charts/chart1.xml": lambda _: chart})
    return convert(data, "a.pptx").sections[0].text


class TestChartAxes:
    """Category names and axis titles of the shared chart code (`_charts.render_chart`). PPTX, DOCX and HWPX use the same function."""

    @staticmethod
    def _numeric_categories(format_code: str, serials: list[str]) -> str:
        points = "".join(f'<c:pt idx="{index}"><c:v>{serial}</c:v></c:pt>' for index, serial in enumerate(serials))
        values = "".join(f'<c:pt idx="{index}"><c:v>{index + 1}</c:v></c:pt>' for index in range(len(serials)))
        return ('<c:ser><c:order val="0"/><c:tx><c:v>값</c:v></c:tx>'
                f"<c:cat><c:numRef><c:numCache><c:formatCode>{format_code}</c:formatCode>{points}</c:numCache></c:numRef></c:cat>"
                f"<c:val><c:numRef><c:numCache>{values}</c:numCache></c:numRef></c:val></c:ser>")

    @pytest.mark.parametrize(("format_code", "serial", "expected"), [
        ("yyyy\\-mm\\-dd", "46023.0", "2026-01-01"),
        ('yyyy"년" m"월"', "46054", "2026-02"),
        ("yyyy/m/d h:mm", "46023.375", "2026-01-01 09:00"),
        ("h:mm:ss", "0.50001157", "12:00:01"),
        ("[$-412]mmm\\-yy", "46023", "2026-01"),
        ("yyyy", "46023", "2026"),
        ("m/d aaa", "46023", "2026-01-01"),
    ])
    def test_date_categories_are_shown_as_dates(self, format_code, serial, expected):
        """When the category axis holds dates (c:cat/c:numRef + a date format), category names came out as Excel serial numbers
        (46023.0). The time is shown only when the format shows it. A format that shows no day (d) or weekday (aaa) gives `YYYY-MM`,
        and with no month either, `YYYY` (the same rule as Excel cell dates — a nonexistent 1st used to be appended, giving
        `2026-02-01`. test_spreadsheet.py::TestDateFormatsWithoutADay)."""
        chart = _chart_xml(f"<c:lineChart>{self._numeric_categories(format_code, [serial])}</c:lineChart>")

        text = _convert_chart(chart)

        assert text == f"[Chart]\n\n| Category | 값 |\n| --- | --- |\n| {expected} | 1 |"

    def test_date_categories_follow_the_1904_date_system(self):
        chart = _chart_xml(f"<c:lineChart>{self._numeric_categories('m/d/yyyy', ['44561'])}</c:lineChart>").replace(
            "<c:chart>", '<c:date1904 val="1"/><c:chart>')

        text = _convert_chart(chart)

        assert "| 2026-01-01 | 1 |" in text

    def test_date_axis_format_is_used_when_the_cache_has_no_date_format(self):
        chart = _chart_xml(f"<c:lineChart>{self._numeric_categories('General', ['46023'])}</c:lineChart>"
                           '<c:dateAx><c:delete val="0"/><c:numFmt formatCode="yyyy/mm/dd" sourceLinked="0"/></c:dateAx>')

        text = _convert_chart(chart)

        assert "| 2026-01-01 | 1 |" in text

    def test_numeric_categories_that_are_not_dates_are_tidied_like_values(self):
        chart = _chart_xml(f"<c:barChart>{self._numeric_categories('General', ['2025.0', '2026.0'])}</c:barChart>")

        text = _convert_chart(chart)

        assert "| 2025 | 1 |\n| 2026 | 2 |" in text

    def test_axis_titles_are_kept_under_the_chart_title(self):
        """Charts whose unit ("단위: 억원") appears only in an axis title are common, yet axis titles were not read. Titles of deleted
        axes (c:delete) are left out."""
        chart = _chart_xml(f"<c:barChart>{_chart_series(0, '매출', ['10'], ['1월'])}</c:barChart>"
                           + _axis("catAx", "b", "기준월") + _axis("valAx", "l", "단위: 억원")
                           + _axis("valAx", "r", "지운 축", deleted=True), title="월별 매출")

        text = _convert_chart(chart)

        assert text == "[Chart] 월별 매출\nCategory axis: 기준월 / Value axis: 단위: 억원\n\n| Category | 매출 |\n| --- | --- |\n| 1월 | 10 |"

    def test_scatter_axis_titles_are_labelled_x_and_y(self):
        series = ('<c:ser><c:order val="0"/><c:tx><c:v>실험</c:v></c:tx><c:xVal><c:numRef><c:numCache><c:pt idx="0"><c:v>10</c:v>'
                  '</c:pt></c:numCache></c:numRef></c:xVal><c:yVal><c:numRef><c:numCache><c:pt idx="0"><c:v>55.5</c:v></c:pt>'
                  "</c:numCache></c:numRef></c:yVal></c:ser>")
        chart = _chart_xml(f"<c:scatterChart>{series}</c:scatterChart>"
                           + _axis("valAx", "l", "수율(%)") + _axis("valAx", "b", "온도(℃)"))

        text = _convert_chart(chart)

        assert text.startswith("[Chart]\nX axis: 온도(℃) / Y axis: 수율(%)\n\n| Series | X | Y |")

    def test_multi_level_categories_join_the_outer_levels(self):
        """For multi-level categories (c:multiLvlStrRef) only the innermost level was read, so it was unclear which year '상반기' and
        '하반기' belonged to. An outer level is written only on the first point of its group and carries on until the next value."""
        def level(points: dict[int, str]) -> str:
            return "<c:lvl>" + "".join(f'<c:pt idx="{index}"><c:v>{text}</c:v></c:pt>' for index, text in points.items()) + "</c:lvl>"

        categories = ('<c:cat><c:multiLvlStrRef><c:f>Sheet1!$A$2:$B$5</c:f><c:multiLvlStrCache><c:ptCount val="4"/>'
                      + level({0: "상반기", 1: "하반기", 2: "상반기", 3: "하반기"}) + level({0: "2024", 2: "2025"})
                      + "</c:multiLvlStrCache></c:multiLvlStrRef></c:cat>")
        series = _chart_series(0, "매출", ["10", "20", "30", "40"]).replace("<c:val>", categories + "<c:val>")

        text = _convert_chart(_chart_xml(f"<c:barChart>{series}</c:barChart>"))

        assert text == ("[Chart]\n\n| Category | 매출 |\n| --- | --- |\n| 2024 상반기 | 10 |\n| 2024 하반기 | 20 |\n"
                        "| 2025 상반기 | 30 |\n| 2025 하반기 | 40 |")

    @pytest.mark.parametrize(("format_code", "value", "expected"), [
        ("0.0%", "0.253", "25.3%"),
        ("0%", "0.125", "13%"),
        ("0.00%", "-0.0525", "-5.25%"),
        ("#,##0.0%;[Red]-#,##0.0%", "12.5", "1,250.0%"),
        ('0.0"%"', "0.253", "0.3%"),  # a quoted % is a literal unit — it is not multiplied by 100 (TestChartValueUnits)
        ("General", "0.253", "0.253"),
        ("0.0%", "해당 없음", "해당 없음"),
    ])
    def test_values_with_a_percent_format_are_shown_as_percentages(self, format_code, value, expected):
        """The value cache's display format is a percentage, yet 0.253 went out and was not read as 25.3%. Other formats without a
        literal unit keep the value as is."""
        series = _chart_series(0, "점유율", [value], ["A"]).replace(
            "<c:numCache>", f"<c:numCache><c:formatCode>{format_code}</c:formatCode>")

        text = _convert_chart(_chart_xml(f"<c:pieChart>{series}</c:pieChart>"))

        assert text.endswith(f"| A | {expected} |")

    def test_point_level_percent_format_applies_to_that_point_only(self):
        series = _chart_series(0, "값", ["0.5", "0.5"], ["A", "B"]).replace(
            '<c:numCache><c:ptCount val="2"/><c:pt idx="0">', '<c:numCache><c:ptCount val="2"/><c:pt idx="0" formatCode="0%">')

        text = _convert_chart(_chart_xml(f"<c:barChart>{series}</c:barChart>"))

        assert text.endswith("| A | 50% |\n| B | 0.5 |")


_WORD_NAMESPACES = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    f'xmlns:mc="{_MC_NS}" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:lc="http://schemas.openxmlformats.org/drawingml/2006/lockedCanvas" '
    f'xmlns:cx="{_CHARTEX_NS}"')


def _docx_with_body(body: str, *, numbering: str | None = None, styles: str = "") -> bytes:
    """A document whose body (inside w:body), numbering definitions (inside w:numbering) and styles (appended at the end of w:styles)
    are given directly as XML. Used for structures python-docx cannot build (shared numbering definitions, form fields, ruby …)."""
    from io import BytesIO

    from docx import Document
    from lxml import etree

    def fragment(xml: str) -> list:
        return list(etree.fromstring(f"<root {_WORD_NAMESPACES}>{xml}</root>"))

    document = Document()
    section = document.element.body[-1]
    for element in fragment(body):
        section.addprevious(element)
    if numbering is not None:
        definitions = document.part.numbering_part.element
        for element in list(definitions):
            definitions.remove(element)
        definitions.extend(fragment(numbering))
    document.styles.element.extend(fragment(styles))
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _paragraph(text: str, num_id: int | None = None, level: int = 0, style: str | None = None) -> str:
    properties = f'<w:pStyle w:val="{style}"/>' if style else ""
    if num_id is not None:
        properties += f'<w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="{num_id}"/></w:numPr>'
    run = f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>' if text else ""
    return f"<w:p><w:pPr>{properties}</w:pPr>{run}</w:p>"


def _number_level(level: int, text: str, number_format: str = "decimal", extra: str = "") -> str:
    return (f'<w:lvl w:ilvl="{level}"><w:start w:val="1"/><w:numFmt w:val="{number_format}"/>{extra}'
            f'<w:lvlText w:val="{text}"/></w:lvl>')


def _number_list(num_id: int, abstract_id: int = 0, override: str = "") -> str:
    return f'<w:num w:numId="{num_id}"><w:abstractNumId w:val="{abstract_id}"/>{override}</w:num>'


class TestDocxNumbering:
    _ABSTRACT = ('<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="multilevel"/>'
                 + _number_level(0, "%1.") + _number_level(1, "%1.%2.") + "</w:abstractNum>")

    def test_lists_sharing_a_definition_continue_counting_until_a_start_override(self):
        """Counters were kept per numId, so another w:num pointing to the same numbering definition (abstractNum) counted from 1 again.
        Word restarts only at a w:num that has a start override (startOverride)."""
        restart = '<w:lvlOverride w:ilvl="0"><w:startOverride w:val="1"/></w:lvlOverride>'
        body = "".join([_paragraph("첫째", 1), _paragraph("둘째", 1), _paragraph("일반 문단"), _paragraph("셋째", 2),
                        _paragraph("하위", 2, 1), _paragraph("넷째", 1), _paragraph("새 목록", 3), _paragraph("새 목록 둘째", 3),
                        _paragraph("이어서", 1)])
        data = _docx_with_body(body, numbering=self._ABSTRACT + _number_list(1) + _number_list(2) + _number_list(3, 0, restart))

        result = convert(data, "a.docx")

        assert result.text == "1. 첫째\n2. 둘째\n\n일반 문단\n\n3. 셋째\n  - 3.1. 하위\n4. 넷째\n1. 새 목록\n2. 새 목록 둘째\n3. 이어서"

    def test_style_numbering_is_inherited_through_based_on(self):
        """Style numbering (numPr) was not looked up in the inherited style (basedOn), so the number was dropped and later numbers
        shifted. The level of a style's numbering is the numbering level that points to that style (w:lvl/w:pStyle)."""
        numbering = ('<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="multilevel"/>'
                     + _number_level(0, "제%1장", extra='<w:pStyle w:val="Chapter"/>')
                     + _number_level(1, "%1.%2", extra='<w:pStyle w:val="Clause"/>') + "</w:abstractNum>" + _number_list(1))
        styles = "".join(
            f'<w:style w:type="paragraph" w:styleId="{style_id}"><w:name w:val="{name}"/>{inner}</w:style>'
            for style_id, name, inner in [
                ("Chapter", "장", '<w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr>'),
                ("Clause", "절", '<w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr>'),
                ("MyChapter", "내 장", '<w:basedOn w:val="Chapter"/>')])
        body = "".join(_paragraph(text, style=style) for text, style in [
            ("총칙", "Chapter"), ("목적", "Clause"), ("정의", "Clause"), ("본칙", "MyChapter"), ("적용", "Clause")])

        result = convert(_docx_with_body(body, numbering=numbering, styles=styles), "a.docx")

        assert result.text == "제1장 총칙\n\n  - 1.1 목적\n  - 1.2 정의\n\n제2장 본칙\n\n  - 2.1 적용"

    def test_definition_linked_to_a_numbering_style_uses_that_style_levels(self):
        """A numbering definition that points to a numbering style (w:numStyleLink) has empty level definitions, so it came out as
        bullets."""
        numbering = ('<w:abstractNum w:abstractNumId="0"><w:styleLink w:val="MyList"/>' + _number_level(0, "%1)")
                     + '</w:abstractNum><w:abstractNum w:abstractNumId="1"><w:numStyleLink w:val="MyList"/></w:abstractNum>'
                     + _number_list(1, 0) + _number_list(2, 1))

        result = convert(_docx_with_body(_paragraph("하나", 2) + _paragraph("둘", 2), numbering=numbering), "a.docx")

        assert result.text == "1) 하나\n\n2) 둘"

    def test_empty_numbered_paragraph_still_counts(self):
        """A numbered paragraph with empty text was not counted, pulling later numbers forward. The number is counted and the empty
        item is not written. A paragraph deleted under track changes is not in the final document, so it is not counted."""
        deleted = ('<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr><w:rPr><w:del w:id="1" w:author="a"/>'
                   '</w:rPr></w:pPr><w:del w:id="2" w:author="a"><w:r><w:delText>지운 항목</w:delText></w:r></w:del></w:p>')
        body = _paragraph("첫째", 1) + _paragraph("", 1) + deleted + _paragraph("셋째", 1)

        result = convert(_docx_with_body(body, numbering=self._ABSTRACT + _number_list(1)), "a.docx")

        assert result.text == "1. 첫째\n3. 셋째"

    def test_level_override_with_its_own_definition_replaces_the_level(self):
        """A w:lvl inside w:lvlOverride (a level whose number text and number format are replaced wholesale) was not read, so '제3조'
        came out as '3.'. The overriding definition changes only the appearance in that w:num and the counter continues that of the
        numbering definition (abstractNum) — the w:start inside the definition does not restart it. A Word-saved file (Apache Tika
        testWORD_override_list_numbering.docx 'Test 4', paragraph text = the number on Word's screen) counts this way."""
        override = ('<w:lvlOverride w:ilvl="0"><w:lvl w:ilvl="0"><w:start w:val="5"/><w:numFmt w:val="decimal"/>'
                    '<w:lvlText w:val="제%1조"/></w:lvl></w:lvlOverride>')
        body = "".join([_paragraph("첫째", 1), _paragraph("둘째", 1), _paragraph("조문A", 2), _paragraph("조문B", 2),
                        _paragraph("이어서", 1)])
        data = _docx_with_body(body, numbering=self._ABSTRACT + _number_list(1) + _number_list(2, 0, override))

        lines = [line for line in convert(data, "a.docx").text.splitlines() if line]

        assert lines == ["1. 첫째", "2. 둘째", "제3조 조문A", "제4조 조문B", "5. 이어서"]

    def test_level_override_restarts_only_with_a_start_override(self):
        """When an overriding level definition comes with a start override (w:startOverride), the count restarts from the
        startOverride value the first time that list appears. A list that only changes the appearance (the uppercase list of Tika
        'Test 4') continues the numbers of the previous list: 'B', 'C'."""
        letters = ('<w:lvlOverride w:ilvl="0"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="upperLetter"/>'
                   '<w:lvlText w:val="%1"/></w:lvl></w:lvlOverride>')
        articles = ('<w:lvlOverride w:ilvl="0"><w:startOverride w:val="5"/><w:lvl w:ilvl="0"><w:start w:val="1"/>'
                    '<w:numFmt w:val="decimal"/><w:lvlText w:val="제%1조"/></w:lvl></w:lvlOverride>')
        body = "".join([_paragraph("하나", 1), _paragraph("둘", 2), _paragraph("셋", 2), _paragraph("넷", 1),
                        _paragraph("조문A", 3), _paragraph("조문B", 3)])
        numbering = self._ABSTRACT + _number_list(1) + _number_list(2, 0, letters) + _number_list(3, 0, articles)

        lines = [line for line in convert(_docx_with_body(body, numbering=numbering), "a.docx").text.splitlines() if line]

        assert lines == ["1. 하나", "B 둘", "C 셋", "4. 넷", "제5조 조문A", "제6조 조문B"]

    def test_level_override_can_turn_a_numbered_level_into_bullets(self):
        override = ('<w:lvlOverride w:ilvl="0"><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/><w:lvlText w:val="•"/></w:lvl>'
                    "</w:lvlOverride>")
        body = _paragraph("번호", 1) + _paragraph("글머리", 2)
        data = _docx_with_body(body, numbering=self._ABSTRACT + _number_list(1) + _number_list(2, 0, override))

        result = convert(data, "a.docx")

        assert result.text == "1. 번호\n- 글머리"

    def test_bullet_at_a_shallower_level_restarts_the_deeper_numbers(self):
        """A bullet level was simply passed over because it counts no numbers, so the numbers under it continued the previous list as
        3 and 4 (the 4-step procedure of a real handover document came out as 6–9). In Word and LibreOffice an item at a shallower
        level, bullet or number, restarts the deeper levels from 1."""
        numbering = ('<w:abstractNum w:abstractNumId="0">' + _number_level(0, "•", "bullet") + _number_level(1, "%2.")
                     + "</w:abstractNum>" + _number_list(1))
        body = "".join(_paragraph(text, 1, level) for text, level in [
            ("신규 입사자", 0), ("계정 신청", 1), ("장비 수령", 1), ("퇴사자", 0), ("계정 회수", 1), ("장비 반납", 1)])

        result = convert(_docx_with_body(body, numbering=numbering), "a.docx")

        assert result.text == "- 신규 입사자\n  1. 계정 신청\n  2. 장비 수령\n- 퇴사자\n  1. 계정 회수\n  2. 장비 반납"

    def test_numbered_paragraph_moved_away_by_tracked_change_does_not_count(self):
        """The original place of a moved paragraph (w:moveFrom on the paragraph mark) had its text left out but was still counted, so
        later numbers shifted by one. Like a deleted paragraph, it is not in the final document. The paragraph at the new place
        (w:moveTo) is counted."""
        number = '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>'
        moved_from = (f'<w:p><w:pPr>{number}<w:rPr><w:moveFrom w:id="1" w:author="a"/></w:rPr></w:pPr>'
                      '<w:moveFrom w:id="2" w:author="a"><w:r><w:t>예산 확보</w:t></w:r></w:moveFrom></w:p>')
        moved_to = (f'<w:p><w:pPr>{number}<w:rPr><w:moveTo w:id="3" w:author="a"/></w:rPr></w:pPr>'
                    '<w:moveTo w:id="4" w:author="a"><w:r><w:t>예산 확보</w:t></w:r></w:moveTo></w:p>')
        body = _paragraph("현황 조사", 1) + moved_from + _paragraph("업체 선정", 1) + moved_to + _paragraph("사업 착수", 1)

        result = convert(_docx_with_body(body, numbering=self._ABSTRACT + _number_list(1)), "a.docx")

        assert result.text == "1. 현황 조사\n2. 업체 선정\n3. 예산 확보\n4. 사업 착수"

    def test_sub_items_with_numbers_other_than_digit_period_keep_their_level(self):
        """Sub-items whose number is not a Markdown list marker, such as 'a.', '1)' or '1.1.', lost their indent in the cleanup step
        and got blank lines around them, so they could not be recognized as sub-items. They are written as `- a. text`, like letter
        numbers in HTML. The top level is left as is."""
        numbering = ('<w:abstractNum w:abstractNumId="0">' + _number_level(0, "%1)") + _number_level(1, "%2.", "lowerLetter")
                     + _number_level(2, "%3.") + _number_level(3, "%3.%4.") + "</w:abstractNum>" + _number_list(1))
        body = "".join(_paragraph(text, 1, level) for text, level in [
            ("개요", 0), ("배경", 1), ("첫째", 2), ("세부", 3), ("목적", 1), ("범위", 0)])

        result = convert(_docx_with_body(body, numbering=numbering), "a.docx")

        assert result.text == "1) 개요\n\n  - a. 배경\n    1. 첫째\n      - 1.1. 세부\n  - b. 목적\n\n2) 범위"

    def test_list_level_without_a_list_id_takes_the_list_of_the_paragraph_style(self):
        """When w:numPr has only a level (ilvl) and no numId, the numbered list is that of the paragraph style. The number was dropped
        entirely."""
        styles = ('<w:style w:type="paragraph" w:styleId="Steps"><w:name w:val="절차"/>'
                  '<w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr></w:style>')
        body = (_paragraph("준비", style="Steps") + _paragraph("확인", style="Steps")
                + '<w:p><w:pPr><w:pStyle w:val="Steps"/><w:numPr><w:ilvl w:val="1"/></w:numPr></w:pPr>'
                  '<w:r><w:t>세부 확인</w:t></w:r></w:p>')

        result = convert(_docx_with_body(body, numbering=self._ABSTRACT + _number_list(1), styles=styles), "a.docx")

        assert result.text == "1. 준비\n2. 확인\n  - 2.1. 세부 확인"

    def test_level_without_a_number_format_is_a_plain_paragraph(self):
        """A level with no number format (numFmt none, an explanatory paragraph under a number) became a `- ` bullet item, so the
        explanation read like a list item. As in Word, that level's number in the number text (lvlText) is emptied, and if no text
        remains the paragraph has no marker."""
        numbering = ('<w:abstractNum w:abstractNumId="0"><w:multiLevelType w:val="multilevel"/>'
                     + _number_level(0, "%1.") + _number_level(1, "", "none") + _number_level(2, "%3", "none")
                     + "</w:abstractNum>" + _number_list(1))
        body = "".join([_paragraph("사업 개요", 1), _paragraph("본 사업은 일자리 창출을 목적으로 한다.", 1, 1),
                        _paragraph("추진 일정", 1), _paragraph("2024년 상반기 착수 예정이다.", 1, 2)])

        result = convert(_docx_with_body(body, numbering=numbering), "a.docx")

        assert result.text == ("1. 사업 개요\n\n본 사업은 일자리 창출을 목적으로 한다.\n\n2. 추진 일정\n\n"
                               "2024년 상반기 착수 예정이다.")


def _indented_level(level: int, text: str, left: int, number_format: str = "decimal") -> str:
    return _number_level(level, text, number_format, extra=f'<w:pPr><w:ind w:left="{left}" w:hanging="360"/></w:pPr>')


class TestDocxListDepthCheckBoxesAndLongHeadings:
    """Review round 20: sub-items made with a different numbered list, list levels inside a cell, black-highlighted boxes, long body
    text in a heading style."""

    @staticmethod
    def _cell(*paragraphs: str) -> str:
        return f"<w:tc>{''.join(paragraphs)}</w:tc>"

    def test_item_of_another_list_nests_under_the_item_it_is_indented_from(self):
        """In a document that makes sub-items with a different numbered list (numId), their level (ilvl) is 0, so the items under
        '1.1 지표명' came out at the top level (a real agreement proposal). Where the list changes, the depth is decided by the indent
        on Word's screen (paragraph → numbering level → w:ind of the style)."""
        numbering = ('<w:abstractNum w:abstractNumId="0">' + _indented_level(0, "%1.", 800) + _indented_level(1, "%1.%2", 1160)
                     + '</w:abstractNum><w:abstractNum w:abstractNumId="1">' + _indented_level(0, "▶", 1600, "bullet")
                     + '</w:abstractNum><w:abstractNum w:abstractNumId="2">' + _indented_level(0, "-", 2000, "bullet")
                     + "</w:abstractNum>" + _number_list(1, 0) + _number_list(2, 1) + _number_list(3, 2))
        items = [_paragraph(text, num_id, level) for text, num_id, level in [
            ("지표 개요", 1, 0), ("지표명", 1, 1), ("온실가스 배출 저감", 2, 0), ("사회문제", 1, 1), ("기후위기 심화", 2, 0),
            ("평균 기온 상승", 3, 0), ("해수면 상승", 3, 0), ("기술 개발 지연", 2, 0), ("SV성과 정의", 1, 0)]]
        # Continuing text indented to match the text of '1.2 사회문제' does not close that item (only more-indented items are closed).
        continuation = '<w:p><w:pPr><w:ind w:left="1160"/></w:pPr><w:r><w:t>이어지는 설명</w:t></w:r></w:p>'
        body = "".join(items[:7]) + continuation + "".join(items[7:])

        result = convert(_docx_with_body(body, numbering=numbering), "a.docx")

        assert result.text == ("1. 지표 개요\n  - 1.1 지표명\n    - 온실가스 배출 저감\n  - 1.2 사회문제\n    - 기후위기 심화\n"
                               "      - 평균 기온 상승\n      - 해수면 상승\n\n이어지는 설명\n\n    - 기술 개발 지연\n2. SV성과 정의")

    def test_indentation_counts_the_paragraph_and_style_indents_and_body_text_ends_the_nesting(self):
        """The paragraph's w:ind comes before that of the numbering level (leftChars="0" turns off the style's character-unit indent).
        After a body paragraph at the margin, counting follows the list levels again."""
        numbering = ('<w:abstractNum w:abstractNumId="0">' + _indented_level(0, "%1.", 360)
                     + '</w:abstractNum><w:abstractNum w:abstractNumId="1">' + _indented_level(0, "▶", 1240, "bullet")
                     + "</w:abstractNum>" + _number_list(1, 0) + _number_list(2, 1) + _number_list(3, 1))
        styles = ('<w:style w:type="paragraph" w:styleId="ListPara"><w:name w:val="목록 단락"/>'
                  '<w:pPr><w:ind w:leftChars="400" w:left="800"/></w:pPr></w:style>')
        same_as_number = ('<w:p><w:pPr><w:pStyle w:val="ListPara"/><w:numPr><w:ilvl w:val="0"/><w:numId w:val="3"/></w:numPr>'
                          '<w:ind w:leftChars="0" w:left="360"/></w:pPr><w:r><w:t>나란한 항목</w:t></w:r></w:p>')
        body = (_paragraph("현황", 1) + _paragraph("세부", 2, style="ListPara") + same_as_number + _paragraph("본문 설명")
                + _paragraph("새 글머리", 2))

        result = convert(_docx_with_body(body, numbering=numbering, styles=styles), "a.docx")

        assert result.text == "1. 현황\n  - 세부\n- 나란한 항목\n\n본문 설명\n\n- 새 글머리"

    def test_hand_typed_dash_paragraph_continuing_an_item_is_indented_under_it(self):
        """A paragraph with a hand-typed '- ' and no numbering, even when indented to the text position of the previous item
        (continuing text that does not close the list), came out at column 0 and read like a top-level item right after a deep item
        (a real agreement proposal). It is written as a list line one level below the remaining item. '- ' lines joined by line
        breaks are not merged into one line but written separately, and the other lines stay as they are. A '- ' paragraph at the
        margin stays as it is too."""
        numbering = ('<w:abstractNum w:abstractNumId="0">' + _indented_level(0, "%1.", 800) + _indented_level(1, "%1.%2", 1160)
                     + '</w:abstractNum><w:abstractNum w:abstractNumId="1">' + _indented_level(0, "▪", 1600, "bullet")
                     + "</w:abstractNum>" + _number_list(1, 0) + _number_list(2, 1))

        def typed(text: str, left: int) -> str:
            return f'<w:p><w:pPr><w:ind w:left="{left}" w:firstLine="195"/></w:pPr><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'

        body = (_paragraph("지표 개요", 1, 0) + _paragraph("지표명", 1, 1) + _paragraph("업무 영역", 2, 0)
                + typed("- 엘디카본 : 열분해", 1600) + typed("-   폐타이어 감축", 1160)
                + typed("- 실시간 측정</w:t><w:br/><w:t>- 이상 감지 시 관리자,</w:t><w:br/><w:t>관제실에 알림", 1160)
                + _paragraph("본문") + _paragraph("- 여백 메모"))

        result = convert(_docx_with_body(body, numbering=numbering), "a.docx")

        assert result.text == ("1. 지표 개요\n  - 1.1 지표명\n    - 업무 영역\n      - 엘디카본 : 열분해\n    - 폐타이어 감축\n"
                               "    - 실시간 측정\n    - 이상 감지 시 관리자,\n관제실에 알림\n\n본문\n\n- 여백 메모")

    def test_line_breaks_inside_a_list_item_keep_their_lines(self):
        """Hand-typed bullet lines joined by line breaks (w:br) inside a numbered or bulleted item paragraph were merged with spaces
        into one 600-character line (a real review request: 'Activity - (SKI) 신규 Pipeline 발굴 : … - (SKI) 탄소배출권 투자 유치 : …').
        The first line is the item, a line starting with a bullet such as '- ', '·' or '○' is an item one level below ('- ' is added
        if absent), and the other lines continue the line before. A '* ' line (actually the gloss of '사회문제*') is a sub-item too —
        at column 0 Markdown would read it as a top-level item and the following items would go under it. Inside a cell the lines
        are separated by `<br>` and sub-items are marked with `└ ` like a list in a cell."""
        numbering = ('<w:abstractNum w:abstractNumId="0">' + _number_level(0, "%1.") + _number_level(1, "-", "bullet")
                     + "</w:abstractNum>" + _number_list(1, 0))
        br = '</w:t></w:r><w:r><w:br/></w:r><w:r><w:t xml:space="preserve">'
        item = _paragraph(f"Activity{br}- (SKI) 신규 발굴{br} : 플랫폼 협업{br}{br}· 투자   유치{br}○ 네트워크{br}* 주석", 1, 1)
        body = (_paragraph("Impact pathway", 1, 0) + item + _paragraph("Output", 1, 1)
                + "<w:tbl><w:tr>" + self._cell(_paragraph("활동")) + self._cell(item) + "</w:tr></w:tbl>")

        result = convert(_docx_with_body(body, numbering=numbering), "a.docx")

        assert result.text == ("1. Impact pathway\n  - Activity\n    - (SKI) 신규 발굴\n: 플랫폼 협업\n    - · 투자 유치\n"
                               "    - ○ 네트워크\n    * 주석\n  - Output\n\n"
                               "| 활동 | └ - Activity<br>└─ - (SKI) 신규 발굴<br>: 플랫폼 협업<br>└─ - · 투자 유치<br>└─ - ○ 네트워크<br>└─ * 주석 |\n"
                               "| --- | --- |")

    @pytest.mark.parametrize("left", [800, 0])
    def test_alternating_lists_at_the_same_indent_do_not_staircase(self, left):
        """When a numbered list (numId 1) and a bulleted list (numId 2) alternate at the same indent, each change of list inherited the
        depth next to the previous item and went one level deeper per chapter (a staircase). Where the indent is the same and the
        depth cannot be known, the list returns to the shift it used before. The same holds for a document whose indents are all 0."""
        numbering = ('<w:abstractNum w:abstractNumId="0">' + _indented_level(0, "%1.", left) + _indented_level(1, "%1.%2", left)
                     + '</w:abstractNum><w:abstractNum w:abstractNumId="1">' + _indented_level(0, "▶", left, "bullet")
                     + "</w:abstractNum>" + _number_list(1, 0) + _number_list(2, 1))
        body = "".join(_paragraph(f"장{i}", 1, 0) + _paragraph(f"소절{i}", 1, 1) + _paragraph("글머리", 2, 0) for i in range(1, 4))

        result = convert(_docx_with_body(body, numbering=numbering), "a.docx")

        assert result.text == ("1. 장1\n  - 1.1 소절1\n  - 글머리\n2. 장2\n  - 2.1 소절2\n  - 글머리\n"
                               "3. 장3\n  - 3.1 소절3\n  - 글머리")

    def test_margin_dash_paragraph_after_a_list_without_indent_stays_at_column_0(self):
        """A hand-typed '- ' paragraph at the margin after a list with indent 0 could not close the previous item and was indented as
        an item under it. Body text at the margin closes everything."""
        numbering = ('<w:abstractNum w:abstractNumId="0">' + _indented_level(0, "%1.", 0) + "</w:abstractNum>"
                     + _number_list(1, 0))
        body = _paragraph("첫 항목", 1) + _paragraph("- 여백 메모")

        result = convert(_docx_with_body(body, numbering=numbering), "a.docx")

        assert result.text == "1. 첫 항목\n- 여백 메모"

    def test_text_box_in_a_list_item_does_not_close_the_items_before_it(self):
        """A text box inside a list item paragraph is rendered before the item, and the margin paragraph of the text box closed the
        earlier items of the body, so that item came out at the top level. Lists inside a text box are counted separately, as in a
        cell."""
        numbering = ('<w:abstractNum w:abstractNumId="0">' + _indented_level(0, "%1.", 800)
                     + '</w:abstractNum><w:abstractNum w:abstractNumId="1">' + _indented_level(0, "▶", 1600, "bullet")
                     + "</w:abstractNum>" + _number_list(1, 0) + _number_list(2, 1))
        box = "<w:r><w:pict><w:txbxContent><w:p><w:r><w:t>상자 글</w:t></w:r></w:p></w:txbxContent></w:pict></w:r>"
        host = ('<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="2"/></w:numPr></w:pPr>'
                f'<w:r><w:t>세부</w:t></w:r>{box}</w:p>')
        body = _paragraph("개요", 1) + host + _paragraph("다음 세부", 2)

        result = convert(_docx_with_body(body, numbering=numbering), "a.docx")

        assert result.text == "1. 개요\n  - 세부\n\n상자 글\n\n  - 다음 세부"

    def test_list_levels_inside_a_table_cell_are_marked_like_hwp(self):
        """The indent of a list inside a cell is erased when the table is built (`markdown._clean_cell`), so sub-items were flattened.
        The level is marked with `└ ` / `└─ ` as in HWP and HWPX."""
        numbering = ('<w:abstractNum w:abstractNumId="0">' + _number_level(0, "□", "bullet") + _number_level(1, "○", "bullet")
                     + _number_level(2, "-", "bullet") + '</w:abstractNum><w:abstractNum w:abstractNumId="1">'
                     + _number_level(0, "%1.") + _number_level(1, "%2.", "ganada") + "</w:abstractNum>"
                     + _number_list(1, 0) + _number_list(2, 1))
        scope = [_paragraph(text, 1, level) for text, level in [("사업 범위", 0), ("시스템 구축", 1), ("서버 10대", 2), ("교육", 0)]]
        plan = [_paragraph(text, 2, level) for text, level in [("1단계", 0), ("설계", 1), ("구현", 1), ("2단계", 0)]]
        table = ("<w:tbl><w:tr>" + self._cell(_paragraph("범위")) + self._cell(*scope) + "</w:tr><w:tr>"
                 + self._cell(_paragraph("일정")) + self._cell(*plan) + "</w:tr></w:tbl>")

        result = convert(_docx_with_body(table, numbering=numbering), "a.docx")

        assert result.text == ("| 범위 | - 사업 범위<br>└ - 시스템 구축<br>└─ - 서버 10대<br>- 교육 |\n| --- | --- |\n"
                               "| 일정 | 1. 1단계<br>└ 가. 설계<br>└ 나. 구현<br>2. 2단계 |")

    def test_box_painted_with_a_black_highlight_is_a_filled_box(self):
        """A □ painted with a black highlight (or dark shading) is a filled box on screen, i.e. a selected item, but it came out as □,
        the same as an unselected one. Only a run painting just the box is changed — a run painting the text too is inverted
        emphasis, and the meaning of a box with a light font colour cannot be asserted."""
        def run(text: str, properties: str = "") -> str:
            return f'<w:r><w:rPr>{properties}</w:rPr><w:t xml:space="preserve">{text}</w:t></w:r>'

        black = '<w:highlight w:val="black"/>'
        body = "".join(f"<w:p>{runs}</w:p>" for runs in [
            run("□", black) + run(" 비용형"), run("□ 편익형"), run("☐", '<w:shd w:val="clear" w:color="auto" w:fill="1F1F1F"/>')
            + run(" 시장평균"), run("□", black + '<w:color w:val="FFFFFF"/>') + run(" 흰 글자"), run("□ 반전 강조", black),
            run("□", '<w:highlight w:val="yellow"/>') + run(" 노란 형광펜")])

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text.split("\n\n") == ["■ 비용형", "□ 편익형", "☒ 시장평균", "□ 흰 글자", "□ 반전 강조", "□ 노란 형광펜"]

    def test_long_paragraph_with_an_outline_level_is_not_a_heading(self):
        """A long body paragraph with an outline level or heading style (the '개요 2' body of a DOCX converted by Hancom, including a
        line break) became a '## ' heading in its entirety. As in HWP and HWPX, over 120 characters it is not treated as a heading."""
        styles = '<w:style w:type="paragraph" w:styleId="Outline2"><w:name w:val="개요 2"/><w:pPr><w:outlineLvl w:val="1"/></w:pPr></w:style>'
        sentence = "본 사업은 2026년 3월부터 12월까지 진행하며, 총 사업비는 12억원이다."
        with_break = (f'<w:p><w:pPr><w:pStyle w:val="Outline2"/></w:pPr><w:r><w:t>{sentence * 3}</w:t><w:br/>'
                      "<w:t>단, 부가세는 별도이다.</w:t></w:r></w:p>")
        direct = f'<w:p><w:pPr><w:outlineLvl w:val="0"/></w:pPr><w:r><w:t>{sentence * 3}</w:t></w:r></w:p>'
        body = _paragraph("사업 개요", style="Outline2") + with_break + direct

        result = convert(_docx_with_body(body, styles=styles), "a.docx")

        assert result.text == f"## 사업 개요\n\n{sentence * 3}\n단, 부가세는 별도이다.\n\n{sentence * 3}"

    def test_title_and_heading_with_a_manual_line_break_stay_headings(self):
        """A real title with a manual line break (w:br) in a heading or cover title ('2023년 신규지표↵1차 합의 제안서') fell to body
        text because of the line break. The line break is not used for heading detection (only the length is) and is merged with a
        space into a one-line heading. Unlike HWP '개요', a Word heading-style paragraph looks like a heading on screen too."""
        def heading(style: str, first: str, second: str) -> str:
            return (f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr><w:r><w:t>{first}</w:t><w:br/>'
                    f"<w:t>{second}</w:t></w:r></w:p>")

        body = heading("Title", "2023년 신규지표", "1차 합의 제안서") + heading("Heading1", "제2장 추진 배경 및", "필요성")

        result = convert(_docx_with_body(body + _paragraph("본문")), "a.docx")

        assert result.text == "# 2023년 신규지표 1차 합의 제안서\n\n# 제2장 추진 배경 및 필요성\n\n본문"


class TestDocxFieldsAndRuns:
    @staticmethod
    def _run(text: str, properties: str = "") -> str:
        return f'<w:r><w:rPr>{properties}</w:rPr><w:t xml:space="preserve">{text}</w:t></w:r>'

    @staticmethod
    def _form_field(kind: str, data: str, result: str = "") -> str:
        return (f'<w:r><w:fldChar w:fldCharType="begin"><w:ffData><w:name w:val="f"/>{data}</w:ffData></w:fldChar></w:r>'
                f'<w:r><w:instrText xml:space="preserve"> {kind} </w:instrText></w:r>'
                f'<w:r><w:fldChar w:fldCharType="separate"/></w:r>{result}<w:r><w:fldChar w:fldCharType="end"/></w:r>')

    def test_legacy_form_field_states_are_kept(self):
        """The check box state and drop-down selection of legacy form fields (w:ffData) are attributes, not text, so they vanished."""
        body = ("<w:p>" + self._run("동의: ")
                + self._form_field("FORMCHECKBOX", '<w:checkBox><w:default w:val="0"/><w:checked/></w:checkBox>')
                + self._run(" 예 ") + self._form_field("FORMCHECKBOX", '<w:checkBox><w:default w:val="1"/></w:checkBox>')
                + self._run(" 기본 ")
                + self._form_field("FORMCHECKBOX", '<w:checkBox><w:default w:val="1"/><w:checked w:val="0"/></w:checkBox>')
                + self._run(" 아니오") + "</w:p><w:p>" + self._run("부서: ")
                + self._form_field("FORMDROPDOWN", '<w:ddList><w:result w:val="1"/><w:listEntry w:val="영업부"/>'
                                                   '<w:listEntry w:val="개발부"/></w:ddList>')
                + self._run(" / ") + self._form_field("FORMDROPDOWN", '<w:ddList><w:listEntry w:val="서울"/></w:ddList>')
                + "</w:p><w:p>" + self._run("이름: ")
                + self._form_field("FORMTEXT", "<w:textInput/>", self._run("홍길동")) + "</w:p>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "동의: ☒ 예 ☒ 기본 ☐ 아니오\n\n부서: 개발부 / 서울\n\n이름: 홍길동"

    def test_text_of_a_pasted_drawing_shape_is_kept(self):
        """Text of a shape pasted from PowerPoint (lc:lockedCanvas) is a:t, not w:t, so it was dropped. It is placed after the
        paragraph like a text box."""
        shape = ("<a:sp><a:txBody><a:bodyPr/><a:p><a:r><a:t>붙여 넣은 </a:t></a:r><a:r><a:t>도형</a:t></a:r></a:p>"
                 "<a:p><a:r><a:t>둘째 줄</a:t></a:r></a:p></a:txBody></a:sp>")
        canvas_uri = "http://schemas.openxmlformats.org/drawingml/2006/lockedCanvas"
        body = (f'<w:p>{self._run("앞 글")}<w:r><w:drawing><wp:inline><wp:extent cx="1" cy="1"/><wp:docPr id="1" name="x"/>'
                f'<a:graphic><a:graphicData uri="{canvas_uri}"><lc:lockedCanvas>{shape}</lc:lockedCanvas></a:graphicData>'
                f'</a:graphic></wp:inline></w:drawing></w:r></w:p><w:p>{self._run("뒤 문단")}</w:p>')

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "앞 글\n\n붙여 넣은 도형\n\n둘째 줄\n\n뒤 문단"

    def test_ruby_is_written_as_base_then_annotation(self):
        """In ruby (w:ruby) the annotation (w:rt) is stored before the base (w:rubyBase), so it came out joined as '한국韓國'. It is
        written in the same shape as HWP ruby."""
        ruby = f"<w:r><w:ruby><w:rubyPr/><w:rt>{self._run('한국')}</w:rt><w:rubyBase>{self._run('韓國')}</w:rubyBase></w:ruby></w:r>"

        result = convert(_docx_with_body(f"<w:p>{ruby}{self._run(' 방문')}</w:p>"), "a.docx")

        assert result.text == "韓國(한국) 방문"

    def test_hidden_run_is_left_out(self):
        """Hidden text (w:vanish) is text not shown on screen. With `w:val` 0 or false it is not hidden."""
        body = ("<w:p>" + self._run("보이는 글") + self._run(" 숨긴 글", "<w:vanish/>")
                + self._run(" 다시 보임", '<w:vanish w:val="0"/>') + self._run(" 끝", '<w:vanish w:val="false"/>') + "</w:p>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "보이는 글 다시 보임 끝"

    def test_equation_array_rows_and_accents_are_kept(self):
        """Rows of an equation array (m:eqArr) were joined with no separator (`a+b=1a-b=3`) and accents (m:acc) and overbars (m:bar)
        were dropped."""
        def math_run(text: str) -> str:
            return f"<m:r><m:t>{text}</m:t></m:r>"

        def accent(char: str, base: str) -> str:
            properties = f'<m:accPr><m:chr m:val="{char}"/></m:accPr>' if char else ""
            return f"<m:acc>{properties}<m:e>{math_run(base)}</m:e></m:acc>"

        body = ("<w:p><m:oMath><m:eqArr><m:e>" + math_run("a+b=1") + "</m:e><m:e>" + math_run("a-b=3") + "</m:e></m:eqArr>"
                "</m:oMath></w:p><w:p><m:oMath>" + accent("\u0305", "x") + accent("", "y") + accent("\u20d7", "AB")
                + accent("\u030c", "z") + '<m:bar><m:barPr><m:pos m:val="top"/></m:barPr><m:e>' + math_run("w")
                + "</m:e></m:bar></m:oMath></w:p>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "$a+b=1 \\\\ a-b=3$\n\n$\\bar{x}\\hat{y}\\vec{AB}\u017e\\overline{w}$"

    def test_chartex_falls_back_to_its_picture_or_a_placeholder(self):
        """A new chart type (chartex) has nothing readable in mc:Choice, and mc:Fallback (the pre-rendered picture) was always
        skipped, so it vanished entirely."""
        from io import BytesIO

        from docx import Document
        from docx.shared import Inches

        document = Document()
        document.add_paragraph("앞 문단")
        document.add_paragraph().add_run().add_picture(BytesIO(builder.png_bytes(seed=5)), width=Inches(2))
        buffer = BytesIO()
        document.save(buffer)
        choice = (f'<mc:Choice Requires="cx1">{_docx_inline(f'<cx:chart xmlns:cx="{_CHARTEX_NS}" '
                  f'xmlns:r="{_RELATIONSHIPS_NS}" r:id="rId99"/>', _CHARTEX_NS)}</mc:Choice>')

        def wrap(fallback: bool) -> Callable[[str], str]:
            def edit(xml: str) -> str:
                drawing = re.search(r"<w:drawing>.*?</w:drawing>", xml, re.DOTALL).group(0)
                return xml.replace(drawing, f'<mc:AlternateContent xmlns:mc="{_MC_NS}">{choice}'
                                            f'{f"<mc:Fallback>{drawing}</mc:Fallback>" if fallback else ""}</mc:AlternateContent>')

            return edit

        with_picture = convert(builder.zip_with(buffer.getvalue(), edit={"word/document.xml": wrap(True)}), "a.docx")
        without = convert(builder.zip_with(buffer.getvalue(), edit={"word/document.xml": wrap(False)}), "a.docx")

        assert with_picture.text == "앞 문단\n\n[Image]"
        assert len(with_picture.images) == 1
        assert without.text == "앞 문단\n\n[Chart]"

    def test_chartex_data_is_rendered_as_a_table(self):
        chart = f'<cx:chart xmlns:cx="{_CHARTEX_NS}" xmlns:r="{_RELATIONSHIPS_NS}" r:id="rIdCx"/>'
        relationship = ('<Relationship Id="rIdCx" Type="http://schemas.microsoft.com/office/2014/relationships/chartEx" '
                        'Target="charts/chartEx1.xml"/></Relationships>')
        body = (f'<w:p><w:r><w:t>분석:</w:t></w:r><w:r><mc:AlternateContent xmlns:mc="{_MC_NS}"><mc:Choice Requires="cx1">'
                f"{_docx_inline(chart, _CHARTEX_NS)}</mc:Choice></mc:AlternateContent></w:r></w:p>")
        data = builder.zip_with(
            _docx_with_table(),
            edit={"word/document.xml": lambda xml: xml.replace("<w:sectPr", body + "<w:sectPr", 1),
                  "word/_rels/document.xml.rels": lambda xml: xml.replace("</Relationships>", relationship)},
            add={"word/charts/chartEx1.xml": _chartex_xml("손익 분석", ["매출", "원가"], ["100", "-60"])})

        result = convert(data, "a.docx")

        assert result.text.endswith("분석:\n\n[Chart] 손익 분석\n\n| Category | 금액 |\n| --- | --- |\n| 매출 | 100 |\n| 원가 | -60 |")


_MATH_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_A14_NS = "http://schemas.microsoft.com/office/drawing/2010/main"
# S=πr² (the shape PowerPoint writes: a14:m > m:oMathPara > m:oMath)
_AREA_EQUATION = (f'<a14:m xmlns:a14="{_A14_NS}"><m:oMathPara xmlns:m="{_MATH_NS}"><m:oMath><m:r><m:t>S=π</m:t></m:r><m:sSup>'
                  "<m:e><m:r><m:t>r</m:t></m:r></m:e><m:sup><m:r><m:t>2</m:t></m:r></m:sup></m:sSup></m:oMath></m:oMathPara>"
                  "</a14:m>")


def _pptx_with_runs(runs: str, *, in_table: bool = False) -> bytes:
    """A slide with `runs` (XML inside a:p) put in place of the runs of a text box (or table cell) paragraph."""
    from io import BytesIO

    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[5])
    slide.shapes.title.text = "수식"
    if in_table:
        slide.shapes.add_table(1, 1, Inches(1), Inches(2), Inches(4), Inches(1)).table.cell(0, 0).text = "자리"
    else:
        slide.shapes.add_textbox(Inches(1), Inches(2), Inches(4), Inches(1)).text_frame.text = "자리"
    buffer = BytesIO()
    presentation.save(buffer)
    return builder.zip_with(buffer.getvalue(), edit={
        "ppt/slides/slide1.xml": lambda xml: re.sub(r"<a:r>(?:(?!<a:r>).)*?<a:t>자리</a:t></a:r>", lambda _: runs, xml, flags=re.DOTALL)})


def _text_run(text: str) -> str:
    return f'<a:r><a:rPr lang="ko-KR"/><a:t>{text}</a:t></a:r>'


class TestPptxEquations:
    """An equation inside a paragraph (OMML inside a14:m) is not a run, so it was dropped without a warning. It is kept in place in
    the same notation as DOCX (` $LaTeX$ `)."""

    def test_equation_inside_a_paragraph_is_kept_as_latex(self):
        data = _pptx_with_runs(_text_run("면적 공식: ") + _AREA_EQUATION + _text_run(" 입니다"))

        result = convert(data, "a.pptx")

        assert result.text == "## Slide 1: 수식\n\n면적 공식: $S=πr^2$ 입니다"

    def test_equation_inside_a_table_cell_is_kept(self):
        data = _pptx_with_runs(_text_run("넓이") + _AREA_EQUATION, in_table=True)

        result = convert(data, "a.pptx")

        assert result.text == "## Slide 1: 수식\n\n넓이 $S=πr^2$"  # a one-cell table comes out as a paragraph

    def test_equation_in_a_run_level_alternate_content_is_written_once(self):
        """PowerPoint wraps an equation inside a paragraph too in mc:Choice (a14:m) + mc:Fallback (text or picture for old versions).
        The two are not both read."""
        wrapped = (f'<mc:AlternateContent xmlns:mc="{_MC_NS}"><mc:Choice Requires="a14">{_AREA_EQUATION}</mc:Choice>'
                   f'<mc:Fallback>{_text_run("S=πr2")}</mc:Fallback></mc:AlternateContent>')
        data = _pptx_with_runs(_text_run("면적 공식: ") + wrapped + _text_run(" 입니다"))

        result = convert(data, "a.pptx")

        assert result.text == "## Slide 1: 수식\n\n면적 공식: $S=πr^2$ 입니다"

    def test_alternate_content_without_a_readable_choice_uses_the_fallback_text(self):
        wrapped = (f'<mc:AlternateContent xmlns:mc="{_MC_NS}"><mc:Choice Requires="x99"><x:new xmlns:x="urn:x"/></mc:Choice>'
                   f'<mc:Fallback>{_text_run("대체 글")}</mc:Fallback></mc:AlternateContent>')
        data = _pptx_with_runs(_text_run("앞 ") + wrapped)

        result = convert(data, "a.pptx")

        assert result.text == "## Slide 1: 수식\n\n앞 대체 글"

    def test_equation_in_a_text_box_wrapped_in_alternate_content_is_kept(self):
        """A text box holding an equation is wrapped in mc:AlternateContent as a whole shape (the fallback is a picture). The equation
        of the new-version shape is read."""
        def wrap(xml: str) -> str:
            shape = re.search(r"<p:sp>(?:(?!<p:sp>).)*?S=π.*?</p:sp>", xml, re.DOTALL).group(0)
            return xml.replace(shape, f'<mc:AlternateContent xmlns:mc="{_MC_NS}"><mc:Choice xmlns:a14="{_A14_NS}" '
                                      f'Requires="a14">{shape}</mc:Choice><mc:Fallback><p:pic><p:nvPicPr><p:cNvPr id="9" '
                                      'name="대체 그림"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr><p:blipFill/><p:spPr/></p:pic>'
                                      "</mc:Fallback></mc:AlternateContent>")

        data = builder.zip_with(_pptx_with_runs(_text_run("면적 공식: ") + _AREA_EQUATION), edit={"ppt/slides/slide1.xml": wrap})

        result = convert(data, "a.pptx")

        assert result.text == "## Slide 1: 수식\n\n면적 공식: $S=πr^2$"


class TestDocxStruckDeletedAndSymbolRuns:
    @staticmethod
    def _run(text: str, properties: str = "") -> str:
        return f'<w:r><w:rPr>{properties}</w:rPr><w:t xml:space="preserve">{text}</w:t></w:r>'

    @staticmethod
    def _symbols(font: str, *codes: str) -> str:
        return "".join(f'<w:r><w:sym w:font="{font}" w:char="{code}"/></w:r>' for code in codes)

    def test_struck_runs_are_marked_like_html_strikethrough(self):
        """Writing struck text as plain text makes '10,000,000원 8,000,000원' read as two valid values. It is written in the same
        notation as `<s>` in HTML. Consecutive struck runs are grouped into one, and runs with `w:val` 0 or false and
        whitespace-only runs are not marked."""
        body = ("<w:p>" + self._run("계약 금액: ") + self._run("10,000,", "<w:strike/>") + self._run("000원 ", "<w:strike/>")
                + self._run("8,000,000원") + "</w:p><w:p>" + self._run("납기: ") + self._run("3월 31일", "<w:dstrike/>")
                + self._run(" 4월 15일") + self._run(" ", "<w:strike/>") + self._run("확정", '<w:strike w:val="0"/>')
                + self._run("본", '<w:dstrike w:val="false"/>') + "</w:p>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "계약 금액: ~~10,000,000원~~ 8,000,000원\n\n납기: ~~3월 31일~~ 4월 15일 확정본"

    def test_struck_superscript_run_keeps_both_marks(self):
        body = ("<w:p>" + self._run("면적 10") + self._run("-", '<w:strike/><w:vertAlign w:val="superscript"/>')
                + self._run("3", '<w:strike/><w:vertAlign w:val="superscript"/>') + self._run(" 끝") + "</w:p>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "면적 10~~^{-3}~~ 끝"

    def test_symbol_font_characters_become_unicode(self):
        """w:sym in the Symbol font (≥ ± ° μ) was moved to the private use area and erased entirely ('≥ 5, ±3%' became '5, 3%'). The
        character code comes in both forms, F0B3 and 00B3. E0, D8 and A8 of Symbol have Symbol's meaning, not that of Wingdings
        (→ ➢ □)."""
        body = ("<w:p>" + self._symbols("Symbol", "F0B3") + self._run(" 5, ") + self._symbols("Symbol", "00B1")
                + self._run("3%, 20") + self._symbols("Symbol", "F0B0") + self._run("C, ") + self._symbols("Symbol", "F06D")
                + self._run("m, ") + self._symbols("Symbol", "F0A3", "F0B4", "F0B8", "F0B9", "F0BB", "F0A5", "F0E5", "F0D6",
                                                   "F0AE", "F0AC", "F0AD", "F0AF", "F0B7", "F044", "F057", "F061", "F070")
                + self._run(" / ") + self._symbols("Symbol", "F0E0", "F0D8", "F0A8", "F028", "F02D", "F029") + "</w:p>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "≥ 5, ±3%, 20°C, μm, ≤×÷≠≈∞∑√→←↑↓•ΔΩαπ / ◊¬♦(−)"

    def test_wingdings_check_marks_and_boxes_become_unicode(self):
        """Checks, boxes and arrows of Wingdings / Wingdings 2 vanished, so '☒ 예 □ 아니오' became '예 아니오'."""
        body = ("<w:p>" + self._symbols("Wingdings", "F0FD") + self._run(" 예 ") + self._symbols("Wingdings", "F0A8")
                + self._run(" 아니오 ") + self._symbols("Wingdings", "F06F", "00FE", "F0FB", "F0FC", "F0E0", "F0DF", "F081")
                + self._run(" / ") + self._symbols("Wingdings 2", "F052") + self._run(" 찬성 ")
                + self._symbols("Wingdings 2", "00A3") + self._run(" 반대 ") + self._symbols("Wingdings 2", "F053", "F054", "F050")
                + "</w:p>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "☒ 예 □ 아니오 □☑✗✓→←① / ☑ 찬성 ☐ 반대 ☒☒✓"

    def test_symbol_outside_the_tables_keeps_the_private_use_fallback(self):
        """A character not in the tables is moved to the private use area as before (clean_text converts only those it knows and
        erases the rest)."""
        body = "<w:p>" + self._run("앞") + self._symbols("Webdings", "F0FC") + self._symbols("Wingdings", "F0FF") + self._run("뒤") + "</w:p>"

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "앞✓뒤"

    def test_content_of_a_tracked_deletion_is_left_out(self):
        """Text inside w:del is w:delText and is left out, but footnote references and pictures came out as if part of the final
        document. It is skipped entirely, like the original place of moved text."""
        body = ("<w:p>" + self._run("본문") + '<w:del w:id="1" w:author="a"><w:r><w:delText>지운 글</w:delText></w:r>'
                '<w:r><w:footnoteReference w:id="2"/></w:r></w:del><w:ins w:id="2" w:author="a">' + self._run("넣은 글")
                + "</w:ins></w:p>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "본문넣은 글"

    def test_picture_inside_a_tracked_deletion_is_not_registered(self):
        from io import BytesIO

        from docx import Document
        from lxml import etree

        document = Document()
        document.add_paragraph("앞 글")
        run = document.add_paragraph().add_run()
        run.add_picture(BytesIO(builder.png_bytes(seed=5)))
        namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        deletion = etree.Element(f"{{{namespace}}}del")
        run._r.getparent().replace(run._r, deletion)
        deletion.append(run._r)
        buffer = BytesIO()
        document.save(buffer)

        result = convert(buffer.getvalue(), "a.docx")

        assert result.text == "앞 글"
        assert not result.images

    def test_position_tab_separates_the_words_on_both_sides(self):
        """A position tab (w:ptab) separates the left and right text of a header. Without it they were joined as '주식회사 가나다문서번호'."""
        body = ("<w:p>" + self._run("주식회사 가나다") + '<w:r><w:ptab w:relativeTo="margin" w:alignment="right" w:leader="none"/>'
                "</w:r>" + self._run("문서번호 2026-001") + "</w:p>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "주식회사 가나다 문서번호 2026-001"

    def test_placeholder_text_of_an_empty_content_control_is_left_out(self):
        """The prompt text of an empty content control (w:showingPlcHdr) is not an entered value. It is left out whether inside a
        paragraph, wrapping a paragraph or wrapping a cell. For one wrapping a cell, the cell itself is kept (so columns do not
        shift)."""
        placeholder = "<w:sdtPr><w:showingPlcHdr/></w:sdtPr>"
        guide = self._run("여기를 클릭하세요.")
        cell = "<w:tc><w:tcPr/><w:p>{}</w:p></w:tc>"
        body = ("<w:p>" + self._run("신청인: ") + f"<w:sdt>{placeholder}<w:sdtContent>{guide}</w:sdtContent></w:sdt></w:p>"
                + "<w:p>" + self._run("부서: ") + "<w:sdt><w:sdtPr/><w:sdtContent>" + self._run("개발부") + "</w:sdtContent></w:sdt></w:p>"
                + f"<w:sdt>{placeholder}<w:sdtContent><w:p>{guide}</w:p></w:sdtContent></w:sdt>"
                + "<w:tbl><w:tr>" + cell.format(self._run("이름")) + cell.format(self._run("연락처")) + cell.format(self._run("비고"))
                + "</w:tr><w:tr>" + cell.format(self._run("홍길동"))
                + f"<w:sdt>{placeholder}<w:sdtContent>{cell.format(guide)}</w:sdtContent></w:sdt>" + cell.format(self._run("없음"))
                + "</w:tr></w:tbl>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "신청인:\n\n부서: 개발부\n\n| 이름 | 연락처 | 비고 |\n| --- | --- | --- |\n| 홍길동 | | 없음 |"

    def test_legacy_horizontal_merge_extends_the_starting_cell(self):
        """The continuing cell of a legacy horizontal merge (w:hMerge) is added to the column span of the cell before it. Left as an
        empty cell, the merge vanished."""
        def cell(text: str, properties: str = "") -> str:
            return f"<w:tc><w:tcPr>{properties}</w:tcPr><w:p>{self._run(text) if text else ''}</w:p></w:tc>"

        body = ("<w:tbl><w:tr>" + cell("구분") + cell("상반기", '<w:hMerge w:val="restart"/>') + cell("", "<w:hMerge/>")
                + cell("하반기") + "</w:tr><w:tr>" + cell("매출") + cell("10") + cell("20") + cell("30") + "</w:tr></w:tbl>")

        result = convert(_docx_with_body(body), "a.docx")

        assert '<th colspan="2">상반기</th><th>하반기</th>' in result.text
        assert "<td>매출</td><td>10</td><td>20</td><td>30</td>" in result.text


class TestPptxStrikethroughTitlesAndHiddenSlides:
    @staticmethod
    def _run(text: str, strike: str | None = None, baseline: str | None = None) -> str:
        attributes = (f' strike="{strike}"' if strike else "") + (f' baseline="{baseline}"' if baseline else "")
        return f'<a:r><a:rPr lang="ko-KR"{attributes}/><a:t>{text}</a:t></a:r>'

    @pytest.mark.parametrize("in_table", [False, True])
    def test_struck_runs_are_marked_in_text_boxes_and_table_cells(self, in_table):
        """The same notation as DOCX and HTML. Consecutive struck runs are grouped into one and noStrike is not marked."""
        runs = (self._run("가격 ") + self._run("10,", "sngStrike") + self._run("000원 ", "sngStrike") + self._run("8,000원 ")
                + self._run("옛값", "dblStrike") + self._run(" 확정", "noStrike") + self._run(" 10") + self._run("6", "sngStrike", "30000"))

        result = convert(_pptx_with_runs(runs, in_table=in_table), "a.pptx")

        assert result.text == "## Slide 1: 수식\n\n가격 ~~10,000원~~ 8,000원 ~~옛값~~ 확정 10~~^6~~"

    def test_second_title_placeholder_is_kept_as_text(self):
        """With two title placeholders the first became the slide title and the second was left out of the body too, so its text
        vanished."""
        from copy import deepcopy
        from io import BytesIO

        from pptx import Presentation

        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        slide.shapes.title.text = "첫 제목"
        duplicate = deepcopy(slide.shapes.title._element)
        duplicate.find(".//{http://schemas.openxmlformats.org/drawingml/2006/main}t").text = "둘째 제목 자리의 글"
        slide.shapes._spTree.append(duplicate)
        buffer = BytesIO()
        presentation.save(buffer)

        result = convert(buffer.getvalue(), "a.pptx")

        assert result.text == "## Slide 1: 첫 제목\n\n둘째 제목 자리의 글"

    @pytest.mark.parametrize(("show", "is_hidden"), [("0", True), ("false", True), ("1", False), ("true", False)])
    def test_hidden_slide_is_marked_for_both_boolean_spellings(self, show, is_hidden):
        data = builder.zip_with(_pptx_with_runs(_text_run("내용")), edit={
            "ppt/slides/slide1.xml": lambda xml: xml.replace("<p:sld ", f'<p:sld show="{show}" ', 1)})

        result = convert(data, "a.pptx")

        assert result.text == f"## Slide 1: 수식{' (hidden)' if is_hidden else ''}\n\n내용"


_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_AUTO_NUMBER = '<a:buAutoNum type="arabicPeriod"/>'
_NO_BULLET = "<a:buNone/>"


def _pptx_with_paragraphs(paragraphs: list[tuple[str, int, str]], *, placeholder: bool = False,
                          layout_list_style: str = "", shape_list_style: str = "") -> bytes:
    """A slide with one text box (or body placeholder) made of (text, level, bullet XML inside a:pPr) paragraphs.

    `layout_list_style` / `shape_list_style` are XML (a:lvl1pPr …) to put inside the a:lstStyle of the layout's body placeholder /
    of this shape.
    """
    from io import BytesIO

    from lxml import etree
    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    layout = presentation.slide_layouts[1]
    slide = presentation.slides.add_slide(layout)
    shape = slide.placeholders[1] if placeholder else slide.shapes.add_textbox(Inches(1), Inches(2), Inches(6), Inches(3))
    text_frame = shape.text_frame
    for index, (text, level, bullet) in enumerate(paragraphs):
        paragraph = text_frame.paragraphs[0] if index == 0 else text_frame.add_paragraph()
        paragraph.text = text
        paragraph.level = level
        paragraph._p.get_or_add_pPr().extend(etree.fromstring(f'<r xmlns:a="{_A_NS}">{bullet}</r>'))

    def add_list_style(body: object, levels: str) -> None:
        style = body.find(f"{{{_A_NS}}}lstStyle")
        style.extend(etree.fromstring(f'<r xmlns:a="{_A_NS}">{levels}</r>'))

    if layout_list_style:
        add_list_style(layout.placeholders[1].text_frame._txBody, layout_list_style)
    if shape_list_style:
        add_list_style(text_frame._txBody, shape_list_style)
    buffer = BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def _slide_body(data: bytes) -> str:
    return convert(data, "a.pptx").text.split("\n\n", 1)[1]


class TestPptxAutoNumbering:
    """Automatic numbering (a:buAutoNum) was not read, so body placeholders came out with '- ' and text boxes with no marker, and the
    numbers vanished (14 of 182 real decks, 251 paragraphs). The counting rules match how LibreOffice renders PowerPoint files."""

    def test_numbers_follow_the_scheme_and_count_per_level(self):
        """A deeper-level paragraph does not break the numbering of a shallower level, and when a shallower level appears the deeper
        level counts from the start again."""
        letter = '<a:buAutoNum type="alphaLcParenR"/>'
        data = _pptx_with_paragraphs([("준비", 0, _AUTO_NUMBER), ("요구 분석", 1, letter), ("인터뷰", 1, letter),
                                      ("설계", 0, _AUTO_NUMBER), ("검토", 1, letter), ("구축", 0, _AUTO_NUMBER)])

        body = _slide_body(data)

        assert body == "1. 준비\n  - a) 요구 분석\n  - b) 인터뷰\n2. 설계\n  - a) 검토\n3. 구축"

    def test_unnumbered_paragraph_restarts_the_count_but_empty_numbered_one_is_skipped(self):
        data = _pptx_with_paragraphs([("가", 0, _AUTO_NUMBER), ("", 0, _AUTO_NUMBER), ("나", 0, _AUTO_NUMBER),
                                      ("끼움", 0, _NO_BULLET), ("다", 0, _AUTO_NUMBER), ("라", 0, _AUTO_NUMBER)])

        body = _slide_body(data)

        assert body == "1. 가\n2. 나\n끼움\n1. 다\n2. 라"

    def test_start_value_and_other_schemes(self):
        """The start number (startAt) is the first value of the run of numbers, and when the number scheme changes the count starts
        over."""
        data = _pptx_with_paragraphs([
            ("셋째", 0, '<a:buAutoNum type="romanUcPeriod" startAt="3"/>'),
            ("넷째", 0, '<a:buAutoNum type="romanUcPeriod" startAt="3"/>'),
            ("동그라미", 0, '<a:buAutoNum type="circleNumDbPlain"/>'),
            ("괄호", 0, '<a:buAutoNum type="arabicParenBoth"/>'),
            ("한자", 0, '<a:buAutoNum type="ea1ChsPeriod"/>')])

        body = _slide_body(data)

        assert body == "III. 셋째\nIV. 넷째\n① 동그라미\n(1) 괄호\n一. 한자"

    def test_body_placeholder_keeps_bullets_but_not_on_paragraphs_without_one(self):
        """In a body placeholder only paragraphs with an inherited bullet get '- '. Putting '- ' on a top-level paragraph whose bullet
        was removed directly (a:buNone) made intro and closing sentences read like list items."""
        data = _pptx_with_paragraphs([("다음 순서로 진행한다", 0, _NO_BULLET), ("접수", 0, _AUTO_NUMBER),
                                      ("심사", 0, _AUTO_NUMBER), ("참고", 0, ""), ("세부", 1, _NO_BULLET)], placeholder=True)

        body = _slide_body(data)

        assert body == "다음 순서로 진행한다\n1. 접수\n2. 심사\n- 참고\n  - 세부"

    def test_numbering_inherited_from_the_shape_or_layout_list_style(self):
        """A paragraph whose numbering is defined not on the paragraph but in the shape's a:lstStyle or the layout placeholder is
        numbered too (a real manual sample)."""
        layout_style = '<a:lvl1pPr><a:buAutoNum type="arabicPeriod"/></a:lvl1pPr>'
        shape_style = '<a:lvl2pPr><a:buAutoNum type="circleNumDbPlain"/></a:lvl2pPr>'
        data = _pptx_with_paragraphs([("조회", 0, ""), ("기간 선택", 1, ""), ("검색", 1, ""), ("출력", 0, "")],
                                     placeholder=True, layout_list_style=layout_style, shape_list_style=shape_style)

        body = _slide_body(data)

        assert body == "1. 조회\n  - ① 기간 선택\n  - ② 검색\n2. 출력"


_ALT_CHUNK_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/aFChunk"


def _docx_with_chunks(body: str, chunks: dict[str, tuple[str, str, bytes]]) -> bytes:
    """A document made of body XML and imported content parts (relationship id → (file name under word/, content type, content)).
    HTML→DOCX converters and mail merge tools put the body in as w:altChunk and keep the content in a separate part (HTML, MHT,
    plain text, RTF). python-docx cannot build this."""
    relationships = "".join(f'<Relationship Id="{rid}" Type="{_ALT_CHUNK_TYPE}" Target="{name}"/>'
                            for rid, (name, _, _) in chunks.items())
    overrides = "".join(f'<Override PartName="/word/{name}" ContentType="{content_type}"/>'
                        for name, content_type, _ in chunks.values())
    return builder.zip_with(_docx_with_body(body), edit={
        "word/_rels/document.xml.rels": lambda xml: xml.replace("</Relationships>", relationships + "</Relationships>"),
        "[Content_Types].xml": lambda xml: xml.replace("</Types>", overrides + "</Types>"),
    }, add={f"word/{name}": content for name, _, content in chunks.values()})


def _mht(html: str) -> bytes:
    return ('MIME-Version: 1.0\r\nContent-Type: multipart/related; boundary="b"\r\n\r\n--b\r\n'
            'Content-Type: text/html; charset="utf-8"\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\n'
            f"{html}\r\n--b--\r\n").encode()


class TestDocxImportedChunks:
    """w:altChunk (imported content) is neither a paragraph nor a table, so it was dropped without a warning when walking the body,
    cells and text boxes. A document from a generator whose whole body is one afchunk.mht (html-docx-js and the like) came out
    entirely empty. The referenced part is read with the extractor matching its content type and kept in place."""

    def test_html_chunk_is_read_in_place(self):
        body = _paragraph("머리 문단") + '<w:altChunk r:id="rIdChunk"/>' + _paragraph("꼬리 문단")
        html = "<html><body><p>가져온 본문: 계약 금액 1,000만원</p></body></html>".encode()
        data = _docx_with_chunks(body, {"rIdChunk": ("chunk1.html", "text/html", html)})

        result = convert(data, "a.docx")

        assert result.text == "머리 문단\n\n가져온 본문: 계약 금액 1,000만원\n\n꼬리 문단"
        assert result.warnings == []

    def test_chunk_text_shaped_like_a_picture_placeholder_stays_text(self):
        """The chunk's picture placeholders are renumbered for the document. Text of the chunk that has the shape of a
        placeholder (`⟦IMG:1⟧`) used to be taken for one: the text vanished and the picture appeared twice."""
        picture = base64.b64encode(builder.png_bytes()).decode()
        html = f'<html><body><p>문자 그대로 ⟦IMG:1⟧ 입니다.</p><p><img src="data:image/png;base64,{picture}" alt="도표"></p></body></html>'
        data = _docx_with_chunks('<w:altChunk r:id="rIdChunk"/>', {"rIdChunk": ("chunk1.html", "text/html", html.encode())})

        result = convert(data, "a.docx")

        assert len(result.images) == 1
        assert "문자 그대로 ⟦IMG:1⟧ 입니다." in result.text
        assert result.text.count("도표") == 1

    def test_mht_chunk_that_is_the_whole_body_is_read(self):
        mht = _mht("<html><body><h1>=EB=B3=B4=EA=B3=A0=EC=84=9C</h1><p>Quarterly revenue 120</p></body></html>")
        data = _docx_with_chunks('<w:altChunk r:id="htmlChunk"/>', {"htmlChunk": ("afchunk.mht", "message/rfc822", mht)})

        result = convert(data, "a.docx")

        assert result.text == "# 보고서\n\nQuarterly revenue 120"

    def test_xhtml_and_plain_text_chunks_inside_a_table_cell_are_read(self):
        cell = ('<w:tc><w:p><w:r><w:t>항목</w:t></w:r></w:p></w:tc>'
                '<w:tc><w:altChunk r:id="rIdText"/><w:p/></w:tc>')
        body = f"<w:tbl><w:tr>{cell}</w:tr></w:tbl>" + '<w:altChunk r:id="rIdXhtml"/>'
        xhtml = '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>XHTML 본문</p></body></html>'.encode()
        data = _docx_with_chunks(body, {"rIdText": ("chunk.txt", "text/plain", "평문 값".encode()),
                                        "rIdXhtml": ("chunk.xhtml", "application/xhtml+xml", xhtml)})

        result = convert(data, "a.docx")

        assert result.text == "| 항목 | 평문 값 |\n| --- | --- |\n\nXHTML 본문"

    def test_table_of_an_html_chunk_inside_a_table_cell_keeps_its_rows_and_columns(self):
        """A table in an HTML fragment inside a cell went into the cell as Markdown table text, a mess of `\\|` and `<br>`. It is left
        to the outer table, like a chart in a cell (`markdown.TableGrid.embed`)."""
        html = ("<html><body><p>세부 내역</p><table><tr><th>항목</th><th>금액</th></tr><tr><td>인건비</td><td>100</td></tr>"
                "<tr><td>재료비</td><td>50</td></tr></table></body></html>").encode()
        rows = [("<w:p><w:r><w:t>구분</w:t></w:r></w:p>", "<w:p><w:r><w:t>내용</w:t></w:r></w:p>"),
                ("<w:p><w:r><w:t>예산</w:t></w:r></w:p>", '<w:altChunk r:id="rIdChunk"/><w:p/>')]
        body = "<w:tbl>" + "".join(f"<w:tr><w:tc>{left}</w:tc><w:tc>{right}</w:tc></w:tr>" for left, right in rows) + "</w:tbl>"
        data = _docx_with_chunks(body, {"rIdChunk": ("chunk.html", "text/html", html)})

        result = convert(data, "a.docx")

        assert result.text == ("<table>\n<tr><th>구분</th><th>내용</th></tr>\n<tr><td>예산</td><td>세부 내역<br><table><tr>"
                               "<th>항목</th><th>금액</th></tr><tr><td>인건비</td><td>100</td></tr><tr><td>재료비</td>"
                               "<td>50</td></tr></table></td></tr>\n</table>")

    def test_cell_text_of_a_chunk_table_keeps_its_pipes_and_line_breaks(self):
        html = "<table><tr><th>조건</th><th>값</th></tr><tr><td>a|b</td><td>1<br>2</td></tr></table>".encode()
        body = ('<w:tbl><w:tr><w:tc><w:p><w:r><w:t>식</w:t></w:r></w:p></w:tc><w:tc><w:altChunk r:id="rIdChunk"/><w:p/></w:tc>'
                "</w:tr></w:tbl>")
        data = _docx_with_chunks(body, {"rIdChunk": ("chunk.html", "text/html", html)})

        result = convert(data, "a.docx")

        assert "<tr><td>a|b</td><td>1<br>2</td></tr></table>" in result.text

    def test_picture_in_an_html_chunk_is_registered_with_the_document(self):
        import base64

        picture = base64.b64encode(builder.png_bytes()).decode()
        html = f'<html><body><p>그림 앞</p><img src="data:image/png;base64,{picture}" alt="구성도"></body></html>'.encode()
        data = _docx_with_chunks(_paragraph("본문") + '<w:altChunk r:id="rIdChunk"/>',
                                 {"rIdChunk": ("chunk.html", "text/html", html)})

        result = convert(data, "a.docx")

        assert [image.alt for image in result.images] == ["구성도"]
        assert result.text.startswith("본문\n\n그림 앞")

    def test_chunk_of_an_unsupported_type_is_reported_with_a_count(self):
        """There is no extractor for RTF and embedded document (docx) chunks. A warning reports that they were dropped (the rest of
        the body is kept)."""
        body = '<w:altChunk r:id="rIdRtf"/>' + _paragraph("본문") + '<w:altChunk r:id="rIdDocx"/>'
        data = _docx_with_chunks(body, {
            "rIdRtf": ("chunk.rtf", "application/rtf", rb"{\rtf1\ansi hello}"),
            "rIdDocx": ("chunk.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
                        b"PK\x03\x04")})

        result = convert(data, "a.docx")

        assert result.text == "본문"
        assert any("2 imported content item(s)" in warning and "altChunk" in warning for warning in result.warnings)


_VML_NAMESPACE = 'xmlns:v="urn:schemas-microsoft-com:vml"'


def _word_art(text: str) -> str:
    return (f'<w:p><w:r><w:pict><v:shape {_VML_NAMESPACE} id="wa" type="#_x0000_t136" style="width:300pt;height:50pt">'
            f'<v:textpath style="font-family:&quot;맑은 고딕&quot;" string="{text}"/></v:shape></w:pict></w:r></w:p>')


class TestDocxLegacyWordArt:
    """Text of legacy WordArt (VML v:textpath) is in the string attribute, not in element text, so it was dropped. It is read only in
    the body — the same shape in a header is a watermark laid on every page ('초안', '대외비')."""

    def test_word_art_text_in_the_body_is_kept(self):
        data = _docx_with_body(_word_art("2024 사업 설명회") + _paragraph("본문"))

        result = convert(data, "a.docx")

        assert result.text == "2024 사업 설명회\n\n본문"

    def test_watermark_in_the_header_is_left_out(self):
        from io import BytesIO

        from docx import Document

        document = Document()
        document.sections[0].header.paragraphs[0].text = "머리말 글"
        document.add_paragraph("본문")
        buffer = BytesIO()
        document.save(buffer)
        data = builder.zip_with(buffer.getvalue(), edit={
            "word/header1.xml": lambda xml: xml.replace("</w:hdr>", _word_art("초안") + "</w:hdr>")})

        result = convert(data, "a.docx", ConvertOptions(include_headers_footers=True))

        assert result.text == "머리말 글\n\n본문"


def _pptx_hidden(data: bytes, *texts: str) -> bytes:
    """A copy in which the shapes holding the text in `texts` are hidden in the selection pane (p:cNvPr hidden="1")."""
    from io import BytesIO

    from pptx import Presentation

    presentation = Presentation(BytesIO(data))
    for shape in presentation.slides[0].shapes:
        if shape.has_text_frame and shape.text_frame.text in texts:
            shape._element[0][0].set("hidden", "1")
    buffer = BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


class TestPptxHiddenShapes:
    """A shape hidden in the selection pane (p:cNvPr hidden) does not show in the slide show or in print. It went into the body and a
    draft price read like a valid value. It is left out like DOCX hidden text (w:vanish) (deliberate)."""

    def test_hidden_text_box_is_left_out(self):
        data = _pptx_hidden(_pptx_with_text_boxes("최종 가격: 8,000원", "초안 가격: 10,000원"), "초안 가격: 10,000원")

        result = convert(data, "a.pptx")

        assert result.text == "## Slide 1\n\n최종 가격: 8,000원"

    def test_hidden_picture_is_not_registered(self):
        from io import BytesIO

        from pptx import Presentation

        presentation = Presentation(BytesIO(builder.pptx_with_image()))
        picture = next(shape for shape in presentation.slides[0].shapes if shape.shape_type == 13)
        picture._element.nvPicPr.cNvPr.set("hidden", "true")
        buffer = BytesIO()
        presentation.save(buffer)

        result = convert(buffer.getvalue(), "a.pptx")

        assert result.images == []


class TestOneOddValueDoesNotRejectTheDocument:
    """`str.isdigit()` is true for '²' too, but `int('²')` is a ValueError. One attribute value, one relationship or one part got the
    whole document rejected as a 'corrupted document'. Only that value or object is skipped and the rest is read."""

    def test_docx_odd_grid_span_is_read_as_one_column(self):
        from io import BytesIO

        from docx import Document

        document = Document()
        table = document.add_table(rows=2, cols=2)
        for (row, col), text in {(0, 0): "칸", (0, 1): "둘", (1, 0): "셋", (1, 1): "넷"}.items():
            table.cell(row, col).text = text
        document.add_paragraph("본문")
        buffer = BytesIO()
        document.save(buffer)
        data = builder.zip_with(buffer.getvalue(), edit={
            "word/document.xml": lambda xml: xml.replace("<w:tcPr>", '<w:tcPr><w:gridSpan w:val="²"/>', 1)})

        result = convert(data, "a.docx")

        assert result.text == "| 칸 | 둘 |\n| --- | --- |\n| 셋 | 넷 |\n\n본문"

    def test_pptx_odd_start_number_starts_at_one(self):
        data = _pptx_with_paragraphs([("항목", 0, '<a:buAutoNum type="arabicPeriod" startAt="²"/>')])

        assert _slide_body(data) == "1. 항목"

    @pytest.mark.parametrize(("level", "expected"), [("x", "항목"), ("-1", "항목"), ("99999", "                - 항목")])
    def test_pptx_odd_paragraph_level_is_clamped(self, level, expected):
        data = builder.zip_with(_pptx_with_text_boxes("항목"), edit={
            "ppt/slides/slide1.xml": lambda xml: xml.replace("<a:p><a:r>", f'<a:p><a:pPr lvl="{level}"/><a:r>', 1)})

        assert _slide_body(data) == expected

    def test_pptx_slide_whose_layout_relationship_is_broken_is_read(self):
        """With a broken layout relationship, asking for a placeholder's (inherited) position raised KeyError. The position is taken
        as 0."""
        from io import BytesIO
        import re as regex

        from pptx import Presentation
        from pptx.util import Inches

        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[1])
        slide.shapes.title.text = "제목"
        slide.placeholders[1].text = "본문 내용"
        slide.shapes.add_textbox(Inches(1), Inches(5), Inches(3), Inches(1)).text = "글상자"
        buffer = BytesIO()
        presentation.save(buffer)
        data = builder.zip_with(buffer.getvalue(), edit={
            "ppt/slides/_rels/slide1.xml.rels": lambda xml: regex.sub(r'<Relationship [^>]*slideLayout"[^>]*/>', "", xml)})

        result = convert(data, "a.pptx")

        assert result.text == "## Slide 1: 제목\n\n- 본문 내용\n\n글상자"

    def test_pptx_smartart_with_an_external_relationship_is_skipped(self):
        data = builder.zip_with(_pptx_with_smartart(), edit={
            "ppt/slides/_rels/slide1.xml.rels": lambda xml: xml.replace(
                'Target="../diagrams/data1.xml"', 'Target="http://example.com/d.xml" TargetMode="External"')})

        result = convert(data, "a.pptx")

        assert result.text == "## Slide 1: 구성도\n\n[Image]"

    def test_smartart_with_a_very_deep_chain_is_read_without_recursion(self):
        """A parent→child chain 1,500 links deep exceeded the recursion limit and the whole document was rejected."""
        main = "http://schemas.openxmlformats.org/drawingml/2006/main"
        points = '<dgm:pt modelId="doc" type="doc"/>' + "".join(
            f'<dgm:pt modelId="n{i}"><dgm:t><a:p><a:r><a:t>단계{i}</a:t></a:r></a:p></dgm:t></dgm:pt>' for i in range(1500))
        links = '<dgm:cxn modelId="c0" srcId="doc" destId="n0" srcOrd="0"/>' + "".join(
            f'<dgm:cxn modelId="c{i + 1}" srcId="n{i}" destId="n{i + 1}" srcOrd="0"/>' for i in range(1499))
        chain = (f'<dgm:dataModel xmlns:dgm="{_DIAGRAM_NS}" xmlns:a="{main}"><dgm:ptLst>{points}</dgm:ptLst>'
                 f"<dgm:cxnLst>{links}</dgm:cxnLst></dgm:dataModel>")
        data = builder.zip_with(_pptx_with_smartart(), edit={"ppt/diagrams/data1.xml": lambda _: chain})

        result = convert(data, "a.pptx")

        assert result.text.count("단계") == 1500
        assert "- 단계0\n  - 단계1\n    - 단계2" in result.text

    def test_smartart_odd_order_and_chart_odd_point_index_skip_only_that_value(self):
        smartart = builder.zip_with(_pptx_with_smartart(), edit={
            "ppt/diagrams/data1.xml": lambda xml: xml.replace('srcId="0" destId="3" srcOrd="1"', 'srcId="0" destId="3" srcOrd="²"')})
        series = _chart_series(0, "값", ["1", "2"], ["A", "B"]).replace('<c:pt idx="1">', '<c:pt idx="²">')

        smartart_text = convert(smartart, "a.pptx").text
        chart_text = _convert_chart(_chart_xml(f"<c:barChart>{series}</c:barChart>"))

        assert "- 과제 발의\n  - 재단 승인" in smartart_text and "- 설비 구축" in smartart_text
        assert chart_text.endswith("| A | 1 |")

    @pytest.mark.parametrize("variant", ["broken", "external"])
    def test_docx_broken_or_external_footnotes_part_keeps_the_body(self, variant):
        from io import BytesIO

        from docx import Document

        word = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        notes = f'<w:footnotes xmlns:w="{word}"><w:footnote w:id="1"><w:p><w:r><w:t>각주</w:t></w:r></w:p></w:footnote></w:footnotes>'
        target = ('Target="http://example.com/fn.xml" TargetMode="External"' if variant == "external"
                  else 'Target="footnotes.xml"')
        document = Document()
        document.add_paragraph("본문 문단")
        buffer = BytesIO()
        document.save(buffer)
        data = builder.zip_with(buffer.getvalue(), edit={
            "word/document.xml": lambda xml: xml.replace(
                "<w:t>본문 문단</w:t></w:r>", '<w:t>본문 문단</w:t></w:r><w:r><w:footnoteReference w:id="1"/></w:r>', 1),
            "word/_rels/document.xml.rels": lambda xml: xml.replace("</Relationships>", '<Relationship Id="rIdFn" Type="'
                                                                    f'{_RELATIONSHIPS_NS}/footnotes" {target}/></Relationships>'),
            "[Content_Types].xml": lambda xml: xml.replace("</Types>", '<Override PartName="/word/footnotes.xml" ContentType='
                                                           '"application/vnd.openxmlformats-officedocument.wordprocessingml'
                                                           '.footnotes+xml"/></Types>'),
        }, add={"word/footnotes.xml": notes[:-5] if variant == "broken" else notes})

        result = convert(data, "a.docx")

        # The body is kept. The reference writes no mark: its note cannot be read, so there is no definition for it to point at
        assert result.text == "본문 문단"


def _without_parts(data: bytes, *names: str) -> bytes:
    """A copy of a ZIP-based document with a few parts removed (the relationships stay and point to the missing parts)."""
    buffer = BytesIO()
    with zipfile.ZipFile(BytesIO(data)) as source, zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for name in source.namelist():
            if name not in names:
                target.writestr(name, source.read(name))
    return buffer.getvalue()


def _with_parts_replaced(data: bytes, parts: dict[str, bytes]) -> bytes:
    """A copy with the content of a few parts replaced wholesale (for putting in content that is not XML)."""
    buffer = BytesIO()
    with zipfile.ZipFile(BytesIO(data)) as source, zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as target:
        for name in source.namelist():
            target.writestr(name, parts.get(name, source.read(name)))
    return buffer.getvalue()


def _relationship_id(data: bytes, rels: str, target: str) -> str:
    with zipfile.ZipFile(BytesIO(data)) as archive:
        return re.search(rf'Id="([^"]+)"[^>]*Target="{re.escape(target)}"', archive.read(rels).decode("utf-8")).group(1)


_NOT_XML = "그냥 글입니다 <tag".encode()
_PART_SKIPPED = "missing or broken part(s)"


class TestOneBrokenPartDoesNotRejectTheDocument:
    """python-docx reads every part referenced by a relationship when it opens the package. One missing part or one part that is not
    XML got a document with a sound body rejected as a 'corrupted document'. When opening fails, it is reopened from a copy with
    only that part and its relationship detached, with one warning (`_package.repair_package`). Parts the library does not read as
    XML, such as the footnotes part, are handled by `parse_part` as before."""

    def test_healthy_package_is_not_rebuilt(self):
        from opendockit.extractors._package import repair_package

        assert repair_package(builder.docx_bytes()) is None and repair_package(builder.pptx_bytes()) is None

    @pytest.mark.parametrize("part", ["word/numbering.xml", "word/styles.xml", "docProps/core.xml", "word/settings.xml",
                                      "word/theme/theme1.xml", "word/fontTable.xml", "customXml/item1.xml",
                                      "docProps/thumbnail.jpeg"])
    def test_docx_missing_part_keeps_the_body_with_one_warning(self, part):
        data = _without_parts(builder.docx_bytes(), part)

        result = convert(data, "a.docx")

        assert "본문 첫 문단입니다." in result.text and "끝 | 파이프" in result.text
        assert len(result.warnings) == 1 and _PART_SKIPPED in result.warnings[0] and part.rsplit("/", 1)[-1] in result.warnings[0]

    @pytest.mark.parametrize("part", ["docProps/core.xml", "word/settings.xml", "word/numbering.xml", "word/styles.xml"])
    def test_docx_part_that_is_not_xml_keeps_the_body_with_one_warning(self, part):
        data = _with_parts_replaced(builder.docx_bytes(), {part: _NOT_XML})

        result = convert(data, "a.docx")

        assert "본문 첫 문단입니다." in result.text and "끝 | 파이프" in result.text
        assert len(result.warnings) == 1 and _PART_SKIPPED in result.warnings[0]

    @pytest.mark.parametrize("how", ["missing", "not-xml"])
    def test_docx_without_a_readable_numbering_part_drops_only_the_numbers(self, how):
        document = builder.docx_bytes()
        data = (_without_parts(document, "word/numbering.xml") if how == "missing"
                else _with_parts_replaced(document, {"word/numbering.xml": _NOT_XML}))

        result = convert(data, "a.docx")

        assert "첫째" in result.text and "둘째" in result.text and "1. 첫째" not in result.text

    def test_docx_missing_picture_part_keeps_the_text_and_the_other_pictures(self):
        data = _without_parts(builder.docx_with_images(), "word/media/image1.png")

        result = convert(data, "a.docx")

        assert result.text == "차트 앞 문단입니다.\n\n아이콘\n\n| 사진 | [Image] |\n| --- | --- |"
        assert len(result.warnings) == 1 and "image1.png" in result.warnings[0]

    @pytest.mark.parametrize(("part", "change"), [
        ("word/_rels/document.xml.rels", lambda xml: xml.replace('Target="media/image1.png"', 'Target=""')),
        ("word/_rels/document.xml.rels", lambda xml: xml.replace('Target="media/image1.png"', 'Target="../../../etc/passwd"')),
        ("[Content_Types].xml", lambda xml: re.sub(r'<Default Extension="png"[^>]*/>', "", xml)),
    ], ids=["empty-target", "target-outside-the-package", "no-content-type-for-the-target"])
    def test_docx_relationship_whose_target_cannot_be_read_is_dropped(self, part, change):
        data = builder.zip_with(builder.docx_with_images(), edit={part: change})

        result = convert(data, "a.docx")

        assert result.text.startswith("차트 앞 문단입니다.\n\n아이콘\n\n") and "사진" in result.text
        assert len(result.warnings) == 1 and _PART_SKIPPED in result.warnings[0]

    def test_docx_header_whose_part_is_missing_is_skipped(self):
        from docx import Document

        document = Document()
        document.sections[0].header.paragraphs[0].text = "머리말"
        document.add_paragraph("본문")
        buffer = BytesIO()
        document.save(buffer)
        data = _without_parts(buffer.getvalue(), "word/header1.xml")

        result = convert(data, "a.docx")

        assert result.text == "본문" and len(result.warnings) == 1 and "header1.xml" in result.warnings[0]

    @pytest.mark.parametrize("part", ["docProps/core.xml", "ppt/slideLayouts/slideLayout2.xml", "ppt/notesSlides/notesSlide1.xml",
                                      "ppt/charts/chart1.xml", "ppt/slideMasters/slideMaster1.xml"])
    def test_pptx_part_that_is_not_xml_keeps_the_slides_with_one_warning(self, part):
        data = _with_parts_replaced(builder.pptx_bytes(), {part: _NOT_XML})

        result = convert(data, "a.pptx")

        assert "## Slide 2: 핵심 목표\n\n- 매출 성장" in result.text and "| 서울 | 100 |" in result.text
        assert len(result.warnings) == 1 and part.rsplit("/", 1)[-1] in result.warnings[0]

    @pytest.mark.parametrize("how", ["missing", "not-xml"])
    def test_pptx_slide_whose_part_cannot_be_read_is_skipped_with_a_warning(self, how):
        deck = builder.pptx_bytes()
        data = (_without_parts(deck, "ppt/slides/slide2.xml") if how == "missing"
                else _with_parts_replaced(deck, {"ppt/slides/slide2.xml": _NOT_XML}))

        result = convert(data, "a.pptx")

        assert "## Slide 1: 사업 계획" in result.text and "## Slide 3: 실적" in result.text
        assert "핵심 목표" not in result.text
        assert result.warnings[-1] == "Slide 2: skipped because its part could not be read."

    def test_pptx_slide_entry_pointing_at_a_part_that_is_not_a_slide_is_skipped(self):
        deck = builder.pptx_bytes()
        other = _relationship_id(deck, "ppt/_rels/presentation.xml.rels", "presProps.xml")
        data = builder.zip_with(deck, edit={"ppt/presentation.xml": lambda xml: re.sub(
            r'(<p:sldId [^>]*r:id=")(rId\d+)(")', lambda m: m.group(1) + other + m.group(3), xml, count=1)})

        result = convert(data, "a.pptx")

        assert "사업 계획" not in result.text and "## Slide 2: 핵심 목표" in result.text
        assert result.warnings == ["Slide 1: skipped because its part could not be read."]


_GRAPHIC_FRAME_OPEN = ('<p:graphicFrame><p:nvGraphicFramePr><p:cNvPr id="21" name="t"/><p:cNvGraphicFramePr/><p:nvPr/>'
                       "</p:nvGraphicFramePr>")
_FRAME_XFRM = '<p:xfrm><a:off x="0" y="0"/><a:ext cx="1" cy="1"/></p:xfrm>'
_TABLE_URI = "http://schemas.openxmlformats.org/drawingml/2006/table"


def _pptx_with_shape_xml(shape_xml: str) -> bytes:
    """A deck whose slide holds one text box '뒤', with XML put as is at the end of the shape tree. Used for attribute values
    python-pptx does not produce."""
    return builder.zip_with(_pptx_with_text_boxes("뒤"), edit={
        "ppt/slides/slide1.xml": lambda xml: xml.replace("</p:spTree>", shape_xml + "</p:spTree>", 1)})


def _raw_shape(text: str, *, x: str = "0", y: str = "0", cy: str = "100", placeholder: str = "") -> str:
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="20" name="x"/><p:cNvSpPr/><p:nvPr>{placeholder}</p:nvPr></p:nvSpPr>'
            f'<p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="100" cy="{cy}"/></a:xfrm></p:spPr>'
            f"<p:txBody><a:bodyPr/><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody></p:sp>")


def _raw_table(rows: list[str]) -> str:
    return (f'{_GRAPHIC_FRAME_OPEN}{_FRAME_XFRM}<a:graphic><a:graphicData uri="{_TABLE_URI}"><a:tbl><a:tblGrid>'
            '<a:gridCol w="100"/><a:gridCol w="100"/></a:tblGrid>' + "".join(f'<a:tr h="1">{row}</a:tr>' for row in rows)
            + "</a:tbl></a:graphicData></a:graphic></p:graphicFrame>")


def _raw_cell(text: str, attributes: str = "") -> str:
    return f"<a:tc {attributes}><a:txBody><a:bodyPr/><a:p><a:r><a:t>{text}</a:t></a:r></a:p></a:txBody></a:tc>"


class TestOneBrokenShapeDoesNotRejectTheDeck:
    """python-pptx raises ValueError, InvalidXmlError or AttributeError the moment a shape attribute is read, and that escaped the
    shape loop, so one shape got the whole document rejected. A shape with one odd value is read with defaults (position 0, no
    merge, no placeholder type or index), and a shape python-pptx cannot build at all (ink annotation, graphicFrame without frame
    or graphic information) is skipped on its own, with one warning per slide."""

    _ONE_SKIPPED = ["Slide 1: skipped 1 unreadable shape(s)."]

    @pytest.mark.parametrize("attributes", ['rowSpan="x"', 'gridSpan="²"', 'gridSpan="1.5"', 'hMerge="maybe"', 'vMerge="2"'])
    def test_table_cell_with_an_odd_merge_value_is_read_as_a_plain_cell(self, attributes):
        data = _pptx_with_shape_xml(_raw_table([_raw_cell("가", attributes) + _raw_cell("나"), _raw_cell("다") + _raw_cell("라")]))

        result = convert(data, "a.pptx")

        assert "| 가 | 나 |\n| --- | --- |\n| 다 | 라 |" in result.text and result.warnings == []

    @pytest.mark.parametrize("position", [{"x": "abc"}, {"y": "1.5"}, {"cy": "²"}])
    def test_shape_with_an_odd_position_value_is_read_at_the_origin(self, position):
        data = _pptx_with_shape_xml(_raw_shape("앞", **position))

        result = convert(data, "a.pptx")

        assert result.text.split("\n\n", 1)[1] == "앞\n\n뒤" and result.warnings == []

    @pytest.mark.parametrize(("frame", "warnings"), [
        (_GRAPHIC_FRAME_OPEN + "</p:graphicFrame>", _ONE_SKIPPED),
        (_GRAPHIC_FRAME_OPEN + _FRAME_XFRM + "</p:graphicFrame>", _ONE_SKIPPED),
        (_GRAPHIC_FRAME_OPEN + _FRAME_XFRM + "<a:graphic/></p:graphicFrame>", _ONE_SKIPPED),
        (_GRAPHIC_FRAME_OPEN + '<a:graphic><a:graphicData uri="urn:nope"/></a:graphic></p:graphicFrame>', []),
    ], ids=["empty", "no-graphic", "no-graphic-data", "no-frame-position"])
    def test_graphic_frame_missing_its_graphic_is_skipped_with_one_warning(self, frame, warnings):
        result = convert(_pptx_with_shape_xml(frame), "a.pptx")

        assert result.text.split("\n\n", 1)[1] == "뒤" and result.warnings == warnings

    @pytest.mark.parametrize("ink", ['<p:contentPart r:id="rIdNope"/>', "<p:contentPart/>"])
    def test_ink_annotation_is_skipped_with_one_warning(self, ink):
        result = convert(_pptx_with_shape_xml(ink), "a.pptx")

        assert result.text.split("\n\n", 1)[1] == "뒤" and result.warnings == self._ONE_SKIPPED

    @pytest.mark.parametrize("placeholder", ['<p:ph type="nope"/>', '<p:ph idx="abc"/>', '<p:ph idx="²"/>', '<p:ph idx="1.5"/>',
                                             '<p:ph idx=""/>'])
    def test_placeholder_with_an_odd_type_or_index_is_read_as_text(self, placeholder):
        result = convert(_pptx_with_shape_xml(_raw_shape("앞", placeholder=placeholder)), "a.pptx")

        assert result.text.split("\n\n", 1)[1] == "- 앞\n\n뒤" and result.warnings == []

    def test_group_with_one_unreadable_child_keeps_the_other_children(self):
        group = ('<p:grpSp><p:nvGrpSpPr><p:cNvPr id="30" name="g"/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>'
                 + _GRAPHIC_FRAME_OPEN + "</p:graphicFrame>" + _raw_shape("안") + "</p:grpSp>")

        result = convert(_pptx_with_shape_xml(group), "a.pptx")

        assert result.text.split("\n\n", 1)[1] == "안\n\n뒤" and result.warnings == self._ONE_SKIPPED


_COMPARISON_LAYOUT = 4  # 'Comparison' in the python-pptx default template: two heading placeholders + two body placeholders below them


class TestOfficeReadingOrderAndListMarks:
    """DOCX/PPTX gaps reproduced in review round 19: reading order of the comparison layout, text box bullets, headings inside a
    cell, footnote marks, drop caps, number formats, macro-enabled shows and templates."""

    def test_comparison_layout_is_read_column_by_column(self):
        """The comparison layout has the two headings in one row and the two bodies in a row below, so it read as 'AS-IS, TO-BE, left
        list, right list' and it was unclear which heading each list belonged to. Placeholders stacked adjoining in the same column
        are read column by column."""
        from io import BytesIO

        from pptx import Presentation

        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[_COMPARISON_LAYOUT])
        slide.shapes.title.text = "현행 대비 개선"
        placeholders = {placeholder.placeholder_format.idx: placeholder for placeholder in slide.placeholders}
        placeholders[1].text = "AS-IS"
        placeholders[2].text_frame.text = "수기 결재"
        placeholders[3].text = "TO-BE"
        placeholders[4].text_frame.text = "전자 결재"
        buffer = BytesIO()
        presentation.save(buffer)

        body = _slide_body(buffer.getvalue())

        assert body == "AS-IS\n\n- 수기 결재\n\nTO-BE\n\n- 전자 결재"

    def test_free_form_text_boxes_in_a_grid_are_still_read_row_by_row(self):
        """A grid of text boxes that are not placeholders (a row of names + a row of values) stays in row order."""
        from io import BytesIO

        from pptx import Presentation
        from pptx.util import Inches

        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        for text, left, top in [("매출", 1, 2), ("이익", 4, 2), ("1,200억원", 1, 2.5), ("80억원", 4, 2.5)]:
            slide.shapes.add_textbox(Inches(left), Inches(top), Inches(2.5), Inches(0.5)).text_frame.text = text
        buffer = BytesIO()
        presentation.save(buffer)

        result = convert(buffer.getvalue(), "a.pptx")

        assert result.text == "## Slide 1\n\n매출\n\n이익\n\n1,200억원\n\n80억원"

    def test_text_box_paragraphs_with_a_bullet_character_or_picture_are_list_items(self):
        """A top-level paragraph of a text box came out with no marker even with a bullet character (a:buChar) or picture bullet
        (a:buBlip), so it could not be told from the title line. A paragraph whose bullet was removed directly (a:buNone) still has
        no marker."""
        bullet = '<a:buChar char="•"/>'
        data = _pptx_with_paragraphs([("2025년 성과 요약", 0, ""), ("매출 15% 증가", 0, bullet), ("수도권 비중 확대", 1, bullet),
                                      ("비용 8% 절감", 0, "<a:buBlip><a:blip/></a:buBlip>"), ("맺음 문장", 0, _NO_BULLET)])

        body = _slide_body(data)

        assert body == "2025년 성과 요약\n- 매출 15% 증가\n  - 수도권 비중 확대\n- 비용 8% 절감\n맺음 문장"

    @pytest.mark.parametrize(("extension", "content_type"), [
        ("ppsm", "application/vnd.ms-powerpoint.slideshow.macroEnabled.main+xml"),
        ("potm", "application/vnd.ms-powerpoint.template.macroEnabled.main+xml"),
    ])
    def test_macro_enabled_show_and_template_are_read_like_a_plain_deck(self, extension, content_type):
        """A macro-enabled show (.ppsm) and template (.potm) differ only in the content type name of the main part. python-pptx does
        not know the name, so they were rejected with 'The PPTX document structure could not be read.'"""
        data = builder.zip_with(_pptx_with_text_boxes("매크로 쇼의 글"), edit={"[Content_Types].xml": lambda xml: xml.replace(
            "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml", content_type)})

        result = convert(data, f"a.{extension}")

        assert result.text == "## Slide 1\n\n매크로 쇼의 글"

    def test_heading_style_inside_a_table_cell_gets_no_heading_mark(self):
        """A heading-style paragraph inside a cell got '## ', so the table cell text became '## 추진 배경'. HWP and HWPX put no mark on
        headings inside a cell. Headings in the body remain headings."""
        def cell(*paragraphs: str) -> str:
            return f"<w:tc>{''.join(paragraphs)}</w:tc>"

        table = ("<w:tbl><w:tr>" + cell(_paragraph("구분")) + cell(_paragraph("내용")) + "</w:tr><w:tr>"
                 + cell(_paragraph("사업목적")) + cell(_paragraph("추진 배경", style="Heading2"), _paragraph("시장 확대에 대응"))
                 + "</w:tr></w:tbl>")
        data = _docx_with_body(_paragraph("사업 개요", style="Heading1") + table)

        result = convert(data, "a.docx")

        assert result.text.startswith("# 사업 개요\n\n")
        assert "추진 배경" in result.text and "## 추진 배경" not in result.text

    def test_note_marks_are_left_out_when_footnotes_are_not_included(self):
        """With `include_footnotes=False` the footnote content is left out, but the `[^1]` mark stayed in the body and pointed
        nowhere. HWP and HWPX leave the mark out too. The same goes for endnotes."""
        data = _docx_with_body('<w:p><w:r><w:t>총액 1억원</w:t></w:r><w:r><w:footnoteReference w:id="1"/></w:r>'
                               '<w:r><w:t>이다.</w:t></w:r><w:r><w:endnoteReference w:id="1"/></w:r></w:p>')

        result = convert(data, "a.docx", ConvertOptions(include_footnotes=False))

        assert result.text == "총액 1억원이다."

    @pytest.mark.parametrize("drop_cap", ["drop", "margin"])
    def test_drop_cap_is_joined_to_the_paragraph_it_starts(self, drop_cap):
        """A drop cap (w:framePr w:dropCap) is stored as a separate paragraph holding only the first letter, so '올' and
        '해 우리 회사는 …' became different paragraphs."""
        data = _docx_with_body(f'<w:p><w:pPr><w:framePr w:dropCap="{drop_cap}" w:lines="3" w:wrap="around"/></w:pPr>'
                               '<w:r><w:t>올</w:t></w:r></w:p>' + _paragraph("해 우리 회사는 창립 20주년을 맞았습니다.")
                               + _paragraph("다음 문단"))

        result = convert(data, "a.docx")

        assert result.text == "올해 우리 회사는 창립 20주년을 맞았습니다.\n\n다음 문단"

    def test_zero_padded_parenthesized_full_stop_and_full_width_numbers(self):
        """The Word number formats decimalZero (01.), decimalEnclosedParen (⑴), decimalEnclosedFullstop (⒈) and decimalFullWidth (１)
        were not in the table and came out as Arabic digits."""
        formats = [("decimalZero", "%1."), ("decimalEnclosedParen", "%1"), ("decimalEnclosedFullstop", "%1"),
                   ("decimalFullWidth", "%1.")]
        numbering = "".join(f'<w:abstractNum w:abstractNumId="{index}">{_number_level(0, text, number_format)}</w:abstractNum>'
                            for index, (number_format, text) in enumerate(formats))
        numbering += "".join(_number_list(index + 1, index) for index in range(len(formats)))
        body = "".join(_paragraph(f"항목{index}{order}", num_id=index + 1) for index in range(len(formats)) for order in "가나")

        result = convert(_docx_with_body(body, numbering=numbering), "a.docx")

        assert result.text.split() == ["01.", "항목0가", "02.", "항목0나", "⑴", "항목1가", "⑵", "항목1나", "⒈", "항목2가", "⒉",
                                       "항목2나", "１.", "항목3가", "２.", "항목3나"]


def _pptx_with_boxes(boxes: list[tuple[str, float, float, float, float]]) -> bytes:
    """A deck with rectangle shapes (text, left, top, width, height — cm) placed on a blank-layout slide."""
    from io import BytesIO

    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Cm

    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    for text, left, top, width, height in boxes:
        slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Cm(left), Cm(top), Cm(width), Cm(height)).text_frame.text = text
    buffer = BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def _pptx_with_picture_filled_oval(*, tiled: bool = False, text: str = "") -> bytes:
    """A circle filled with a photo (spPr/a:blipFill of p:sp) + a caption text box below it. The picture relationship is made by
    adding a picture shape and removing it."""
    from io import BytesIO

    from lxml import etree
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE
    from pptx.util import Cm

    drawing = "http://schemas.openxmlformats.org/drawingml/2006/main"
    presentation = Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    picture = slide.shapes.add_picture(BytesIO(builder.png_bytes(seed=7)), Cm(1), Cm(1), Cm(5), Cm(5))
    relationship_id = picture._element.blipFill.blip.rEmbed
    slide.shapes._spTree.remove(picture._element)
    oval = slide.shapes.add_shape(MSO_SHAPE.OVAL, Cm(8), Cm(4), Cm(5), Cm(5))
    oval.text_frame.text = text
    fill = etree.fromstring(f'<a:blipFill xmlns:a="{drawing}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/'
                            f'relationships"><a:blip r:embed="{relationship_id}"/>'
                            + ("<a:tile/>" if tiled else "<a:stretch><a:fillRect/></a:stretch>") + "</a:blipFill>")
    oval._element.spPr.find(f"{{{drawing}}}prstGeom").addnext(fill)
    slide.shapes.add_textbox(Cm(8), Cm(9.5), Cm(5), Cm(1)).text_frame.text = "신제품 X100"
    buffer = BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


class TestPptxSideLabelsAndPictureFills:
    """PPTX gaps reproduced in review round 20: reading order of a vertically centred label beside a large content box, shapes filled
    with a picture."""

    def test_label_centered_beside_a_taller_box_is_read_with_it(self):
        """The label box sits at the vertical centre of the large content box on its right, so its top is 0.8cm lower and it was read
        after the content; '현황' was misplaced like the heading of the next line's content. A label vertically centred to the left
        of a large shape is in the same row."""
        boxes = []
        for index, (label, content) in enumerate([("현황", "수작업 정산 월 40시간"), ("문제점", "오류율 3.2%")]):
            top = 4 + 3.2 * index
            boxes += [(content, 6, top, 16, 2.6), (label, 1, top + 0.8, 4.5, 1)]

        result = convert(_pptx_with_boxes(boxes), "a.pptx")

        assert result.text == "## Slide 1\n\n현황\n\n수작업 정산 월 40시간\n\n문제점\n\n오류율 3.2%"

    def test_boxes_stacked_beside_a_tall_sidebar_keep_their_top_to_bottom_order(self):
        """Even when they overlap a tall box beside them, boxes stacked above one another are not grouped into one row (grouping
        would mix them in left-to-right order within the row)."""
        boxes = [("목차", 0, 0, 5, 19), ("1. 배경", 6, 1, 16, 1), ("세부 항목", 7, 2.5, 15, 1), ("2. 목표", 6, 4, 16, 1)]

        result = convert(_pptx_with_boxes(boxes), "a.pptx")

        assert result.text == "## Slide 1\n\n목차\n\n1. 배경\n\n세부 항목\n\n2. 목표"

    def test_label_joined_to_a_row_does_not_pull_the_next_row_into_it(self):
        """After a label joined a row, the next shape was compared with the label's top, so even the card value box with the same top
        as the label was grouped into the card title row. Mixed in left-to-right order within the row, the value was read before
        the title, like '1,200억, 매출'. The row check uses the top of the row's original shape, not the label."""
        boxes = [("핵심 지표", 0.5, 4.6, 3, 0.8)]
        for index, (name, value) in enumerate([("매출", "1,200억"), ("영업이익", "150억")]):
            left = 4 + 9.5 * index
            boxes += [("", left, 3, 9, 4), (name, left + 0.5, 3.1, 8, 0.8), (value, left + 0.1, 4.6, 8.8, 1.2)]

        result = convert(_pptx_with_boxes(boxes), "a.pptx")

        assert result.text == "## Slide 1\n\n핵심 지표\n\n매출\n\n영업이익\n\n1,200억\n\n150억"

    def test_shapes_level_with_a_joined_label_stay_in_its_row(self):
        """Neighbouring figures with the same top as a figure joined as a label are on that line. Leaving the label top out of the row
        check put the box beside them between the level figures, like '42%, NEW, 18분, 0g' (a real deck). Only shapes stacked above
        one another are sent to the next line."""
        boxes = [("NEW", 20, 5, 4, 4.5), ("42%", 1, 6.35, 6, 1.8), ("18분", 6.5, 6.35, 6, 1.8), ("0g", 12, 6.35, 5, 1.8),
                 ("비용 절감", 1, 8.2, 5, 0.75), ("배송 시간", 6.5, 8.2, 5, 0.75)]

        result = convert(_pptx_with_boxes(boxes), "a.pptx")

        assert result.text.split("\n\n")[1:] == ["42%", "18분", "0g", "NEW", "비용 절감", "배송 시간"]

    def test_shape_filled_with_a_picture_is_registered_as_a_picture(self):
        """A circle holding a photo (a:blipFill in spPr) is not a picture shape (p:pic), so it was dropped with no trace and no
        warning."""
        result = convert(_pptx_with_picture_filled_oval(), "a.pptx")

        assert result.text == "## Slide 1\n\n[Image]\n\n신제품 X100"
        assert len(result.images) == 1

    def test_text_on_a_picture_filled_shape_follows_the_picture(self):
        result = convert(_pptx_with_picture_filled_oval(text="대표 모델"), "a.pptx")

        assert result.text == "## Slide 1\n\n[Image]\n\n대표 모델\n\n신제품 X100"

    def test_tiled_picture_fill_is_a_texture_and_is_not_registered(self):
        """A tiled fill (a:tile) is a pattern or texture. Registering it as a picture only adds decorative noise."""
        result = convert(_pptx_with_picture_filled_oval(tiled=True), "a.pptx")

        assert result.text == "## Slide 1\n\n신제품 X100"
        assert not result.images


def _without_core_properties(data: bytes) -> bytes:
    """A copy with no document properties part (docProps/core.xml) and no relationship to it at all (not damage: a package that
    never wrote properties)."""
    stripped = builder.zip_with(data, edit={
        "_rels/.rels": lambda xml: re.sub(r'<Relationship [^>]*Target="docProps/core\.xml"/>', "", xml)})
    return _without_parts(stripped, "docProps/core.xml")


class TestOfficeLineBreaksNestedFieldsAndMetadata:
    @staticmethod
    def _run(text: str, properties: str = "") -> str:
        return f'<w:r><w:rPr>{properties}</w:rPr><w:t xml:space="preserve">{text}</w:t></w:r>'

    @staticmethod
    def _field(*parts: str) -> str:
        """A complex field. `parts` holds, in order, instruction runs, nested fields, '|' (the separator, w:fldChar separate) and
        result runs."""
        chars = {"|": "separate"}
        runs = [f'<w:r><w:fldChar w:fldCharType="{chars[part]}"/></w:r>' if part in chars else part for part in parts]
        return '<w:r><w:fldChar w:fldCharType="begin"/></w:r>' + "".join(runs) + '<w:r><w:fldChar w:fldCharType="end"/></w:r>'

    @staticmethod
    def _instruction(text: str) -> str:
        return f'<w:r><w:instrText xml:space="preserve">{text}</w:instrText></w:r>'

    def test_pptx_line_breaks_in_a_text_box_paragraph_keep_their_lines(self):
        """Line breaks (a:br, Shift+Enter) inside a text box paragraph were merged with spaces, so '분석 항목: 1. 수익성 비교 … 2. 안정성
        비교 …' became one line. Table cells and speaker notes kept their lines. Lines of a non-list paragraph are kept as they are."""
        br = "<a:br/>"
        runs = br.join(_text_run(line) for line in ["분석 항목:", "1. 수익성 비교 (영업이익률,  ROE)", "2. 안정성 비교", "",
                                                     "비교표로 정리"])

        body = _slide_body(_pptx_with_runs(runs))

        assert body == "분석 항목:\n1. 수익성 비교 (영업이익률, ROE)\n2. 안정성 비교\n비교표로 정리"

    def test_pptx_hand_typed_bullet_lines_inside_a_list_item_are_its_sub_items(self):
        """Inside a list item paragraph, '- ', '•' and '1.' lines joined by line breaks are items one level below that item ('- ' is
        added to a bullet-character line such as '•'), and the other lines continue the line before (same as DOCX
        `_list_item_lines`)."""
        data = _pptx_with_paragraphs([("사업기간\v- 1차: 2024.03\v• 2차: 2025.01\v1. 3차: 2026\v추가 협의", 0, ""),
                                      ("예산\v- 국비", 1, ""), ("검토", 0, _AUTO_NUMBER)], placeholder=True)

        body = _slide_body(data)

        assert body == ("- 사업기간\n  - 1차: 2024.03\n  - • 2차: 2025.01\n  1. 3차: 2026\n추가 협의\n"
                        "  - 예산\n    - 국비\n1. 검토")

    def test_pptx_title_with_a_line_break_stays_on_one_line(self):
        """The slide title is the section heading (one line), so line breaks are merged with spaces (deliberate)."""
        from io import BytesIO

        from pptx import Presentation

        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[5])
        slide.shapes.title.text = "2023년 신규지표\v1차 합의 제안서"
        buffer = BytesIO()
        presentation.save(buffer)

        result = convert(buffer.getvalue(), "a.pptx")

        assert result.text == "## Slide 1: 2023년 신규지표 1차 합의 제안서"

    def test_docx_result_of_a_field_nested_in_an_instruction_is_left_out(self):
        """In `{ IF { NUMPAGES } > 1 "다음 쪽에 계속" "" }` the result of the inner field (4) is part of the outer field's instruction
        but came out as text, giving '4다음 쪽에 계속'. A nested field on the result side (PAGEREF in a table of contents) is still
        text."""
        number_of_pages = self._field(self._instruction(" NUMPAGES "), "|", self._run("4"))
        condition = self._field(self._instruction(" IF "), number_of_pages, self._instruction(' > 1 "다음 쪽에 계속" "" '), "|",
                                self._run("다음 쪽에 계속"))
        page = self._field(self._instruction(" PAGEREF _Toc1 \\h "), "|", self._run("3"))
        body = ("<w:p>" + condition + "</w:p><w:p>"
                + self._field(self._instruction(" HYPERLINK \\l _Toc1 "), "|", self._run("1. 추진 배경 "), page) + "</w:p>")

        result = convert(_docx_with_body(body), "a.docx")

        assert result.text == "다음 쪽에 계속\n\n1. 추진 배경 3"

    @pytest.mark.parametrize(("make", "name"), [(builder.docx_bytes, "a.docx"), (builder.pptx_bytes, "a.pptx")])
    def test_package_without_document_properties_reports_no_library_default_title(self, make, name):
        """Without a document properties part, python-docx / python-pptx create default properties ('Word Document', 'PowerPoint
        Presentation'), so a title not in the document came out as metadata."""
        data = _without_core_properties(make())

        result = convert(data, name)

        assert "title" not in result.metadata and "author" not in result.metadata
        assert not result.warnings

    def test_docx_legal_numbering_shows_every_level_in_arabic_digits(self):
        """A w:isLgl level writes the numbers of the preceding levels in Arabic digits too (legal numbering). It was ignored, giving
        'I.1. 배경'."""
        numbering = ('<w:abstractNum w:abstractNumId="0">' + _number_level(0, "%1.", "upperRoman")
                     + _number_level(1, "%1.%2.", "decimal", "<w:isLgl/>") + _number_level(2, "%1.%2.%3", "lowerLetter")
                     + "</w:abstractNum>" + _number_list(1, 0))
        body = _paragraph("개요", 1, 0) + _paragraph("배경", 1, 1) + _paragraph("세부", 1, 2) + _paragraph("계획", 1, 0)

        result = convert(_docx_with_body(body, numbering=numbering), "a.docx")

        assert result.text == "I. 개요\n\n  - 1.1. 배경\n    - I.1.a 세부\n\nII. 계획"

    def test_docx_strikethrough_across_a_line_break_is_marked_per_line(self):
        """A strikethrough spanning a line break (w:br) was grouped into one mark, giving '~~- 1차 …\\n- 2차 …~~', and the first line
        started with '~~' so it was not read as a sub-item. The mark is closed and reopened on every line, and the bullet ('- ') at
        the start of a line stays outside the mark."""
        strike = "<w:strike/>"
        br = "<w:r><w:br/></w:r>"
        struck_br = f"<w:r><w:rPr>{strike}</w:rPr><w:br/></w:r>"
        numbering = '<w:abstractNum w:abstractNumId="0">' + _number_level(0, "-", "bullet") + "</w:abstractNum>" + _number_list(1, 0)
        item = ('<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>' + self._run("사업비") + br
                + self._run("- 1차: 10억원", strike) + struck_br + self._run("- 2차: 5억원", strike) + br
                + self._run("- 합계: 12억원") + "</w:p>")
        plain = "<w:p>" + self._run("삭제 예정 첫 줄", strike) + struck_br + self._run("둘째 줄", strike) + "</w:p>"

        result = convert(_docx_with_body(item + plain, numbering=numbering), "a.docx")

        assert result.text == ("- 사업비\n  - ~~1차: 10억원~~\n  - ~~2차: 5억원~~\n  - 합계: 12억원\n\n"
                               "~~삭제 예정 첫 줄~~\n~~둘째 줄~~")
