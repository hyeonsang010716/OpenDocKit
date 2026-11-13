"""Detailed rules for formatting values and reading rows of spreadsheets (XLSX, XLS, CSV)."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from io import BytesIO
import re
import signal
import struct
import zipfile

import openpyxl
from openpyxl.formatting.rule import CellIsRule
from openpyxl.styles import Font
import pytest
import xlrd

from opendockit import ConvertOptions, convert
from opendockit.exceptions import CorruptedDocumentError, EncryptedDocumentError, UnsupportedFormatError
from opendockit.extractors import xls
from opendockit.extractors._sheet_format import format_cell
from opendockit.extractors.spreadsheet import _merged_cell_count
from opendockit.extractors.xls import _xls_value
from opendockit.models import Extraction, image_marker
from tests import builder


def _xlsx(rows: list[list[object]], number_formats: dict[str, str] | None = None) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    for coordinate, number_format in (number_formats or {}).items():
        sheet[coordinate].number_format = number_format
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _xlsx_with_builtin_format_ids(values: list[float], format_ids: list[int]) -> bytes:
    """openpyxl cannot produce built-in format ids 27-36 and 50-58 (East Asian dates and times). Write the styles part by hand
    and give each cell from row 2 down one format id (column B)."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["서식", "값"])
    for value, format_id in zip(values, format_ids, strict=True):
        sheet.append([f"번호 {format_id}", value])
    buffer = BytesIO()
    workbook.save(buffer)

    formats = "".join(f'<xf numFmtId="{format_id}" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
                      for format_id in format_ids)
    styles = ('<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts><fills count="1"><fill><patternFill/></fill>'
              '</fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" '
              f'borderId="0"/></cellStyleXfs><cellXfs count="{len(format_ids) + 1}"><xf numFmtId="0" fontId="0" fillId="0" '
              f'borderId="0" xfId="0"/>{formats}</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/>'
              "</cellStyles></styleSheet>")

    def styled_cells(xml: str) -> str:
        for row in range(2, len(values) + 2):
            xml = xml.replace(f'<c r="B{row}"', f'<c r="B{row}" s="{row - 1}"')
        return xml

    return builder.zip_with(buffer.getvalue(), edit={"xl/styles.xml": lambda _: styles,
                                                     "xl/worksheets/sheet1.xml": styled_cells})


class TestNumberFormatSections:
    """Only the first of a format's `;` sections (positive;negative;zero) was used, so a negative got the positive mark
    (`+-5.2%`, `▲-1,200`)."""

    @pytest.mark.parametrize(
        ("value", "number_format", "expected"),
        [
            (0.052, "+0.0%;-0.0%;0.0%", "+5.2%"),
            (-0.052, "+0.0%;-0.0%;0.0%", "-5.2%"),
            (0, "+0.0%;-0.0%;0.0%", "0.0%"),
            (1200, '"▲"#,##0;"▼"#,##0', "▲1,200"),
            (-1200, '"▲"#,##0;"▼"#,##0', "▼1,200"),
            (-1200, "#,##0;(#,##0)", "(1,200)"),
            (-1200, '"₩"#,##0', "-₩1,200"),
            (-42, "#,##0;[Red]-#,##0", "-42"),
            (-1200, '_-* #,##0_-;\\-* #,##0_-;_-* "-"_-;_-@_-', "-1,200"),
            (0, '_-* #,##0_-;\\-* #,##0_-;_-* "-"_-;_-@_-', "0"),
            (0, '_-* #,##0_-;\\-* #,##0_-;_-* "-"??_-;_-@_-', "0"),
        ],
    )
    def test_uses_the_section_for_the_sign_of_the_value(self, value, number_format, expected):
        result = format_cell(value, number_format)

        assert result == expected


class TestNumberFormatRounding:
    """Python rounding (half to even) differs from Excel (0.5 rounds up), so 72.5 came out as 72 and 0.125 as 0.12."""

    @pytest.mark.parametrize(
        ("value", "number_format", "expected"),
        [
            (72.5, "0", "73"),
            (-72.5, "0", "-73"),
            (1234.5, "#,##0", "1,235"),
            (0.125, "0.00", "0.13"),
            (2.5, "0", "3"),
            (0.0125, "0.0%", "1.3%"),
            (1e300, "#,##0", f"{10 ** 300:,}"),
            (1.5, "0." + "0" * 600, "1.5"),  # a format beyond the rounding precision keeps the raw value
        ],
    )
    def test_rounds_half_up_like_excel(self, value, number_format, expected):
        result = format_cell(value, number_format)

        assert result == expected


class TestElapsedTime:
    @pytest.mark.parametrize(
        ("value", "number_format", "expected"),
        [
            (timedelta(hours=26), "[h]:mm", "26:00"),
            (timedelta(hours=26, minutes=5, seconds=7), "[h]:mm:ss", "26:05:07"),
            (timedelta(minutes=90, seconds=3), "[mm]:ss", "90:03"),
        ],
    )
    def test_elapsed_time_is_shown_as_total_hours(self, value, number_format, expected):
        """A `[h]:mm` duration came out as `1 day, 2:00:00`. It is shown as total hours, as in Excel."""
        result = format_cell(value, number_format)

        assert result == expected

    @pytest.mark.parametrize(
        ("value", "number_format", "expected"),
        [
            (timedelta(hours=26), "[Magenta][h]:mm", "26:00"),
            (timedelta(hours=26, seconds=7), "[Magenta][h]:mm:ss", "26:00:07"),
            (timedelta(minutes=90, seconds=3), "[Green][mm]:ss", "90:03"),
            (timedelta(seconds=95), "[Magenta][ss]", "95"),
        ],
    )
    def test_colour_name_starting_with_m_or_s_does_not_change_the_unit(self, value, number_format, expected):
        """The '[m' of `[Magenta][h]:mm` was read as elapsed minutes, so 26 hours became `1560:00`."""
        result = format_cell(value, number_format)

        assert result == expected

    def test_xlsx_elapsed_time_cell(self):
        data = _xlsx([["작업", "기간"], ["설치", timedelta(hours=26)]], {"B2": "[h]:mm"})

        result = convert(data, "a.xlsx")

        assert "| 설치 | 26:00 |" in result.text


class TestXlsValues:
    def test_error_cell_shows_its_error_text(self):
        """An error cell (#REF! etc.) became blank."""
        cell = xlrd.sheet.Cell(xlrd.XL_CELL_ERROR, 0x17)

        value = _xls_value(cell, 0, None)

        assert value == "#REF!"

    def test_time_only_value_is_a_time(self):
        """A time-only value (h:mm) was attached to Excel day 0 and came out as `1899-12-31 09:30:00`."""
        cell = xlrd.sheet.Cell(xlrd.XL_CELL_DATE, 9.5 / 24)

        value = _xls_value(cell, 0, "h:mm")

        assert value == time(9, 30)

    def test_elapsed_time_value_is_a_duration(self):
        cell = xlrd.sheet.Cell(xlrd.XL_CELL_DATE, 26 / 24)

        value = format_cell(_xls_value(cell, 0, "[h]:mm"), "[h]:mm")

        assert value == "26:00"

    def test_elapsed_time_value_too_large_for_a_duration_is_shown_as_the_number(self):
        """A value of 1 billion or more in a `[h]:mm` cell made timedelta raise OverflowError, so the whole document was
        rejected as 'corrupted'."""
        cell = xlrd.sheet.Cell(xlrd.XL_CELL_DATE, 1234567890.0)

        value = format_cell(_xls_value(cell, 0, "[h]:mm"), "[h]:mm")

        assert value == "1234567890"

    @pytest.mark.parametrize(("number_format", "days", "expected"),
                             [("[ss]", 330 / 86400, "330"), ("[mm]", 330 / 86400, "5:30"), ("[h]", 26 / 24, "26:00")])
    def test_number_cell_with_only_an_elapsed_section_is_a_duration(self, number_format, days, expected):
        """xlrd strips bracketed sections before checking for a date format, so a cell whose format is only an elapsed section
        (`[ss]`) comes as a number cell. The serial 0.0038… came out as is. It is output in the same shape as XLSX
        (`_elapsed_text`)."""
        cell = xlrd.sheet.Cell(xlrd.XL_CELL_NUMBER, days)

        value = format_cell(_xls_value(cell, 0, number_format), number_format)

        assert value == expected


class TestEastAsianBuiltinDateFormats:
    """Built-in format ids 27-36 and 50-58 (Korean/Chinese/Japanese dates and times) are General to openpyxl, so dates came out
    as serials (45365)."""

    @pytest.mark.parametrize("streamed", [False, True], ids=["full-load", "streamed"])
    def test_serial_numbers_become_dates_and_times(self, monkeypatch, streamed):
        from opendockit.extractors import spreadsheet

        if streamed:
            monkeypatch.setattr(spreadsheet, "_FULL_LOAD_MAX_SHEET_BYTES", 0)
        data = _xlsx_with_builtin_format_ids([45365, 45365, 45365.5, 0.5, 1234], [27, 31, 57, 32, 0])

        result = convert(data, "a.xlsx")

        assert ("| 번호 27 | 2024-03-14 |\n| 번호 31 | 2024-03-14 |\n| 번호 57 | 2024-03-14 12:00:00 |\n"
                "| 번호 32 | 12:00:00 |\n| 번호 0 | 1234 |") in result.text


class TestBuiltinAccountingFormat:
    """For built-in format id 44 (the accounting format of Excel's 'Currency' cell style), openpyxl's format string lacks the
    section separators (;), so it came out as `$1,234,567.5000000000@`. It must read the same as a cell whose format is that
    string written out."""

    @pytest.mark.parametrize("streamed", [False, True], ids=["full-load", "streamed"])
    def test_id_44_reads_like_the_explicit_accounting_format(self, monkeypatch, streamed):
        from opendockit.extractors import spreadsheet

        if streamed:
            monkeypatch.setattr(spreadsheet, "_FULL_LOAD_MAX_SHEET_BYTES", 0)
        data = _xlsx_with_builtin_format_ids([1234567.5, -98765.25, 0, 1234567.5], [44, 44, 44, 43])

        result = convert(data, "a.xlsx")

        assert ("| 번호 44 | $1,234,567.50 |\n| 번호 44 | $(98,765.25) |\n| 번호 44 | $0.00 |\n"
                "| 번호 43 | 1,234,567.50 |") in result.text


class TestEmptyRowGaps:
    """A run of 5,000 empty rows was taken as the end, so the content after it vanished without a warning."""

    def test_xlsx_data_after_a_long_gap_is_kept(self):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["이름", "값"])
        sheet.append(["가", 1])
        sheet["A6000"] = "멀리 떨어진 합계"
        buffer = BytesIO()
        workbook.save(buffer)

        result = convert(buffer.getvalue(), "a.xlsx")

        assert "멀리 떨어진 합계" in result.text
        assert not result.warnings

    def test_csv_data_after_many_blank_lines_is_kept(self):
        data = "이름,값\n가,1\n" + "\n" * 5_100 + "나,2\n"

        result = convert(data.encode(), "a.csv")

        assert "| 나 | 2 |" in result.text

    def test_streamed_sheet_that_stops_at_a_long_gap_warns(self, monkeypatch):
        from opendockit.extractors import spreadsheet

        monkeypatch.setattr(spreadsheet, "_FULL_LOAD_MAX_SHEET_BYTES", 0)
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.title = "목록"
        sheet["A1"] = "첫 행"
        sheet["A9000"] = "먼 행"
        buffer = BytesIO()
        workbook.save(buffer)

        result = convert(buffer.getvalue(), "a.xlsx", ConvertOptions(max_chars=None))

        assert "첫 행" in result.text
        assert any("목록" in warning and "empty rows" in warning for warning in result.warnings)


class TestCsvDialect:
    """Only the delimiter the Sniffer found was used and its quote character and skip-space-after-delimiter were dropped, so cells
    split at commas inside quotes."""

    def test_space_after_delimiter_does_not_break_quoted_fields(self):
        data = '이름, 나이, 주소\n홍길동, 30, "서울시, 강남구"\n김철수, 25, "부산시, 해운대구"\n'

        result = convert(data.encode(), "a.csv")

        assert result.text == ("| 이름 | 나이 | 주소 |\n| --- | --- | --- |\n| 홍길동 | 30 | 서울시, 강남구 |\n"
                               "| 김철수 | 25 | 부산시, 해운대구 |")

    def test_single_quote_as_quote_character(self):
        data = "'이름','금액'\n'홍길동','1,000'\n'김철수','2,500'\n"

        result = convert(data.encode(), "a.csv")

        assert result.text == "| 이름 | 금액 |\n| --- | --- |\n| 홍길동 | 1,000 |\n| 김철수 | 2,500 |"

    def test_apostrophe_inside_a_field_is_not_a_quote_character(self):
        """The Sniffer picked the apostrophes written inside cells such as `'24년` and `'목표'` as the quote character, which
        swallowed delimiters and shifted cells. An apostrophe is the quote character only when every quoted cell is wrapped whole."""
        data = "번호,기간,내용\n1,'24년 상반기,매출 '목표' 달성\n2,'24년 하반기,매출 '목표' 미달\n3,'25년 상반기,계획 수립\n"

        result = convert(data.encode(), "a.csv")

        assert result.text == ("| 번호 | 기간 | 내용 |\n| --- | --- | --- |\n| 1 | '24년 상반기 | 매출 '목표' 달성 |\n"
                               "| 2 | '24년 하반기 | 매출 '목표' 미달 |\n| 3 | '25년 상반기 | 계획 수립 |")

    @pytest.mark.parametrize(("data", "filename", "expected"), [
        ("서울\t1,234\t5,678\n부산\t2,345\t6,789\n대구\t3,456\t7,890\n", "매출.tsv",
         "| 서울 | 1,234 | 5,678 |\n| --- | --- | --- |\n| 부산 | 2,345 | 6,789 |\n| 대구 | 3,456 | 7,890 |"),
        ("지역\t매출(원, 세전)\t비용(원, 세전)\n서울\t1,234\t5,678\n부산\t2,345\t6,789\n", "매출.tsv",
         "| 지역 | 매출(원, 세전) | 비용(원, 세전) |\n| --- | --- | --- |\n| 서울 | 1,234 | 5,678 |\n| 부산 | 2,345 | 6,789 |"),
        ("2024-01-01;1,5;2,5\n2024-01-02;3,5;4,5\n2024-01-03;3,5;4,5\n", "price.csv",
         "| 2024-01-01 | 1,5 | 2,5 |\n| --- | --- | --- |\n| 2024-01-02 | 3,5 | 4,5 |\n| 2024-01-03 | 3,5 | 4,5 |"),
        ("A|1,000|x\nB|2,000|y\nC|3,000|z\n", "a.csv",
         "| A | 1,000 | x |\n| --- | --- | --- |\n| B | 2,000 | y |\n| C | 3,000 | z |"),
        ('1\t키워드는 "매출", "이익", "성장" 입니다\t김철수\n2\t키워드는 "비용", "손실", "감소" 입니다\t이영희\n', "a.tsv",
         '| 1 | 키워드는 "매출", "이익", "성장" 입니다 | 김철수 |\n| --- | --- | --- |\n'
         '| 2 | 키워드는 "비용", "손실", "감소" 입니다 | 이영희 |'),
        ("서울\t1,234\t5,678\n", "a.tsv", "| 서울 | 1,234 | 5,678 |\n| --- | --- | --- |"),
    ], ids=["tab-thousands", "tab-header-commas", "semicolon-decimal-commas", "pipe-thousands", "tab-quoted-words", "tab-one-line"])
    def test_delimiter_with_a_steady_column_count_wins_over_commas_inside_values(self, data, filename, expected):
        """When every line has the same number of commas (thousands separators, decimal commas, word lists) the Sniffer picked the
        comma over tab/semicolon and `1,234` split into two cells. When splitting by tab, semicolon or pipe gives the same cell
        count on every line, that delimiter is used."""
        result = convert(data.encode(), filename)

        assert result.text == expected

    def test_tab_delimiter_is_found_in_a_file_longer_than_the_sample(self):
        """The last line of the sample cut from the front is broken off midway and is short of cells. That line does not make
        the detection give up."""
        data = "".join(f"지점 {index:04d}\t1,234\t5,678\n" for index in range(1000))

        result = convert(data.encode(), "a.tsv")

        assert "| 지점 0999 | 1,234 | 5,678 |" in result.text

    @pytest.mark.parametrize(("data", "expected"), [
        ('이름,"비고; 메모"\n가,"확인; 완료"\n나,"보류; 취소"\n',
         "| 이름 | 비고; 메모 |\n| --- | --- |\n| 가 | 확인; 완료 |\n| 나 | 보류; 취소 |"),
        ('이름,메모; 비고\n가,"1차; 확인\n2차; 완료"\n나,"1차; 보류\n2차; 취소"\n',
         "| 이름 | 메모; 비고 |\n| --- | --- |\n| 가 | 1차; 확인<br>2차; 완료 |\n| 나 | 1차; 보류<br>2차; 취소 |"),
        ("이름,비고\n가,A;B\n나,C\n", "| 이름 | 비고 |\n| --- | --- |\n| 가 | A;B |\n| 나 | C |"),
        ("번호,이름,비고;메모\n", "| 번호 | 이름 | 비고;메모 |\n| --- | --- | --- |"),
    ], ids=["quoted", "quoted-multiline", "uneven", "one-line"])
    def test_comma_file_with_other_delimiters_inside_values_stays_comma_delimited(self, data, expected):
        """A comma CSV is read by commas even when its quoted cells hold one semicolon per line (line breaks inside quotes
        included)."""
        result = convert(data.encode(), "a.csv")

        assert result.text == expected

    def test_tab_file_whose_rows_omit_trailing_empty_cells_is_tab_delimited(self):
        """A TSV that omits trailing empty cells has a different tab count per line, so the tab cell count was uneven, the
        Sniffer's comma stayed, and `1,100,000` split at the thousands commas and shifted columns. When the header line has tabs and
        every line with a comma has a tab, it is read as tab-delimited."""
        rows = ["거래처\t공급가액\t부가세\t비고"] + [
            f"상사{i}\t{(i + 1) * 1100000:,}\t{(i + 1) * 110000:,}" + ("\t반품" if i % 3 == 0 else "") for i in range(5)]

        result = convert(("\n".join(rows) + "\n").encode(), "매입.tsv")

        assert result.text.startswith("| 거래처 | 공급가액 | 부가세 | 비고 |\n| --- | --- | --- | --- |\n"
                                      "| 상사0 | 1,100,000 | 110,000 | 반품 |\n| 상사1 | 2,200,000 | 220,000 |")
        assert "| 상사3 | 4,400,000 | 440,000 | 반품 |" in result.text

    @pytest.mark.parametrize(("data", "expected"), [
        ('이름,메모\n가,"A\tB"\n나,C\n', "| 이름 | 메모 |\n| --- | --- |\n| 가 | A\tB |\n| 나 | C |"),
        ("번호\t구분,이름\n1\t가,홍길동\n2,김철수\n", "| 번호\t구분 | 이름 |\n| --- | --- |\n| 1\t가 | 홍길동 |\n| 2 | 김철수 |"),
    ], ids=["no-tab-in-header", "comma-line-without-tab"])
    def test_comma_file_with_some_tabs_stays_comma_delimited(self, data, expected):
        """Without a tab in the header, or with a line that has commas but no tab, it is a comma CSV."""
        result = convert(data.encode(), "a.csv")

        assert result.text.replace("\t", " ") == expected.replace("\t", " ")


def test_csv_in_cp949_with_a_cut_last_character_is_read_as_korean():
    """When the last CP949 character was cut and strict decoding failed, it fell through to statistical guessing and the whole
    text came out garbled in another script."""
    data = ("이름,금액\n" + "홍길동,1000\n" * 50 + "2000,김철수").encode("cp949")[:-1]

    result = convert(data, "a.csv")

    assert "| 홍길동 | 1000 |" in result.text
    assert any("replaced with �" in warning for warning in result.warnings)


def test_xls_reader_does_not_write_to_stdout(monkeypatch, capsys):
    """xlrd writes minor inconsistencies of a file to stdout as `WARNING *** …`. It got mixed into the caller's output and broke
    the CLI's `--json`."""
    import sys

    from opendockit import CorruptedDocumentError

    def noisy_open(*args, **kwargs):
        kwargs.get("logfile", sys.stdout).write("WARNING *** OLE2 inconsistency\n")
        raise xlrd.biffh.XLRDError("cannot be read")

    monkeypatch.setattr(xls.xlrd, "open_workbook", noisy_open)

    with pytest.raises(CorruptedDocumentError):
        xls.extract_xls(b"xls", ConvertOptions())

    assert capsys.readouterr().out == ""


class TestSuperscriptInCells:
    """Superscripts/subscripts on part of a cell's text were joined as plain text, so m² read as m2 and 10⁶ as 106."""

    def test_xlsx_rich_text_superscript_and_subscript_are_marked(self):
        from openpyxl.cell.rich_text import CellRichText, TextBlock
        from openpyxl.cell.text import InlineFont

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["항목", "값"])
        sheet["A2"] = CellRichText("면적(m", TextBlock(InlineFont(vertAlign="superscript"), "2"), ")")
        sheet["B2"] = CellRichText("10", TextBlock(InlineFont(vertAlign="superscript"), "6"), ", CO",
                                   TextBlock(InlineFont(vertAlign="subscript"), "2"), TextBlock(InlineFont(b=True), " 굵게"))
        buffer = BytesIO()
        workbook.save(buffer)

        result = convert(buffer.getvalue(), "a.xlsx")

        assert "| 면적(m^2) | 10^6, CO_2 굵게 |" in result.text

    def test_xls_rich_text_runs_with_raised_or_lowered_fonts_are_marked(self):
        """XLS keeps a list of (character position, font index) per cell; a font's escapement 1 is superscript, 2 is subscript."""
        from opendockit.extractors.xls import _xls_rich_text

        escapements = {0: 0, 5: 1, 6: 2}

        text = _xls_rich_text("10의 6승 CO2 끝", [(1, 0), (4, 5), (5, 0), (9, 6), (10, 0)], escapements.get)

        assert text == "10의 ^6승 CO_2 끝"


def _xml_spreadsheet(number_format: str, kind: str, value: str) -> bytes:
    """Excel 2003 XML spreadsheet: one `name | value` row. The value cell gets a number format."""
    number_format = number_format.replace("&", "&amp;").replace('"', "&quot;")
    return ('<?xml version="1.0" encoding="UTF-8"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
            'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><Styles><Style ss:ID="s1">'
            f'<NumberFormat ss:Format="{number_format}"/></Style></Styles><Worksheet ss:Name="표"><Table>'
            '<Row><Cell><Data ss:Type="String">항목</Data></Cell><Cell><Data ss:Type="String">값</Data></Cell></Row>'
            f'<Row><Cell><Data ss:Type="String">가</Data></Cell><Cell ss:StyleID="s1"><Data ss:Type="{kind}">{value}</Data>'
            "</Cell></Row></Table></Worksheet></Workbook>").encode()


class TestNegativeSectionWithoutSign:
    """With a format whose negative section has no sign character (`#,##0;[Red]#,##0`), Excel marks negatives only in red. The
    colour is dropped, so a negative came out as its absolute value and a loss read as a profit."""

    @pytest.mark.parametrize(
        ("value", "number_format", "expected"),
        [
            (-234567, "#,##0;[Red]#,##0", "-234,567"),
            (-1234.5, "#,##0.00;[Red]#,##0.00", "-1,234.50"),
            (-1200, "#,##0_ ;[Red]#,##0_ ", "-1,200"),
            (-1200, '"$"#,##0;[Red]"$"#,##0', "-$1,200"),
            (-1200, '#,##0"원";[Red]#,##0"원"', "-1,200원"),
            (-0.052, "0.0%;[Red]0.0%", "-5.2%"),
            (234567, "#,##0;[Red]#,##0", "234,567"),
        ],
    )
    def test_minus_sign_is_added_when_the_negative_section_shows_none(self, value, number_format, expected):
        result = format_cell(value, number_format)

        assert result == expected

    @pytest.mark.parametrize(
        ("number_format", "expected"),
        [
            ("#,##0;[Red](#,##0)", "(1,200)"),
            ("#,##0_);[Red]\\(#,##0\\)", "(1,200)"),
            ("#,##0;[Red]-#,##0", "-1,200"),
            ("#,##0;△#,##0", "△1,200"),
            ('#,##0;[Red]"▼"#,##0', "▼1,200"),
            ('#,##0" 흑자";#,##0" 적자"', "1,200 적자"),
        ],
    )
    def test_negative_section_with_its_own_mark_is_left_alone(self, number_format, expected):
        result = format_cell(-1200, number_format)

        assert result == expected

    def test_xlsx_and_xml_spreadsheet_cells_keep_the_sign(self):
        """XLSX, XLS and Excel 2003 XML all go through the one `format_cell`."""
        xlsx = _xlsx([["항목", "값"], ["가", -234567]], {"B2": "#,##0;[Red]#,##0"})
        xml = _xml_spreadsheet("#,##0;[Red]#,##0", "Number", "-234567")

        results = [convert(xlsx, "a.xlsx"), convert(xml, "a.xls")]

        assert ["| 가 | -234,567 |" in result.text for result in results] == [True, True]


class TestFormatsWithoutIntegerDigits:
    @pytest.mark.parametrize(
        ("value", "number_format", "expected"),
        [
            (0.5, ".00", ".50"),
            (12.345, ".00", "12.35"),
            (-0.5, ".00", "-.50"),
            (0.5, "#.00", "0.50"),
            (0.5, "0.00", "0.50"),
        ],
    )
    def test_format_that_starts_with_the_decimal_point(self, value, number_format, expected):
        """`.00`, which has no integer digits, was read as 'leading character . + integer format 00', so 0.5 came out as `.01`."""
        result = format_cell(value, number_format)

        assert result == expected


class TestDateFormats:
    @pytest.mark.parametrize(
        "number_format",
        ["[$-en-US]mmmm d, yyyy;@", "[$-x-sysdate]dddd, mmmm dd, yyyy", "[$-409]d-mmm-yy", '[$-ko-KR]yyyy"년" m"월" d"일"'],
    )
    def test_letters_inside_a_locale_bracket_are_not_time_codes(self, number_format):
        """The s and h of a bracketed locale mark (`[$-en-US]`) were taken as time codes, so `00:00:00` was appended to date
        cells."""
        from datetime import datetime

        result = format_cell(datetime(2024, 3, 15), number_format)

        assert result == "2024-03-15"

    def test_time_codes_outside_brackets_still_show_the_time(self):
        from datetime import datetime

        result = format_cell(datetime(2024, 3, 15, 13, 45), "[$-en-US]yyyy-mm-dd hh:mm")

        assert result == "2024-03-15 13:45"

    def test_xml_spreadsheet_elapsed_time_is_shown_as_total_hours(self):
        """A `[h]:mm` value (30 hours) in Excel 2003 XML came out as the date `1900-01-01 06:00:00`. Total hours, as in XLSX and
        XLS."""
        data = _xml_spreadsheet("[h]:mm", "DateTime", "1900-01-01T06:00:00.000")

        result = convert(data, "a.xls")

        assert "| 가 | 30:00 |" in result.text

    def test_xml_spreadsheet_elapsed_time_under_a_day(self):
        data = _xml_spreadsheet("[h]:mm:ss", "DateTime", "1899-12-31T09:30:15.000")

        result = convert(data, "a.xls")

        assert "| 가 | 9:30:15 |" in result.text

    @pytest.mark.parametrize("number_format", ["aaa", "aaaa", "[$-412]aaaa", '"("aaa")"'])
    def test_xlsx_weekday_only_format_shows_the_date(self, number_format):
        """A weekday-only format (`aaa` = 목) is not recognised as a date format by openpyxl or xlrd, so the serial (45365) came out
        as is."""
        data = _xlsx([["항목", "값"], ["가", 45365]], {"B2": number_format})

        result = convert(data, "a.xlsx")

        assert "| 가 | 2024-03-14 |" in result.text

    def test_xls_weekday_only_format_shows_the_date(self):
        cell = xlrd.sheet.Cell(xlrd.XL_CELL_NUMBER, 45365.0)

        value = format_cell(_xls_value(cell, 0, "aaa"), "aaa")

        assert value == "2024-03-14"

    def test_number_format_with_other_letters_is_not_a_weekday_format(self):
        result = format_cell(45365, '0"aaa"')

        assert result == "45365aaa"


def _with_stored_results(data: bytes, results: dict[str, str]) -> bytes:
    """Put computed results, as if saved by Excel, into the formula cells openpyxl wrote without a result
    (`<f>formula</f><v></v>`)."""
    def edit(xml: str) -> str:
        for formula, value in results.items():
            xml = xml.replace(f"<f>{formula}</f><v></v>", f"<f>{formula}</f><v>{value}</v>")
        return xml

    return builder.zip_with(data, edit={"xl/worksheets/sheet1.xml": edit})


class TestFormulasPartlyWithoutResults:
    """When a library adds more formulas to a template file saved by Excel, only some formulas have a computed result. Values
    alone are read when any result exists, so the other formula cells became blank without a warning."""

    @pytest.mark.parametrize("streamed", [False, True], ids=["full-load", "streamed"])
    def test_formulas_without_results_are_shown_as_formulas_with_a_warning(self, monkeypatch, streamed):
        from opendockit.extractors import spreadsheet

        if streamed:
            monkeypatch.setattr(spreadsheet, "_MAX_MERGED_CELLS", -1)
        rows = [["국내", "해외", "합계"], [100, 200, "=A2+B2"], [150, 250, "=A3+B3"], ["=SUM(A2:A3)", "=SUM(B2:B3)", None]]
        data = _with_stored_results(_xlsx(rows), {"A2+B2": "300", "SUM(B2:B3)": "450"})

        result = convert(data, "a.xlsx")

        assert "| 100 | 200 | 300 |\n| 150 | 250 | =A3+B3 |\n| =SUM(A2:A3) | 450 |" in result.text
        assert result.warnings == ["Sheet 'Sheet': 2 formula(s) with no stored result were copied as written "
                                   "(opening and re-saving the file in Excel converts them to values)."]

    def test_formula_that_evaluated_to_an_empty_string_is_not_reported(self):
        """A formula that evaluated to an empty string (`IF(A2="","",…)`) is saved by Excel as `t="str"` with an empty `<v>`.
        It does not lack a result."""
        data = _with_stored_results(_xlsx([["값", "표시", "합"], [1, '=IF(A2=1,"","x")', "=A2+1"]]), {"A2+1": "2"})
        data = builder.zip_with(data, edit={"xl/worksheets/sheet1.xml": lambda xml: xml.replace('<c r="B2">', '<c r="B2" t="str">')})

        result = convert(data, "a.xlsx")

        assert "| 1 | | 2 |" in result.text
        assert not result.warnings

    def test_shared_formula_without_result_is_translated_to_its_own_row(self):
        """A shared formula has its text only in the first cell. The other cells show the first cell's formula shifted to their
        own position."""
        data = _with_stored_results(_xlsx([["가", "나", "합"], [1, 2, "=A2+B2"], [3, 4, "=A3+B3"], [5, 6, "=A4+B4"]]), {"A2+B2": "3"})
        data = builder.zip_with(data, edit={"xl/worksheets/sheet1.xml": lambda xml: (
            xml.replace("<f>A3+B3</f><v></v>", '<f t="shared" ref="C3:C4" si="0">A3+B3</f>')
            .replace("<f>A4+B4</f><v></v>", '<f t="shared" si="0"/>'))})

        result = convert(data, "a.xlsx")

        assert "| 1 | 2 | 3 |\n| 3 | 4 | =A3+B3 |\n| 5 | 6 | =A4+B4 |" in result.text
        assert any("2 formula(s)" in warning for warning in result.warnings)


_DRAWING_NAMESPACES = ('xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
                       'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')
_RELATIONSHIP_TYPES = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _anchor(row: int, content: str) -> str:
    return (f"<xdr:twoCellAnchor><xdr:from><xdr:col>3</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>{row}</xdr:row>"
            f"<xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:to><xdr:col>6</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>{row + 3}"
            f"</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:to>{content}<xdr:clientData/></xdr:twoCellAnchor>")


def _text_box(row: int, *paragraphs: str) -> str:
    body = "".join(f"<a:p><a:r><a:t>{paragraph}</a:t></a:r></a:p>" for paragraph in paragraphs)
    return _anchor(row, '<xdr:sp macro="" textlink=""><xdr:nvSpPr><xdr:cNvPr id="2" name="TextBox 1"/><xdr:cNvSpPr txBox="1"/>'
                        f"</xdr:nvSpPr><xdr:spPr/><xdr:txBody><a:bodyPr/>{body}</xdr:txBody></xdr:sp>")


def _picture(row: int, relationship_id: str) -> str:
    return _anchor(row, '<xdr:pic><xdr:nvPicPr><xdr:cNvPr id="3" name="Picture 1"/><xdr:cNvPicPr/></xdr:nvPicPr><xdr:blipFill>'
                        f'<a:blip r:embed="{relationship_id}"/><a:stretch><a:fillRect/></a:stretch></xdr:blipFill><xdr:spPr/></xdr:pic>')


def _xlsx_with_drawing(anchors: str, media: dict[str, bytes] | None = None) -> bytes:
    """Attach a drawing part (text boxes, pictures) to a sheet with a `항목 | 값` table. `media` is picture file name → content
    (relationship ids start at rId1)."""
    media = media or {}
    relationships = "".join(f'<Relationship Id="rId{index}" Type="{_RELATIONSHIP_TYPES}/image" Target="../media/{name}"/>'
                            for index, name in enumerate(media, start=1))
    package = "http://schemas.openxmlformats.org/package/2006/relationships"
    return builder.zip_with(
        _xlsx([["항목", "값"], ["가", 1]]),
        edit={"xl/worksheets/sheet1.xml": lambda xml: xml.replace(
            "</sheetData>", f'</sheetData><drawing xmlns:r="{_RELATIONSHIP_TYPES}" r:id="rId1"/>').replace(
                # in the order openpyxl writes, <drawing> must come after <pageMargins>
                f'<drawing xmlns:r="{_RELATIONSHIP_TYPES}" r:id="rId1"/><pageMargins', "<pageMargins").replace(
                    "</worksheet>", f'<drawing xmlns:r="{_RELATIONSHIP_TYPES}" r:id="rId1"/></worksheet>')},
        add={"xl/worksheets/_rels/sheet1.xml.rels": f'<Relationships xmlns="{package}"><Relationship Id="rId1" '
             f'Type="{_RELATIONSHIP_TYPES}/drawing" Target="../drawings/drawing1.xml"/></Relationships>',
             "xl/drawings/drawing1.xml": f"<xdr:wsDr {_DRAWING_NAMESPACES}>{anchors}</xdr:wsDr>",
             "xl/drawings/_rels/drawing1.xml.rels": f'<Relationships xmlns="{package}">{relationships}</Relationships>',
             **{f"xl/media/{name}": content for name, content in media.items()}})


@pytest.fixture(params=[False, True], ids=["full-load", "streamed"])
def streamed(request, monkeypatch):
    """Read the same XLSX through both paths, full load and streaming (lowering the merged-cell cap sends it to streaming)."""
    from opendockit.extractors import spreadsheet

    if request.param:
        monkeypatch.setattr(spreadsheet, "_MAX_MERGED_CELLS", -1)
    return request.param


class TestSheetDrawings:
    def test_text_box_text_is_kept_below_the_table(self, streamed):
        """A note (unit, basis) written in a text box beside the table was dropped without a warning."""
        data = _xlsx_with_drawing(_text_box(1, "※ 부가세 별도, 환율 1,350원 기준", "둘째 줄"))

        result = convert(data, "a.xlsx")

        assert result.text == "## Sheet: Sheet\n\n| 항목 | 값 |\n| --- | --- |\n| 가 | 1 |\n\n※ 부가세 별도, 환율 1,350원 기준\n둘째 줄"
        assert not result.warnings

    def test_text_boxes_and_pictures_follow_their_position_on_the_sheet(self, streamed):
        anchors = _text_box(9, "아래 글상자") + _picture(5, "rId1") + _text_box(1, "위 글상자")
        data = _xlsx_with_drawing(anchors, {"image1.png": builder.png_bytes(seed=3)})

        result = convert(data, "a.xlsx")

        assert result.text.endswith("| 가 | 1 |\n\n위 글상자\n\n[Image]\n\n아래 글상자")
        assert len(result.images) == 1

    def test_shape_with_a_fallback_copy_is_read_once(self, streamed):
        """A new shape keeps the real shape (Choice) and a copy for older programs (Fallback) together in
        `mc:AlternateContent`."""
        markup = "http://schemas.openxmlformats.org/markup-compatibility/2006"
        anchors = (f'<mc:AlternateContent xmlns:mc="{markup}"><mc:Choice Requires="a14">{_text_box(1, "단위: 천원")}</mc:Choice>'
                   f'<mc:Fallback>{_text_box(1, "단위: 천원")}</mc:Fallback></mc:AlternateContent>')
        data = _xlsx_with_drawing(anchors)

        result = convert(data, "a.xlsx")

        assert result.text.endswith("| 가 | 1 |\n\n단위: 천원")

    def test_text_box_is_kept_when_pictures_are_not_extracted(self, streamed):
        data = _xlsx_with_drawing(_text_box(1, "단위: 천원") + _picture(5, "rId1"), {"image1.png": builder.png_bytes(seed=3)})

        result = convert(data, "a.xlsx", ConvertOptions(extract_images=False))

        assert result.text.endswith("| 가 | 1 |\n\n단위: 천원")
        assert not result.images

    def test_text_box_counts_toward_the_character_limit(self, streamed):
        data = _xlsx_with_drawing(_text_box(1, "가" * 500))

        result = convert(data, "a.xlsx", ConvertOptions(max_chars=100))

        assert result.exceeded

    @pytest.mark.filterwarnings("ignore:wmf image format:UserWarning")
    def test_vector_picture_leaves_a_placeholder_and_a_warning(self, streamed):
        """openpyxl discards EMF/WMF pictures, so in a full load they vanished with neither placeholder nor warning and survived
        only when streaming."""
        import struct

        emf = struct.pack("<II4i4i", 1, 108, 0, 0, 100, 100, 0, 0, 2540, 2540) + b" EMF" + bytes(64)
        data = _xlsx_with_drawing(_picture(5, "rId1"), {"image1.emf": emf})

        result = convert(data, "a.xlsx")

        assert result.text.endswith("| 가 | 1 |\n\n[Image]")
        assert any("unreadable format" in warning for warning in result.warnings)


def test_xml_spreadsheet_with_a_damaged_byte_reports_the_replacement():
    """When Excel 2003 XML was decoded and read directly, there was no warning even if replacement characters went in."""
    rows = "".join(f'<Row><Cell><Data ss:Type="String">홍길동 {number}번 회의록</Data></Cell></Row>' for number in range(40))
    xml = ('<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">'
           f'<Worksheet ss:Name="표"><Table>{rows}</Table></Worksheet></Workbook>')
    data = xml.encode("cp949").replace("회의록".encode("cp949"), "회의록".encode("cp949") + b"\xc9\xa1", 1)

    result = convert(data, "a.xls")

    assert "홍길동 39번 회의록" in result.text
    assert any("replaced with �" in warning for warning in result.warnings)


def _xlsx_with_damaged_styles(damage: Callable[[str], str]) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["항목", "값"])
    sheet.append(["날짜", datetime(2026, 3, 5)])
    sheet.append(["금액", 1234.5])
    sheet["B2"].number_format = "yyyy-mm-dd"
    sheet["B3"].number_format = "#,##0.00"
    sheet.conditional_formatting.add("B3", CellIsRule(operator="greaterThan", formula=["0"], font=Font(bold=True)))
    buffer = BytesIO()
    workbook.save(buffer)
    return builder.zip_with(buffer.getvalue(), {"xl/styles.xml": damage})


class TestDamagedStylesheet:
    """openpyxl rejects a styles part that violates the spec (empty `<fill>`, unknown attribute) with TypeError, so the whole
    document became 'corrupted' (2 real POI samples; kordoc read them). It is read again with a stylesheet keeping only the display
    formats so that dates and amounts do not become serials and raw values."""

    @pytest.mark.parametrize("damage", [
        lambda xml: xml.replace("<fills count=\"2\">", "<fills count=\"3\"><fill></fill>", 1),
        lambda xml: xml.replace("<cellStyle ", "<cellStyle ZMIENNA_contentFontsCount=\"1\" ", 1),
        # openpyxl wraps a value outside the allowed list as ValueError("could not read stylesheet")
        lambda xml: xml.replace('patternType="gray125"', 'patternType="bogus"', 1),
        # a non-numeric id on an unused format was rejected again even with the minimal stylesheet
        lambda xml: xml.replace('<numFmt numFmtId="164"', '<numFmt numFmtId="abc"', 1),
    ], ids=["empty-fill", "unknown-attribute", "value-outside-the-allowed-list", "non-numeric-format-id"])
    def test_number_formats_survive_a_stylesheet_openpyxl_rejects(self, streamed, damage):
        data = _xlsx_with_damaged_styles(damage)

        result = convert(data, "a.xlsx")

        assert "| 날짜 | 2026-03-05 |" in result.text
        assert "| 금액 | 1,234.50 |" in result.text
        assert any("cell style information" in warning for warning in result.warnings)

    def test_warning_says_dates_may_show_as_numbers_when_a_used_format_is_lost(self):
        """When the format id a cell uses is not a number, that cell is General and its date comes out as a serial. The warning
        'date formats were kept' was untrue."""
        data = _xlsx_with_damaged_styles(lambda xml: xml.replace('<xf numFmtId="165"', '<xf numFmtId="x165"', 1))

        result = convert(data, "a.xlsx")

        assert any("may appear as numbers" in warning for warning in result.warnings)

    def test_memory_error_during_the_retry_is_not_hidden(self, monkeypatch):
        """Running out of memory must keep its cause (MemoryError) so the server can report 'document too large'
        (docs/server.md, resource limits)."""
        from opendockit.extractors import spreadsheet

        data = _xlsx_with_damaged_styles(lambda xml: xml.replace('<fills count="2">', '<fills count="3"><fill></fill>', 1))
        original = spreadsheet.openpyxl.load_workbook
        calls = []

        def load(*args, **kwargs):
            calls.append(1)
            if len(calls) == 2:
                raise MemoryError
            return original(*args, **kwargs)

        monkeypatch.setattr(spreadsheet.openpyxl, "load_workbook", load)

        with pytest.raises(CorruptedDocumentError) as caught:
            convert(data, "a.xlsx")

        assert isinstance(caught.value.__cause__, MemoryError)

    def test_stylesheet_that_is_not_xml_is_still_a_corrupted_document(self):
        data = _xlsx_with_damaged_styles(lambda xml: "<styleSheet")

        with pytest.raises(CorruptedDocumentError):
            convert(data, "a.xlsx")


class TestEscapedCharactersInStrings:
    """OOXML writes characters that XML cannot hold as `_xHHHH_` (the CR of a line break is `_x000D_`). They came out as is in
    the cell text."""

    @staticmethod
    def _with_shared_string(text: str) -> bytes:
        """Turn B2 into a shared string (the way Excel saves; openpyxl writes it directly inside the cell)."""
        main = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        content_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"
        return builder.zip_with(
            _xlsx([["항목", "비고"], ["가", "자리"]]),
            edit={"xl/worksheets/sheet1.xml": lambda xml: re.sub(r'<c r="B2"[^>]*>.*?</c>', '<c r="B2" t="s"><v>0</v></c>', xml),
                  "[Content_Types].xml": lambda xml: xml.replace(
                      "</Types>", f'<Override PartName="/xl/sharedStrings.xml" ContentType="{content_type}"/></Types>'),
                  "xl/_rels/workbook.xml.rels": lambda xml: xml.replace(
                      "</Relationships>", f'<Relationship Id="rId99" Type="{_RELATIONSHIP_TYPES}/sharedStrings" '
                                          'Target="sharedStrings.xml"/></Relationships>')},
            add={"xl/sharedStrings.xml": f'<sst xmlns="{main}" count="1" uniqueCount="1"><si><t>{text}</t></si></sst>'})

    def test_carriage_return_escape_does_not_leak_or_double_the_line_break(self, streamed):
        data = self._with_shared_string("1차 출고_x000D_\n2차 출고")

        result = convert(data, "a.xlsx")

        assert "| 가 | 1차 출고<br>2차 출고 |" in result.text

    def test_escape_of_a_printable_character_is_left_as_text(self, streamed):
        """Excel writes only control characters this way. `_x0041_` is text written like that in the cell."""
        data = self._with_shared_string("열 이름 pos_x0041_y")

        result = convert(data, "a.xlsx")

        assert "| 가 | 열 이름 pos_x0041_y |" in result.text

    def test_escaped_underscore_keeps_the_literal_text_in_a_streamed_inline_string(self, monkeypatch):
        """`_x005F_` means the following `x000D_` is literal. They can be told apart only on the path where openpyxl does not
        strip it first (streamed inlineStr) (Known accepted gap: the other paths read it as a line break)."""
        from opendockit.extractors import spreadsheet

        monkeypatch.setattr(spreadsheet, "_MAX_MERGED_CELLS", -1)
        data = builder.zip_with(_xlsx([["항목", "비고"], ["가", "자리"]]), edit={"xl/worksheets/sheet1.xml": lambda xml: re.sub(
            r'<c r="B2"[^>]*>.*?</c>', '<c r="B2" t="inlineStr"><is><t>코드 _x005F_x000D_ 끝</t></is></c>', xml)})

        result = convert(data, "a.xlsx")

        assert "| 가 | 코드 _x000D_ 끝 |" in result.text

    def test_inline_string_is_unescaped_too(self, streamed):
        def inline(xml: str) -> str:
            return re.sub(r'<c r="B2"[^>]*>.*?</c>', '<c r="B2" t="inlineStr"><is><t>1차_x000D_\n2차</t></is></c>', xml)

        data = builder.zip_with(_xlsx([["항목", "비고"], ["가", "자리"]]), edit={"xl/worksheets/sheet1.xml": inline})

        result = convert(data, "a.xlsx")

        assert "| 가 | 1차<br>2차 |" in result.text


class TestGeneralFormatWithLiterals:
    @pytest.mark.parametrize(("value", "number_format", "expected"), [
        (1234, 'General"명"', "1234명"),
        (1234.5, 'General"명"', "1234.5명"),
        (-5, 'General"명"', "-5명"),
        (1234, '"약 "General', "약 1234"),
        (1234, '"약 "General"개"', "약 1234개"),
        (1234, "General\\%", "1234%"),
        (1234, "[Red]General", "1234"),
        (1234, "General", "1234"),
    ])
    def test_general_keeps_the_literal_text_around_it(self, value, number_format, expected):
        """`General"명"` has no digit place, so the whole format was dropped and 1234 came out without its unit."""
        result = format_cell(value, number_format)

        assert result == expected


_XLS_ROWS = [["항목", "값"], ["가", "1"]]
_XLS_TABLE = "| 항목 | 값 |\n| --- | --- |\n| 가 | 1 |"


class TestXlsDrawings:
    """Text box and shape text (TXO records) and pictures (the global picture store) of XLS (BIFF8) were dropped without a
    warning. They go after the table, as in XLSX."""

    @staticmethod
    def _convert(data: bytes, options: ConvertOptions | None = None) -> Extraction:

        return xls.extract_xls(data, options or ConvertOptions())

    def test_text_box_text_is_kept_below_the_table_of_its_sheet(self):
        note = builder.xls_text_box("※ 부가세 별도, 환율 1,350원 기준\n둘째 줄")
        data = builder.xls_bytes([("견적", _XLS_ROWS, note), ("빈 시트", [["나", "다"]], b"")])

        extraction = self._convert(data)

        assert [section.text for section in extraction.sections] == [
            f"{_XLS_TABLE}\n\n※ 부가세 별도, 환율 1,350원 기준\n둘째 줄", "| 나 | 다 |\n| --- | --- |"]
        assert not extraction.warnings

    def test_text_stored_as_single_bytes_or_split_over_records_is_read(self):
        split = (builder.xls_record(0x00EC, builder.escher_record(0xF004, version=0xF)) + builder.xls_object(6)
                 + builder.xls_record(0x01B6, struct.pack("<HH6xHHI", 0x0212, 0, 7, 16, 0))
                 + builder.xls_record(0x003C, b"\x00Unit: ") + builder.xls_record(0x003C, b"\x01" + "원".encode("utf-16-le"))
                 + builder.xls_record(0x003C, bytes(16)))
        data = builder.xls_bytes([("Sheet1", _XLS_ROWS, builder.xls_text_box("USD", unicode=False) + split)])

        extraction = self._convert(data)

        assert extraction.sections[0].text.endswith("USD\n\nUnit: 원")

    def test_cell_comment_is_not_read_as_a_text_box(self):
        """A cell comment is also held in a TXO record. It is not read as a text box — comments go at the end of the sheet with
        their cell through the NOTE record (test_spreadsheet_notes_pictures_and_times.py::TestXlsCellComments). Without a NOTE it
        is no cell's comment."""
        data = builder.xls_bytes([("Sheet1", _XLS_ROWS, builder.xls_text_box("검토 의견", object_type=0x19))])

        extraction = self._convert(data)

        assert extraction.sections[0].text == _XLS_TABLE

    def test_picture_is_registered_after_the_table(self):
        picture = builder.png_bytes(seed=5)
        data = builder.xls_bytes([("Sheet1", _XLS_ROWS, builder.xls_text_box("위 글상자") + builder.xls_picture(1))],
                                 builder.xls_picture_store(builder.xls_png_blip(picture)))

        extraction = self._convert(data)
        without_images = self._convert(data, ConvertOptions(extract_images=False))

        assert [image.data for image in extraction.images] == [picture]
        assert extraction.sections[0].text == f"{_XLS_TABLE}\n\n위 글상자\n\n{image_marker(1)}"
        assert without_images.sections[0].text == f"{_XLS_TABLE}\n\n위 글상자"
        assert not extraction.warnings and not without_images.warnings

    def test_picture_whose_properties_follow_a_text_box_in_a_continue_record_is_found(self):
        """Excel writes the properties of the shape that follows a shape with text in a CONTINUE record, not MSODRAWING (the
        shape of real Excel files)."""
        picture = builder.png_bytes(seed=5)
        in_continue = struct.pack("<H", 0x003C) + builder.xls_picture(1)[2:]
        data = builder.xls_bytes([("Sheet1", _XLS_ROWS, builder.xls_text_box("위 글상자") + in_continue)],
                                 builder.xls_picture_store(builder.xls_png_blip(picture)))

        extraction = self._convert(data)

        assert [image.data for image in extraction.images] == [picture]
        assert not extraction.warnings

    def test_compressed_metafiles_share_one_decompression_budget(self):
        """Compressed metafiles (EMF) are inflated and registered, but the total inflated size is bound by the per-document
        decompression limit."""
        first, second = (struct.pack("<II", 1, 108) + bytes([seed]) * 4000 for seed in (1, 2))
        store = builder.xls_picture_store(builder.xls_emf_blip(first), builder.xls_emf_blip(second))
        data = builder.xls_bytes([("견적", _XLS_ROWS, builder.xls_picture(1) + builder.xls_picture(2) + builder.xls_picture(1))], store)

        unlimited = self._convert(data)
        limited = self._convert(data, ConvertOptions(max_uncompressed_bytes=6000))

        assert [image.data for image in unlimited.images] == [first, second, first]
        assert [image.data for image in limited.images] == [first, first]
        assert limited.warnings == ["Sheet '견적': 1 picture(s) could not be read."]

    def test_picture_that_cannot_be_found_is_reported(self):
        data = builder.xls_bytes([("견적", _XLS_ROWS, builder.xls_picture(3))], builder.xls_picture_store(builder.xls_png_blip(builder.png_bytes())))

        extraction = self._convert(data)

        assert extraction.sections[0].text == _XLS_TABLE
        assert extraction.warnings == ["Sheet '견적': 1 picture(s) could not be read."]

    def test_failure_while_reading_drawings_degrades_to_a_warning(self, monkeypatch):
        """Whatever fails while parsing drawing records, the cell contents are still converted."""

        def broken(*args, **kwargs):
            raise struct.error("unpack requires a buffer of 2 bytes")

        monkeypatch.setattr(xls, "_escher_picture_index", broken)
        data = builder.xls_bytes([("Sheet1", _XLS_ROWS, builder.xls_picture(1))], builder.xls_picture_store(builder.xls_png_blip(builder.png_bytes())))

        extraction = self._convert(data)

        assert extraction.sections[0].text == _XLS_TABLE
        assert extraction.warnings == [
            "Text box and shape text and pictures in the XLS could not be read, so only cell contents were converted."]

    def test_truncated_drawing_records_do_not_reject_the_workbook(self):
        """A shape whose property table is cut midway, a workbook whose picture store is truncated. Only the pictures cannot be
        read."""
        cut = builder.xls_record(0x00EC, builder.escher_record(0xF00B, b"\x04\x41\x01", version=3, instance=9))
        store = builder.xls_picture_store(builder.xls_png_blip(builder.png_bytes()))
        store = builder.xls_record(0x00EB, store[4:60])
        data = builder.xls_bytes([("견적", _XLS_ROWS, cut + builder.xls_object(8) + builder.xls_picture(1))], store)

        extraction = self._convert(data)

        assert extraction.sections[0].text.startswith(_XLS_TABLE)
        assert not any("only cell contents" in warning for warning in extraction.warnings)

    def test_text_box_counts_toward_the_character_limit(self):
        data = builder.xls_bytes([("Sheet1", _XLS_ROWS, builder.xls_text_box("가" * 500)), ("Sheet2", _XLS_ROWS, b"")])

        extraction = self._convert(data, ConvertOptions(max_chars=100))

        assert extraction.exceeded
        assert len(extraction.sections) == 1


class TestTextOnlyNumberFormats:
    """A format with only text and no digit place (`"매우 좋음"` on the value 5) shows just that text on screen in Excel. The format
    was dropped and the raw value 5 came out."""

    @pytest.mark.parametrize(("value", "number_format", "expected"), [
        (5, '"매우 좋음"', "매우 좋음"),
        (5, '" Excellent"', "Excellent"),
        (1, '"예";"예";"아니요"', "예"),
        (0, '"예";"예";"아니요"', "아니요"),
        (3, '[Blue]"합격"', "합격"),
        (2, "\\O\\K", "OK"),
        (-5, '"매우 좋음"', "-매우 좋음"),
        (-1, '"흑자";"적자"', "적자"),
        (0, 'General;General;"없음"', "없음"),
    ])
    def test_section_without_digit_places_shows_its_text(self, value, number_format, expected):
        result = format_cell(value, number_format)

        assert result == expected

    @pytest.mark.parametrize(("value", "number_format", "expected"), [
        (0, '_-* #,##0_-;\\-* #,##0_-;_-* "-"_-;_-@_-', "0"),  # the accounting format's zero section stays 0 (TestNumberFormatSections)
        (5, "[Red]", "5"),
        (5, "@", "5"),
        (5, '"번호 "@', "5"),
        (5, "General", "5"),
        (5, '[>=3]"상";"하"', "5"),
        (5, ";;;", "5"),
        (5, "-", "5"),
        (45365, 'yyyy"년"', "45365"),
    ])
    def test_sections_that_are_not_plain_text_keep_the_value(self, value, number_format, expected):
        result = format_cell(value, number_format)

        assert result == expected

    def test_xlsx_cell_with_a_text_only_format(self):
        data = _xlsx([["항목", "평가"], ["변속", 5]], {"B2": '"매우 좋음"'})

        result = convert(data, "a.xlsx")

        assert "| 변속 | 매우 좋음 |" in result.text


def _xls_formula(row: int, column: int, result: bytes) -> bytes:
    """A BIFF8 formula record (=A1+B1). `result` is the 8 bytes of the stored result."""
    return builder.xls_record(0x0006, struct.pack("<HHH", row, column, 0) + result + bytes(6)
                              + struct.pack("<H", 11) + bytes.fromhex("44000000c044000001c003"))


_XLS_NO_RESULT = bytes.fromhex("030000000000ffff")  # the 'empty string' result a library (xlwt) writes for every formula


class TestXlsFormulasWithoutResults:
    """An XLS made by a library (xlwt) stores no formula results, so formula cells became blank without a warning (XLSX
    warns)."""

    def test_workbook_whose_formulas_all_lack_results_is_reported(self):

        formulas = _xls_formula(1, 1, _XLS_NO_RESULT) + _xls_formula(2, 1, _XLS_NO_RESULT)
        data = builder.xls_bytes([("Sheet1", _XLS_ROWS, formulas)])

        extraction = xls.extract_xls(data, ConvertOptions())

        assert extraction.sections[0].text == "| 항목 | 값 |\n| --- | --- |\n| 가 |  |"
        assert extraction.warnings == ["The results of 2 formula(s) are not stored in the file, so those cells were read as blank "
                                       "(opening and re-saving the file in Excel converts them to values)."]

    def test_workbook_with_any_stored_result_is_not_reported(self):
        """With even one result it is a file saved by Excel. A formula that evaluated to an empty string normally has an empty
        result."""

        formulas = _xls_formula(1, 1, struct.pack("<d", 3.0)) + _xls_formula(2, 1, _XLS_NO_RESULT)
        data = builder.xls_bytes([("Sheet1", _XLS_ROWS, formulas)])

        extraction = xls.extract_xls(data, ConvertOptions())

        assert "| 가 | 3 |" in extraction.sections[0].text
        assert not extraction.warnings


def test_strict_open_xml_workbook_is_reported_as_unsupported():
    """A Strict Open XML workbook uses different namespaces (purl.oclc.org), so not a single sheet was found and it passed with
    an empty result."""
    def strict(xml: str) -> str:
        return (xml.replace("http://schemas.openxmlformats.org/spreadsheetml/2006/main", "http://purl.oclc.org/ooxml/spreadsheetml/main")
                .replace("http://schemas.openxmlformats.org/officeDocument/2006/relationships",
                         "http://purl.oclc.org/ooxml/officeDocument/relationships"))

    data = builder.zip_with(_xlsx([["항목", "값"], ["가", 1]]), edit=dict.fromkeys(
        ("_rels/.rels", "xl/workbook.xml", "xl/_rels/workbook.xml.rels", "xl/worksheets/sheet1.xml", "xl/styles.xml"), strict))

    with pytest.raises(UnsupportedFormatError, match="Strict Open XML"):
        convert(data, "a.xlsx")


def _with_zero_results(rows: list[list[object]], *, application: str, full_calc: bool, zero: str = "0") -> bytes:
    """Put `zero` into the result slot of the formula cells openpyxl wrote without a result, and change the producing program
    (docProps/app.xml) and the recalculate-on-open flag (calcPr fullCalcOnLoad). This is what libraries that do not calculate
    (POI SXSSF, xlsxwriter) save."""
    edit = {"xl/worksheets/sheet1.xml": lambda xml: re.sub(r"(<f>[^<]*</f>)<v></v>", rf"\1<v>{zero}</v>", xml),
            "docProps/app.xml": lambda xml: re.sub(r"<Application>[^<]*</Application>", f"<Application>{application}</Application>", xml)}
    if not full_calc:
        edit["xl/workbook.xml"] = lambda xml: xml.replace(' fullCalcOnLoad="1"', "")
    return builder.zip_with(_xlsx(rows), edit=edit)


_PRICE_ROWS = [["단가", "수량", "금액"], [1000, 3, "=A2*B2"], [2000, 4, "=A3*B3"], [None, "합계", "=SUM(C2:C3)"]]


class TestFormulasWithPlaceholderZeroResults:
    """Libraries that do not calculate (Apache POI SXSSF, xlsxwriter) write 0 in every formula's result slot. Excel recalculates
    on open and shows the totals, but reading the values gave 0 for every total and amount, passing wrong figures on as fact."""

    @pytest.mark.parametrize(("application", "full_calc", "zero"), [
        ("Apache POI", False, "0.0"), ("Microsoft Excel", True, "0")], ids=["poi-sxssf", "xlsxwriter"])
    @pytest.mark.parametrize("large", [False, True], ids=["small", "large"])
    def test_zero_results_from_a_library_that_does_not_calculate_are_shown_as_formulas(
            self, monkeypatch, application, full_calc, zero, large):
        from opendockit.extractors import spreadsheet

        if large:  # the path where the sheet XML is large and the other formula check is skipped
            monkeypatch.setattr(spreadsheet, "_FULL_LOAD_MAX_SHEET_BYTES", 0)
        data = _with_zero_results(_PRICE_ROWS, application=application, full_calc=full_calc, zero=zero)

        result = convert(data, "a.xlsx")

        assert "| 1000 | 3 | =A2*B2 |\n| 2000 | 4 | =A3*B3 |\n| | 합계 | =SUM(C2:C3) |" in result.text
        assert result.warnings == ["All formula results are stored as 0 (the file was made by a program that does not calculate), "
                                   "so the formulas were copied as written "
                                   "(opening and re-saving the file in Excel converts them to values)."]

    def test_zero_results_saved_by_excel_are_kept(self):
        """All-zero results in a file saved by Excel (no recalculate flag) are the normal values of an empty form."""
        data = _with_zero_results(_PRICE_ROWS, application="Microsoft Excel", full_calc=False)

        result = convert(data, "a.xlsx")

        assert "| 1000 | 3 | 0 |\n| 2000 | 4 | 0 |\n| | 합계 | 0 |" in result.text
        assert not result.warnings

    def test_any_nonzero_result_keeps_the_stored_results(self):
        """If even one result is not 0, the file was calculated (the library wrote the values along with the formulas)."""
        data = _with_zero_results(_PRICE_ROWS, application="Apache POI", full_calc=True)
        data = builder.zip_with(data, edit={"xl/worksheets/sheet1.xml": lambda xml: xml.replace(
            "<f>SUM(C2:C3)</f><v>0</v>", "<f>SUM(C2:C3)</f><v>11000</v>")})

        result = convert(data, "a.xlsx")

        assert "| 1000 | 3 | 0 |" in result.text
        assert "| | 합계 | 11000 |" in result.text
        assert not result.warnings

    def test_zero_logical_result_is_not_a_placeholder(self):
        """A formula that evaluated to FALSE (`t="b"` with 0) is not a 0 placeholder."""
        data = _with_zero_results([["값", "판정"], [1, "=A2>5"]], application="Apache POI", full_calc=False)
        data = builder.zip_with(data, edit={"xl/worksheets/sheet1.xml": lambda xml: xml.replace('<c r="B2">', '<c r="B2" t="b">')})

        result = convert(data, "a.xlsx")

        assert "| 1 | FALSE |" in result.text
        assert not result.warnings


_POI_BOF = (struct.pack("<HHHH", 0x0600, 0x0005, 0x0DBB, 0x07CC), struct.pack("<HHHH", 0x0600, 0x0005, 0x10D3, 0x07CC))


class TestXlsFormulasWithPlaceholderZeroResults:
    """Apache POI (HSSF) writes the number 0 in every formula's result slot. Excel recalculates on open, but xlrd returns the 0,
    so totals came out as 0."""

    def test_zero_results_in_a_workbook_written_by_poi_are_blanked_with_a_warning(self):

        formulas = _xls_formula(1, 1, struct.pack("<d", 0.0)) + _xls_formula(2, 1, struct.pack("<d", 0.0))
        data = builder.xls_bytes([("Sheet1", [*_XLS_ROWS, ["나", "2"]], formulas)]).replace(*_POI_BOF, 1)

        extraction = xls.extract_xls(data, ConvertOptions())

        assert extraction.sections[0].text == "| 항목 | 값 |\n| --- | --- |\n| 가 |  |\n| 나 |  |"
        assert extraction.warnings == ["The results of 2 formula(s) are stored only as 0 (the file was made by a program that does not "
                                       "calculate), so those cells were read as blank "
                                       "(opening and re-saving the file in Excel converts them to values)."]

    def test_zero_results_in_a_workbook_written_by_another_program_are_kept(self):
        """All-zero results are normal in an empty form saved by a program other than POI (Excel, LibreOffice)."""

        formulas = _xls_formula(1, 1, struct.pack("<d", 0.0)) + _xls_formula(2, 1, struct.pack("<d", 0.0))
        data = builder.xls_bytes([("Sheet1", [*_XLS_ROWS, ["나", "2"]], formulas)])

        extraction = xls.extract_xls(data, ConvertOptions())

        assert extraction.sections[0].text == "| 항목 | 값 |\n| --- | --- |\n| 가 | 0 |\n| 나 | 0 |"
        assert not extraction.warnings

    def test_poi_workbook_with_a_nonzero_result_is_kept(self):

        formulas = _xls_formula(1, 1, struct.pack("<d", 0.0)) + _xls_formula(2, 1, struct.pack("<d", 5.0))
        data = builder.xls_bytes([("Sheet1", [*_XLS_ROWS, ["나", "2"]], formulas)]).replace(*_POI_BOF, 1)

        extraction = xls.extract_xls(data, ConvertOptions())

        assert extraction.sections[0].text == "| 항목 | 값 |\n| --- | --- |\n| 가 | 0 |\n| 나 | 5 |"
        assert not extraction.warnings


def _xml_spreadsheet_rows(rows: str) -> bytes:
    return ('<?xml version="1.0" encoding="UTF-8"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
            f'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><Worksheet ss:Name="정산"><Table>{rows}</Table>'
            "</Worksheet></Workbook>").encode()


class TestXmlSpreadsheetFormulasWithoutResults:
    """A formula cell of Excel 2003 XML with no computed value (SheetJS etc. write only `ss:Formula`) became blank without a
    warning."""

    def test_formula_without_a_value_is_shown_as_an_a1_formula_with_a_warning(self):
        data = _xml_spreadsheet_rows(
            '<Row><Cell><Data ss:Type="String">교통비</Data></Cell><Cell><Data ss:Type="Number">35000</Data></Cell></Row>'
            '<Row><Cell><Data ss:Type="String">식비</Data></Cell><Cell><Data ss:Type="Number">120000</Data></Cell></Row>'
            '<Row><Cell><Data ss:Type="String">합계</Data></Cell><Cell ss:Formula="=SUM(R[-2]C:R[-1]C)"/></Row>'
            '<Row><Cell><Data ss:Type="String">부가세</Data></Cell><Cell ss:Formula="=R[-1]C*0.1">'
            '<Data ss:Type="Number"></Data></Cell></Row>'
            '<Row><Cell><Data ss:Type="String">고정</Data></Cell><Cell ss:Formula="=R1C2+R2C[0]&amp;&quot;RC&quot;"/></Row>')

        result = convert(data, "a.xml")

        assert "| 합계 | =SUM(B1:B2) |\n| 부가세 | =B3*0.1 |\n| 고정 | =$B$1+B$2&\"RC\" |" in result.text
        assert result.warnings == ["Sheet '정산': 3 formula(s) with no stored result were copied as written "
                                   "(opening and re-saving the file in Excel converts them to values)."]

    def test_formula_with_a_value_shows_the_value(self):
        data = _xml_spreadsheet_rows(
            '<Row><Cell><Data ss:Type="Number">5</Data></Cell>'
            '<Cell ss:Formula="=RC[-1]*2"><Data ss:Type="Number">10</Data></Cell></Row>')

        result = convert(data, "a.xml")

        assert result.text == "## Sheet: 정산\n\n| 5 | 10 |\n| --- | --- |"
        assert not result.warnings


def test_vertical_tab_escape_in_a_cell_becomes_a_line_break(streamed):
    """Excel writes a vertical tab inside a cell as `_x000B_`. It was removed as a control character, joining words as in
    `제시1)PACS`."""
    data = TestEscapedCharactersInStrings._with_shared_string("자료 제시_x000B_1)PACS 연동")

    result = convert(data, "a.xlsx")

    assert "| 가 | 자료 제시<br>1)PACS 연동 |" in result.text


def _xlsx_with_chart(cached: bool) -> bytes:
    from openpyxl.chart import BarChart, Reference

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "대시보드"
    sheet["A1"] = "월별 현황"
    data = workbook.create_sheet("data")
    for row in [["월", "매출"], ["1월", 120], ["2월", 135]]:
        data.append(row)
    chart = BarChart()
    chart.title = "월별 매출"
    chart.add_data(Reference(data, min_col=2, min_row=1, max_row=3), titles_from_data=True)
    chart.set_categories(Reference(data, min_col=1, min_row=2, max_row=3))
    sheet.add_chart(chart, "A3")
    buffer = BytesIO()
    workbook.save(buffer)
    if not cached:
        return buffer.getvalue()

    def with_cache(xml: str) -> str:
        """Like Excel, put the stored values (cache) of the series name, categories and values after each reference (the
        chart part's default namespace is the chart one)."""
        caches = {"'data'!B1": '<strCache><ptCount val="1"/><pt idx="0"><v>매출</v></pt></strCache>',
                  "'data'!$A$2:$A$3": '<strCache><ptCount val="2"/><pt idx="0"><v>1월</v></pt><pt idx="1"><v>2월</v></pt></strCache>',
                  "'data'!$B$2:$B$3": '<numCache><ptCount val="2"/><pt idx="0"><v>120</v></pt><pt idx="1"><v>135</v></pt></numCache>'}
        for reference, cache in caches.items():
            xml = xml.replace(f"<f>{reference}</f>", f"<f>{reference}</f>{cache}")
        return xml

    return builder.zip_with(buffer.getvalue(), edit={"xl/charts/chart1.xml": with_cache})


class TestSheetCharts:
    """A chart placed on a sheet vanished without a trace, leaving a chart-only dashboard sheet with just its title. As in PPTX
    and DOCX, a table is built from the chart's stored values."""

    def test_chart_with_cached_values_is_rendered_below_the_table(self, streamed):
        data = _xlsx_with_chart(cached=True)

        result = convert(data, "a.xlsx")

        assert "월별 현황\n\n[Chart] 월별 매출\n\n| Category | 매출 |\n| --- | --- |\n| 1월 | 120 |\n| 2월 | 135 |" in result.text

    def test_chart_without_cached_values_leaves_only_its_title(self):
        """A chart that stores no values (openpyxl) leaves only its title instead of an empty table."""
        data = _xlsx_with_chart(cached=False)

        result = convert(data, "a.xlsx")

        assert "월별 현황\n\n[Chart] 월별 매출\n\n## Sheet: data" in result.text


def _xlsx_with_chart_sheet(hidden: bool = False) -> bytes:
    """A workbook with the source table on a hidden sheet and the chart on a chart sheet (Excel F11). The chart has stored
    values, as in Excel."""
    from openpyxl.chart import BarChart, Reference

    workbook = openpyxl.Workbook()
    data = workbook.active
    data.title = "data"
    for row in [["월", "매출"], ["1월", 120], ["2월", 135]]:
        data.append(row)
    data.sheet_state = "hidden"
    chart = BarChart()
    chart.title = "월별 매출"
    chart.add_data(Reference(data, min_col=2, min_row=1, max_row=3), titles_from_data=True)
    chart.set_categories(Reference(data, min_col=1, min_row=2, max_row=3))
    chart_sheet = workbook.create_chartsheet("매출 차트")
    chart_sheet.add_chart(chart)
    chart_sheet.sheet_state = "hidden" if hidden else "visible"
    workbook.create_sheet("메모")["A1"] = "끝"
    buffer = BytesIO()
    workbook.save(buffer)
    caches = {"'data'!B1": '<strCache><ptCount val="1"/><pt idx="0"><v>매출</v></pt></strCache>',
              "'data'!$A$2:$A$3": '<strCache><ptCount val="2"/><pt idx="0"><v>1월</v></pt><pt idx="1"><v>2월</v></pt></strCache>',
              "'data'!$B$2:$B$3": '<numCache><ptCount val="2"/><pt idx="0"><v>120</v></pt><pt idx="1"><v>135</v></pt></numCache>'}

    def with_cache(xml: str) -> str:
        for reference, cache in caches.items():
            xml = xml.replace(f"<f>{reference}</f>", f"<f>{reference}</f>{cache}")
        return xml

    return builder.zip_with(buffer.getvalue(), edit={"xl/charts/chart1.xml": with_cache})


class TestChartSheets:
    """A chart sheet (xl/chartsheets) is not in `workbook.worksheets`, so a workbook whose source is a hidden sheet became an
    empty result ('No text was extracted.'). It is placed at its position in sheet order with the same `render_chart` as sheet
    charts."""

    def test_chart_sheet_is_rendered_in_sheet_order(self, streamed):
        data = _xlsx_with_chart_sheet()

        result = convert(data, "a.xlsx")

        assert ("## Sheet: 매출 차트\n\n[Chart] 월별 매출\n\n| Category | 매출 |\n| --- | --- |\n| 1월 | 120 |\n| 2월 | 135 |"
                "\n\n## Sheet: 메모\n\n끝") in result.text

    def test_hidden_chart_sheet_is_left_out_with_a_warning(self):
        data = _xlsx_with_chart_sheet(hidden=True)

        result = convert(data, "a.xlsx")

        assert "[Chart]" not in result.text
        assert "Hidden sheet '매출 차트' was excluded." in result.warnings

    def test_xls_chart_sheet_is_reported(self):
        """XLS charts are not converted to value tables (xlrd does not count chart sheets). A warning makes the reason for an
        empty result visible."""

        name = "매출 차트"
        chart_sheet = builder.xls_record(0x0085, struct.pack("<IBBBB", 0, 0, 2, len(name), 1) + name.encode("utf-16-le"))
        data = builder.xls_bytes([("표", [["월", "매출"]], b"")], globals_extra=chart_sheet)

        extraction = xls.extract_xls(data, ConvertOptions())

        assert extraction.warnings == ["Chart sheet '매출 차트' was not read "
                                       "(XLS charts are not converted to value tables — saving as XLSX converts them)."]


class TestNumberFormatLiteralsAndOptionalDecimals:
    """Spaces in quoted text and after the number were dropped (`약0원`, `0.00USD`), and optional decimal places (`#`) were filled
    with 0 (`12.000`). Matched against the values LibreOffice computes for the same formats."""

    @pytest.mark.parametrize(("value", "number_format", "expected"), [
        (1234567.891, '"약 "#,##0"원"', "약 1,234,568원"),
        (12, '#,##0.00 "USD"', "12.00 USD"),
        (12, '#,##0 "개"', "12 개"),
        (0.025, '0.0%" 증가"', "2.5% 증가"),
        (12, "0.###", "12"),
        (2.5, "0.###", "2.5"),
        (0.125, "#,##0.0#", "0.13"),
        (12, "#,##0.0#", "12.0"),
        (99999.5, "#,##0.0#", "99,999.5"),
        (1234567, "\\#,##0", "#1,234,567"),
    ])
    def test_matches_libreoffice(self, value, number_format, expected):
        assert format_cell(value, number_format) == expected


def _xls_label(row: int, column: int, value: str) -> bytes:
    """One BIFF8 text cell record (LABEL)."""
    return builder.xls_record(0x0204, struct.pack("<HHHH", row, column, 0, len(value)) + b"\x01" + value.encode("utf-16-le"))


def _xls_merges(ranges: list[tuple[int, int, int, int]]) -> bytes:
    """MERGEDCELLS records. A range is (first row, last row, first column, last column), ends included. 1,000 per record."""
    records = b""
    for start in range(0, len(ranges), 1000):
        chunk = ranges[start:start + 1000]
        records += builder.xls_record(0x00E5, struct.pack("<H", len(chunk)) + b"".join(struct.pack("<HHHH", *r) for r in chunk))
    return records


class TestXlsResourceBounds:
    """XLS lacks the column and merge limits of XLSX, so a 5.6KB file with one far cell (row 65536, column 256) built every
    row × column cell and took 42s, and the same merge range repeated 20,000 times took 17s."""

    @pytest.mark.timing
    def test_far_cell_does_not_make_every_row_full_width(self):
        import time


        far = _xls_label(65535, 255, "먼 칸") + _xls_label(65535, 1, "끝 행")
        data = builder.xls_bytes([(f"시트{index}", _XLS_ROWS, far) for index in range(3)])

        started = time.perf_counter()
        extraction = xls.extract_xls(data, ConvertOptions(max_chars=None))

        assert time.perf_counter() - started < 3
        assert extraction.sections[0].text == "| 항목 | 값 |\n| --- | --- |\n| 가 | 1 |\n|  | 끝 행 |"
        assert "Sheet '시트0': columns beyond column 100 were excluded." in extraction.warnings

    @pytest.mark.timing
    def test_repeated_merge_ranges_are_applied_once(self):
        import time


        rows = [["구분", "값"], ["가", "1"], ["", "2"], ["", "3"]]
        merges = _xls_merges([(1, 65535, 0, 0)] * 20_000) + _xls_label(65535, 1, "끝")
        data = builder.xls_bytes([("Sheet1", rows, merges)])

        started = time.perf_counter()
        extraction = xls.extract_xls(data, ConvertOptions(max_chars=None))

        assert time.perf_counter() - started < 3
        assert extraction.sections[0].text == ("| 구분 | 값 |\n| --- | --- |\n| 가 | 1 |\n| 가 | 2 |\n| 가 | 3 |\n"
                                               "| 가 | 끝 |")

    def test_merge_is_repeated_into_rows_without_a_cell_in_the_merged_column(self):
        """Each row is read only up to its own cells, so some rows have no cell in the merged column. The merge value is repeated
        into those rows too."""

        extra = _xls_label(2, 0, "나") + _xls_merges([(1, 2, 2, 2)])
        data = builder.xls_bytes([("Sheet1", [["구분", "값", "비고"], ["가", "1", "x"]], extra)])

        extraction = xls.extract_xls(data, ConvertOptions())

        assert extraction.sections[0].text == "| 구분 | 값 | 비고 |\n| --- | --- | --- |\n| 가 | 1 | x |\n| 나 |  | x |"

    def test_merges_covering_more_than_the_cell_budget_are_not_repeated(self, monkeypatch):
        """When merges cover more cells than the same cap as XLSX (500,000 cells), vertical merges are not repeated (first cell
        only) and a warning is given."""
        monkeypatch.setattr(xls, "_MAX_MERGED_CELLS", 2)
        rows = [["구분", "값"], ["가", "1"], ["", "2"], ["", "3"]]
        data = builder.xls_bytes([("Sheet1", rows, _xls_merges([(1, 3, 0, 0)]))])

        extraction = xls.extract_xls(data, ConvertOptions())

        assert extraction.sections[0].text == "| 구분 | 값 |\n| --- | --- |\n| 가 | 1 |\n|  | 2 |\n|  | 3 |"
        assert extraction.warnings == [
            "Sheet 'Sheet1': there are too many merged cells, so vertical merge values were left in the first cell only."]


class TestXlsxRowsFarApart:
    """A fully loaded sheet built empty rows one by one down to the last row, so 50 sheets with one cell on row 1,048,576 (28KB)
    took 28s."""

    @pytest.mark.timing
    def test_sheets_with_a_far_last_row_are_read_quickly(self):
        import time

        workbook = openpyxl.Workbook()
        for index in range(30):
            sheet = workbook.active if index == 0 else workbook.create_sheet()
            sheet.title = f"시트{index}"
            sheet.append(["항목", "값"])
            sheet.append(["가", 1])
            sheet["B1048576"] = "끝"
        buffer = BytesIO()
        workbook.save(buffer)

        started = time.perf_counter()
        result = convert(buffer.getvalue(), "a.xlsx", ConvertOptions(max_chars=None))

        assert time.perf_counter() - started < 5
        assert result.sections[29].text.endswith("| 항목 | 값 |\n| --- | --- |\n| 가 | 1 |\n| | 끝 |")

    def test_vertical_merge_is_repeated_down_to_a_far_row(self):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["구분", "값"])
        sheet["A2"], sheet["B2"], sheet["B9000"] = "가", 1, 2
        sheet.merge_cells("A2:A9000")
        buffer = BytesIO()
        workbook.save(buffer)

        result = convert(buffer.getvalue(), "a.xlsx")

        assert result.text.endswith("| 구분 | 값 |\n| --- | --- |\n| 가 | 1 |\n| 가 | 2 |")

    def test_formula_without_a_result_in_a_far_row_is_kept(self):
        data = _with_stored_results(_xlsx([["항목", "값"], ["가", 1], ["나", "=B2"]]), {"B2": "1"})
        data = _with_far_formula(data, "B7000", "SUM(B2:B3)")

        result = convert(data, "a.xlsx")

        assert result.text.endswith("| 나 | 1 |\n| | =SUM(B2:B3) |")


def _with_far_formula(data: bytes, reference: str, formula: str) -> bytes:
    """Add one formula cell without a result, together with its row, at the end of the sheet XML."""
    row = re.match(r"[A-Z]+(\d+)", reference).group(1)
    cell = f'<row r="{row}"><c r="{reference}"><f>{formula}</f></c></row></sheetData>'
    return builder.zip_with(data, edit={"xl/worksheets/sheet1.xml": lambda xml: xml.replace("</sheetData>", cell)})


class TestXmlSpreadsheetDatesAndTimes:
    """Named time formats of Excel 2003 XML (Long Time, Medium Time) came out as dates, and number cells with a date format
    (`ss:Type="Number"`) as serials (45356)."""

    @pytest.mark.parametrize(("number_format", "kind", "value", "expected"), [
        ("Long Time", "DateTime", "2024-03-05T14:30:15.000", "14:30:15"),
        ("Medium Time", "DateTime", "2024-03-05T14:30:00.000", "14:30"),
        ("Short Time", "DateTime", "2024-03-05T14:30:00.000", "14:30"),
        ("hh:mm", "DateTime", "2024-03-05T14:30:00.000", "14:30"),
        ("Short Date", "Number", "45356", "2024-03-05"),
        ("yyyy\\-mm\\-dd", "Number", "45356", "2024-03-05"),
        ("hh:mm", "Number", "0.6041666666666666", "14:30"),
        ("Long Time", "Number", "0.5", "12:00:00"),
        ("General Date", "Number", "45356.5", "2024-03-05 12:00:00"),
        ("[h]:mm", "Number", "1.25", "30:00"),
        ("0.0%", "Number", "0.25", "25.0%"),
    ])
    def test_value_is_shown_as_its_format_shows_it(self, number_format, kind, value, expected):
        data = _xml_spreadsheet(number_format, kind, value)

        result = convert(data, "a.xls")

        assert f"| 가 | {expected} |" in result.text


class TestTimeDisplay:
    """A time-format value came out with fractional seconds (`00:01:43.680000`; Excel shows `01:44`), and an `h:mm` value past
    one day as a date (`1900-01-01 12:00:00`; Excel shows `12:00`). Round to seconds, and a time format without date letters shows
    only the time. A format with hours (h) follows the format (`h:mm` → `12:00`, AM/PM included), seconds only when the format shows
    them (test_spreadsheet_notes_pictures_and_times.py::TestTimeFormats)."""

    @pytest.mark.parametrize(("value", "number_format", "expected"), [
        (time(0, 1, 43, 680000), "mm:ss", "00:01:44"),
        (time(23, 59, 59, 700000), "h:mm:ss", "0:00:00"),
        (datetime(1900, 1, 1, 12), "h:mm", "12:00"),
        (datetime(1900, 1, 1, 12), "[$-412]AM/PM h:mm", "오후 12:00"),
        (datetime(2024, 3, 5, 14, 29, 59, 600000), "yyyy-mm-dd hh:mm:ss", "2024-03-05 14:30:00"),
        (datetime(2024, 3, 5, 14, 30), "mm/dd hh:mm", "2024-03-05 14:30"),
        (datetime(2024, 3, 5, 14, 30), 'm"월" d"일" h"시"', "2024-03-05 14:30"),
    ])
    def test_time_is_rounded_to_seconds_and_time_only_formats_show_no_date(self, value, number_format, expected):
        result = format_cell(value, number_format)

        assert result == expected

    def test_xlsx_cells(self):
        data = _xlsx([["항목", "값"], ["가", 0.0012], ["나", 1.5]], {"B2": "mm:ss", "B3": "h:mm"})

        result = convert(data, "a.xlsx")

        assert "| 가 | 00:01:44 |\n| 나 | 12:00 |" in result.text

    @pytest.mark.parametrize(("value", "number_format", "expected"), [(0.0012, "mm:ss", "00:01:44"), (1.5, "h:mm", "12:00")])
    def test_xls_cells(self, value, number_format, expected):
        cell = xlrd.sheet.Cell(xlrd.XL_CELL_DATE, value)

        result = format_cell(_xls_value(cell, 0, number_format), number_format)

        assert result == expected


class TestNegativeDateSerials:
    """A negative -0.0625 (a difference of -1 hour 30 minutes) with a time format (`h:mm`) came out as `22:30:00`, and -5 with a
    date format (`yyyy-mm-dd`) as the nonexistent day `1899-12-25` (`1899-12-26` in XLS). Excel's 1904 system shows a negative time
    as `-1:30`, and a date cannot be shown in either system, hence `####`. A time-only format shows a signed time, a format that
    shows a date shows the original number."""

    @pytest.mark.parametrize("streamed", [False, True], ids=["full-load", "streamed"])
    @pytest.mark.parametrize("date1904", [False, True], ids=["1900", "1904"])
    def test_xlsx(self, monkeypatch, streamed, date1904):
        from openpyxl.utils.datetime import CALENDAR_MAC_1904

        from opendockit.extractors import spreadsheet

        if streamed:
            monkeypatch.setattr(spreadsheet, "_FULL_LOAD_MAX_SHEET_BYTES", 0)
        workbook = openpyxl.Workbook()
        if date1904:
            workbook.epoch = CALENDAR_MAC_1904
        sheet = workbook.active
        sheet.append(["이름", "차이", "일자차", "일시"])
        sheet.append(["홍길동", -0.0625, -5, -1.25])
        sheet["B2"].number_format = "h:mm"
        sheet["C2"].number_format = "yyyy-mm-dd"
        sheet["D2"].number_format = "yyyy-mm-dd hh:mm"
        buffer = BytesIO()
        workbook.save(buffer)

        result = convert(buffer.getvalue(), "a.xlsx")

        assert "| 홍길동 | -01:30:00 | -5 | -1.25 |" in result.text

    @pytest.mark.parametrize("datemode", [0, 1], ids=["1900", "1904"])
    @pytest.mark.parametrize(("value", "number_format", "expected"), [
        (-0.0625, "h:mm", "-01:30:00"), (-1.5, "h:mm:ss", "-36:00:00"), (-5, "yyyy-mm-dd", "-5"), (-1.25, "yyyy-mm-dd hh:mm", "-1.25"),
        (-0.5, "[h]:mm", "-12:00"),
    ])
    def test_xls(self, datemode, value, number_format, expected):
        cell = xlrd.sheet.Cell(xlrd.XL_CELL_DATE, value)

        result = format_cell(_xls_value(cell, datemode, number_format), number_format)

        assert result == expected

    @pytest.mark.parametrize(("number_format", "value", "expected"), [("hh:mm", "-0.0625", "-01:30:00"),
                                                                       ("yyyy\\-mm\\-dd", "-5", "-5")])
    def test_xml_spreadsheet(self, number_format, value, expected):
        data = _xml_spreadsheet(number_format, "Number", value)

        result = convert(data, "a.xls")

        assert f"| 가 | {expected} |" in result.text

    def test_east_asian_builtin_date_format(self):
        data = _xlsx_with_builtin_format_ids([-5, 45365], [27, 27])

        result = convert(data, "a.xlsx")

        assert "| 번호 27 | -5 |\n| 번호 27 | 2024-03-14 |" in result.text


class TestCsvLimitsAndHints:
    def test_very_wide_row_is_cut_at_the_column_limit_while_reading(self):
        """One line of 2 million commas was built into 2 million cells, so a 45MB file took 5.2s and 1.55GB. Only up to column
        limit + 1 cells are built."""
        import tracemalloc

        data = ("머리," * 2_000_000 + "끝\n가,나\n").encode()

        tracemalloc.start()
        result = convert(data, "a.csv", ConvertOptions(max_chars=None))
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()

        assert peak < 80 * 1024 * 1024
        assert result.text.startswith("| 머리 | 머리 |")
        assert "| 가 | 나 |" in result.text
        assert "Sheet 'CSV': columns beyond column 100 were excluded." in result.warnings

    def test_quoted_wide_row_is_still_read_whole(self):
        """A line with a quote is not cut (a comma inside quotes is not a delimiter)."""
        data = ('이름,메모\n가,"' + "a," * 300 + '끝"\n').encode()

        result = convert(data, "a.csv")

        assert "끝 |" in result.text
        assert not result.warnings

    @pytest.mark.parametrize(("data", "expected"), [
        ("sep=;\n이름;값\n가;1,5\n나;2,5\n", "| 이름 | 값 |\n| --- | --- |\n| 가 | 1,5 |\n| 나 | 2,5 |"),
        ("sep=,\r\n이름,값\r\n가,1\r\n", "| 이름 | 값 |\n| --- | --- |\n| 가 | 1 |"),
        ("SEP=|\n이름|값\n가|1\n", "| 이름 | 값 |\n| --- | --- |\n| 가 | 1 |"),
    ], ids=["semicolon", "comma-crlf", "upper-pipe"])
    def test_leading_separator_hint_line_sets_the_delimiter_and_is_dropped(self, data, expected):
        """The first line `sep=;` that Excel writes and reads became the header row and was not used for delimiter detection
        either."""
        result = convert(data.encode(), "a.csv")

        assert result.text == expected

    def test_first_cell_that_only_starts_like_a_hint_is_data(self):
        result = convert("sep=;,값\n가,1\n".encode(), "a.csv")

        assert result.text == "| sep=; | 값 |\n| --- | --- |\n| 가 | 1 |"


class TestXmlSpreadsheetNamedFormats:
    """Named formats of Excel 2003 XML missing from the table (Scientific, Euro Currency, Yes/No, True/False, On/Off) were caught
    by openpyxl's `is_date_format` (the s, y, d … in the name) and became dates/times: 0.000123 → `00:00:11`,
    1234.5 → `1903-05-18`, 1 → `1900-01-01`."""

    @pytest.mark.parametrize(("number_format", "value", "expected"), [
        ("Scientific", "0.000123", "0.000123"),
        ("Euro Currency", "1234.5", "€1,234.50"),
        ("Yes/No", "1", "Yes"),
        ("Yes/No", "0", "No"),
        ("True/False", "1", "TRUE"),
        ("True/False", "0", "FALSE"),
        ("On/Off", "1", "On"),
        ("On/Off", "0", "Off"),
    ])
    def test_named_formats_are_not_read_as_dates(self, number_format, value, expected):
        data = _xml_spreadsheet(number_format, "Number", value)

        result = convert(data, "a.xls")

        assert f"| 가 | {expected} |" in result.text

    def test_unknown_format_name_is_not_read_as_a_date(self):
        """An English name not in the table (`Fraction`) is treated as General — letters in the name are not read as date
        codes."""
        data = _xml_spreadsheet("Fraction", "Number", "0.5")

        result = convert(data, "a.xls")

        assert "| 가 | 0.5 |" in result.text

    def test_format_made_only_of_date_codes_is_still_a_date(self):
        data = _xml_spreadsheet("mmmm d yyyy", "Number", "45356")

        result = convert(data, "a.xls")

        assert "| 가 | 2024-03-05 |" in result.text


class TestCsvDelimiterOfMostLines:
    """A semicolon/pipe CSV with title or metadata lines in front, or with lines omitting trailing empty cells, has a different
    cell count per line, so the Sniffer's comma stayed and the amount `-1.234,56` split into `-1.234 | 56`. When most lines have
    the same cell count (3 or more, no line with more) and the comma cell count is uneven from that header on, that delimiter is
    used."""

    def test_semicolon_file_with_preamble_lines(self):
        data = ("Umsatzanzeige;Datei erstellt am: 05.03.2024 10:00\n\nKontonummer:;DE12 3456 7890;\n\n"
                "Buchungstag;Wertstellung;Buchungstext;Betrag;Waehrung\n01.03.2024;01.03.2024;Miete;-1.234,56;EUR\n"
                "02.03.2024;02.03.2024;Gehalt;3.500,00;EUR\n04.03.2024;04.03.2024;Strom;-89,90;EUR\n")

        result = convert(data.encode(), "umsatz.csv")

        assert "| Buchungstag | Wertstellung | Buchungstext | Betrag | Waehrung |" in result.text
        assert "| 01.03.2024 | 01.03.2024 | Miete | -1.234,56 | EUR |" in result.text
        assert "| 04.03.2024 | 04.03.2024 | Strom | -89,90 | EUR |" in result.text

    @pytest.mark.parametrize(("data", "expected"), [
        ("고객 목록\n이름|전화|주소\n홍길동|010-1234-5678|서울시 강남구, 101동\n김철수|010-2222-3333|부산시 해운대구, 202동\n",
         "고객 목록\n\n| 이름 | 전화 | 주소 |\n| --- | --- | --- |\n| 홍길동 | 010-1234-5678 | 서울시 강남구, 101동 |\n"
         "| 김철수 | 010-2222-3333 | 부산시 해운대구, 202동 |"),
        ("이름|전화|주소|비고\n홍길동|010-1234-5678|서울시 강남구, 101동|VIP\n김철수|010-2222-3333|부산시 해운대구, 202동\n",
         "| 이름 | 전화 | 주소 | 비고 |\n| --- | --- | --- | --- |\n| 홍길동 | 010-1234-5678 | 서울시 강남구, 101동 | VIP |\n"
         "| 김철수 | 010-2222-3333 | 부산시 해운대구, 202동 | |"),
        ("Region;Q1;Q2;Bemerkung\nNord;1.234,50;2.345,75;ok\nSüd;3.456,00;4.567,25\nOst;1,5;2,5;\n",
         "| Region | Q1 | Q2 | Bemerkung |\n| --- | --- | --- | --- |\n| Nord | 1.234,50 | 2.345,75 | ok |\n"
         "| Süd | 3.456,00 | 4.567,25 | |\n| Ost | 1,5 | 2,5 | |"),
    ], ids=["pipe-title-line", "pipe-short-last-row", "semicolon-short-row"])
    def test_delimiter_of_most_lines_wins(self, data, expected):
        result = convert(data.encode(), "a.csv")

        assert result.text == expected

    @pytest.mark.parametrize(("data", "expected"), [
        ("매출 현황\n이름,금액,비고\n홍길동,1000,메모\n김철수,2000,\n",
         "매출 현황\n\n| 이름 | 금액 | 비고 |\n| --- | --- | --- |\n| 홍길동 | 1000 | 메모 |\n| 김철수 | 2000 | |"),
        ("보고서\n일자,내용\n1,a;b;c\n2,d;e;f\n3,g;h;i\n", "| 1 | a;b;c |\n| 2 | d;e;f |\n| 3 | g;h;i |"),
        ("작성: 2024-03-05; 담당 홍길동\n번호,이름,메모\n1,가,x|y|z\n2,나,p|q|r\n3,다,s|t|u\n", "| 1 | 가 | x\\|y\\|z |"),
    ], ids=["korean-title", "semicolon-lists-in-values", "pipe-lists-in-values"])
    def test_comma_file_with_title_lines_stays_comma_delimited(self, data, expected):
        """When the comma cell count is even from the header on, it is a comma CSV (lists of `;` or `|` inside values do not
        change the delimiter)."""
        result = convert(data.encode(), "a.csv")

        assert expected in result.text


class TestTitleRowWithUnitNote:
    """When the top row was the two cells 'title + (단위: 천원)', it became the header and the real header dropped to a data row."""

    def test_xlsx_title_and_unit_row_is_lifted_above_the_table(self):
        data = _xlsx([["2024년 월별 매출 현황", None, None, None, "(단위: 천원)"], ["월", "국내", "해외", "합계", "전년비"],
                      ["1월", 100, 50, 150, "5%"]])

        result = convert(data, "a.xlsx")

        assert "2024년 월별 매출 현황 (단위: 천원)\n\n| 월 | 국내 | 해외 | 합계 | 전년비 |\n| --- |" in result.text


class TestDateTimeAtMidnight:
    """A midnight value with a format showing both date and time (`yyyy-mm-dd hh:mm`) came out as `2024-01-01` without the time.
    The other cells of the same column had a time, so only midnight looked different. General Date of Excel 2003 XML shows only
    the date at midnight, as Excel does (Deliberate)."""

    @pytest.mark.parametrize(("number_format", "expected"), [
        ("yyyy-mm-dd hh:mm", "2024-01-01 00:00"),
        ("yyyy/mm/dd h:mm:ss", "2024-01-01 00:00:00"),
        ("yyyy-mm-dd", "2024-01-01"),
    ])
    def test_time_is_shown_at_midnight_when_the_format_shows_it(self, number_format, expected):
        result = format_cell(datetime(2024, 1, 1), number_format)

        assert result == expected

    def test_xlsx_and_xml_spreadsheet_cells(self):
        xlsx = _xlsx([["항목", "값"], ["가", datetime(2024, 1, 1)]], {"B2": "yyyy-mm-dd hh:mm"})
        xml = _xml_spreadsheet("yyyy-mm-dd hh:mm", "DateTime", "2024-01-01T00:00:00.000")

        results = [convert(xlsx, "a.xlsx"), convert(xml, "a.xls")]

        assert ["| 가 | 2024-01-01 00:00 |" in result.text for result in results] == [True, True]

    def test_xml_general_date_at_midnight_shows_only_the_date(self):
        data = _xml_spreadsheet("General Date", "Number", "45356")

        result = convert(data, "a.xls")

        assert "| 가 | 2024-03-05 |" in result.text


class TestCurrencyPosition:
    """The currency symbol (`[$₩-412]`) was put at the very front wherever it sat in the format, so the negative section
    `\\-[$₩-412]#,##0` came out as `₩-169,135,789` instead of Excel's `-₩…`. The currency symbol stays where the format puts it."""

    @pytest.mark.parametrize(("value", "number_format", "expected"), [
        (-169135789, "[$₩-412]#,##0;[RED]\\-[$₩-412]#,##0", "-₩169,135,789"),
        (169135789, "[$₩-412]#,##0;[RED]\\-[$₩-412]#,##0", "₩169,135,789"),
        (-1200, "[$₩-412]#,##0", "-₩1,200"),
        (-1200, "[$₩-412]#,##0;[RED]([$₩-412]#,##0)", "(₩1,200)"),
        (1234.5, "#,##0.00 [$€-407]", "1,234.50 €"),
    ])
    def test_currency_symbol_stays_where_the_format_puts_it(self, value, number_format, expected):
        result = format_cell(value, number_format)

        assert result == expected


@contextmanager
def _deadline(seconds: int) -> Iterator[None]:
    """Fail the test with TimeoutError if it does not finish within `seconds` (to reproduce a hang — before the fix xlrd ran
    forever)."""
    def expired(*_: object) -> None:
        raise TimeoutError(f"Did not finish within {seconds} seconds.")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


class TestXlsOleContainer:
    """xlrd's OLE parsing does not check for cycles in the chain of a stream under 4KB (mini stream), so a 5KB file whose mini
    FAT points to itself ran forever and used several GB. The workbook stream is taken out with olefile and only those bytes are
    handed to xlrd."""

    @staticmethod
    def _extract(data: bytes) -> Extraction:

        return xls.extract_xls(data, ConvertOptions())

    @staticmethod
    def _looped_mini_fat(data: bytes) -> bytes:
        """Make the first mini FAT entry (the first mini sector of the workbook stream) point to itself."""
        import olefile

        with olefile.OleFileIO(BytesIO(data)) as ole:
            offset = (ole.minifatsect + 1) * 512
        return data[:offset] + struct.pack("<I", 0) + data[offset + 4:]

    @pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="platform without SIGALRM")
    @pytest.mark.timing
    def test_mini_fat_chain_that_loops_is_rejected_within_a_second(self):
        import time as clock

        data = self._looped_mini_fat(builder.ole_container({"Workbook": builder.xls_bytes([("견적", _XLS_ROWS, b"")])}))

        started = clock.perf_counter()
        with _deadline(5), pytest.raises(CorruptedDocumentError):
            convert(data, "a.xls")

        assert clock.perf_counter() - started < 1

    def test_workbook_in_a_container_reads_the_same_as_the_bare_stream(self):
        stream = builder.xls_bytes([("견적", _XLS_ROWS, builder.xls_text_box("※ 부가세 별도")), ("둘째", [["나", "다"]], b"")])

        bare, contained = self._extract(stream), self._extract(builder.ole_container({"Workbook": stream}))

        assert [section.text for section in contained.sections] == [section.text for section in bare.sections] == [
            f"{_XLS_TABLE}\n\n※ 부가세 별도", "| 나 | 다 |\n| --- | --- |"]
        assert contained.warnings == bare.warnings == []

    def test_book_stream_of_older_workbooks_is_found(self):
        data = builder.ole_container({"Book": builder.xls_bytes([("견적", _XLS_ROWS, b"")])})

        result = convert(data, "a.xls")

        assert result.text == f"## Sheet: 견적\n\n{_XLS_TABLE}"

    def test_encrypted_workbook_is_still_reported_as_encrypted(self):
        stream = builder.xls_bytes([("견적", _XLS_ROWS, b"")])
        encrypted = stream[:20] + builder.xls_record(0x002F, bytes(6)) + stream[20:]  # FILEPASS right after the BOF

        with pytest.raises(EncryptedDocumentError):
            convert(builder.ole_container({"Workbook": encrypted}), "a.xls")

    def test_container_without_a_workbook_stream_is_a_corrupted_document(self):
        data = builder.ole_container({"Nope": builder.xls_bytes([("견적", _XLS_ROWS, b"")]), "Empty": b""})

        with pytest.raises(CorruptedDocumentError):
            self._extract(data)


class TestXmlSpreadsheetIntegerAttributes:
    """`ss:Index`, `ss:Span`, `ss:MergeAcross` and `ss:MergeDown` of Excel 2003 XML were read with a bare `int()`, so a single
    non-integer value (`abc`, `²`) rejected the whole document. They are treated as absent, with one warning for the document."""

    def test_non_integer_attributes_are_ignored_with_a_single_warning(self):
        xml = ('<?xml version="1.0"?><?mso-application progid="Excel.Sheet"?>'
               '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet">'
               '<Worksheet ss:Name="s"><Table><Column ss:Index="abc" ss:Span="x"/>'
               '<Row><Cell ss:Index="²"><Data ss:Type="String">가</Data></Cell><Cell ss:MergeAcross="abc"><Data ss:Type="String">나</Data></Cell></Row>'
               '<Row ss:Index="1.5"><Cell ss:MergeDown="-1"><Data ss:Type="String">다</Data></Cell>'
               '<Cell ss:Index=" 3 "><Data ss:Type="String">라</Data></Cell></Row>'
               '<Row><Cell ss:Index="2" ss:MergeDown="x"><Data ss:Type="String">마</Data></Cell></Row></Table></Worksheet></Workbook>').encode()

        result = convert(xml, "a.xml")

        assert result.text == "## Sheet: s\n\n| 가 | 나 | |\n| --- | --- | --- |\n| 다 | | 라 |\n| | 마 | |"
        assert result.warnings == ["Non-integer cell position and merge attributes (Index, MergeAcross, MergeDown, Span) were ignored. "
                                   "Cells may have been placed in a different column."]


class TestEmptyRangeReferences:
    """The empty group of `<mergeCell ref=""/>` / `<hyperlink ref=""/>` was None, so the merged-cell count check blew up with
    AttributeError and came out as 'Could not parse …'. Empty references are not counted. Empty merges are removed by the copy
    that is read again (`_without_view_settings`), and hyperlinks are not read (`_SHEET_PARTS_LEFT_UNREAD`)."""

    @staticmethod
    def _workbook() -> bytes:
        return builder.zip_with(builder.xlsx_bytes(), edit={"xl/worksheets/sheet1.xml": lambda xml: (
            xml.replace("</mergeCells>", '<mergeCell ref=""/><mergeCell ref=" "/></mergeCells>')
            .replace("<pageMargins", "<hyperlinks><hyperlink ref=\"\"/><hyperlink ref=''/></hyperlinks><pageMargins"))})

    def test_empty_references_are_skipped_in_the_merged_cell_count(self):
        data = self._workbook()

        with zipfile.ZipFile(BytesIO(data)) as archive:
            count = _merged_cell_count(archive, ["xl/worksheets/sheet1.xml"])

        assert count == 4 + 2  # A1:D1, A3:A4

    def test_workbook_with_empty_references_reads_like_the_one_without_them(self):
        data = self._workbook()

        result = convert(data, "a.xlsx")

        assert result.text == convert(builder.xlsx_bytes(), "a.xlsx").text


class TestMissingPartRelationships:
    """openpyxl refuses to load with KeyError('Unknown relationship') when the relationship that a sheet's picture/chart
    (`r:embed`, `r:id`), table (`tablePart`) or hyperlink points to is missing, so the whole document came out as 'corrupted'.
    For pictures and charts it is read once more from a copy with only those removed, with a warning. Table definitions and
    hyperlinks are not used in conversion and so are not read (`_SHEET_PARTS_LEFT_UNREAD`); nothing is lost and no warning is given."""

    _WARNING = "Pictures and charts of a sheet that have no relationship information were left out."
    _TABLE = "| 항목 | 값 |\n| --- | --- |\n| a | 1 |"

    @staticmethod
    def _with_relationship_namespace(xml: str) -> str:
        return xml.replace('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
                           '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                           'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">')

    def test_picture_without_a_relationship_is_dropped_and_the_table_is_kept(self):
        data = builder.zip_with(builder.xlsx_with_image(),
                                edit={"xl/drawings/_rels/drawing1.xml.rels": lambda xml: xml.replace('Id="rId1"', 'Id="rId9"')})

        result = convert(data, "a.xlsx")

        assert result.text == f"## Sheet: Sheet\n\n{self._TABLE}"
        assert result.images == []
        assert result.warnings == [self._WARNING]

    def test_other_pictures_of_the_same_drawing_are_kept(self):
        from openpyxl.drawing.image import Image as SheetImage

        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["항목", "값"])
        sheet.append(["a", 1])
        sheet.add_image(SheetImage(BytesIO(builder.png_bytes(seed=5))), "D2")
        sheet.add_image(SheetImage(BytesIO(builder.png_bytes(seed=6))), "D20")
        buffer = BytesIO()
        workbook.save(buffer)
        data = builder.zip_with(buffer.getvalue(),
                                edit={"xl/drawings/_rels/drawing1.xml.rels": lambda xml: xml.replace('Id="rId2"', 'Id="rId9"')})

        result = convert(data, "a.xlsx")

        assert result.text == f"## Sheet: Sheet\n\n{self._TABLE}\n\n[Image]"
        assert len(result.images) == 1
        assert result.warnings == [self._WARNING]

    @pytest.mark.parametrize("extra", [
        pytest.param(lambda xml: xml.replace("</worksheet>", '<tableParts count="1"><tablePart r:id="rIdNOPE"/></tableParts></worksheet>'),
                     id="table"),
        pytest.param(lambda xml: xml.replace("<pageMargins", '<hyperlinks><hyperlink ref="A1" r:id="rIdNOPE"/></hyperlinks><pageMargins'),
                     id="hyperlink"),
    ])
    def test_table_or_hyperlink_without_a_relationship_changes_nothing(self, extra: Callable[[str], str]):
        data = builder.zip_with(builder.xlsx_bytes(),
                                edit={"xl/worksheets/sheet1.xml": lambda xml: extra(self._with_relationship_namespace(xml))})

        result = convert(data, "a.xlsx")

        assert result.text == convert(builder.xlsx_bytes(), "a.xlsx").text
        assert result.warnings == convert(builder.xlsx_bytes(), "a.xlsx").warnings


class TestRawBiffEncoding:
    """An OLE-less BIFF 'Excel download' writes no CODEPAGE record. xlrd then decoded 8-bit text as latin-1 and '이름' came out
    as 'ÀÌ¸§'. Without CODEPAGE it is decoded as CP949; text that does not decode as CP949 keeps the xlrd default (latin-1)."""

    def test_korean_label_without_codepage_is_read_as_cp949(self):
        data = builder.raw_biff_xls([["이름", "금액"], ["홍길동", 1234.5]])

        result = convert(data, "export.xls")

        assert result.text == "## Sheet: Sheet 1\n\n| 이름 | 금액 |\n| --- | --- |\n| 홍길동 | 1234.5 |"

    def test_latin1_label_that_is_not_cp949_keeps_the_default(self):
        data = builder.raw_biff_xls([["Name", "Ort"], ["Müller", "Köln"]], encoding="latin-1")

        result = convert(data, "export.xls")

        assert "| Müller | Köln |" in result.text

    def test_declared_codepage_is_kept(self):
        """A file that declares CODEPAGE follows that code page (1252 = Western European)."""
        data = builder.raw_biff_xls([["Name", "No"], ["Ñandú", 1]], encoding="cp1252", codepage=1252)

        result = convert(data, "export.xls")

        assert "| Ñandú | 1 |" in result.text


class TestDateCellsOutsideTheDateRange:
    """When the value of a date-format cell is outside Excel's date range (9999-12-31), openpyxl turned it into '#VALUE!' and
    wrote a UserWarning per cell to stderr. Excel shows such a cell as '####' and the value stays the number. As in XLS, the
    original number is shown."""

    def test_out_of_range_serial_is_shown_as_the_number(self, streamed, recwarn):
        data = _xlsx([["구분", "값"], ["a", 99999999], ["b", 2958466], ["c", 1e12]],
                     {"B2": "yyyy-mm-dd", "B3": "yyyy-mm-dd", "B4": "[h]:mm"})

        result = convert(data, "a.xlsx")

        assert "| a | 99999999 |\n| b | 2958466 |\n| c | 1000000000000 |" in result.text
        assert "#VALUE!" not in result.text
        assert not [warning for warning in recwarn if "outside the limits for dates" in str(warning.message)]

    def test_zero_with_a_date_format_is_the_number(self, streamed):
        """Excel shows 0 with a date format as '1900-01-00' (a nonexistent day). '00:00:00' would read as a time and change the
        meaning. 0 with a time-only format is midnight."""
        data = _xlsx([["구분", "값"], ["날짜", 0], ["시각", 0]], {"B2": "yyyy-mm-dd", "B3": "h:mm"})

        result = convert(data, "a.xlsx")

        assert "| 날짜 | 0 |\n| 시각 | 0:00 |" in result.text

    def test_xls_zero_with_a_date_format_is_the_number(self):
        cell = xlrd.sheet.Cell(xlrd.XL_CELL_DATE, 0.0)

        value = format_cell(_xls_value(cell, 0, "yyyy-mm-dd"), "yyyy-mm-dd")

        assert value == "0"

    def test_genuine_error_cell_is_still_an_error(self, streamed):
        """A genuine error cell (t="e") stays the error text."""
        data = builder.zip_with(_xlsx([["구분", "값"], ["a", 1]]),
                                edit={"xl/worksheets/sheet1.xml": lambda xml: re.sub(
                                    r'<c r="B2"([^>]*)><v>1</v></c>', r'<c r="B2" t="e"><v>#VALUE!</v></c>', xml)})

        result = convert(data, "a.xlsx")

        assert "| a | #VALUE! |" in result.text


class TestCsvExcelTextFormula:
    """`="00123"`, used in Excel to keep leading zeros, came out as is. It is unwrapped to the inner text only when the whole
    cell has this shape."""

    def test_whole_cell_text_formula_is_unwrapped(self):
        data = '사번,이름,메모\n="00123",홍길동,=A1+1\n"=""00456""",김철수,="a"&"b"\n="",박영희,="따옴표 ""안"""\n'

        result = convert(data.encode("cp949"), "a.csv")

        assert result.text == ("| 사번 | 이름 | 메모 |\n| --- | --- | --- |\n| 00123 | 홍길동 | =A1+1 |\n"
                               '| 00456 | 김철수 | ="a"&"b" |\n| | 박영희 | 따옴표 "안" |')


_TWO_TABLES = [["1. 매출"], ["사업부", "1월", "2월", "3월"], ["A사업부", 100, 110, 120], [], [],
               ["2. 인원"], ["부서", "정원", "현원"], ["영업", 10, 9]]
_TWO_TABLES_TEXT = ("1. 매출\n\n| 사업부 | 1월 | 2월 | 3월 |\n| --- | --- | --- | --- |\n| A사업부 | 100 | 110 | 120 |\n\n"
                    "2. 인원\n\n| 부서 | 정원 | 현원 |\n| --- | --- | --- |\n| 영업 | 10 | 9 |")


def _xml_spreadsheet_of(rows: list[list[object]]) -> bytes:
    """One sheet of an Excel 2003 XML spreadsheet. An empty row writes no `<Row>` and is skipped via the next row's `ss:Index`
    (the shape Excel saves)."""
    body, skipped = "", False
    for number, row in enumerate(rows, start=1):
        if not row:
            skipped = True
            continue
        cells = "".join(f'<Cell><Data ss:Type="{"Number" if isinstance(value, (int, float)) else "String"}">{value}</Data></Cell>'
                        for value in row)
        body += f'<Row{f" ss:Index=\"{number}\"" if skipped else ""}>{cells}</Row>'
        skipped = False
    return ('<?xml version="1.0" encoding="UTF-8"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
            'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><Worksheet ss:Name="보고"><Table>'
            f"{body}</Table></Worksheet></Workbook>").encode()


class TestSeveralTablesInOneSheet:
    """A sheet with several tables separated by empty rows (a report form) was merged into one table under the first table's
    header, so the title '2. 인원' and the second table's header '부서 | 정원 | 현원' became data rows of the first table. When the
    first table row of the block after an empty row looks like a header (text only, with a column that has numbers below), it
    is split into a new table if a title precedes it or the column count differs, or if it equals the previous table's header."""

    @pytest.mark.parametrize("streamed", [False, True], ids=["full-load", "streamed"])
    def test_xlsx(self, monkeypatch, streamed):
        from opendockit.extractors import spreadsheet

        if streamed:
            monkeypatch.setattr(spreadsheet, "_FULL_LOAD_MAX_SHEET_BYTES", 0)
        data = _xlsx(_TWO_TABLES)

        result = convert(data, "a.xlsx")

        assert result.text == "## Sheet: Sheet\n\n" + _TWO_TABLES_TEXT

    def test_xls(self):
        data = builder.raw_biff_xls(_TWO_TABLES)

        result = convert(data, "a.xls")

        assert result.text.endswith(_TWO_TABLES_TEXT)

    def test_xml_spreadsheet(self):
        data = _xml_spreadsheet_of(_TWO_TABLES)

        result = convert(data, "a.xml")

        assert result.text == "## Sheet: 보고\n\n" + _TWO_TABLES_TEXT

    def test_csv(self):
        data = ('[거래 요약]\n구분,건수,금액\n입금,12,"3,450,000"\n\n[거래 내역]\n일자,적요,금액,잔액\n'
                '2024-01-02,급여,"3,450,000","5,120,300"\n').encode("cp949")

        result = convert(data, "bank.csv")

        assert result.text == ("[거래 요약]\n\n| 구분 | 건수 | 금액 |\n| --- | --- | --- |\n| 입금 | 12 | 3,450,000 |\n\n"
                               "[거래 내역]\n\n| 일자 | 적요 | 금액 | 잔액 |\n| --- | --- | --- | --- |\n"
                               "| 2024-01-02 | 급여 | 3,450,000 | 5,120,300 |")

    def test_repeated_header_starts_a_new_table_without_a_title(self):
        rows = [["지점", "매출"], ["서울", 100], [], ["지점", "매출"], ["부산", 80]]

        result = convert(_xlsx(rows), "a.xlsx")

        assert result.text.endswith("| 지점 | 매출 |\n| --- | --- |\n| 서울 | 100 |\n\n| 지점 | 매출 |\n| --- | --- |\n| 부산 | 80 |")

    def test_title_block_separated_by_a_blank_row_is_a_paragraph_above_the_next_table(self):
        rows = [["지점", "매출", "비용"], ["서울", 100, 50], [], ["2. 인원 현황"], [], ["부서", "정원", "현원", "결원"],
                ["영업", 10, 9, 1]]

        result = convert(_xlsx(rows), "a.xlsx")

        assert result.text.endswith("| 서울 | 100 | 50 |\n\n2. 인원 현황\n\n| 부서 | 정원 | 현원 | 결원 |\n| --- | --- | --- | --- |\n"
                                    "| 영업 | 10 | 9 | 1 |")

    def test_note_block_after_the_last_table_is_a_paragraph(self):
        rows = [["지점", "매출"], ["서울", 100], ["부산", 80], [], ["※ 출처: 내부 집계"]]

        result = convert(_xlsx(rows), "a.xlsx")

        assert result.text.endswith("| 지점 | 매출 |\n| --- | --- |\n| 서울 | 100 |\n| 부산 | 80 |\n\n※ 출처: 내부 집계")

    @pytest.mark.parametrize("rows", [
        [["사업부", "1월", "2월"], ["A", 100, 110], ["B", 200, 210], [], ["합계", 300, 320]],
        [["사업부", "1월", "2월"], ["A", 100, 110], [], ["B", 200, 210], ["C", 300, 310]],
        [["사업부", "1월", "2월"], ["A", 100, 110], [], [None, None, 210]],
        [["이름", "부서", "직급"], ["홍길동", "영업", "대리"], [], ["김철수", "개발", "과장"], ["이영희", "개발", "사원"]],
        [["이름", "부서", "점수"], ["홍길동", "영업", 90], [], ["김철수", "개발", "결시"], ["이영희", "개발", 85]],
        [["품목", "수량", "금액"], [], ["[과일]"], ["사과", 10, 1000], [], ["[채소]"], ["배추", 5, 3000]],
        [["이름"], ["홍길동"], [], ["김철수"], ["이영희"]],
    ], ids=["blank-row-before-total", "blank-row-between-groups", "lone-total-number", "all-text-rows",
            "text-value-in-a-number-column", "group-labels", "one-column"])
    def test_blank_rows_inside_one_table_do_not_split_it(self, rows):
        data = _xlsx(rows)

        result = convert(data, "a.xlsx")

        assert result.text.count("\n| --- ") == (0 if len(rows[0]) == 1 else 1)
        assert result.text.endswith(spreadsheet_render_table(rows))


def spreadsheet_render_table(rows: list[list[object]]) -> str:
    """The shape drawn as one table with the empty rows removed (the former behaviour)."""
    from opendockit.markdown import render_table

    return re.sub(" {2,}", " ", render_table([["" if value is None else str(value) for value in row] for row in rows]))


def _xlsx_with_indents(rows: list[tuple[object, int, object]]) -> bytes:
    """A `과목 | 금액` table. Each row is (label, indent of the label cell, amount)."""
    from openpyxl.styles import Alignment

    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["과목", "금액"])
    for number, (name, indent, amount) in enumerate(rows, start=2):
        sheet.cell(number, 1, name).alignment = Alignment(indent=indent)
        sheet.cell(number, 2, amount)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


class TestIndentedLabels:
    """Sub-items of financial statements are shown by cell indent (`alignment.indent`) or leading spaces; both were dropped, so
    '매출원가' and its sub-item '재료비' read at the same level. The first text cell of a row gets the same mark as list levels
    inside table cells (`└ `, `└─ `, `_lists.cell_level_mark`)."""

    def test_xlsx_cell_indent_becomes_a_level_mark(self, streamed):
        data = _xlsx_with_indents([("매출원가", 0, 600), ("재료비", 1, 400), ("원재료", 2, 300)])

        result = convert(data, "a.xlsx")

        assert "| 매출원가 | 600 |\n| └ 재료비 | 400 |\n| └─ 원재료 | 300 |" in result.text

    def test_leading_spaces_of_the_first_label_become_a_level_mark(self, streamed):
        data = _xlsx([["과목", "금액"], ["판매비", 150], ["  급여", 100], ["　　복리후생비", 50], ["합계", "  1,000"]])

        result = convert(data, "a.xlsx")

        assert "| 판매비 | 150 |\n| └ 급여 | 100 |\n| └─ 복리후생비 | 50 |\n| 합계 | 1,000 |" in result.text

    def test_numbers_and_cells_after_the_first_label_are_not_marked(self):
        data = _xlsx_with_indents([(" 100", 1, 1), ("가", 0, " 나")])

        result = convert(data, "a.xlsx")

        assert "| 100 | 1 |\n| 가 | 나 |" in result.text

    def test_xls_leading_spaces_become_a_level_mark(self):
        from opendockit.extractors.xls import extract_xls

        data = builder.xls_bytes([("손익", [["과목", "금액"], ["매출원가", "600"], ["  재료비", "400"]], b"")])

        result = extract_xls(data, ConvertOptions())

        assert "| 매출원가 | 600 |\n| └ 재료비 | 400 |" in result.sections[0].text

    def test_xml_spreadsheet_indent_becomes_a_level_mark(self):
        data = ('<?xml version="1.0" encoding="UTF-8"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
                'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><Styles><Style ss:ID="i1"><Alignment ss:Indent="1"/>'
                '</Style></Styles><Worksheet ss:Name="손익"><Table>'
                '<Row><Cell><Data ss:Type="String">과목</Data></Cell><Cell><Data ss:Type="String">금액</Data></Cell></Row>'
                '<Row><Cell><Data ss:Type="String">매출원가</Data></Cell><Cell><Data ss:Type="Number">600</Data></Cell></Row>'
                '<Row><Cell ss:StyleID="i1"><Data ss:Type="String">재료비</Data></Cell><Cell><Data ss:Type="Number">400</Data>'
                '</Cell></Row></Table></Worksheet></Workbook>').encode()

        result = convert(data, "a.xls")

        assert "| 매출원가 | 600 |\n| └ 재료비 | 400 |" in result.text


class TestDateFormatsWithoutADay:
    """Dates with a year-month-only format (`yyyy"년" m"월"`, `mmm-yy`) or a year-only format (`yyyy`) got a day that is not shown
    (the 1st) and came out as `2024-01-01`. A format without a day gives `2024-01`, a year-only format `2024`."""

    @pytest.mark.parametrize(("number_format", "expected"), [
        ('yyyy"년" m"월"', "2024-03"), ("mmm-yy", "2024-03"), ("[$-412]yyyy\\-mm", "2024-03"), ("mmmm", "2024-03"),
        ("yyyy", "2024"), ('yyyy"년"', "2024"), ("yyyy-mm-dd", "2024-03-01"), ("mmm d", "2024-03-01"), ("yy/mm (aaa)", "2024-03-01"),
    ])
    def test_shown_parts_decide_the_date_shape(self, number_format, expected):
        result = format_cell(datetime(2024, 3, 1), number_format)

        assert result == expected

    def test_xlsx_month_cell(self):
        data = _xlsx([["월", "매출"], [datetime(2024, 3, 1), 100]], {"A2": 'yyyy"년" m"월"'})

        result = convert(data, "a.xlsx")

        assert "| 2024-03 | 100 |" in result.text

    def test_xls_month_cell(self):
        cell = xlrd.sheet.Cell(xlrd.XL_CELL_DATE, 45352.0)

        value = format_cell(_xls_value(cell, 0, "mmm-yy"), "mmm-yy")

        assert value == "2024-03"

    def test_xml_spreadsheet_month_cell(self):
        data = _xml_spreadsheet('yyyy"년" m"월"', "Number", "45352")

        result = convert(data, "a.xls")

        assert "| 가 | 2024-03 |" in result.text
