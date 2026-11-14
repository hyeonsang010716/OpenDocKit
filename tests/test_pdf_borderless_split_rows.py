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

    @pytest.mark.parametrize("periods", [("2023", "2024"), ("2024.01", "2024.02"), ("FY2023", "FY2024"), ("1분기", "2분기")])
    def test_table_under_a_header_of_years_or_dates_is_not_joined(self, periods):
        """Years and dates in a header are not values: '구분 | 2023 | 2024 | 증감' starts a table of its own."""
        rows = [("구분", *periods, "증감"), ("매출", "1,200", "1,350", "+150"), ("비용", "800", "820", "+20"),
                ("이익", "400", "530", "+130"), ("세금", "90", "110", "+20")]

        text = convert(_regions_pdf(3, 0, rows=rows), "a.pdf", ConvertOptions(page_markers=False)).text

        regions = _tables(text)[0]
        assert regions[-1] == ["인천", "1,333", "4.3%", "증가"]
        assert not any(cell.startswith(("구분", "매출")) for row in regions for cell in row)
        if periods == ("2023", "2024"):  # (the other headers merge into one piece and are not taken as a header row at all)
            assert ("| 인천 | 1,333 | 4.3% | 증가 |\n\n| 구분 | 2023 | 2024 | 증감 |\n| --- | --- | --- | --- |\n"
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

    @pytest.mark.parametrize(("before", "after", "merged"), [(3, 3, ()), (3, 5, (4,)), (4, 5, (6,)), (6, 6, ())])
    def test_borderless_tables_split_at_merged_rows(self, before, after, merged):
        text = convert(_regions_pdf(before, after, merged), "a.pdf", ConvertOptions(page_markers=False)).text

        assert not {cell for cell in _cells_in_two_tables(text) if cell in _REGIONS or cell[0].isdigit()}
