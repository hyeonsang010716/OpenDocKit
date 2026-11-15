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
from tests.test_office import (
    _CHART_NS,
    _RELATIONSHIPS_NS,
    _chart_series,
    _chart_xml,
    _docx_inline,
    _docx_with_body,
    _pptx_with_chart,
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


def _pptx_with_big_chart(points: int, *, scatter: bool = False, valid: bool = True) -> bytes:
    """A slide with one chart of `points` cached points (a bar chart with a percentage format, or a scatter chart). With `valid`
    False every point index is past the range a chart can write, so the same XML is parsed and walked but there is no value row."""
    def cache(offset: int, format_code: str = "") -> str:
        values = "".join(f'<c:pt idx="{index if valid else index + 2**32}"><c:v>{index + offset}</c:v></c:pt>' for index in range(points))
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

    @pytest.mark.timing
    @pytest.mark.parametrize("scatter", [False, True])
    def test_chart_of_many_points_is_not_rendered_to_the_end(self, scatter):
        """Stopped at the limit, a chart of 200,000 points takes about the time of parsing and walking its XML: measured against
        the same chart whose point indexes cannot exist (walked, no row), so the speed of the machine does not matter."""
        def best(valid: bool) -> float:
            data = _pptx_with_big_chart(200_000, scatter=scatter, valid=valid)
            return min(_seconds(lambda: convert(data, "a.pptx", ConvertOptions(max_chars=_LIMIT))) for _ in range(2))

        assert best(valid=True) < best(valid=False) * 1.6 + 0.3
