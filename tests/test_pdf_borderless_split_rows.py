"""Borderless PDF tables with rows whose cells pdfminer merged differently: rows between two found tables, and the rule that
no cell text of a document comes out in two tables."""

import random
import re

import pytest

from opendockit import ConvertOptions, convert
from tests.test_pdf import _FONT, _korean_pdf


_REGIONS = ["서울", "부산", "대구", "인천", "광주", "대전", "울산", "세종", "제주", "수원", "청주", "전주", "포항"]


def _regions_pdf(before: int, after: int, merged: tuple[int, ...] = (), *, rows: list[tuple[str, ...]] | None = None,
                 space: dict[int, float] | None = None, odd: bool = True) -> bytes:
    """Borderless four-column table as printed by Chrome (narrow cells, no ruling lines). pdfminer merges the first two cells
    of every row ('서울 1,000') except row `before` and the rows in `merged`, where it merges the last two ('4.3% 증가'), so
    the rows above and below that row are found as two separate tables first. `after` rows follow it. `rows` replaces the
    rows after row `before`, `space` adds space (pt) above the rows it names, and with `odd` False row `before` is merged
    like the others."""
    rows = [*[(name, f"{1000 + number * 111:,}", f"{number + 1}.{number}%", "증가")
              for number, name in enumerate(_REGIONS[:before + 1 + (0 if rows is not None else after)])], *(rows or [])]
    space = space or {}
    joined, apart = 14.0, 24.0  # pdfminer joins a gap within twice the character width (10pt) into one piece

    def draw(pdf, index):
        pdf.setFont(_FONT, 10)
        pdf.drawString(34, 760, "지역별 실적은 다음과 같다.")
        for row, cells in enumerate(rows):
            gaps = (apart, apart, joined) if (row == before and odd) or row in merged else (joined, apart, apart)
            x, y = 44.0, 736 - row * 20 - sum(extra for at, extra in space.items() if at <= row)
            for column, text in enumerate(cells):
                pdf.drawString(x, y, text)
                if column < len(gaps):
                    x += pdf.stringWidth(text, _FONT, 10) + gaps[column]
        pdf.drawString(34, 736 - len(rows) * 20 - 10 - sum(space.values()), "이상.")

    return _korean_pdf(draw)


def _tables(text: str) -> list[list[list[str]]]:
    """The Markdown tables of `text` as lists of rows of cell texts (the separator row left out)."""
    tables: list[list[list[str]]] = []
    previous = False
    for line in text.split("\n"):
        is_row = line.startswith("| ")
        if is_row and not line.startswith("| --- "):
            if not previous:
                tables.append([])
            tables[-1].append([cell.strip() for cell in line.strip().strip("|").split(" | ")])
        previous = is_row
    return tables


def _cells_in_two_tables(text: str) -> set[str]:
    """Cell texts that occur in more than one table of `text` (repeating a value inside one table is a merged cell)."""
    seen: dict[str, int] = {}
    shared = set()
    for number, table in enumerate(_tables(text)):
        for cell in {cell for row in table for cell in row if cell}:
            if seen.setdefault(cell, number) != number:
                shared.add(cell)
    return shared


class TestRowMergedDifferentlyBetweenTwoTables:
    """A row merged differently between two borderless tables found apart was taken by the table above (growing downwards)
    and by the table below (growing upwards), so it came out in both tables and the one table stayed split in two."""

    @pytest.mark.parametrize(("before", "after"), [(3, 3), (4, 5), (6, 4)])
    def test_the_row_comes_out_once_in_one_table(self, before, after):
        text = convert(_regions_pdf(before, after), "a.pdf", ConvertOptions(page_markers=False)).text

        assert len(_tables(text)) == 1
        assert all(text.count(name) == 1 for name in _REGIONS[:before + 1 + after])
        middle = _REGIONS[before]
        assert f"| {middle} | {1000 + before * 111:,} | {before + 1}.{before}% | 증가 |" in text
        assert text.index("증가 |") > text.index("다음과 같다.") and text.rindex("증가 |") < text.index("이상.")

    def test_rows_above_a_second_merged_row_are_not_taken_twice(self):
        text = convert(_regions_pdf(3, 5, merged=(5,)), "a.pdf", ConvertOptions(page_markers=False)).text

        assert all(text.count(name) == 1 for name in _REGIONS[:9])
        assert not {cell for cell in _cells_in_two_tables(text) if cell in _REGIONS}

    def test_row_merged_differently_in_the_middle_keeps_the_rows_below(self):
        """The round-44 shape (only two rows below the merged row) still gives one table."""
        text = convert(_regions_pdf(4, 2), "a.pdf", ConvertOptions(page_markers=False)).text

        assert len(_tables(text)) == 1
        assert all(text.count(name) == 1 for name in _REGIONS[:7])


_PRODUCTS = [("품목", "단가", "수량", "금액"), ("사과", "1,200", "30", "36,000"), ("배", "2,500", "12", "30,000"),
             ("포도", "3,100", "8", "24,800"), ("귤", "900", "50", "45,000")]
_PRODUCT_TABLE = ("| 품목 | 단가 | 수량 | 금액 |\n| --- | --- | --- | --- |\n| 사과 | 1,200 | 30 | 36,000 |\n"
                  "| 배 | 2,500 | 12 | 30,000 |\n| 포도 | 3,100 | 8 | 24,800 |\n| 귤 | 900 | 50 | 45,000 |")


class TestSeparateTableBelowARowInNoTable:
    """Joining a table with the table its rows below lead into is meant for one table cut at a row merged differently. It
    joined a separate table with its own header ('품목 | 단가 | …') into the table above, and the rows that table would have
    grown below itself ('귤') fell out as paragraphs."""

    def test_totals_row_then_a_table_with_its_own_header(self):
        data = _regions_pdf(2, 0, rows=[("합계", "3,333"), *_PRODUCTS], odd=False)
        text = convert(data, "a.pdf", ConvertOptions(page_markers=False)).text

        assert "| 대구 | 1,222 | 3.2% | 증가 |\n| 합계 | 3,333 | | |\n\n" + _PRODUCT_TABLE in text

    @pytest.mark.parametrize("space", [0.0, 12.0])
    def test_row_merged_differently_then_a_table_with_its_own_header(self, space):
        text = convert(_regions_pdf(3, 0, rows=_PRODUCTS, space={4: space}), "a.pdf", ConvertOptions(page_markers=False)).text

        assert "| 인천 | 1,333 | 4.3% | 증가 |\n\n" + _PRODUCT_TABLE in text

    @pytest.mark.parametrize("space", [4.0, 12.0])
    def test_rows_set_apart_from_the_row_pitch_are_not_joined(self, space):
        """Space above the rows after the merged row: the two parts are two tables (each row once), not one."""
        text = convert(_regions_pdf(3, 4, space={4: space}), "a.pdf", ConvertOptions(page_markers=False)).text

        assert len(_tables(text)) == 2
        assert all(text.count(name) == 1 for name in _REGIONS[:8])
        assert "| 인천 | 1,333 | 4.3% | 증가 |\n\n| 광주 | 1,444 | 5.4% | 증가 |" in text

    @pytest.mark.parametrize("periods", [("2023", "2024"), ("2024.01", "2024.02"), ("FY2023", "FY2024"), ("1분기", "2분기"),
                                         ("10%", "20%"), ("1", "2")])
    def test_table_under_a_header_of_years_or_dates_is_not_joined(self, periods):
        """Years, dates, bare percentages and bare integers in a header are not measured values: '구분 | 2023 | 2024 |
        증감' and '구분 | 10% | 20% | 증감' start a table of their own. Only a number with a thousands separator or a
        decimal point is taken as a data row."""
        rows = [("구분", *periods, "증감"), ("매출", "1,200", "1,350", "+150"), ("비용", "800", "820", "+20"),
                ("이익", "400", "530", "+130"), ("세금", "90", "110", "+20")]

        text = convert(_regions_pdf(3, 0, rows=rows), "a.pdf", ConvertOptions(page_markers=False)).text

        regions = _tables(text)[0]
        assert regions[-1] == ["인천", "1,333", "4.3%", "증가"]
        assert not any(cell.startswith(("구분", "매출")) for row in regions for cell in row)
        if periods in (("2023", "2024"), ("10%", "20%")):  # (the other headers merge into one piece and are
            # not taken as a header row at all)
            assert (f"| 인천 | 1,333 | 4.3% | 증가 |\n\n| 구분 | {periods[0]} | {periods[1]} | 증감 |\n| --- | --- | --- | --- |\n"
                    "| 매출 | 1,200 | 1,350 | +150 |") in text
            assert "| 세금 | 90 | 110 | +20 |\n\n이상." in text

    @pytest.mark.parametrize("space", [{4: 12.0, 6: 12.0}, {1: 14.0, 4: 14.0}])
    def test_one_wide_gap_inside_a_table_does_not_let_a_wide_gap_between_tables_through(self, space):
        """The bound is the usual row gap of the two tables, not the widest: a ranking list set 12pt apart, with one more
        12pt gap inside it (or a 14pt gap inside the table above), stays a table of its own."""
        rows = [("1", "강남구", "5,483", "12.1%"), ("2", "송파구", "4,920", "10.9%"), ("3", "서초구", "4,410", "9.7%"),
                ("4", "마포구", "3,200", "7.0%")]

        text = convert(_regions_pdf(3, 0, rows=rows, space=space), "a.pdf", ConvertOptions(page_markers=False)).text

        assert "| 인천 | 1,333 | 4.3% | 증가 |\n\n| 1 | 강남구 | 5,483 | 12.1% |" in text
        assert text.count("인천") == 1

    @pytest.mark.parametrize("after", [5, 6])
    def test_joined_table_grows_below_the_end_of_the_other_table(self, after):
        """The last row of the lower table is merged differently too, so that table took it only while growing below itself;
        the joined table takes it as well."""
        text = convert(_regions_pdf(3, after, merged=(3 + after,)), "a.pdf", ConvertOptions(page_markers=False)).text

        assert len(_tables(text)) == 1
        assert all(text.count(name) == 1 for name in _REGIONS[:4 + after])
        last = 3 + after
        assert f"| {_REGIONS[last]} | {1000 + last * 111:,} | {last + 1}.{last}% | 증가 |\n\n이상." in text


def _stacked_pdf(size: float, *tables: tuple[list[tuple[str, ...]], tuple[float, ...], float, float]) -> bytes:
    """Borderless tables one under the other in `size` pt type. Each is (rows, the left edges of its cells, the distance
    between the baselines of its rows, the distance between the baseline of the row above it and that of its first
    row)."""
    def draw(pdf, index):
        pdf.setFont(_FONT, size)
        pdf.drawString(34, 790, "부서별 현황과 품목별 현황은 다음과 같다.")
        y = 760.0
        for rows, lefts, pitch, gap in tables:
            y -= gap
            for number, cells in enumerate(rows):
                for x, text in zip(lefts, cells, strict=True):
                    pdf.drawString(x, y - number * pitch, text)
            y -= (len(rows) - 1) * pitch
        pdf.drawString(34, y - 40, "이상.")

    return _korean_pdf(draw)


_ITEMS = [("구분", "금액", "수량", "비율"), ("여수", "59,378", "52", "14,852"), ("West", "17,116", "21", "38,899"),
          ("배추", "19,884", "88", "89,386"), ("춘천", "27,856", "34", "32,169"), ("목포", "6,035", "25", "42,529"),
          ("호두", "60,684", "82", "92,460")]
_ITEM_TABLE = ("| 구분 | 금액 | 수량 | 비율 |\n| --- | --- | --- | --- |\n| 여수 | 59,378 | 52 | 14,852 |\n"
               "| West | 17,116 | 21 | 38,899 |\n| 배추 | 19,884 | 88 | 89,386 |\n| 춘천 | 27,856 | 34 | 32,169 |\n"
               "| 목포 | 6,035 | 25 | 42,529 |\n| 호두 | 60,684 | 82 | 92,460 |\n\n이상.")
_DEPARTMENTS = [("부서", "인원", "예산", "비고"), ("영업부", "35", "95,616", "증가"), ("개발부", "58", "72,059", "유지"),
                ("총무부", "84", "65,996", "감소"), ("기획부", "57", "95,607", "증가")]


class TestRowsBetweenTwoTablesThatBelongToTheTableBelow:
    """Rows in no table between two borderless tables found apart — the header of the lower table, which pdfminer groups
    differently from its rows ('구분 금액 | 수량 | 비율'), and the rows under it grouped like the header — went to the
    upper table, which is searched for first, as misaligned rows of its own: the lower table came out without its column
    names, its first data row as the Markdown header. They line up with the columns of the lower table, not with those of
    the upper one, and belong to the lower table."""

    @pytest.mark.parametrize("gap", [22.0, 30.0])
    def test_header_of_the_lower_table_under_a_wide_table(self, gap):
        """Three wide columns above, four narrow ones below; `gap` is less and more than a row of the upper table."""
        data = _stacked_pdf(11, ([row[:3] for row in _DEPARTMENTS], (40, 353.6, 496.4), 26.4, 0),
                            (_ITEMS, (40, 82.9, 146.9, 195.3), 17.6, gap))

        text = convert(data, "a.pdf", ConvertOptions(page_markers=False)).text

        assert "| 총무부 | 84 | 65,996 |\n| 기획부 | 57 | 95,607 |\n\n" + _ITEM_TABLE in text
        assert text.count("구분") == 1 and text.count("여수") == 1

    @pytest.mark.parametrize("gap", [18.0, 27.0])
    def test_header_of_the_lower_table_at_the_row_pitch_of_the_upper_table(self, gap):
        """Both tables have four columns and the same row pitch; the lower one follows at that pitch or half a row more."""
        data = _stacked_pdf(10, (_DEPARTMENTS, (40, 160, 280, 400), 18, 0), (_ITEMS, (40, 79, 137, 181), 18, gap))

        text = convert(data, "a.pdf", ConvertOptions(page_markers=False)).text

        assert "| 총무부 | 84 | 65,996 | 감소 |\n| 기획부 | 57 | 95,607 | 증가 |\n\n" + _ITEM_TABLE in text
        assert text.count("구분") == 1 and text.count("여수") == 1

    @pytest.mark.parametrize(("size", "upper", "lefts", "pitch", "wrapped_at", "lower_lefts", "expected"), [
        (11, [row[:3] for row in _DEPARTMENTS], (40, 353.6, 496.4), 26.4, 353.6, (40, 82.9, 146.9, 195.3),
         "| 기획부 | 57<br>추가설명 | 95,607 |"),
        (10, _DEPARTMENTS, (40, 160, 280, 400), 18, 280, (40, 79, 137, 181), "| 기획부 | 57 | 95,607<br>추가설명 | 증가 |"),
    ])
    def test_second_line_of_a_wrapped_cell_stays_in_its_row(self, size, upper, lefts, pitch, wrapped_at, lower_lefts, expected):
        """The last row of the upper table has a cell of two lines. With the header of the lower table left to that table,
        the upper table ended at the second line, which is no row of its own, and it came out as a paragraph between the
        two tables. The upper table then takes the rows below it as before."""
        data = _stacked_pdf(size, (upper, lefts, pitch, 0), ([("추가설명",)], (wrapped_at,), pitch, 12),
                            (_ITEMS, lower_lefts, 18, 20))

        text = convert(data, "a.pdf", ConvertOptions(page_markers=False)).text

        assert expected in text
        assert text.count("추가설명") == 1 and text.count("여수") == 1

    @pytest.mark.parametrize("gap", [22.0, 30.0])
    def test_first_data_rows_of_a_lower_table_without_a_header(self, gap):
        """The first two rows of the lower table are grouped differently ('28.7% pending') from the row the table is
        found from ('2.0% | ok'). They were left only as rows of the six-column table above."""
        upper = [("구분", "금액", "수량", "비율", "증감", "비고"), ("양파", "23,360", "11,857", "6", "48", "27.5%"),
                 ("통영", "73,612", "14,132", "48", "8", "2.3%"), ("울산", "71,394", "1,607", "92", "56", "76.8%"),
                 ("호두", "37,966", "83,440", "4", "73", "98.0%"), ("배추", "68,711", "81,652", "76", "18", "46.3%")]
        lower = [("구미", "28.7%", "pending", "9.40"), ("원주", "52.5%", "완료", "7.88"), ("North", "2.0%", "ok", "3.60"),
                 ("대추", "48.7%", "pending", "3.19"), ("Sigma", "12.6%", "유지", "3.86"), ("서울", "62.2%", "유지", "2.85"),
                 ("목포", "75.2%", "완료", "6.88")]
        data = _stacked_pdf(11, (upper, (40, 103.1, 156, 209, 251.9, 289.3), 17.6, 0),
                            (lower, (40, 105.8, 155.1, 217.7), 26.4, gap))

        text = convert(data, "a.pdf", ConvertOptions(page_markers=False)).text

        assert ("| 배추 | 68,711 | 81,652 | 76 | 18 | 46.3% |\n\n| 구미 | 28.7% | pending | 9.40 |\n| --- | --- | --- | --- |\n"
                "| 원주 | 52.5% | 완료 | 7.88 |\n| North | 2.0% | ok | 3.60 |\n| 대추 | 48.7% | pending | 3.19 |") in text
        assert "| 목포 | 75.2% | 완료 | 6.88 |\n\n이상." in text
        assert text.count("구미") == 1 and text.count("원주") == 1

    def test_rows_the_lower_table_does_not_take_stay_in_the_upper_table(self):
        """The lower table is not found again with its header ('Name | Value | Rate Count') because of the row under it
        ('품목 단가 | 수량'), so the header stays a row of the upper table as before instead of falling out of both."""
        upper = [("지역", "매출", "비용", "이익"), ("상추", "15.6%", "86,613", "7.8%"), ("순천", "14.8%", "76,729", "56.1%"),
                 ("양산", "36.9%", "63,559", "32.3%"), ("Theta", "42.5%", "8,550", "38.0%")]
        lower = [("Name", "Value", "Rate", "Count"), ("포도", "26,954", "17", "99"), ("울산", "15,200", "49", "40"),
                 ("Omega", "43,597", "17", "54")]
        third = [("품목", "단가", "수량"), ("대추", "47,266", "증가"), ("세종", "75,046", "pending"), ("포항", "46,729", "done")]
        data = _stacked_pdf(11, (upper, (70, 122.5, 182.8, 246.8), 17.6, 0), (lower, (80, 137.6, 196.0, 235.7), 26.4, 22),
                            (third, (80, 117.4, 170.4), 22, 16.5))

        text = convert(data, "a.pdf", ConvertOptions(page_markers=False)).text

        assert ("| Theta | 42.5% | 8,550 | 38.0% |\n| Name | Value | Rate | Count |\n\n| 포도 | 26,954 | 17 | 99 |\n"
                "| --- | --- | --- | --- |\n| 울산 | 15,200 | 49 | 40 |") in text
        assert text.count("Name") == 1

    def test_data_row_lined_up_with_the_rows_below_is_still_joined(self):
        """One table whose lower rows are set differently: the row in no table ('김해 | 유지 23 | 22,717') holds a measured
        value and lines up with the rows below it. The two parts are joined as before, not left as two tables."""
        upper = [("구분", "2023", "2024", "증감"), ("대추", "54.8%", "9.21", "1.22"), ("자두", "72.8%", "3.20", "9.92"),
                 ("수원", "74.7%", "3.82", "8.49")]
        lower = [("Lower", "감소", "84", "81,615"), ("김해", "유지", "23", "22,717"), ("가지", "done", "15", "89,100"),
                 ("South", "유지", "93", "52,588"), ("양산", "유지", "43", "83,133")]
        data = _stacked_pdf(10, (upper, (40, 74, 118.8, 167.8), 24, 0), (lower, (40, 90.2, 127.5, 164.1), 16, 20))

        text = convert(data, "a.pdf", ConvertOptions(page_markers=False)).text

        assert len(_tables(text)) == 1
        assert ("| 수원 | 74.7% | 3.82 | 8.49 |\n| Lower | 감소 | 84 | 81,615 |\n| 김해 | 유지 | 23 | 22,717 |\n"
                "| 가지 | done | 15 | 89,100 |\n| South | 유지 | 93 | 52,588 |\n| 양산 | 유지 | 43 | 83,133 |\n\n이상.") in text


def _platypus_table_pdf(*tables: list[str], space: float = 6.0) -> bytes:
    """Reportlab platypus tables with their default style (no rules, 6pt cell padding) in 9pt type between two paragraphs,
    `space` pt below the first paragraph and 20pt apart. Each row is 'a|b|…' with empty parts for empty cells. The cells of
    a row stand 12pt apart, so pdfminer merges some neighbouring cells into one piece ('R1C0값 572,1001') and others not,
    differently from row to row."""
    from io import BytesIO

    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table

    pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
    body = ParagraphStyle("b", fontName=_FONT, fontSize=10, leading=14)
    style = [("FONTNAME", (0, 0), (-1, -1), _FONT), ("FONTSIZE", (0, 0), (-1, -1), 9)]
    story = [Paragraph("표 앞의 설명 문단입니다.", body), Spacer(1, space)]
    for number, rows in enumerate(tables):
        story += [*([Spacer(1, 20)] if number else []), Table([row.split("|") for row in rows], style=style)]
    buffer = BytesIO()
    SimpleDocTemplate(buffer, pagesize=A4).build([*story, Spacer(1, 12), Paragraph("표 뒤 문단.", body)])
    return buffer.getvalue()


class TestUniformTableFoundInSeveralParts:
    """One borderless table whose cells all stand at the same x on every row, with neighbouring cells merged by pdfminer
    differently from row to row. Its rows 1–3 and 8–11 were found as tables first, rows 4–7 (two of them a single piece
    wider than a table cell) only by splitting the lines in no table, and the three parts were never joined: they came
    out as three tables, and the last one, its columns taken from its own first row ('R8C0값 410,2081 | 373,2082 |
    322,2084'), had four columns, so 'R10C3값' and 'R11C4값' stood one column to the left."""

    _ROWS = ["H20열|H21열|H22열|H23열|H24열", "R201C0값|572,2011|R201C2값|780,2013|R201C4값", "R202C0값||794,2022||",
             "R203C0값|591,2031|564,2032|R203C3값|704,2034", "R204C0값|R204C1값|R204C2값|R204C3값|45,2044",
             "|R205C1값|R205C2값|R205C3값|274,2054", "R206C0값|R206C1값|256,2062||824,2064",
             "R207C0값|139,2071|R207C2값|R207C3값|R207C4값", "R208C0값|410,2081|373,2082||322,2084",
             "R209C0값|876,2091|782,2093|R209C3값|", "|R210C1값||R210C3값|880,2104", "R211C0값|859,2111|R211C2값||R211C4값"]

    def test_the_parts_come_out_as_one_table_with_every_cell_in_its_column(self):
        text = convert(_platypus_table_pdf(self._ROWS), "a.pdf", ConvertOptions(page_markers=False)).text

        assert ("표 앞의 설명 문단입니다.\n\n| H20열 | H21열 | H22열 | H23열 | H24열 |\n| --- | --- | --- | --- | --- |\n"
                "| R201C0값 | 572,2011 | R201C2값 | 780,2013 | R201C4값 |\n| R202C0값 | | 794,2022 | | |\n"
                "| R203C0값 | 591,2031 | 564,2032 | R203C3값 | 704,2034 |\n"
                "| R204C0값 | R204C1값 | R204C2값 | R204C3값 | 45,2044 |\n| | R205C1값 | R205C2값 | R205C3값 | 274,2054 |\n"
                "| R206C0값 | R206C1값 | 256,2062 | | 824,2064 |\n| R207C0값 | 139,2071 | R207C2값 | R207C3값 | R207C4값 |\n"
                "| R208C0값 | 410,2081 | 373,2082 | | 322,2084 |\n| R209C0값 | 876,2091 | 782,2093 | R209C3값 | |\n"
                "| | R210C1값 | | R210C3값 | 880,2104 |\n| R211C0값 | 859,2111 | R211C2값 | | R211C4값 |\n\n표 뒤 문단.") in text


class TestLowerPartOfATableAtThePageEnd:
    """One table set in two parts on a page — its lower part starts with a row of text only, so the search keeps the parts
    apart — and continued on the next page without its header. The first row of the lower part was carried to the next page
    as the header, and that row came out twice."""

    _ROWS = [*TestUniformTableFoundInSeveralParts._ROWS[:4], "R204C0값|R204C1값|R204C2값|R204C3값|R204C4값",
             *TestUniformTableFoundInSeveralParts._ROWS[5:],
             *(f"R{n}C0값|{n},{n}1|R{n}C2값|{n * 3},{n}3|R{n}C4값" for n in range(212, 230))]

    def test_the_next_page_gets_the_header_of_the_upper_part(self):
        text = convert(_platypus_table_pdf(self._ROWS, space=450), "a.pdf").text

        page = text.split("<!-- Page 2 -->")
        assert "| R203C0값 | 591,2031 | 564,2032 | R203C3값 | 704,2034 |\n\n| R204C0값 | R204C1값 |" in page[0]
        assert page[1].startswith("\n\n| H20열 | H21열 | H22열 | H23열 | H24열 |\n| --- | --- | --- | --- | --- |\n"
                                  "| R212C0값 | 212,2121 |")
        assert text.count("R204C0값") == 1

    def test_a_table_of_its_own_under_another_table_still_gets_its_own_header(self):
        """A separate table with its own header 20pt under another table with the same columns is not a part of it."""
        upper = ["부서|인원|예산|비고", *(f"부서{n}|{n}|{n * 1000:,}|유지" for n in range(1, 8))]
        lower = ["품목|수량|단가|메모", *(f"품목{n}|{n}|{n * 1234:,}|확인" for n in range(1, 40))]

        page = convert(_platypus_table_pdf(upper, lower, space=0), "a.pdf").text.split("<!-- Page 2 -->")

        assert page[1].startswith("\n\n| 품목 | 수량 | 단가 | 메모 |\n| --- | --- | --- | --- |\n| 품목")


def _random_ruled_tables(seed: int) -> tuple[bytes, list[str]]:
    """One or two pages of ruled, open-sided, partly ruled and shaded tables (1–14 rows, 1–7 columns, some columns and rows
    of almost no size) with a distinct word in every filled cell, drawn from `seed`. Returns the PDF and the words."""
    from io import BytesIO

    from reportlab.lib import colors
    from reportlab.pdfgen import canvas

    rnd = random.Random(seed)
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(595, 842))
    words: list[str] = []
    for _ in range(rnd.choice([1, 1, 2])):
        pdf.setFont("Helvetica", 9)
        y = 800.0
        for _ in range(rnd.choice([1, 1, 2])):
            rows, columns = rnd.randint(1, 14), rnd.randint(1, 7)
            xs = [rnd.choice([20.0, 40.0, 60.0])]
            for _ in range(columns):
                xs.append(min(xs[-1] + rnd.choice([0, 0.5, 55, 70, 90, 140]), 570))
            ys = [y]
            for _ in range(rows):
                ys.append(max(ys[-1] - rnd.choice([0.5, 14, 18, 24, 36]), 30))
            style = rnd.choice(["full", "open", "rules", "shaded", "partial"])
            pdf.setLineWidth(rnd.choice([0.3, 0.5, 1]))
            if style == "shaded" or rnd.random() < 0.2:
                for row in range(rows):
                    if rnd.random() < 0.5:
                        pdf.setFillColor(rnd.choice([colors.HexColor("#E8EEF7"), colors.HexColor("#3366cc"),
                                                     colors.HexColor("#888888")]))
                        for column in range(columns):
                            if rnd.random() < 0.8:
                                pdf.rect(xs[column], ys[row + 1], xs[column + 1] - xs[column], ys[row] - ys[row + 1],
                                         stroke=0, fill=1)
                pdf.setFillColor(colors.black)
            for level in ys:
                if style == "shaded" and rnd.random() < 0.7:
                    continue
                if style == "partial":
                    for column in range(columns):
                        if rnd.random() < 0.75:
                            pdf.line(xs[column], level, xs[column + 1], level)
                else:
                    pdf.line(xs[0], level, xs[-1], level)
            for number, x in enumerate(xs):
                if style == "rules":
                    break
                if (style == "open" and number in (0, len(xs) - 1)) or (style == "shaded" and rnd.random() < 0.7):
                    continue
                if style == "partial":
                    for row in range(rows):
                        if rnd.random() < 0.75:
                            pdf.line(x, ys[row], x, ys[row + 1])
                else:
                    pdf.line(x, ys[0], x, ys[-1])
            for row in range(rows):
                for column in range(columns):
                    if rnd.random() < 0.8 and xs[column + 1] - xs[column] >= 50 and ys[row] - ys[row + 1] >= 14:
                        for line in range(1 if ys[row] - ys[row + 1] < 24 else rnd.choice([1, 2])):
                            word = str(rnd.randint(1, 99999)) if rnd.random() < 0.3 else f"w{len(words) + 1}"
                            words.append(word)
                            pdf.drawString(xs[column] + 2, ys[row] - 10 - line * 10, word)
            y = ys[-1] - rnd.choice([10, 30, 60])
            if rnd.random() < 0.5:
                words.append(f"w{len(words) + 1}")
                pdf.drawString(40, y, f"para {words[-1]}")
                y -= 20
        pdf.showPage()
    pdf.save()
    return buffer.getvalue(), words


class TestNoCellTextInTwoTables:
    """Whole-document invariant: every word drawn once in a table cell is kept, and no cell text comes out in two tables of
    a page (a value is repeated only inside its own table, for a merged cell)."""

    @pytest.mark.parametrize("seed", range(40))
    def test_random_ruled_tables(self, seed):
        data, words = _random_ruled_tables(seed)

        sections = convert(data, "a.pdf", ConvertOptions(page_markers=False)).sections

        found = set(re.findall(r"w\d+|\d+", "\n".join(section.text for section in sections)))
        assert [word for word in words if word not in found] == []
        for section in sections:
            shared = _cells_in_two_tables(section.text)
            assert not {cell for cell in shared if any(word in cell.split() for word in words)}

    def test_rows_with_wrapped_cells_between_rules_stay_in_their_table(self):
        """Seed 20 draws a table ruled under every row, without column rules, whose rows have cells wrapped onto two lines.
        Kept out of the header joining, the rows with two wrapped cells were split in two and one with a wrapped value broke
        the table in two, its other cells scattered below it."""
        data, _ = _random_ruled_tables(20)

        text = convert(data, "a.pdf", ConvertOptions(page_markers=False)).text

        assert ("| | 61512 | w31 | | w32<br>38537 |\n| | 27128 | w35<br>47149 | w37 | w38<br>60147 |\n"
                "| | 44126 | w41 | 15460 | w44 |") in text

    @pytest.mark.parametrize(("before", "after", "merged"), [(3, 3, ()), (3, 5, (4,)), (4, 5, (6,)), (6, 6, ())])
    def test_borderless_tables_split_at_merged_rows(self, before, after, merged):
        text = convert(_regions_pdf(before, after, merged), "a.pdf", ConvertOptions(page_markers=False)).text

        assert not {cell for cell in _cells_in_two_tables(text) if cell in _REGIONS or cell[0].isdigit()}


def _three_column_pdf(rows: list[str], top: float = 742.0, header: bool = True) -> bytes:
    """A borderless three-column table under a paragraph: text at x 60, a number right-aligned at x 220 and text at x 230
    (or a number right-aligned at x 320 with `header` False). Each row is 'a/b/c' with empty parts for empty cells; the
    number and the text beside it are 10pt apart, so pdfminer merges them into one piece ('79,619 사과')."""
    def draw(pdf, index):
        pdf.drawString(72, 782, "보고서 본문 첫 문단입니다. 표는 다음과 같습니다.")
        cells = [row.split("/") for row in rows]
        if header:
            cells = [["항목0", "항목1", "항목2"], *cells]
        y = top
        for number, (first, second, third) in enumerate(cells):
            if first:
                pdf.drawString(60, y, first)
            if second:
                if header and number == 0:
                    pdf.drawString(140, y, second)
                else:
                    pdf.drawRightString(220, y, second)
            if third:
                if header:
                    pdf.drawString(230, y, third)
                else:
                    pdf.drawRightString(320, y, third)
            y -= 16
        pdf.drawString(72, y - 30, "표 아래 본문 문단입니다.")

    return _korean_pdf(draw)


class TestLabelOnlyRowAmongMergedCells:
    """A label-only row ('합계') was judged on the merged lines (no number in '29,985 배') when the table was first found,
    and on the split cells when it was searched again, where the row ended the table: the search failed and every merged
    cell stayed unsplit. A row of two cells also ended the table above as if it were the header of a new one."""

    def test_merged_cells_are_split_around_a_label_only_row(self):
        data = _three_column_pdf(["사과/79,619/사과", "Alpha/29,985/배", "합계//", "수량//해당 없음", "서울/69,158/금액",
                                  "금액/88,716/금액"])

        text = convert(data, "a.pdf", ConvertOptions(page_markers=False)).text

        assert ("| 항목0 | 항목1 | 항목2 |\n| --- | --- | --- |\n| 사과 | 79,619 | 사과 |\n| Alpha | 29,985 | 배 |\n"
                "| 합계 | | |\n| 수량 | | 해당 없음 |\n| 서울 | 69,158 | 금액 |\n| 금액 | 88,716 | 금액 |") in text

    def test_row_of_two_cells_after_a_label_only_row_stays_in_the_table(self):
        data = _three_column_pdf(["서울/1,250/증가", "비고/16,293/감소", "비고//", "미정//보류", "포도/69.0%/증가",
                                  "배/68,717/유지"])

        text = convert(data, "a.pdf", ConvertOptions(page_markers=False)).text

        assert "| 비고 | 16,293 | 감소 |\n| 비고 | | |\n| 미정 | | 보류 |\n| 포도 | 69.0% | 증가 |\n| 배 | 68,717 | 유지 |" in text


class TestSparseRowsAboveABorderlessTable:
    """In a sparse three-column table the rows with an empty cell have two fragments, so they could not start a table, and
    the table found from the first full row below could only take rows above it under a row of three. They came out as
    lines scattered column by column above the table."""

    def test_rows_with_empty_cells_above_the_first_full_row_join_the_table(self):
        data = _three_column_pdf(["배//27,789", "Alpha//5,660", "Alpha/59,905/", "합계/62,595/54,915", "금액/30,360/71,902",
                                  "비고/11,051/9.6%", "합계/54,627/89,307"], header=False)

        text = convert(data, "a.pdf", ConvertOptions(page_markers=False)).text

        assert ("| 배 | | 27,789 |\n| --- | --- | --- |\n| Alpha | | 5,660 |\n| Alpha | 59,905 | |\n"
                "| 합계 | 62,595 | 54,915 |\n| 금액 | 30,360 | 71,902 |\n| 비고 | 11,051 | 9.6% |\n| 합계 | 54,627 | 89,307 |") in text

    def test_caption_with_a_unit_above_the_table_stays_a_line(self):
        """A line of two fragments without a numeric value, its unit at the right edge of the last column, is no row."""
        data = _three_column_pdf(["표 1//(단위: 원)", "합계/62,595/54,915", "금액/30,360/71,902", "비고/11,051/9.6%"],
                                 header=False)

        text = convert(data, "a.pdf", ConvertOptions(page_markers=False)).text

        assert "| 합계 | 62,595 | 54,915 |\n| --- | --- | --- |" in text and "| 표 1" not in text

    def test_lines_above_a_text_header_stay_out_of_the_table(self):
        """The lines of a table of contents right above a table that has its header: the page number of a line sits under
        the second column, but nothing above a header row without a numeric value is a data row. The first contents line
        became the header and the real header a data row."""
        def draw(pdf, index):
            pdf.drawString(60, 790, "Contents")
            for number, (title, page) in enumerate([("1. Overview of the year", "3"), ("2. Sales by region", "12"), ("3. Outlook", "27")]):
                pdf.drawString(64, 770 - number * 16, title)
                pdf.drawRightString(252, 770 - number * 16, page)
            for number, row in enumerate([("Region", "Units", "Revenue"), ("North", "1,250", "44,288"), ("South", "980", "46,001"),
                                          ("East", "2,680", "37,138"), ("West", "770", "66,585")]):
                pdf.drawString(64, 722 - number * 16, row[0])
                pdf.drawRightString(252, 722 - number * 16, row[1])
                pdf.drawRightString(352, 722 - number * 16, row[2])
            pdf.drawString(60, 622, "This report was prepared by the finance team.")

        text = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False)).text

        assert text == ("Contents\n1. Overview of the year 3\n2. Sales by region 12\n3. Outlook 27\n\n| Region | Units | Revenue |\n"
                        "| --- | --- | --- |\n| North | 1,250 | 44,288 |\n| South | 980 | 46,001 |\n| East | 2,680 | 37,138 |\n"
                        "| West | 770 | 66,585 |\n\nThis report was prepared by the finance team.")

    def test_header_of_the_table_below_stays_out_of_a_key_value_block(self):
        """Three lines of a key-value block (three pieces, one piece, two pieces) and, 32pt below, a table of four columns.
        The header of the table lines up with no value of the block, and one value in its column is enough to see that: the
        block became a table of three columns that took the header, and the values of the items moved one column left."""
        def draw(pdf, index):
            pdf.setFont(_FONT, 10)
            pdf.drawString(60, 760, "항목")
            pdf.drawString(264, 760, "221")
            pdf.drawRightString(552, 760, "86,656")
            pdf.drawString(60, 746, "감소")
            pdf.drawString(60, 730, "수량")
            pdf.drawString(264, 730, "81,951")
            for number, row in enumerate([("단가", "없음", "공통", "품목"), ("자산", "63,571", "98.4%", ""), ("합계", "44,190", "", "25,339"),
                                          ("품목", "52,636", "25,527", ""), ("합계", "", "48,985", "59,159")]):
                pdf.drawString(64, 698 - number * 18, row[0])
                for column in (1, 2, 3):
                    pdf.drawRightString(152 + column * 100, 698 - number * 18, row[column])
            pdf.drawString(60, 588, "전 지점 목표 달성률은 102%이다.")

        text = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False)).text

        assert ("| 단가 | 없음 | 공통 | 품목 |\n| --- | --- | --- | --- |\n| 자산 | 63,571 | 98.4% | |\n| 합계 | 44,190 | | 25,339 |\n"
                "| 품목 | 52,636 | 25,527 | |\n| 합계 | | 48,985 | 59,159 |") in text
        assert "| 항목" not in text and "| 수량" not in text

    def test_two_label_rows_in_a_row_stay_in_the_table(self):
        data = _three_column_pdf(["항목0/항목1/항목2", "합계/27,368/71,946", "금액/67,601/", "비고//", "해당 없음//",
                                  "금액/88,763/", "부산//96,157", "포도/2.6%/"], header=False)

        text = convert(data, "a.pdf", ConvertOptions(page_markers=False)).text

        assert ("| 금액 | 67,601 | |\n| 비고 | | |\n| 해당 없음 | | |\n| 금액 | 88,763 | |\n| 부산 | | 96,157 |\n"
                "| 포도 | 2.6% | |") in text

    def test_label_rows_right_under_the_header_stay_in_the_table(self):
        data = _three_column_pdf(["항목0/항목1/항목2", "금액//", "배//", "서울//", "비고/99,598/3,646", "서울//",
                                  "미정//73,270", "미정/60.1%/"], header=False)

        text = convert(data, "a.pdf", ConvertOptions(page_markers=False)).text

        assert ("| 항목0 | 항목1 | 항목2 |\n| --- | --- | --- |\n| 금액 | | |\n| 배 | | |\n| 서울 | | |\n"
                "| 비고 | 99,598 | 3,646 |\n| 서울 | | |\n| 미정 | | 73,270 |\n| 미정 | 60.1% | |") in text


def _pieces_pdf(pieces: list[tuple[float, float, str]]) -> bytes:
    """A page with a sentence above and below, and each (x, y, text) piece in between, in 11pt Helvetica."""
    from io import BytesIO

    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.setFont("Helvetica", 11)
    pdf.drawString(72, 760, "The Lagrange equations are traditionally written as follows in the text.")
    for x, y, text in pieces:
        pdf.drawString(x, y, text)
    pdf.drawString(72, 600, "These hold for every path the system is allowed to take, at each moment of time.")
    pdf.save()
    return buffer.getvalue()


class TestDisplayedFormulas:
    def test_a_displayed_formula_is_not_a_table(self):
        pieces = [(150, 700, "d"), (200, 700, "dL"), (260, 700, "dL"), (150, 686, "="), (230, 686, "±"), (290, 686, "= 0."),
                  (150, 672, "dt"), (200, 672, "dv"), (260, 672, "dq")]

        text = convert(_pieces_pdf(pieces), "a.pdf").text

        assert "|" not in text
        assert "d dL dL" in text and "dt dv dq" in text

    def test_a_table_of_measurements_with_signs_stays_a_table(self):
        rows = [("Sample", "Mass", "Error"), ("A-1", "12.5 g", "± 0.3 g"), ("A-2", "13.1 g", "± 0.2 g"),
                ("A-3", "11.8 g", "± 0.4 g")]
        pieces = [(150 + column * 90, 700 - row * 16, text) for row, values in enumerate(rows) for column, text in enumerate(values)]

        text = convert(_pieces_pdf(pieces), "a.pdf").text

        assert "| A-2 | 13.1 g | ± 0.2 g |" in text
