"""The character limit (`max_chars`) is also kept inside a single table (DOCX, PPTX, HWPX, HWP) and a single chart (DOCX, PPTX).
Counting only per top-level block, one table of hundreds of thousands of rows was drawn to the end before the excess was noticed
(200,000-row DOCX 33 s / 2.3GB, 100,000-row HWPX 7.4 s / 2.3GB). It stops per cell, but the counted value must not be larger than
the real one (the same boundary check as `test_converter.py::TestLimitIsExact`)."""

import re
import struct
import time

import pytest

from opendockit import ConvertOptions, convert
from tests import builder
from tests.test_hwp import (
    TAG_CTRL_HEADER,
    TAG_LIST_HEADER,
    TAG_PARA_HEADER,
    TAG_PARA_TEXT,
    TAG_TABLE,
    _fake_hwp,
)
from tests.test_odf import _NAMESPACES as _ODF_NAMESPACES
from tests.test_odf import _package as _odf_document
from tests.test_office import (
    _ALT_CHUNK_TYPE,
    _CHART_NS,
    _DIAGRAM_NS,
    _RELATIONSHIPS_NS,
    _chart_series,
    _chart_xml,
    _docx_inline,
    _docx_with_body,
    _docx_with_chunks,
    _pptx_with_chart,
    _pptx_with_smartart,
)


_ROWS = 20_000
_COLS = 4
_LIMIT = 5_000
# Text of a cell placed at the same position as the previous cell. TableGrid moves it to the next free position of that row
# (`place_or_next`) and TableBudget does not count it (undercounts).
# It must be longer than the slack of the table syntax (| --- |) for a miscount to show.
_DISPLACED = "겹친칸옮겨짐" * 50


def _docx_cell(text: str, properties: str = "") -> str:
    return f'<w:tc><w:tcPr>{properties}</w:tcPr><w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p></w:tc>'


def _docx_table(rows: list[str]) -> str:
    return "<w:tbl><w:tblGrid>" + "<w:gridCol/>" * _COLS + "</w:tblGrid>" + "".join(f"<w:tr>{row}</w:tr>" for row in rows) + "</w:tbl>"


def _docx_big_table() -> bytes:
    """Appending 20,000 rows with python-docx takes 30 s, so one placeholder paragraph is made and then replaced in the document.xml
    text."""
    table = _docx_table(["".join(_docx_cell(f"r{row}c{col}") for col in range(_COLS)) for row in range(_ROWS)])
    base = _docx_with_body("<w:p><w:r><w:t>BIGTABLE</w:t></w:r></w:p>")
    return builder.zip_with(base, edit={"word/document.xml": lambda xml: re.sub(r"<w:p>(?:(?!<w:p>).)*?BIGTABLE.*?</w:p>",
                                                                                lambda _: table, xml, count=1)})


def _docx_big_header_table(kind: str, rows: int, cell: str = "") -> bytes:
    """A table of `rows` rows in the header (or footer) part, and a short body. With kind "body" the same table is put in
    the body instead (the header then holds only its paragraph)."""
    from io import BytesIO

    from docx import Document

    document = Document()
    getattr(document.sections[0], "header" if kind == "body" else kind).paragraphs[0].text = "STORY"
    document.add_paragraph("body")
    buffer = BytesIO()
    document.save(buffer)
    table = _docx_table(["".join(_docx_cell(f"r{row}c{col}{cell}") for col in range(_COLS)) for row in range(rows)])
    part = "word/document.xml" if kind == "body" else f"word/{kind}1.xml"
    return builder.zip_with(buffer.getvalue(), edit={part: lambda xml: xml.replace("<w:p>", table + "<w:p>", 1)})


def _seconds(action) -> float:
    started = time.perf_counter()
    action()
    return time.perf_counter() - started


def _hwpx_table(cells: str, rows: int) -> str:
    return f'<hp:p><hp:run><hp:tbl rowCnt="{rows}" colCnt="{_COLS}">{cells}</hp:tbl></hp:run></hp:p>'


def _hwpx_big_table() -> bytes:
    rows = "".join("<hp:tr>" + "".join(builder._hwpx_cell(row, col, f"r{row}c{col}") for col in range(_COLS)) + "</hp:tr>"
                   for row in range(_ROWS))
    return builder.hwpx_bytes(_hwpx_table(rows, _ROWS))


def _hwp_table_section(cells: list[tuple[int, int, str, int, int]], rows: int) -> bytes:
    """One top-level table paragraph. `cells` is (row, column, text, row span, column span)."""
    record = builder.hwp_record
    parts = [record(TAG_PARA_HEADER, 0, builder.hwp_para_header()),
             record(TAG_PARA_TEXT, 1, builder.hwp_text((11, "tbl "))),
             record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("tbl ")),
             record(TAG_TABLE, 2, struct.pack("<IHH", 0, rows, _COLS) + b"\x00" * 10)]
    for row, col, text, row_span, col_span in cells:
        parts += [record(TAG_LIST_HEADER, 2, builder.hwp_cell(row, col, row_span=row_span, col_span=col_span)),
                  record(TAG_PARA_HEADER, 2, builder.hwp_para_header()),
                  record(TAG_PARA_TEXT, 3, builder.hwp_text(text))]
    return b"".join(parts)


def _hwp_paragraph(text: str) -> bytes:
    return (builder.hwp_record(TAG_PARA_HEADER, 0, builder.hwp_para_header())
            + builder.hwp_record(TAG_PARA_TEXT, 1, builder.hwp_text(text)))


class TestCharacterLimitInsideOneTable:
    def test_docx_stops_reading_rows_of_a_huge_table(self):
        data = _docx_big_table()

        result = convert(data, "a.docx", ConvertOptions(max_chars=_LIMIT))

        assert result.exceeded
        assert result.char_count < _LIMIT * 3  # drawn to the end it would be over 500,000 characters
        assert result.text.startswith("| r0c0 |")

    @pytest.mark.parametrize("kind", ["header", "footer"])
    def test_header_table_is_not_cut_by_the_characters_of_the_body(self, kind):
        """Headers are read after the body but written before it. The body's characters stopped the header table after its first
        row, so the text returned under the limit was not the front of the full conversion."""
        from io import BytesIO

        from docx import Document
        from docx.shared import Inches

        document = Document()
        story = getattr(document.sections[0], kind)
        table = story.add_table(rows=4, cols=2, width=Inches(4))
        for row in range(4):
            for col in range(2):
                table.cell(row, col).text = f"H{row}{col}xxxxxxxx"
        document.add_paragraph("B" * 95)
        buffer = BytesIO()
        document.save(buffer)
        full = convert(buffer.getvalue(), "a.docx", ConvertOptions(include_headers_footers=True)).text

        result = convert(buffer.getvalue(), "a.docx", ConvertOptions(max_chars=110, include_headers_footers=True))

        assert result.exceeded and result.text == full[:110].rstrip()

    @pytest.mark.parametrize("kind", ["header", "footer"])
    def test_header_table_longer_than_the_limit_stops_reading_rows(self, kind):
        """A header table is not charged the characters of the body, but its own text is part of the output: a table that passes
        the limit alone is not read to the end."""
        data = _docx_big_header_table(kind, _ROWS)

        result = convert(data, "a.docx", ConvertOptions(max_chars=_LIMIT, include_headers_footers=True))

        assert result.exceeded
        assert result.char_count < _LIMIT * 3  # drawn to the end it would be over 500,000 characters
        assert "| r0c0 |" in result.text

    @pytest.mark.timing
    def test_header_table_of_many_rows_is_not_rendered_to_the_end(self):
        """With no check at all on a header table, 60,000 rows in word/header1.xml took 13 s under the default limit (200,000 rows
        39 s and 2.0 GB); stopped at the limit it is the time of parsing the XML — the time the same table takes in the body,
        where it always was stopped (measured against that, so the speed of the machine does not matter: rendered to the end
        the header table took 5 to 7 times as long)."""
        options = ConvertOptions(include_headers_footers=True)

        def best(kind: str) -> float:
            data = _docx_big_header_table(kind, 60_000, cell="cell text abcdefghij")
            return min(_seconds(lambda: convert(data, "a.docx", options)) for _ in range(2))

        assert best("header") < best("body") * 2.5 + 0.5

    def test_hwpx_stops_reading_rows_of_a_huge_table(self):
        data = _hwpx_big_table()

        result = convert(data, "a.hwpx", ConvertOptions(max_chars=_LIMIT))

        assert result.exceeded
        assert result.char_count < _LIMIT * 3
        assert result.text.startswith("| r0c0 |")

    def test_hwp_stops_reading_rows_of_a_huge_table(self, monkeypatch):
        cells = [(row, col, f"r{row}c{col}", 1, 1) for row in range(_ROWS) for col in range(_COLS)]
        data = _fake_hwp(monkeypatch, sections=[_hwp_table_section(cells, _ROWS)])

        result = convert(data, "a.hwp", ConvertOptions(max_chars=_LIMIT))

        assert result.exceeded
        assert result.char_count < _LIMIT * 3
        assert result.text.startswith("| r0c0 |")


def _docx_with_stories(headers: list[str], footers: list[str], body: str) -> bytes:
    """One section per header, each with a header and a footer part of its own."""
    from io import BytesIO

    from docx import Document
    from docx.enum.section import WD_SECTION

    document = Document()
    for index, (header, footer) in enumerate(zip(headers, footers, strict=True)):
        section = document.sections[-1] if index == 0 else document.add_section(WD_SECTION.NEW_PAGE)
        section.header.is_linked_to_previous = False
        section.header.paragraphs[0].text = header
        section.footer.is_linked_to_previous = False
        section.footer.paragraphs[0].text = footer
    document.add_paragraph(body)
    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


class TestHeaderPartsPastTheLimit:
    """Header and footer parts are rendered one after another, each table against its own characters only: 40 header parts
    and 40 footer parts with a 5,000-row table each, every one under the limit, were all rendered to the end (74 s). Once the
    text written before a part is over the limit, the output is cut inside that text and the part is not rendered."""

    @pytest.fixture
    def rendered(self, monkeypatch):
        """The parts rendered as a header or footer, in order (the first word of their text)."""
        from opendockit.extractors import docx as docx_module

        parts: list[str] = []
        render_story = docx_module._DocxRenderer.render_story

        def spy(self, part, container):
            parts.append("".join(container.itertext()).split()[0])
            return render_story(self, part, container)

        monkeypatch.setattr(docx_module._DocxRenderer, "render_story", spy)
        return parts

    @staticmethod
    def _document(body: str = "body text") -> bytes:
        return _docx_with_stories([f"H{index} " + "h" * 60 for index in range(3)], [f"F{index} " + "f" * 60 for index in range(3)], body)

    def test_under_the_limit_every_part_is_rendered(self, rendered):
        result = convert(self._document(), "a.docx", ConvertOptions(include_headers_footers=True))

        assert not result.exceeded and all(f"{kind}{index} " in result.text for kind in "HF" for index in range(3))
        assert rendered == ["H0", "H1", "H2", "F0", "F1", "F2"]

    def test_headers_after_the_one_that_passes_the_limit_are_not_rendered(self, rendered):
        data = self._document()
        full = convert(data, "a.docx", ConvertOptions(include_headers_footers=True)).text
        rendered.clear()

        result = convert(data, "a.docx", ConvertOptions(max_chars=100, include_headers_footers=True))

        assert result.exceeded and result.text == full[:100].rstrip()
        assert rendered == ["H0", "H1"]   # 63 characters, then 126

    def test_footers_are_not_rendered_once_headers_and_body_pass_the_limit(self, rendered):
        data = self._document("B" * 300)
        full = convert(data, "a.docx", ConvertOptions(include_headers_footers=True)).text
        rendered.clear()

        result = convert(data, "a.docx", ConvertOptions(max_chars=400, include_headers_footers=True))

        assert result.exceeded and result.text == full[:400].rstrip()
        assert rendered == ["H0", "H1", "H2"]

    def test_footers_stop_at_the_one_that_passes_the_limit(self, rendered):
        data = self._document("B" * 100)
        full = convert(data, "a.docx", ConvertOptions(include_headers_footers=True)).text
        rendered.clear()

        result = convert(data, "a.docx", ConvertOptions(max_chars=360, include_headers_footers=True))

        assert result.exceeded and result.text == full[:360].rstrip()
        assert rendered == ["H0", "H1", "H2", "F0", "F1"]   # 189 + 100 = 289 before the footers, 352, then 415

    def test_part_that_repeats_an_earlier_one_is_not_counted(self, rendered):
        """Three header parts with the same text are written once: counted three times, a document under the limit was cut."""
        data = _docx_with_stories(["Same " + "h" * 60] * 3, [f"F{index} " + "f" * 20 for index in range(3)], "body text")
        full = convert(data, "a.docx", ConvertOptions(include_headers_footers=True)).text
        rendered.clear()

        result = convert(data, "a.docx", ConvertOptions(max_chars=170, include_headers_footers=True))

        assert not result.exceeded and result.text == full and full.count("Same") == 1
        assert rendered == ["Same", "Same", "Same", "F0", "F1", "F2"]


def _docx_boundary_document() -> bytes:
    """Document mixing a vertical merge, a horizontal merge (HTML table), a title row, a table in a table and a paragraph after the
    table."""
    restart, keep = '<w:vMerge w:val="restart"/>', "<w:vMerge/>"
    inner = _docx_table(["".join(_docx_cell(f"안{row}{col}") for col in range(2)) for row in range(2)])
    rows = [_docx_cell("분기별 실적 현황", '<w:gridSpan w:val="4"/>'),
            _docx_cell("구분") + _docx_cell("항목") + _docx_cell("값") + _docx_cell("비고"),
            _docx_cell("인건비", restart) + _docx_cell("1분기") + _docx_cell("1,200") + _docx_cell("메모 하나"),
            _docx_cell("", keep) + _docx_cell("2분기") + _docx_cell("합계 칸", '<w:gridSpan w:val="2"/>'),
            _docx_cell("기타") + _docx_cell("3분기") + f"<w:tc>{inner}<w:p/></w:tc>" + _docx_cell("끝")]
    plain = _docx_table(["".join(_docx_cell(f"행{row}칸{col}") for col in range(_COLS)) for row in range(6)])
    after = "<w:p><w:r><w:t>표 뒤 문단입니다.</w:t></w:r></w:p>"
    return _docx_with_body(_docx_table(rows) + plain + after)


def _hwpx_boundary_document() -> bytes:
    """A table, a paragraph, and a last table with vertical/horizontal merges and a cell overlapping the same position (it is moved
    to the next free position). If the position where the overlapping cell lands is miscounted, a document exactly as long as
    the limit stops early in the last table."""
    cell = builder._hwpx_cell
    merged = ("<hp:tr>" + cell(0, 0, "구분") + cell(0, 1, "내용", col_span=3) + "</hp:tr>"
              "<hp:tr>" + cell(1, 0, "일정", row_span=2) + cell(1, 1, "3월") + cell(1, 2, "4월") + cell(1, 3, "5월") + "</hp:tr>"
              "<hp:tr>" + cell(2, 1, "6월") + cell(2, 1, _DISPLACED) + cell(2, 2, "7월") + "</hp:tr>")
    plain = "".join("<hp:tr>" + "".join(cell(row, col, f"행{row}칸{col}") for col in range(_COLS)) + "</hp:tr>" for row in range(6))
    after = "<hp:p><hp:run><hp:t>표 뒤 문단입니다.</hp:t></hp:run></hp:p>"
    return builder.hwpx_bytes(_hwpx_table(plain, 6) + after + _hwpx_table(merged, 3))


def _hwp_boundary_sections() -> list[bytes]:
    merged = [(0, 0, "구분", 1, 1), (0, 1, "내용", 1, 3), (1, 0, "일정", 2, 1), (1, 1, "3월", 1, 1), (1, 2, "4월", 1, 1),
              (1, 3, "5월", 1, 1), (2, 1, "6월", 1, 1), (2, 1, _DISPLACED, 1, 1), (2, 2, "7월", 1, 1)]
    plain = [(row, col, f"행{row}칸{col}", 1, 1) for row in range(6) for col in range(_COLS)]
    return [_hwp_table_section(plain, 6) + _hwp_paragraph("표 뒤 문단입니다.") + _hwp_table_section(merged, 3)]


class TestLimitIsExactAroundTables:
    @pytest.mark.parametrize("kind", ["docx", "hwpx", "hwp"])
    def test_exceeded_matches_the_real_length_at_every_limit(self, kind, monkeypatch):
        """Changing the limit one by one, exceeded still equals 'characters converted to the end > limit'. That holds wherever
        counting the table cells stops."""
        make = {"docx": _docx_boundary_document, "hwpx": _hwpx_boundary_document,
                "hwp": lambda: _fake_hwp(monkeypatch, sections=_hwp_boundary_sections())}[kind]
        data = make()
        full = convert(data, f"a.{kind}", ConvertOptions(max_chars=None))

        results = {limit: convert(data, f"a.{kind}", ConvertOptions(max_chars=limit)) for limit in range(full.char_count + 2)}

        assert ("겹친칸옮겨짐" in full.text) == (kind != "docx")
        assert [limit for limit, result in results.items() if result.exceeded != (full.char_count > limit)] == []
        assert all(results[limit].text == full.text for limit in (full.char_count, full.char_count + 1))

    @pytest.mark.parametrize("kind", ["pptx", "docx_chart"])
    def test_slide_tables_and_charts_match_the_real_length_at_every_limit(self, kind):
        """A PPTX table stops reading its rows and a chart its value rows once the limit is passed. Wherever that happens,
        exceeded still equals 'characters converted to the end > limit', and the text is the full one at and past the limit."""
        data = {"pptx": _pptx_boundary_deck, "docx_chart": _docx_chart_boundary_document}[kind]()
        name = "a.pptx" if kind == "pptx" else "a.docx"
        full = convert(data, name, ConvertOptions(max_chars=None))

        results = {limit: convert(data, name, ConvertOptions(max_chars=limit)) for limit in range(full.char_count + 2)}

        assert "[Chart]" in full.text and "| --- |" in full.text
        assert [limit for limit, result in results.items() if result.exceeded != (full.char_count > limit)] == []
        assert all(results[limit].text == full.text for limit in (full.char_count, full.char_count + 1))


def _pptx_boundary_deck() -> bytes:
    """A text slide; a slide with a table with a horizontally merged title row and a vertical merge, then a text box; a slide
    with a percentage bar chart and a scatter chart."""
    from io import BytesIO

    from pptx import Presentation
    from pptx.chart.data import CategoryChartData, XyChartData
    from pptx.enum.chart import XL_CHART_TYPE
    from pptx.util import Inches

    presentation = Presentation()
    blank = presentation.slide_layouts[6]
    presentation.slides.add_slide(blank).shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1)).text = "첫 슬라이드 문단"

    slide = presentation.slides.add_slide(blank)
    table = slide.shapes.add_table(4, _COLS, Inches(1), Inches(1), Inches(6), Inches(2)).table
    for row in range(4):
        for col in range(_COLS):
            table.cell(row, col).text = f"행{row}칸{col}"
    table.cell(0, 0).merge(table.cell(0, _COLS - 1))
    table.cell(0, 0).text = "분기별 실적 현황"
    table.cell(1, 0).merge(table.cell(2, 0))
    slide.shapes.add_textbox(Inches(1), Inches(4), Inches(4), Inches(1)).text = "표 뒤 문단입니다."

    slide = presentation.slides.add_slide(blank)
    bars = CategoryChartData(number_format="0.0%")
    bars.categories = ["1분기", "2분기", "3분기", "4분기"]
    bars.add_series("점유율", (0.125, 0.25, 0.375, 0.5))
    bars.add_series("목표", (0.2, 0.3, 0.4, 0.5))
    slide.shapes.add_chart(XL_CHART_TYPE.COLUMN_CLUSTERED, Inches(1), Inches(1), Inches(4), Inches(3), bars)
    points = XyChartData()
    series = points.add_series("온도-수율")
    for x, y in [(10, 55.5), (20, 61.2), (30, 64.8)]:
        series.add_data_point(x, y)
    slide.shapes.add_chart(XL_CHART_TYPE.XY_SCATTER, Inches(1), Inches(4), Inches(4), Inches(3), points)
    buffer = BytesIO()
    presentation.save(buffer)
    return buffer.getvalue()


def _docx_frame_of_chart() -> str:
    return _docx_inline(f'<c:chart xmlns:c="{_CHART_NS}" xmlns:r="{_RELATIONSHIPS_NS}" r:id="rIdChart"/>', _CHART_NS)


def _docx_with_chart(body: str, chart: str) -> bytes:
    """A document whose body holds frames of one chart part (relationship rIdChart)."""
    relationship = f'<Relationship Id="rIdChart" Type="{_RELATIONSHIPS_NS}/chart" Target="charts/chart1.xml"/></Relationships>'
    return builder.zip_with(_docx_with_body(body), edit={
        "word/_rels/document.xml.rels": lambda xml: xml.replace("</Relationships>", relationship)},
        add={"word/charts/chart1.xml": chart})


def _docx_chart_boundary_document() -> bytes:
    """A paragraph, a bar chart of two series, a paragraph, and a table with a chart in a cell (its value table handed to the
    table)."""
    chart = _chart_xml("<c:barChart>" + _chart_series(0, "매출", [str(10 * index) for index in range(1, 7)],
                                                       [f"{month}월" for month in range(1, 7)])
                       + _chart_series(1, "비용", [str(7 * index) for index in range(1, 7)]) + "</c:barChart>", title="상반기 실적")
    cell_chart = _chart_xml("<c:barChart>" + _chart_series(0, "건수", ["3", "5", "8"], ["가", "나", "다"]) + "</c:barChart>")
    cell_frame = _docx_inline(f'<c:chart xmlns:c="{_CHART_NS}" xmlns:r="{_RELATIONSHIPS_NS}" r:id="rIdCell"/>', _CHART_NS)
    body = ("<w:p><w:r><w:t>차트 앞 문단입니다.</w:t></w:r></w:p>"
            f"<w:p><w:r>{_docx_frame_of_chart()}</w:r></w:p>"
            "<w:p><w:r><w:t>차트 뒤 문단입니다.</w:t></w:r></w:p>"
            + _docx_table([_docx_cell("구분") + _docx_cell("값") + _docx_cell("비고") + f"<w:tc><w:p><w:r>{cell_frame}</w:r></w:p></w:tc>"])
            + "<w:p><w:r><w:t>끝 문단입니다.</w:t></w:r></w:p>")
    relationship = f'<Relationship Id="rIdCell" Type="{_RELATIONSHIPS_NS}/chart" Target="charts/chart2.xml"/></Relationships>'
    return builder.zip_with(_docx_with_chart(body, chart), edit={
        "word/_rels/document.xml.rels": lambda xml: xml.replace("</Relationships>", relationship)},
        add={"word/charts/chart2.xml": cell_chart})


def _pptx_big_table(rows: int, *, hidden: bool = False, lead: str = "") -> bytes:
    """A slide with one table of `rows` rows of `_COLS` cells. python-pptx takes minutes to add that many rows, so one row is made
    and the others are put into the slide XML. With `hidden` the table frame is hidden (p:cNvPr hidden): the slide XML is the same
    size and is parsed the same way, but the table is not rendered. With `lead` a slide with that text comes first."""
    from io import BytesIO

    from pptx import Presentation
    from pptx.util import Inches

    presentation = Presentation()
    blank = presentation.slide_layouts[6]
    if lead:
        presentation.slides.add_slide(blank).shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1)).text = lead
    presentation.slides.add_slide(blank).shapes.add_table(1, _COLS, Inches(1), Inches(1), Inches(6), Inches(2))
    buffer = BytesIO()
    presentation.save(buffer)

    def cell(text: str) -> str:
        return f"<a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>{text}</a:t></a:r></a:p></a:txBody><a:tcPr/></a:tc>"

    table = "".join('<a:tr h="370840">' + "".join(cell(f"r{row}c{col}") for col in range(_COLS)) + "</a:tr>" for row in range(rows))

    def fill(xml: str) -> str:
        xml = re.sub(r"<a:tr .*?</a:tr>", lambda _: table, xml, count=1, flags=re.DOTALL)
        return re.sub(r'(<p:cNvPr [^>]*name="Table \d+")', r'\1 hidden="1"', xml, count=1) if hidden else xml

    return builder.zip_with(buffer.getvalue(), edit={f"ppt/slides/slide{2 if lead else 1}.xml": fill})


class TestPptxCharacterLimitInsideOneTable:
    """A PPTX table was the only one not checked per cell (`_notation.TableBudget`): counted per slide, a table of 1,000,000 rows
    (587 KB) was rendered to the end before the slide was found to be over the limit (20 s, 3.2 GB)."""

    def test_pptx_stops_reading_rows_of_a_huge_table(self):
        result = convert(_pptx_big_table(_ROWS), "a.pptx", ConvertOptions(max_chars=_LIMIT))

        assert result.exceeded
        assert result.char_count < _LIMIT * 3  # drawn to the end it would be over 500,000 characters
        assert "| r0c0 | r0c1 | r0c2 | r0c3 |" in result.text

    def test_table_under_the_limit_is_read_to_the_end(self):
        data = _pptx_big_table(300)
        full = convert(data, "a.pptx", ConvertOptions(max_chars=None))

        result = convert(data, "a.pptx", ConvertOptions(max_chars=full.char_count))

        assert not result.exceeded and result.text == full.text and "| r299c0 |" in result.text

    def test_characters_of_the_earlier_slides_are_charged_to_the_table(self):
        """The table stops as soon as the earlier slides and the cells read pass the limit together, not after its own cells
        alone have."""
        data = _pptx_big_table(_ROWS, lead="x" * (_LIMIT - 10))

        result = convert(data, "a.pptx", ConvertOptions(max_chars=_LIMIT))

        assert result.exceeded and result.text.startswith("## Slide 1\n\nxxx")
        assert result.char_count < _LIMIT + 200  # charged with its own cells only, the table would read 5,000 more

    @pytest.mark.timing
    def test_table_of_many_rows_is_not_rendered_to_the_end(self):
        """Stopped at the limit, a table of 60,000 rows takes about the time of parsing the slide XML: measured against the same
        table hidden (parsed, not rendered), so the speed of the machine does not matter."""
        def best(hidden: bool) -> float:
            data = _pptx_big_table(60_000, hidden=hidden)
            return min(_seconds(lambda: convert(data, "a.pptx", ConvertOptions(max_chars=_LIMIT))) for _ in range(2))

        assert best(hidden=False) < best(hidden=True) * 2 + 0.3


def _odf_chart(rows: int) -> bytes:
    """The sub-document of an ODF chart titled 'Big' with its own copy of `rows` values (`C<i>` → i) in a local table."""
    values = "".join(f'<table:table-row><table:table-cell><text:p>C{index}</text:p></table:table-cell><table:table-cell '
                     f'office:value-type="float" office:value="{index}"><text:p>{index}</text:p></table:table-cell></table:table-row>'
                     for index in range(rows))
    return (f'<?xml version="1.0" encoding="UTF-8"?><office:document-content {_ODF_NAMESPACES}><office:body><office:chart>'
            '<chart:chart chart:class="chart:bar"><chart:title><text:p>Big</text:p></chart:title><table:table table:name="local-table">'
            "<table:table-header-rows><table:table-row><table:table-cell/><table:table-cell><text:p>Sales</text:p></table:table-cell>"
            f"</table:table-row></table:table-header-rows><table:table-rows>{values}</table:table-rows></table:table></chart:chart>"
            "</office:chart></office:body></office:document-content>").encode()


def _pptx_with_big_chart(points: int, *, scatter: bool = False) -> bytes:
    """A slide with one chart of `points` cached points (a bar chart with a percentage format, or a scatter chart)."""
    def cache(offset: int, format_code: str = "") -> str:
        values = "".join(f'<c:pt idx="{index}"><c:v>{index + offset}</c:v></c:pt>' for index in range(points))
        code = f"<c:formatCode>{format_code}</c:formatCode>" if format_code else ""
        return f'<c:numRef><c:numCache>{code}<c:ptCount val="{points}"/>{values}</c:numCache></c:numRef>'

    if scatter:
        plot = (f'<c:scatterChart><c:ser><c:idx val="0"/><c:order val="0"/><c:tx><c:v>XY</c:v></c:tx>'
                f"<c:xVal>{cache(0)}</c:xVal><c:yVal>{cache(7)}</c:yVal></c:ser></c:scatterChart>")
    else:
        plot = (f'<c:barChart><c:ser><c:idx val="0"/><c:order val="0"/><c:tx><c:v>Sales</c:v></c:tx>'
                f'<c:val>{cache(0, "0.0%")}</c:val></c:ser></c:barChart>')
    return builder.zip_with(_pptx_with_chart(), edit={"ppt/charts/chart1.xml": lambda _: _chart_xml(plot, title="Big")})


class TestChartValuesPastTheLimit:
    """Chart value tables had no limit: the slide or block holding the chart was counted only after the whole table was built, so
    1,000,000 cached points (a 5 MB PPTX) took 9 s and 2.5 GB. The value rows stop once their text passes what the output can
    still take; there is no fixed cap on points, `max_chars` stays the only volume limit."""

    @pytest.mark.parametrize("scatter", [False, True])
    def test_pptx_chart_stops_at_the_limit(self, scatter):
        result = convert(_pptx_with_big_chart(_ROWS, scatter=scatter), "a.pptx", ConvertOptions(max_chars=_LIMIT))

        assert result.exceeded
        assert result.char_count < _LIMIT * 3  # drawn to the end it would be over 200,000 characters
        assert "[Chart] Big" in result.text
        assert ("| XY | 0 | 7 |" if scatter else "| 1 | 0.0% |") in result.text

    def test_docx_chart_stops_at_the_limit(self):
        chart = _chart_xml("<c:barChart>" + _chart_series(0, "Sales", [str(index) for index in range(_ROWS)]) + "</c:barChart>",
                           title="Big")
        data = _docx_with_chart(f"<w:p><w:r>{_docx_frame_of_chart()}</w:r></w:p>", chart)

        result = convert(data, "a.docx", ConvertOptions(max_chars=_LIMIT))

        assert result.exceeded
        assert result.char_count < _LIMIT * 3
        assert result.text.startswith("[Chart] Big\n\n| Category | Sales |\n| --- | --- |\n| 1 | 0 |")

    def test_header_chart_is_not_cut_by_the_characters_of_the_body(self):
        """A chart in a header is checked against its own characters only, as a header table is: headers are read after the body
        but written before it, so charged with the body's characters the chart stopped after its first row and the text returned
        was not the front of the full conversion."""
        from io import BytesIO

        from docx import Document

        document = Document()
        document.sections[0].header.paragraphs[0].text = "HEADER"
        document.add_paragraph("B" * 95)
        buffer = BytesIO()
        document.save(buffer)
        months = range(1, 13)
        chart = _chart_xml("<c:barChart>" + _chart_series(0, "Sales", [str(10 * month) for month in months], [f"M{month}" for month in months])
                           + "</c:barChart>", title="Months")
        relationships = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                         f'relationships"><Relationship Id="rIdChart" Type="{_RELATIONSHIPS_NS}/chart" Target="charts/chart1.xml"/>'
                         "</Relationships>")
        data = builder.zip_with(buffer.getvalue(), edit={
            "word/header1.xml": lambda xml: xml.replace("<w:t>HEADER</w:t></w:r>", f"<w:t>HEADER</w:t></w:r><w:r>{_docx_frame_of_chart()}</w:r>")},
            add={"word/charts/chart1.xml": chart, "word/_rels/header1.xml.rels": relationships})
        options = ConvertOptions(include_headers_footers=True)
        full = convert(data, "a.docx", options).text

        result = convert(data, "a.docx", ConvertOptions(max_chars=110, include_headers_footers=True))

        assert "| M12 | 120 |" in full and full.endswith("B" * 95)
        assert result.exceeded and result.text == full[:110].rstrip()

    def test_chart_under_the_limit_is_read_to_the_end(self):
        data = _pptx_with_big_chart(300)
        full = convert(data, "a.pptx", ConvertOptions(max_chars=None))

        result = convert(data, "a.pptx", ConvertOptions(max_chars=full.char_count))

        assert not result.exceeded and result.text == full.text and "| 300 | 29900.0% |" in result.text

    def test_xlsx_chart_stops_at_the_limit(self):
        """A chart placed on a sheet goes through the same value rows (`_xlsx_parts._chart_text`)."""
        from tests.test_spreadsheet import _xlsx_with_chart

        def xlsx(points: int) -> bytes:
            chart = _chart_xml("<c:barChart>" + _chart_series(0, "Sales", [str(index) for index in range(points)]) + "</c:barChart>",
                               title="Big")
            return builder.zip_with(_xlsx_with_chart(cached=True), edit={"xl/charts/chart1.xml": lambda _: chart})

        result = convert(xlsx(_ROWS), "a.xlsx", ConvertOptions(max_chars=_LIMIT))
        full = convert(xlsx(300), "a.xlsx", ConvertOptions(max_chars=None))

        assert result.exceeded
        assert result.char_count < _LIMIT * 3  # drawn to the end it would be over 200,000 characters
        assert "[Chart] Big\n\n| Category | Sales |\n| --- | --- |\n| 1 | 0 |" in result.text
        assert "| 300 | 299 |" in full.text
        assert convert(xlsx(300), "a.xlsx", ConvertOptions(max_chars=full.char_count)).text == full.text

    def test_hwpx_chart_stops_at_the_limit(self):
        """A chart in a Hangul (HWPX) document goes through the same value rows (`hwpx._render_chart`)."""
        def hwpx(points: int) -> bytes:
            chart = _chart_xml("<c:barChart>" + _chart_series(0, "Sales", [str(index) for index in range(points)]) + "</c:barChart>",
                               title="Big")
            body = '<hp:p><hp:run><hp:t>Sales:</hp:t><hp:chart chartIDRef="Chart/chart1.xml"/></hp:run></hp:p>'
            return builder.zip_with(builder.hwpx_bytes(body), add={"Chart/chart1.xml": chart})

        result = convert(hwpx(_ROWS), "a.hwpx", ConvertOptions(max_chars=_LIMIT))
        full = convert(hwpx(300), "a.hwpx", ConvertOptions(max_chars=None))

        assert result.exceeded
        assert result.char_count < _LIMIT * 3
        assert result.text.startswith("Sales:\n\n[Chart] Big\n\n| Category | Sales |\n| --- | --- |\n| 1 | 0 |")
        assert "| 300 | 299 |" in full.text
        assert convert(hwpx(300), "a.hwpx", ConvertOptions(max_chars=full.char_count)).text == full.text

    def test_hwpx_header_chart_is_not_cut_by_the_characters_of_the_body(self):
        """A header is written before the body but read where it stands in it. Charged with the body's characters, a chart in
        a header stopped after its fourth row and the cut text was not the front of the full conversion."""
        months = range(1, 13)
        chart = _chart_xml("<c:barChart>" + _chart_series(0, "Sales", [str(10 * month) for month in months],
                                                          [f"M{month}" for month in months]) + "</c:barChart>", title="Months")
        body = ("<hp:p><hp:run><hp:t>" + "B" * 95 + "</hp:t></hp:run></hp:p>"
                "<hp:p><hp:run><hp:ctrl><hp:header><hp:subList><hp:p><hp:run><hp:t>HEADER</hp:t>"
                '<hp:chart chartIDRef="Chart/chart1.xml"/></hp:run></hp:p></hp:subList></hp:header></hp:ctrl>'
                "<hp:t>tail</hp:t></hp:run></hp:p>")
        data = builder.zip_with(builder.hwpx_bytes(body), add={"Chart/chart1.xml": chart})
        full = convert(data, "a.hwpx", ConvertOptions(max_chars=None, include_headers_footers=True))

        cuts = {limit: convert(data, "a.hwpx", ConvertOptions(max_chars=limit, include_headers_footers=True))
                for limit in (110, 150, full.char_count - 1)}

        assert "| M12 | 120 |" in full.text and full.text.endswith("tail")
        assert all(result.exceeded and result.text == full.text[:limit].rstrip() for limit, result in cuts.items())

    @pytest.mark.parametrize("place", ["text", "footnote", "slide"])
    def test_odf_chart_stops_at_the_limit(self, place):
        """A chart in an ODT or ODP document writes its own copy of its values (`odf._Renderer._chart`): 20,000 rows under a
        5,000-character limit were all built, 517,000 characters counted."""
        frame = '<draw:frame><draw:object xlink:href="./Object 1"/></draw:frame>'
        kind, extension, body = {
            "text": ("text", "odt", f"<text:p>Sales:</text:p><text:p>{frame}</text:p>"),
            "footnote": ("text", "odt", '<text:p>Sales<text:note text:note-class="footnote"><text:note-citation>1</text:note-citation>'
                                        f"<text:note-body><text:p>{frame}</text:p></text:note-body></text:note></text:p>"),
            "slide": ("presentation", "odp", f"<draw:page>{frame}</draw:page>")}[place]

        def odf(rows: int) -> bytes:
            return _odf_document(kind, body, parts={"Object 1/content.xml": _odf_chart(rows)})

        result = convert(odf(_ROWS), f"a.{extension}", ConvertOptions(max_chars=_LIMIT))
        full = convert(odf(300), f"a.{extension}", ConvertOptions(max_chars=None))

        assert result.exceeded
        assert result.char_count < _LIMIT * 3  # drawn to the end it would be over 500,000 characters
        assert "[Chart] Big" in result.text and ("C0 / 0; C1 / 1" if place == "footnote" else "| --- | --- |\n| C0 | 0 |") in result.text
        assert ("C299 / 299" if place == "footnote" else "| C299 | 299 |") in full.text
        assert convert(odf(300), f"a.{extension}", ConvertOptions(max_chars=full.char_count)).text == full.text

    @pytest.mark.parametrize("place", ["header", "body", "footnote"])
    def test_odf_cut_chart_is_the_front_of_the_full_conversion(self, place):
        """Headers and footers of an ODT are read before the body, and notes are written after the text they are read in, so a
        chart anywhere is checked against the characters counted so far: all of them are written before it. The cut text is the
        front of the full conversion at every limit (a body chart with a footer after it as well)."""
        frame = '<draw:frame><draw:object xlink:href="./Object 1"/></draw:frame>'
        header = f"<text:p>{frame}</text:p>" if place == "header" else ""
        styles = ('<office:master-styles><style:master-page style:name="Standard">'
                  f"<style:header><text:p>HEADER</text:p>{header}</style:header><style:footer><text:p>{'F' * 60}</text:p></style:footer>"
                  "</style:master-page></office:master-styles>")
        body = "<text:p>" + "B" * 95 + "</text:p>" + {
            "header": "",
            "body": f"<text:p>{frame}</text:p>",
            "footnote": ('<text:p>tail<text:note text:note-class="footnote"><text:note-citation>1</text:note-citation>'
                         f"<text:note-body><text:p>{frame}</text:p></text:note-body></text:note></text:p>")}[place]
        data = _odf_document("text", body, styles=styles, parts={"Object 1/content.xml": _odf_chart(12)})
        options = {"include_headers_footers": True}
        full = convert(data, "a.odt", ConvertOptions(max_chars=None, **options))

        cuts = {limit: convert(data, "a.odt", ConvertOptions(max_chars=limit, **options))
                for limit in (110, 150, 200, full.char_count - 1)}

        assert "C11" in full.text and full.text.startswith("HEADER")
        assert all(result.exceeded and result.text == full.text[:limit].rstrip() for limit, result in cuts.items())

    @pytest.mark.timing
    @pytest.mark.parametrize("scatter", [False, True])
    def test_chart_of_many_points_is_not_rendered_to_the_end(self, scatter):
        """Stopped at the limit, a chart of 200,000 points takes a fraction of the time of the same chart drawn to its end
        (about a quarter for bars, two fifths for scatter); drawn to the end under the limit too, it took as long. Measured
        against that full conversion of the same file, so the speed and load of the machine do not matter."""
        data = _pptx_with_big_chart(200_000, scatter=scatter)

        def best(limit: int | None) -> float:
            return min(_seconds(lambda: convert(data, "a.pptx", ConvertOptions(max_chars=limit))) for _ in range(2))

        assert best(_LIMIT) < best(None) * 0.7


_REFERENCES = 40
_CHUNK_REFERENCE = '<w:altChunk r:id="rIdChunk"/>'
_CHUNK_TYPE = "text/html"


def _chunk_html(words: str) -> bytes:
    return f"<html><body><p>{words}</p></body></html>".encode()


def _docx_with_repeated_chunk(where: str, words: str = "Begin " + "chunk " * 149) -> bytes:
    """`_REFERENCES` references to one imported HTML part from the text boxes of one paragraph, from one table cell, or (`where`
    "header") from the header part, and a last paragraph."""
    chunks = {"rIdChunk": ("chunk1.html", _CHUNK_TYPE, _chunk_html(words))}
    if where != "header":
        boxes = "".join(f"<w:txbxContent>{_CHUNK_REFERENCE}</w:txbxContent>" for _ in range(_REFERENCES))
        body = {"text_boxes": f"<w:p><w:r><w:t>Intro</w:t></w:r>{boxes}</w:p>",
                "cell": f"<w:tbl><w:tr><w:tc>{_CHUNK_REFERENCE * _REFERENCES}<w:p/></w:tc></w:tr></w:tbl>"}[where]
        return _docx_with_chunks(body + "<w:p><w:r><w:t>Tail</w:t></w:r></w:p>", chunks)

    from io import BytesIO

    from docx import Document

    document = Document()
    document.sections[0].header.paragraphs[0].text = "HEADER"
    document.add_paragraph("Body")
    buffer = BytesIO()
    document.save(buffer)
    relationships = ('<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
                     f'relationships"><Relationship Id="rIdChunk" Type="{_ALT_CHUNK_TYPE}" Target="chunk1.html"/></Relationships>')
    return builder.zip_with(buffer.getvalue(), edit={
        "word/header1.xml": lambda xml: re.sub(r"<w:p>(?:(?!<w:p>).)*?HEADER.*?</w:p>",
                                               lambda _: _CHUNK_REFERENCE * _REFERENCES + "<w:p/>", xml, count=1, flags=re.DOTALL),
        "[Content_Types].xml": lambda xml: xml.replace(
            "</Types>", f'<Override PartName="/word/chunk1.html" ContentType="{_CHUNK_TYPE}"/></Types>')},
        add={"word/_rels/header1.xml.rels": relationships, "word/chunk1.html": _chunk_html(words).decode()})


def _smartart_model(words: str) -> str:
    return (f'<dgm:dataModel xmlns:dgm="{_DIAGRAM_NS}" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><dgm:ptLst>'
            f'<dgm:pt modelId="0" type="doc"/><dgm:pt modelId="1"><dgm:t><a:p><a:r><a:t>{words}</a:t></a:r></a:p></dgm:t></dgm:pt>'
            '</dgm:ptLst><dgm:cxnLst><dgm:cxn modelId="2" srcId="0" destId="1" srcOrd="0"/></dgm:cxnLst></dgm:dataModel>')


_SMARTART_FRAME = _docx_inline(f'<dgm:relIds xmlns:dgm="{_DIAGRAM_NS}" xmlns:r="{_RELATIONSHIPS_NS}" r:dm="rIdDgm"/>', _DIAGRAM_NS)


def _docx_with_smartart_frames(body: str, words: str) -> bytes:
    relationship = f'<Relationship Id="rIdDgm" Type="{_RELATIONSHIPS_NS}/diagramData" Target="diagrams/data1.xml"/></Relationships>'
    return builder.zip_with(_docx_with_body(body), edit={
        "word/_rels/document.xml.rels": lambda xml: xml.replace("</Relationships>", relationship)},
        add={"word/diagrams/data1.xml": _smartart_model(words)})


def _pptx_with_smartart_frames(frames: int, words: str) -> bytes:
    def repeat_frame(xml: str) -> str:
        frame = re.search(r"<p:graphicFrame>(?:(?!</p:graphicFrame>).)*<dgm:relIds (?:(?!</p:graphicFrame>).)*</p:graphicFrame>",
                          xml, flags=re.DOTALL).group(0)
        return xml.replace(frame, frame * frames)

    return builder.zip_with(_pptx_with_smartart(), edit={"ppt/slides/slide1.xml": repeat_frame,
                                                          "ppt/diagrams/data1.xml": lambda _: _smartart_model(words)})


def _count_parts_parsed(monkeypatch: pytest.MonkeyPatch, module: object, marker: bytes) -> list[int]:
    """Count how many times `module` parses a part holding `marker`. The count is the single item of the returned list."""
    parses = [0]
    parse = module.parse_part

    def counted(data: bytes) -> object:
        parses[0] += marker in data
        return parse(data)

    monkeypatch.setattr(module, "parse_part", counted)
    return parses


class TestRepeatedContentPastTheLimit:
    """One imported part (w:altChunk) or one SmartArt writes its whole content at every reference, and the text boxes of one
    paragraph, the blocks of one cell, a header part and the shapes of one slide were all rendered before anything was counted:
    200 references to a 400 K-character chunk from one paragraph (a 39 KB file) built 80 M characters (0.8 s and 300 MB for 30)
    before the cut. Once the output is past the limit, further references write nothing; the text cut at the limit is the same."""

    @pytest.mark.parametrize("where", ["text_boxes", "cell", "header"])
    def test_docx_chunk_references_stop_at_the_limit(self, where, monkeypatch: pytest.MonkeyPatch):
        from opendockit.extractors import docx as docx_module

        data = _docx_with_repeated_chunk(where)
        options = {"include_headers_footers": True}
        full = convert(data, "a.docx", ConvertOptions(max_chars=None, **options))
        writes = [0]
        renumber = docx_module.renumber_image_markers

        def counted(text: str, markers: dict) -> str:
            writes[0] += 1
            return renumber(text, markers)

        monkeypatch.setattr(docx_module, "renumber_image_markers", counted)

        result = convert(data, "a.docx", ConvertOptions(max_chars=_LIMIT, **options))

        assert full.text.count("Begin") == _REFERENCES and full.char_count > _LIMIT * 4
        assert result.exceeded and result.text == full.text[:_LIMIT].rstrip()
        assert writes[0] <= _LIMIT // 750 + 2  # 750 characters a reference; all 40 were written

    @pytest.mark.parametrize("kind", ["docx", "docx_cell", "pptx"])
    def test_smartart_frames_are_read_once_and_stop_at_the_limit(self, kind, monkeypatch: pytest.MonkeyPatch):
        from opendockit.extractors import docx as docx_module
        from opendockit.extractors import pptx as pptx_module

        words = "Begin " + "smart " * 149
        frames = f"<w:r>{_SMARTART_FRAME}</w:r>" * _REFERENCES
        data, name = {
            "docx": lambda: (_docx_with_smartart_frames(f"<w:p><w:r><w:t>Intro</w:t></w:r>{frames}</w:p>", words), "a.docx"),
            "docx_cell": lambda: (_docx_with_smartart_frames(f"<w:tbl><w:tr><w:tc><w:p>{frames}</w:p></w:tc></w:tr></w:tbl>", words),
                                  "a.docx"),
            "pptx": lambda: (_pptx_with_smartart_frames(_REFERENCES, words), "a.pptx")}[kind]()
        full = convert(data, name, ConvertOptions(max_chars=None))
        parses = _count_parts_parsed(monkeypatch, pptx_module if kind == "pptx" else docx_module, b"dataModel")

        result = convert(data, name, ConvertOptions(max_chars=_LIMIT))

        assert full.text.count("Begin") == _REFERENCES and full.char_count > _LIMIT * 4
        assert result.exceeded and result.text == full.text[:_LIMIT].rstrip()
        assert result.text.count("Begin") <= _LIMIT // 750 + 1
        assert parses == [1]

    def test_cut_text_is_the_front_of_the_full_text_at_every_limit(self):
        """A paragraph whose text boxes repeat a chunk, a SmartArt frame in a paragraph, a cell repeating the chunk and the
        SmartArt, and a last paragraph: wherever the limit falls, exceeded is 'the full text is longer' and the text is the
        front of the full text."""
        frame = f"<w:r>{_SMARTART_FRAME}</w:r>"
        boxes = "".join(f"<w:txbxContent>{_CHUNK_REFERENCE}</w:txbxContent>" for _ in range(3))
        body = (f"<w:p><w:r><w:t>첫 문단</w:t></w:r>{boxes}</w:p><w:p>{frame}</w:p>"
                f"<w:tbl><w:tr><w:tc><w:p><w:r><w:t>칸</w:t></w:r></w:p></w:tc>"
                f"<w:tc>{_CHUNK_REFERENCE * 2}<w:p>{frame * 2}</w:p></w:tc></w:tr></w:tbl>"
                "<w:p><w:r><w:t>끝 문단입니다.</w:t></w:r></w:p>")
        relationship = (f'<Relationship Id="rIdDgm" Type="{_RELATIONSHIPS_NS}/diagramData" Target="diagrams/data1.xml"/>'
                        "</Relationships>")
        data = builder.zip_with(_docx_with_chunks(body, {"rIdChunk": ("chunk1.html", _CHUNK_TYPE, _chunk_html("가져온 글 조각"))}),
                                edit={"word/_rels/document.xml.rels": lambda xml: xml.replace("</Relationships>", relationship)},
                                add={"word/diagrams/data1.xml": _smartart_model("도식 항목")})
        full = convert(data, "a.docx", ConvertOptions(max_chars=None))

        results = {limit: convert(data, "a.docx", ConvertOptions(max_chars=limit)) for limit in range(full.char_count + 2)}

        assert full.text.count("가져온 글 조각") == 5 and full.text.count("도식 항목") == 3
        assert [limit for limit, result in results.items() if result.exceeded != (full.char_count > limit)] == []
        assert [limit for limit, result in results.items() if result.text != full.text[:limit].rstrip()] == []
