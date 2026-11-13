"""Spreadsheets: two side-by-side tables separated by an empty column, summary cells to the right of the header row
('총원 | 26'), the column limit of a sheet whose leading columns are empty."""

from datetime import date
from io import BytesIO

import openpyxl

from opendockit import ConvertOptions, convert
from opendockit.extractors.xls import extract_xls
from tests import builder


def _xlsx(rows: list[list[object]], *, start_column: int = 1) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for row_index, row in enumerate(rows, start=1):
        for offset, value in enumerate(row):
            if value is not None:
                sheet.cell(row_index, start_column + offset, value)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


_SIDE_BY_SIDE = [
    ["지점", "1월", "2월", None, "담당자", "연락처", "입사일"],
    ["서울", 120, 130, None, "김대리", "010-1234-5678", "2020-03-02"],
    ["부산", 80, 95, None, "박과장", "02-123-4567", "2015-07-01"],
    ["대구", 60, 70],
]
_LEFT_TABLE = "| 지점 | 1월 | 2월 |\n| --- | --- | --- |\n| 서울 | 120 | 130 |\n| 부산 | 80 | 95 |\n| 대구 | 60 | 70 |"
_RIGHT_TABLE = ("| 담당자 | 연락처 | 입사일 |\n| --- | --- | --- |\n| 김대리 | 010-1234-5678 | 2020-03-02 |\n"
                "| 박과장 | 02-123-4567 | 2015-07-01 |")


class TestSideBySideTables:
    """Two tables placed side by side with one empty column between them were merged into one table and paired row by row —
    '김대리' read as the person in charge of the 서울 branch. Every row, header included, is split at the empty column, but only
    when both sides have two or more columns, the header is text and the first column holds text names."""

    def test_xlsx_tables_separated_by_an_empty_column_are_drawn_apart(self):
        data = _xlsx(_SIDE_BY_SIDE)

        result = convert(data, "a.xlsx")

        assert result.text == f"## Sheet: Sheet\n\n{_LEFT_TABLE}\n\n{_RIGHT_TABLE}"

    def test_csv_tables_separated_by_an_empty_column_are_drawn_apart(self):
        data = "\n".join(",".join("" if value is None else str(value) for value in row) for row in _SIDE_BY_SIDE).encode()

        result = convert(data, "a.csv")

        assert result.text == f"{_LEFT_TABLE}\n\n{_RIGHT_TABLE}"

    def test_xls_tables_separated_by_an_empty_column_are_drawn_apart(self):
        rows = [["" if value is None else str(value) for value in row] for row in _SIDE_BY_SIDE]
        data = builder.xls_bytes([("Sheet1", rows, b"")])

        extraction = extract_xls(data, ConvertOptions())

        assert extraction.sections[0].text == f"{_LEFT_TABLE}\n\n{_RIGHT_TABLE}"

    def test_empty_spacer_column_before_number_columns_keeps_one_table(self):
        """When the right-hand group is numbers only (no name column) it is a column group of the same table (the spacer column
        between '상반기 | | 하반기')."""
        rows = [["지점", "1월", "2월", None, "7월", "8월"], ["서울", 1, 2, None, 3, 4], ["부산", 5, 6, None, 7, 8]]

        result = convert(_xlsx(rows), "a.xlsx")

        assert "| 지점 | 1월 | 2월 | 7월 | 8월 |" in result.text
        assert "| 서울 | 1 | 2 | 3 | 4 |" in result.text

    def test_one_column_side_keeps_one_table(self):
        rows = [["구분", None, "1월", "2월"], ["매출", None, 10, 20], ["비용", None, 5, 6]]

        result = convert(_xlsx(rows), "a.xlsx")

        assert "| 구분 | 1월 | 2월 |\n| --- | --- | --- |\n| 매출 | 10 | 20 |" in result.text

    def test_column_empty_only_in_data_rows_is_not_a_gap(self):
        """An empty column that has a header ('비고') is a column of the table."""
        rows = [["성명", "부서", "비고", "직급", "연락처"], ["김철수", "인사", None, "대리", "1234"], ["이영희", "재무", None, "과장", "5678"]]

        result = convert(_xlsx(rows), "a.xlsx")

        assert "| 성명 | 부서 | 비고 | 직급 | 연락처 |" in result.text

    def test_tables_split_by_empty_rows_and_columns_both(self):
        rows = [*_SIDE_BY_SIDE, [], ["※ 단위: 백만원"]]

        result = convert(_xlsx(rows), "a.xlsx")

        assert result.text == f"## Sheet: Sheet\n\n{_LEFT_TABLE}\n\n{_RIGHT_TABLE}\n\n※ 단위: 백만원"

    def test_dates_formatted_in_the_right_table_stay_formatted(self):
        rows = [list(row) for row in _SIDE_BY_SIDE]
        rows[1][6], rows[2][6] = date(2020, 3, 2), date(2015, 7, 1)
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        for row in rows:
            sheet.append(row)
        sheet["G2"].number_format = sheet["G3"].number_format = "yyyy-mm-dd"
        buffer = BytesIO()
        workbook.save(buffer)

        result = convert(buffer.getvalue(), "a.xlsx")

        assert _RIGHT_TABLE in result.text


class TestSummaryCellsRightOfTheHeader:
    """Summary cells placed to the right of the header row ('총원 | 26') became column names, giving
    '| 본부 | 팀 | 인원 | 총원 | 26 |'. When right-hand columns that are empty in every data row hold a number, they are taken out
    as a paragraph above the table. An empty column without a number ('비고') stays a column name."""

    def test_summary_beside_the_header_becomes_a_paragraph(self):
        rows = [["본부", "팀", "인원", "총원", 26], ["경영지원", "인사팀", 5], ["영업", "국내팀", 10]]

        result = convert(_xlsx(rows), "a.xlsx")

        assert result.text == ("## Sheet: Sheet\n\n총원 26\n\n"
                               "| 본부 | 팀 | 인원 |\n| --- | --- | --- |\n| 경영지원 | 인사팀 | 5 |\n| 영업 | 국내팀 | 10 |")

    def test_summary_beyond_an_empty_column_becomes_a_paragraph(self):
        rows = [["본부", "팀", "인원", None, "총원", 26], ["경영지원", "인사팀", 5], ["영업", "국내팀", 10]]

        result = convert(_xlsx(rows), "a.xlsx")

        assert result.text.startswith("## Sheet: Sheet\n\n총원 26\n\n| 본부 | 팀 | 인원 |\n")

    def test_header_with_an_empty_named_column_is_kept(self):
        rows = [["본부", "팀", "인원", "비고"], ["경영지원", "인사팀", 5], ["영업", "국내팀", 10]]

        result = convert(_xlsx(rows), "a.xlsx")

        assert "| 본부 | 팀 | 인원 | 비고 |" in result.text

    def test_year_columns_without_data_stay_column_names(self):
        rows = [["구분", "2023", "2024"], ["매출", 10], ["비용", 5]]

        result = convert(_xlsx(rows), "a.xlsx")

        assert "| 구분 | 2023 | 2024 |" in result.text


class TestColumnLimitFromTheFirstUsedColumn:
    """The column limit (100) was counted from column A of the sheet, so the last 4 columns of a 10-column table starting at
    column CQ vanished with a warning. It is counted from the first used column."""

    def test_xlsx_table_starting_far_right_keeps_all_columns(self):
        rows = [[f"항목{index}" for index in range(10)], [index * 10 for index in range(10)]]

        result = convert(_xlsx(rows, start_column=95), "a.xlsx")

        assert "| 항목0 | 항목1 | 항목2 | 항목3 | 항목4 | 항목5 | 항목6 | 항목7 | 항목8 | 항목9 |" in result.text
        assert not result.warnings

    def test_xlsx_limit_still_counts_columns_from_the_first_used_one(self):
        rows = [[f"항목{index}" for index in range(12)], list(range(12))]

        result = convert(_xlsx(rows, start_column=50), "a.xlsx", ConvertOptions(max_sheet_cols=10))

        assert "| 항목0 |" in result.text and "항목9" in result.text and "항목10" not in result.text
        assert "Sheet 'Sheet': columns beyond column 10 were excluded." in result.warnings

    def test_xlsx_merge_far_right_is_still_repeated(self):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        for column, value in zip(range(101, 104), ["본부", "팀", "인원"], strict=True):
            sheet.cell(1, column, value)
        sheet.cell(2, 101, "영업")
        sheet.cell(2, 102, "국내팀")
        sheet.cell(2, 103, 10)
        sheet.cell(3, 102, "해외팀")
        sheet.cell(3, 103, 4)
        sheet.merge_cells(start_row=2, start_column=101, end_row=3, end_column=101)
        buffer = BytesIO()
        workbook.save(buffer)

        result = convert(buffer.getvalue(), "a.xlsx")

        assert "| 영업 | 해외팀 | 4 |" in result.text
        assert not result.warnings

    def test_xls_table_starting_far_right_keeps_all_columns(self):
        rows = [[""] * 95 + [f"항목{index}" for index in range(10)], [""] * 95 + [str(index) for index in range(10)]]
        data = builder.xls_bytes([("Sheet1", rows, b"")])

        extraction = extract_xls(data, ConvertOptions())

        assert "항목9" in extraction.sections[0].text
        assert not extraction.warnings
