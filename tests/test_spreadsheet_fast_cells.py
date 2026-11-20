"""The short paths of the XLSX cell reading give the same text as the long ones: `spreadsheet._cell_text` for text, numbers
and dates (looked up once per format), and the shared and inline strings that are one plain `<t>` (`_single_text`)."""

from datetime import datetime, time, timedelta
from io import BytesIO

import openpyxl
from openpyxl.styles import Alignment
import pytest

from opendockit import convert
from opendockit.extractors import _openpyxl_reading, spreadsheet
from opendockit.models import ConvertOptions
from tests import builder
from tests.test_spreadsheet_quick_rows import _MERGES, _ODD_CELLS, _USUAL, _xlsx


_FORMATS = ["General", "#,##0.00", "0.0%", "yyyy-mm-dd", "mmm-yy", "h:mm", "[h]:mm:ss", "[hhh]:mm", "aaa", '@" 님"', "@",
            '"약 "#,##0"원"', "0.00E+00", '#,##0;[Red]-#,##0', '_-* #,##0_-;-* #,##0_-;_-* "-"??_-;_-@_-', "000000", "# ?/?",
            'yyyy-mm-dd" [h]"', "[$-412]yyyy\"년\" m\"월\" d\"일\"", "0;-0;;@"]
_VALUES = ["text", "  padded  ", "line_x000D_\nbreak", "", 0, 1, -1, 1234.5, -0.0, 0.5, 45000, 45000.75, 1e20, 2958466,
           True, False, datetime(2024, 1, 5), datetime(2024, 1, 5, 13, 30, 15), datetime(1900, 1, 1), time(6, 30),
           timedelta(hours=36), "=A1+1"]


def _workbook() -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for row, number_format in enumerate(_FORMATS, start=1):
        for column, value in enumerate(_VALUES, start=1):
            sheet.cell(row=row, column=column, value=value).number_format = number_format
    sheet.cell(row=len(_FORMATS) + 1, column=1, value="indented").alignment = Alignment(indent=2)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _without_short_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spreadsheet, "_cell_style", lambda cell, reading: None)


def _row_texts(monkeypatch: pytest.MonkeyPatch, data: bytes, options: ConvertOptions) -> list[tuple[int, tuple[str, ...]]]:
    """The texts of every row as the sheet reading gives them, before they are laid out."""
    rows: list[tuple[int, tuple[str, ...]]] = []
    noting = spreadsheet._noting_numbers

    def recording(cells: object, numbers: list[int]) -> object:
        def recorded() -> object:
            for number, row in cells:
                rows.append((number, row))
                yield number, row
        return noting(recorded(), numbers)

    monkeypatch.setattr(spreadsheet, "_noting_numbers", recording)
    convert(data, "a.xlsx", options=options)
    monkeypatch.setattr(spreadsheet, "_noting_numbers", noting)
    return rows


@pytest.mark.parametrize("streamed", [False, True], ids=["full load", "streamed"])
class TestCellTextShortPath:
    def test_every_format_and_value(self, monkeypatch, streamed):
        if streamed:
            monkeypatch.setattr(spreadsheet, "_FULL_LOAD_MAX_SHEET_BYTES", 0)
        data = _workbook()
        options = ConvertOptions(max_chars=None, max_sheet_cols=200)
        short = _row_texts(monkeypatch, data, options)
        _without_short_paths(monkeypatch)
        assert short == _row_texts(monkeypatch, data, options)
        texts = {text for _, row in short for text in row}
        assert {"1,234.50", "36:00:00", "  padded"} <= texts or {"1,234.50", "36:00:00", "padded"} <= texts

    @pytest.mark.parametrize("rows", [_USUAL, _ODD_CELLS], ids=["usual cells", "odd cells"])
    def test_written_cells(self, monkeypatch, streamed, rows):
        if streamed:
            monkeypatch.setattr(spreadsheet, "_FULL_LOAD_MAX_SHEET_BYTES", 0)
        # ISO date cells under an elapsed format and under a weekday format are durations and weekdays, not dates
        data = _xlsx(rows + '<row r="30"><c r="A30" s="4" t="d"><v>2024-01-05T12:00:00</v></c>'
                            '<c r="B30" s="5" t="d"><v>2024-01-05</v></c><c r="C30" s="8"><v>45000</v></c></row>', _MERGES)
        short = _row_texts(monkeypatch, data, ConvertOptions())
        _without_short_paths(monkeypatch)
        assert short == _row_texts(monkeypatch, data, ConvertOptions())


class TestPlainStrings:
    """A shared or inline string of one plain `<t>` is taken straight to its text; it is what openpyxl's readers give."""

    _STRINGS = ["plain", "", " spaced ", "x005F_x000D_ kept", "a&amp;b", "line\nbreak"]

    def _data(self) -> bytes:
        rows = "".join(f'<row r="{row}"><c r="A{row}" t="s"><v>{row - 1}</v></c>'
                       f'<c r="B{row}" t="inlineStr"><is><t>{text}</t></is></c></row>'
                       for row, text in enumerate(self._STRINGS, start=1))
        strings = "".join(f"<si><t>{text}</t></si>" for text in self._STRINGS)
        strings += '<si foo="1"><t>attribute on si</t></si><si><t>a</t><t>b</t></si><si><t>t<b/>child</t></si>'
        rows += ('<row r="20"><c r="A20" t="s"><v>6</v></c><c r="B20" t="s"><v>7</v></c><c r="C20" t="s"><v>8</v></c>'
                 '<c r="D20" t="inlineStr"><is foo="1"><t>inline attribute</t></is></c></row>')
        data = _xlsx(rows)
        return builder.zip_with(data, edit={"xl/sharedStrings.xml": lambda xml: xml[:xml.index(">") + 1] + strings + "</sst>"})

    @pytest.mark.parametrize("streamed", [False, True], ids=["full load", "streamed"])
    def test_same_text_as_openpyxls_readers(self, monkeypatch, streamed):
        if streamed:
            monkeypatch.setattr(spreadsheet, "_FULL_LOAD_MAX_SHEET_BYTES", 0)
        data = self._data()
        short = convert(data, "a.xlsx", options=ConvertOptions(max_chars=None))
        monkeypatch.setattr(_openpyxl_reading, "_single_text", lambda element: None)
        monkeypatch.setattr(_openpyxl_reading, "QUICK_ROWS", False)
        long = convert(data, "a.xlsx", options=ConvertOptions(max_chars=None))
        assert short.text == long.text and short.warnings == long.warnings
        assert "plain" in short.text and "inline attribute" in short.text
