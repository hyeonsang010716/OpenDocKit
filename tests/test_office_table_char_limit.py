"""The character limit (`max_chars`) is also kept inside a single table (DOCX, HWPX, HWP). Counting only per top-level block, one
table of hundreds of thousands of rows was drawn to the end before the excess was noticed (200,000-row DOCX 33 s / 2.3GB,
100,000-row HWPX 7.4 s / 2.3GB). It stops per cell, but the counted value must not be larger than the real one (the same boundary
check as `test_converter.py::TestLimitIsExact`)."""

import re
import struct

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
from tests.test_office import _docx_with_body


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
