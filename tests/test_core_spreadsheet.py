"""Spreadsheets: E- and e+ in scientific formats, scripts in the rich text of Excel 2003 XML, openpyxl warnings, leading spaces of
headers and of names written with spaces between their characters."""

from io import BytesIO
import re
import warnings
import zipfile

import openpyxl
import pytest

from opendockit import ConvertOptions, convert
from opendockit.extractors.spreadsheet import extract_xls, format_cell
from tests import builder


class TestScientificFormatsWithAMinusSign:
    """Only `E+` was recognised as a scientific format, so with `0.00E-00` / `##0.0E-0` the value 0.0000123 became `0.0000` (the
    value turned into 0) and 12345.678 became `12345.6780`. Excel also uses `E-`, `e+` and `e-` as scientific formats. The raw
    value is output, as with `E+`."""

    @pytest.mark.parametrize("number_format", ["0.00E-00", "##0.0E-0", "0.00e+00", "0.0e-0", "#,##0.00E-00"])
    @pytest.mark.parametrize("value", [0.0000123, 12345.678, -0.5])
    def test_minus_and_lowercase_exponent_formats_are_handled_like_e_plus(self, number_format, value):
        expected = format_cell(value, "0.00E+00")

        text = format_cell(value, number_format)

        assert text == expected

    def test_quoted_e_minus_is_only_a_literal(self):
        text = format_cell(1234.5, '#,##0.0 "E-mail"')

        assert text == "1,234.5 E-mail"

    def test_xlsx_cell_with_an_e_minus_format_keeps_its_small_value(self):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["항목", "값"])
        sheet.append(["작은값", 0.0000123])
        sheet["B2"].number_format = "0.00E-00"
        buffer = BytesIO()
        workbook.save(buffer)

        result = convert(buffer.getvalue(), "a.xlsx")

        assert "| 작은값 | 1.23e-05 |" in result.text


class TestXmlSpreadsheetScripts:
    """Rich text of Excel 2003 XML (`<Sup>`, `<Sub>`) was joined as plain characters, so `m²` became `m2` and `10⁶원` became
    `106원`."""

    def test_superscript_and_subscript_in_rich_text_are_marked(self):
        data = ('<?xml version="1.0"?><Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet" '
                'xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet" xmlns:html="http://www.w3.org/TR/REC-html40">'
                '<Worksheet ss:Name="단위"><Table>'
                '<Row><Cell><Data ss:Type="String">항목</Data></Cell><Cell><Data ss:Type="String">값</Data></Cell></Row>'
                '<Row><Cell><ss:Data ss:Type="String" xmlns="http://www.w3.org/TR/REC-html40">면적(m<Sup>2</Sup>)</ss:Data></Cell>'
                '<Cell><Data ss:Type="Number">120</Data></Cell></Row>'
                '<Row><Cell><ss:Data ss:Type="String" xmlns="http://www.w3.org/TR/REC-html40">한도 10<Sup>6</Sup>원, '
                'H<Sub>2</Sub>O, <B>굵게</B><!-- 주석 --> 끝</ss:Data></Cell>'
                '<Cell><Data ss:Type="Number">3</Data></Cell></Row>'
                '</Table></Worksheet></Workbook>').encode()

        result = convert(data, "a.xls")

        assert result.text == ("## Sheet: 단위\n\n| 항목 | 값 |\n| --- | --- |\n| 면적(m^2) | 120 |\n"
                               "| 한도 10^6원, H_2O, 굵게 끝 | 3 |")


def _xlsx_with_unsupported_extensions() -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["a", 1])
    sheet.append(["b", 2])
    buffer = BytesIO()
    workbook.save(buffer)

    source = zipfile.ZipFile(BytesIO(buffer.getvalue()))
    output = BytesIO()
    with zipfile.ZipFile(output, "w") as target:
        for info in source.infolist():
            content = source.read(info.filename)
            if info.filename == "xl/worksheets/sheet1.xml":
                content = content.replace(
                    b"</worksheet>",
                    b'<extLst><ext uri="{CCE6A557-97BC-4b89-ADB6-D9C93CAAB3DF}" '
                    b'xmlns:x14="http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"><x14:dataValidations count="0"/></ext>'
                    b'<ext uri="{78C0D931-6437-407d-A8EE-F0AAD7539E65}" '
                    b'xmlns:x14="http://schemas.microsoft.com/office/spreadsheetml/2009/9/main"><x14:conditionalFormattings/></ext>'
                    b"</extLst></worksheet>")
            target.writestr(info, content)
    return output.getvalue()


class TestOpenpyxlWarnings:
    """openpyxl wrote a UserWarning to stderr saying it cannot read Excel extensions (data validation, conditional formatting
    x14). The notice has nothing to do with the conversion result, so it is suppressed only while an XLSX is read (the global
    warning settings are not changed)."""

    @pytest.mark.parametrize("streaming", [False, True])
    def test_unsupported_extension_warnings_are_not_emitted(self, streaming, monkeypatch):
        data = _xlsx_with_unsupported_extensions()
        if streaming:
            monkeypatch.setattr("opendockit.extractors.spreadsheet._FULL_LOAD_MAX_SHEET_BYTES", 0)

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = convert(data, "a.xlsx")

        assert [str(warning.message) for warning in caught] == []
        assert result.text == "## Sheet: Sheet\n\n| a | 1 |\n| --- | --- |\n| b | 2 |"

    def test_warning_filters_are_restored_after_reading(self):
        data = _xlsx_with_unsupported_extensions()
        before = list(warnings.filters)

        convert(data, "a.xlsx")

        assert warnings.filters == before

    @pytest.mark.parametrize("entry", ["xlsx", "prepare_image", "split_tiff_pages"])
    def test_overlapping_conversions_on_threads_leave_the_filters_as_they_were(self, entry, monkeypatch):
        """`warnings.catch_warnings` is not thread-safe (Python 3.12): it saves the list on entry and restores it on exit, so when
        two conversions overlapped and the one that entered first left first, the later one restored its saved 'list with the
        filter added' and the filter stayed in the process for good (ignoring openpyxl UserWarning remained after 64 XLSX
        conversions on 8 threads). The first enters, then the second enters, and the first is made to leave first."""
        import threading

        from opendockit import image
        from opendockit.extractors import spreadsheet

        first_entered, second_entered, first_left = threading.Event(), threading.Event(), threading.Event()

        def overlapping(*_args, **_kwargs):
            if threading.current_thread().name == "first":
                first_entered.set()
                assert second_entered.wait(5)
            else:
                second_entered.set()
                assert first_left.wait(5)
            raise image.ImageUnreadable("stop")

        if entry == "xlsx":
            monkeypatch.setattr(spreadsheet, "_extract_xlsx", overlapping)
            call = lambda: spreadsheet.extract_xlsx(b"", None)  # noqa: E731
        elif entry == "prepare_image":
            monkeypatch.setattr(image, "_open_within_pixel_limit", overlapping)
            call = lambda: image.prepare_image(b"", min_side=1, max_side=10)  # noqa: E731
        else:
            monkeypatch.setattr(image.Image, "open", overlapping)
            call = lambda: image.split_tiff_pages(b"II*\x00")  # noqa: E731
        before = list(warnings.filters)

        def run(signal_done: bool) -> None:
            try:
                call()
            except image.ImageUnreadable:
                pass
            finally:
                if signal_done:
                    first_left.set()

        first = threading.Thread(target=run, args=(True,), name="first")
        second = threading.Thread(target=run, args=(False,), name="second")
        first.start()
        assert first_entered.wait(5)
        second.start()
        first.join(10)
        second.join(10)

        assert warnings.filters == before


def _two_tables_with_unit_formats() -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "현황"
    for row in (["1. 매출 현황"], ["지점", "1월", "2월", "3월"], ["서울", 1200, 1300, 1250], ["부산", 900, 950, 980], [],
                ["2. 인원 현황"], ["부서", "정원", "현원", "결원"], ["영업부", 12, 10, 2], ["관리부", 8, 8, 0]):
        sheet.append(row)
    for row, number_format in ((3, '#,##0"원"'), (4, '#,##0"원"'), (8, '0"명"'), (9, '0" 명"')):
        for column in range(2, 5):
            sheet.cell(row, column).number_format = number_format
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


_UNIT_TABLES_TEXT = ("1. 매출 현황\n\n| 지점 | 1월 | 2월 | 3월 |\n| --- | --- | --- | --- |\n| 서울 | 1,200원 | 1,300원 | 1,250원 |\n"
                     "| 부산 | 900원 | 950원 | 980원 |\n\n2. 인원 현황\n\n| 부서 | 정원 | 현원 | 결원 |\n| --- | --- | --- | --- |\n"
                     "| 영업부 | 12명 | 10명 | 2명 |\n| 관리부 | 8 명 | 8 명 | 0 명 |")


class TestTablesWithUnitFormats:
    """Values with a number format that attaches a unit (`#,##0"원"`, `0"명"`) did not look like numbers (`_NUMBER_LIKE`), so the
    second table's header was not judged a header and '2. 인원 현황' and its table were merged into the first table as data rows.
    A short quantity unit after a number counts as a number."""

    @pytest.mark.parametrize("streamed", [False, True], ids=["full-load", "streamed"])
    def test_second_table_after_unit_formatted_values_is_its_own_table(self, monkeypatch, streamed):
        if streamed:
            monkeypatch.setattr("opendockit.extractors.spreadsheet._FULL_LOAD_MAX_SHEET_BYTES", 0)
        data = _two_tables_with_unit_formats()

        result = convert(data, "a.xlsx")

        assert result.text == "## Sheet: 현황\n\n" + _UNIT_TABLES_TEXT

    def test_csv_with_unit_suffixed_values(self):
        data = ("1. 매출 현황\n지점,1월,2월\n서울,\"1,200원\",\"1,300원\"\n부산,900원,950원\n\n2. 인원 현황\n부서,정원,현원\n"
                "영업부,12명,10명\n").encode()

        result = convert(data, "a.csv")

        assert result.text == ("1. 매출 현황\n\n| 지점 | 1월 | 2월 |\n| --- | --- | --- |\n| 서울 | 1,200원 | 1,300원 |\n"
                               "| 부산 | 900원 | 950원 |\n\n2. 인원 현황\n\n| 부서 | 정원 | 현원 |\n| --- | --- | --- |\n"
                               "| 영업부 | 12명 | 10명 |")

    @pytest.mark.parametrize("header", [["구분", "1월", "2월"], ["구분", "2023년", "2024년"], ["구분", "1차", "2차"]])
    def test_period_and_ordinal_headers_are_still_headers(self, header):
        """Deliberate: words common in column names such as 월, 년, 차 are not taken as units — the header would look like numbers
        and the new table would be missed."""
        data = ("1. 매출\n지점,값\n서울,10\n\n2. 실적\n" + ",".join(header) + "\nA,5,6\n").encode()

        result = convert(data, "a.csv")

        assert f"2. 실적\n\n| {' | '.join(header)} |" in result.text


class TestCsvBackslashEscapes:
    """MySQL `SELECT … INTO OUTFILE` (ESCAPED BY '\\\\' by default) writes a double quote inside a cell as `\\"`. Reading only by
    the doubled-quote rule (`""`) broke `"27\\" 모니터"` into `27\\ 모니터"` and `"\\"한정\\" 판매"` into `\\한정\\" 판매"`."""

    def test_backslash_escaped_quotes_are_read_as_quotes(self):
        data = ('id,title,price,memo\n1,"27\\" 모니터",350000,"재고 있음"\n2,"키보드, 무선",45000,"\\"한정\\" 판매"\n'
                '3,"C:\\\\temp\\\\",12000,"\\N"\n').encode()

        result = convert(data, "dump.csv")

        assert result.text == ('| id | title | price | memo |\n| --- | --- | --- | --- |\n| 1 | 27" 모니터 | 350000 | 재고 있음 |\n'
                               '| 2 | 키보드, 무선 | 45000 | "한정" 판매 |\n| 3 | C:\\temp\\ | 12000 | \\N |')

    @pytest.mark.parametrize(("text", "expected"), [
        ('name,path\nreport,"C:\\temp\\a.txt"\nimg,"D:\\data\\"\n',
         "| name | path |\n| --- | --- |\n| report | C:\\temp\\a.txt |\n| img | D:\\data\\ |"),
        ('q,a\n"정규식 \\"\\d+",yes\n"끝 \\",no\n', '| q | a |\n| --- | --- |\n| 정규식 \\\\d+" | yes |\n| 끝 \\ | no |'),
        ('col1,col2\n"He said ""hi""",x\n"a\\b",y\n', '| col1 | col2 |\n| --- | --- |\n| He said "hi" | x |\n| a\\b | y |'),
        ('q,a\n"Is it \\"ok\\"?",yes\n"path c:\\",no\n', '| q | a |\n| --- | --- |\n| Is it \\ok\\"?" | yes |\n| path c:\\ | no |'),
    ], ids=["windows-paths", "other-backslashes", "doubled-quotes", "path-that-ends-in-a-backslash"])
    def test_backslashes_of_ordinary_csv_are_kept(self, text, expected):
        """Deliberate: when there are other backslashes (path `\\t`, regex `\\d`) or `\\"` is only ever followed by the end of a
        cell, it is read by the doubled-quote rule as before — reading backslashes as escapes would lose characters of `C:\\temp`."""
        result = convert(text.encode(), "a.csv")

        assert result.text == expected


def _xlsx_rows(rows: list[list[object]], *, indents: dict[str, int] | None = None) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    for coordinate, indent in (indents or {}).items():
        sheet[coordinate].alignment = openpyxl.styles.Alignment(indent=indent)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


class TestLeadingSpacesThatAreNotIndentation:
    """Leading spaces of a row's first text cell are read as an indent level (`└ `), but headers spaced out instead of centred
    ('  구   분') and names written with spaces between their characters ('   합   계', '구 분') became sub-items too. No level mark
    is put on the first row of a table (the header), and the leading spaces of a name with spaces between its characters are not
    read as indentation."""

    def test_spaced_header_and_total_are_not_marked_but_sub_items_are(self):
        data = _xlsx_rows([["  구   분", "예산"], ["인건비", 1000], ["  급여", 700], ["   합   계", 1500]])

        result = convert(data, "a.xlsx")

        assert "| 구 분 | 예산 |\n| --- | --- |\n| 인건비 | 1000 |\n| └ 급여 | 700 |\n| 합 계 | 1500 |" in result.text

    @pytest.mark.parametrize("label", ["  합 계", "  소  계", "\u3000총\u3000계"])
    def test_label_with_spaces_between_its_characters_is_not_a_sub_item(self, label):
        data = _xlsx_rows([["과목", "금액"], ["급여", 700], [label, 700]])

        result = convert(data, "a.xlsx")

        assert "└" not in result.text

    def test_header_row_with_a_cell_indent_is_not_marked(self):
        data = _xlsx_rows([["과목", "금액"], ["매출원가", 600], ["재료비", 400]], indents={"A1": 1, "A3": 1})

        result = convert(data, "a.xlsx")

        assert "| 과목 | 금액 |\n| --- | --- |\n| 매출원가 | 600 |\n| └ 재료비 | 400 |" in result.text

    def test_xls_spaced_header_and_total_are_not_marked(self):
        data = builder.xls_bytes([("예산", [["  구   분", "예산"], ["인건비", "1000"], ["  급여", "700"],
                                          ["   합   계", "1500"]], b"")])

        result = extract_xls(data, ConvertOptions())

        assert "| 구 분 | 예산 |\n| --- | --- |\n| 인건비 | 1000 |\n| └ 급여 | 700 |\n| 합 계 | 1500 |" in result.sections[0].text


def _xlsx_with_merges(rows: list[list[object]], merges: list[str]) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    for merged in merges:
        sheet.merge_cells(merged)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


_LEAVE_ROWS = [["성명", "1월", "2월", "3월", "4월"], ["홍길동", 20, "육아휴직", None, None], ["김철수", 21, 19, 22, 20],
               ["이영희", "파견", None, None, 18]]
_LEAVE_TABLE = ("| 성명 | 1월 | 2월 | 3월 | 4월 |\n| --- | --- | --- | --- | --- |\n| 홍길동 | 20 | 육아휴직 | 육아휴직 | 육아휴직 |\n"
                "| 김철수 | 21 | 19 | 22 | 20 |\n| 이영희 | 파견 | 파견 | 파견 | 18 |")


class TestHorizontalMergesInDataRows:
    """A horizontal merge value in a data row ('육아휴직' spanning March and April) stayed only in the first cell, so the other
    months read as blank. As docs/formats.md says ('merge information is spread into the cells … always Markdown'), it is repeated
    in every cell of the merge range (like the repetition of vertical merges). A merge starting in the leftmost column (table
    title and group name rows) and a merge with no text to its left stay in the first cell only, as before — the title row
    detection works unchanged."""

    def test_xlsx_data_row_merge_is_repeated_across(self):
        data = _xlsx_with_merges(_LEAVE_ROWS, ["C2:E2", "B4:D4"])

        result = convert(data, "a.xlsx")

        assert _LEAVE_TABLE in result.text

    def test_xls_data_row_merge_is_repeated_across(self):
        from tests.test_spreadsheet import _xls_merges

        rows = [[str(value) if value is not None else "" for value in row] for row in _LEAVE_ROWS]
        data = builder.xls_bytes([("근무", rows, _xls_merges([(1, 1, 2, 4), (3, 3, 1, 3)]))])

        result = extract_xls(data, ConvertOptions())

        assert result.sections[0].text == _LEAVE_TABLE

    def test_xml_spreadsheet_data_row_merge_is_repeated_across(self):
        def cell(value: str, extra: str = "") -> str:
            return f'<Cell{extra}><Data ss:Type="String">{value}</Data></Cell>'

        rows = [
            "".join(cell(value) for value in ["성명", "1월", "2월", "3월", "4월"]),
            cell("홍길동") + cell("20") + cell("육아휴직", ' ss:MergeAcross="2"'),
            "".join(cell(value) for value in ["김철수", "21", "19", "22", "20"]),
            cell("이영희") + cell("파견", ' ss:MergeAcross="2"') + cell("18"),
        ]
        data = ('<?xml version="1.0"?><?mso-application progid="Excel.Sheet"?><Workbook xmlns="urn:schemas-microsoft-com:office:'
                'spreadsheet" xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"><Worksheet ss:Name="근무"><Table>'
                + "".join(f"<Row>{row}</Row>" for row in rows) + "</Table></Worksheet></Workbook>").encode()

        result = convert(data, "a.xml")

        assert _LEAVE_TABLE in result.text

    def test_block_merge_fills_every_covered_cell(self):
        data = _xlsx_with_merges([["성명", "1월", "2월", "3월"], ["홍길동", "교육", None, 5], ["김철수", None, None, 6]],
                                 ["B2:C3"])

        result = convert(data, "a.xlsx")

        assert ("| 성명 | 1월 | 2월 | 3월 |\n| --- | --- | --- | --- |\n| 홍길동 | 교육 | 교육 | 5 |\n"
                "| 김철수 | 교육 | 교육 | 6 |") in result.text

    def test_merge_over_columns_without_a_name_above_is_left_in_its_first_cell(self):
        """A merge that extends over unnamed empty columns, like '합계금액 | 일금 … 원정' at the top of a quotation, and a merge in the
        header row stay in the first cell only, as before — the table splitting is also unchanged."""
        rows = [["견적서"], [], ["수신", "(주)대한물산 귀중", None, "견적일", "2024-03-05", None],
                ["합계금액", "일금 일천만원정", None, None, None, None], [],
                ["No", "품명", "규격", "1분기", None, "금액"], [1, "서버", "2U", "임대", None, 1000]]
        data = _xlsx_with_merges(rows, ["B3:C3", "E3:F3", "B4:F4", "D6:E6", "D7:E7"])

        result = convert(data, "a.xlsx")

        assert ("| 수신 | (주)대한물산 귀중 | 견적일 | 2024-03-05 |\n| --- | --- | --- | --- |\n"
                "| 합계금액 | 일금 일천만원정 | | |\n\n| No | 품명 | 규격 | 1분기 | 금액 |\n| --- | --- | --- | --- | --- |\n"
                "| 1 | 서버 | 2U | 임대 | 1000 |") in result.text

    def test_title_and_unit_rows_merged_from_the_first_column_stay_titles(self):
        rows = [["2024년 근무 현황", None, None, None, "(단위: 일)"], ["부서별", None, None, None, None], *_LEAVE_ROWS]
        data = _xlsx_with_merges(rows, ["A1:D1", "A2:E2", "C4:E4", "B6:D6"])

        result = convert(data, "a.xlsx")

        assert result.text.startswith("## Sheet: Sheet\n\n2024년 근무 현황 (단위: 일)\n\n부서별\n\n| 성명 | 1월 |")
        assert "| 홍길동 | 20 | 육아휴직 | 육아휴직 | 육아휴직 |" in result.text


def _xlsx_with_undefined_format(edit_styles) -> bytes:
    """A copy of a workbook that has cells using `#,##0"원"` (numFmtId 164), with its styles part edited by `edit_styles`."""
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "매출"
    for row in [["월", "매출"], ["1월", 1200000], ["2월", 1500000]]:
        sheet.append(row)
    sheet["B2"].number_format = sheet["B3"].number_format = '#,##0"원"'
    buffer = BytesIO()
    workbook.save(buffer)
    return builder.zip_with(buffer.getvalue(), edit={"xl/styles.xml": edit_styles})


class TestUndefinedNumberFormatId:
    """When a cell format points to an undefined custom format id (164 missing from numFmts, 99999 outside the built-in range),
    openpyxl raised IndexError while reading the cell's format and the whole document was rejected as 'corrupted'. It is read
    as General, as Excel does."""

    @pytest.mark.parametrize("streamed", [False, True], ids=["full-load", "streamed"])
    @pytest.mark.parametrize("damage", [
        lambda xml: re.sub(r"<numFmts.*?</numFmts>", "", xml),
        lambda xml: xml.replace('<xf numFmtId="164"', '<xf numFmtId="99999"'),
    ], ids=["missing-definition", "out-of-range-id"])
    def test_cell_with_an_undefined_format_is_read_as_general(self, monkeypatch, streamed, damage):
        if streamed:
            monkeypatch.setattr("opendockit.extractors.spreadsheet._FULL_LOAD_MAX_SHEET_BYTES", 0)
        data = _xlsx_with_undefined_format(damage)

        result = convert(data, "a.xlsx")

        assert result.text == "## Sheet: 매출\n\n| 월 | 매출 |\n| --- | --- |\n| 1월 | 1200000 |\n| 2월 | 1500000 |"


class TestSheetChartUserShapes:
    """Text boxes inside a chart placed on a sheet (c:userShapes) were not read, so '(단위: 억원)' was dropped. As with DOCX, PPTX
    and HWPX charts it goes under the title."""

    def test_chart_text_box_is_kept_under_the_title(self):
        from tests.test_office_chart_units_and_ole import _chart_relationships, _user_shapes
        from tests.test_spreadsheet import _xlsx_with_chart

        data = builder.zip_with(_xlsx_with_chart(cached=True), edit={
            "xl/charts/chart1.xml": lambda xml: xml.replace(
                "</chartSpace>", '<userShapes xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
                                 'r:id="rIdU1"/></chartSpace>'),
        }, add={"xl/charts/_rels/chart1.xml.rels": _chart_relationships("../drawings/drawing9.xml"),
                "xl/drawings/drawing9.xml": _user_shapes(["(단위: 억원)"])})

        result = convert(data, "a.xlsx")

        assert "[Chart] 월별 매출\n(단위: 억원)\n\n| Category | 매출 |" in result.text


def _xlsx_with_damaged_part(part: str, damage) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "매출"
    for row in [["월", "매출"], ["1월", 1200], ["2월", 1500]]:
        sheet.append(row)
    sheet.merge_cells("A5:B5")
    sheet["A5"] = "합계 2700"
    buffer = BytesIO()
    workbook.save(buffer)
    return builder.zip_with(buffer.getvalue(), edit={part: damage})


_SALES_TEXT = "## Sheet: 매출\n\n| 월 | 매출 |\n| --- | --- |\n| 1월 | 1200 |\n| 2월 | 1500 |\n\n합계 2700"


class TestMalformedViewAndPrintSettings:
    """When one attribute of the view/print settings, which have nothing to do with values and formats, broke the format
    (`pageMargins top="abc"`, `tabRatio="x"`), openpyxl rejected the whole document and it became 'corrupted'. It is read once
    more from a copy without those settings (bookViews, calcPr, sheetViews, sheetFormatPr, pageMargins, pageSetup, printOptions).
    The settings are not used in the result, so no warning is given."""

    @pytest.mark.parametrize(("part", "damage"), [
        ("xl/worksheets/sheet1.xml", lambda xml: xml.replace('top="1"', 'top="abc"')),
        ("xl/worksheets/sheet1.xml", lambda xml: xml.replace('defaultRowHeight="15"', 'defaultRowHeight=""')),
        ("xl/worksheets/sheet1.xml", lambda xml: xml.replace('workbookViewId="0"', 'workbookViewId="x"')),
        ("xl/workbook.xml", lambda xml: xml.replace('tabRatio="600"', 'tabRatio="x"')),
        ("xl/workbook.xml", lambda xml: xml.replace('calcId="124519"', 'calcId="1.5"')),
    ], ids=["page-margins", "sheet-format", "sheet-view", "book-view", "calculation"])
    def test_workbook_is_read_without_the_damaged_setting(self, part, damage):
        data = _xlsx_with_damaged_part(part, damage)

        result = convert(data, "a.xlsx")

        assert (result.text, result.warnings) == (_SALES_TEXT, [])

    @pytest.mark.timing
    def test_unclosed_settings_take_linear_time(self):
        """Removing the settings does not take quadratic time on a sheet (2.4MB) with many unclosed `<sheetViews>`."""
        import time

        from opendockit.extractors.spreadsheet import _without_view_settings

        data = _xlsx_with_damaged_part("xl/worksheets/sheet1.xml",
                                       lambda xml: xml.replace("<sheetData>", "<sheetViews>" * 200_000 + "<sheetData>"))

        started = time.perf_counter()
        cleaned = _without_view_settings(data)

        assert cleaned is not None
        assert time.perf_counter() - started < 3

    def test_empty_cell_style_inside_the_cells_reads_as_the_default_style(self):
        """A broken cell attribute (`s=""`) could not be fixed by the settings copy and was rejected. Now it is read as the default
        format where the cell is parsed (`spreadsheet._tolerant_parse_cell`,
        test_spreadsheet_cell_values_and_controls.py::TestOneMalformedCellValue)."""
        data = _xlsx_with_damaged_part("xl/worksheets/sheet1.xml", lambda xml: xml.replace('<c r="B2"', '<c r="B2" s=""'))

        result = convert(data, "a.xlsx")

        assert result.text == "## Sheet: 매출\n\n| 월 | 매출 |\n| --- | --- |\n| 1월 | 1200 |\n| 2월 | 1500 |\n\n합계 2700"


class TestMergeReferenceWithoutRows:
    """When a merge range was a column name without rows (`ref="abc"` — the whole column ABC), counting the merged cells raised
    TypeError and the whole document became 'corrupted'. A merge that is not a cell range is not counted."""

    def test_merge_reference_without_rows_is_skipped(self):
        data = _xlsx_with_damaged_part("xl/worksheets/sheet1.xml", lambda xml: xml.replace('ref="A5:B5"', 'ref="abc"'))

        result = convert(data, "a.xlsx")

        assert result.text == _SALES_TEXT
