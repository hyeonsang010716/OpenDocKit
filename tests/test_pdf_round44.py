"""PDF defects from review round 44: a Type3 Descent in the wrong unit, striped tables whose vertically merged groups start
on shaded rows, and a row merged differently in the middle of a borderless table."""

import pytest

from opendockit import ConvertOptions, convert
from tests.test_pdf import _FONT, _korean_pdf
from tests.test_pdf_fonts import _page_pdf


def _type3_digits_in_glyph_units(descent: bytes) -> bytes:
    """Type3 digit font whose glyph space equals text space (FontMatrix [1 0 0 1 0 0], bottom of FontBBox -1.05). The Descent
    of the font descriptor is `descent`."""
    procs = b" ".join(b"/%s 5 0 R" % name for name in (b"zero", b"one", b"two", b"three", b"four", b"five", b"six", b"seven",
                                                       b"eight", b"nine"))
    names = b"/zero /one /two /three /four /five /six /seven /eight /nine"
    return (b"<< /Type /Font /Subtype /Type3 /FontBBox [0 -1.05 1 0.9] /FontMatrix [1 0 0 1 0 0]"
            b" /CharProcs << %s >> /Encoding << /Type /Encoding /Differences [48 %s] >> /FirstChar 48 /LastChar 57"
            b" /Widths [%s] /Resources << >> /FontDescriptor << /Type /FontDescriptor /FontName /Digits /Flags 32"
            b" /Ascent 0.9 /Descent %s /CapHeight 0.7 /ItalicAngle 0 /StemV 80 /FontBBox [0 -1.05 1 0.9] >> >>"
            % (procs, names, b" ".join([b"0.56"] * 10), descent))


class TestType3DescentInTheWrongUnit:
    """Round 43 made the Type3 descent come from the Descent of the font descriptor, but some generators write Descent in
    1000 units (-288) for a font whose glyph space is text space. The descent became -288em, the glyph boxes went off the
    page, and the digits disappeared entirely without a warning."""

    _GLYPH = {5: b"<< /Length 8 >>\nstream\n0.56 0 d0\nendstream"}

    @pytest.mark.parametrize("descent", [b"-288", b"-0.288"], ids=["thousand-units", "glyph-units"])
    def test_digits_of_a_type3_font_with_a_descent_outside_its_bounding_box_are_kept(self, descent):
        content = b"BT /F1 12 Tf 72 700 Td (Total row) Tj /F2 12 Tf (450) Tj ET"
        fonts = {b"F1": b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", b"F2": _type3_digits_in_glyph_units(descent)}

        result = convert(_page_pdf(content, fonts, self._GLYPH), "a.pdf", ConvertOptions(page_markers=False))

        assert result.text == "Total row450"


def _zebra_groups_pdf(groups: tuple[int, ...], header_rule: bool = False, row_height: float = 24.0) -> bytes:
    """Striped table as printed by Chrome (each cell of the even tr rows is shaded, no ruling lines). The first column holds
    cells merged vertically over `groups` rows each ('본부0', …), and a merged cell takes the shading of the row it starts
    on. Unshaded rows sit beside shaded merged cells, and the paragraph '끝.' follows the table."""
    edges, top = [34.0, 99.0, 151.0, 203.0, 255.0], 732.0
    starts = [sum(groups[:index]) + 1 for index in range(len(groups))]  # first data row of each group (1-based)
    rows = sum(groups)

    def draw(pdf, index):
        from reportlab.lib import colors

        def row_top(row):  # 0 = header row, 1.. = data rows
            return top - row * row_height

        pdf.setFont(_FONT, 10)
        pdf.drawString(34, top + 20, "표 2. 본부별 현황")
        pdf.setFillColor(colors.HexColor("#EEEEEE"))
        for start, size in zip(starts, groups, strict=False):
            if start % 2:
                pdf.rect(edges[0], row_top(start + size - 1) - row_height, edges[1] - edges[0], size * row_height, stroke=0, fill=1)
        for row in range(1, rows + 1, 2):
            for x0, x1 in zip(edges[1:], edges[2:], strict=False):
                pdf.rect(x0, row_top(row) - row_height, x1 - x0, row_height, stroke=0, fill=1)
        pdf.setFillColor(colors.black)
        if header_rule:
            pdf.line(edges[0], row_top(1), edges[-1], row_top(1))
        for x0, text in zip(edges, ("구분", "항목1", "항목2", "항목3"), strict=False):
            pdf.drawString(x0 + 12, row_top(0) - 16, text)
        for number, (start, size) in enumerate(zip(starts, groups, strict=False)):
            pdf.drawString(46, row_top(start) - size * row_height / 2 - 4, f"본부{number}")  # middle of the group
        for row in range(1, rows + 1):
            for column, x0 in enumerate(edges[1:4]):
                pdf.drawString(x0 + 12, row_top(row) - 16, f"{row},{column}0{row}")
        pdf.drawString(34, row_top(rows + 1) - 22, "끝.")  # lower by the paragraph margin (1em of a Chrome p)

    return _korean_pdf(draw)


def _zebra_groups_table(groups: tuple[int, ...]) -> str:
    names = [f"본부{number}" for number, size in enumerate(groups) for _ in range(size)]
    rows = [f"| {name} | " + " | ".join(f"{row},{column}0{row}" for column in range(3)) + " |" for row, name in enumerate(names, 1)]
    return "\n".join(["| 구분 | 항목1 | 항목2 | 항목3 |", "| --- | --- | --- | --- |", *rows])


class TestZebraTableWhoseMergedGroupsStartOnShadedRows:
    """In a striped table that only has vertically merged groups starting on shaded rows, the unshaded rows were scattered
    outside the table one number at a time and the header row became paragraphs (the table pinned in round 33 has unshaded
    groups of its own, so it was not affected). The row height measured from the merged-cell rows was too large, so the
    paragraph below the table was attached to a cell of the last row ('본부2<br>끝.') or the caption overlapped the header
    cell."""

    @pytest.mark.parametrize(("groups", "header_rule", "row_height"), [
        ((2, 2), False, 24.0), ((2, 2), True, 24.0), ((2, 3, 1), False, 24.0), ((2, 3, 1), False, 18.0)],
        ids=["pairs", "pairs-ruled-header", "last-row-unshaded", "tight-rows"])
    def test_unshaded_rows_beside_shaded_merged_cells_stay_in_the_table(self, groups, header_rule, row_height):
        text = convert(_zebra_groups_pdf(groups, header_rule, row_height), "a.pdf", ConvertOptions(page_markers=False)).text

        assert f"표 2. 본부별 현황\n\n{_zebra_groups_table(groups)}\n\n끝." == text


def _borderless_with_split_row_pdf() -> bytes:
    """Borderless four-column table as printed by Chrome (narrow cells, no ruling lines). The gaps between cells differ
    slightly from row to row, so pdfminer merges different cells on different rows: the first two cells on rows 1–4 and 6–7
    ('서울 5,483'), the last two cells on row 5 ('10.8% 감소')."""
    rows = [("서울", "5,483", "4.9%", "증가"), ("부산", "8,449", "24.7%", "감소"), ("대구", "6,579", "11.7%", "유지"),
            ("인천", "8,393", "26.1%", "유지"), ("광주", "5,955", "10.8%", "감소"), ("대전", "3,409", "9.6%", "감소"),
            ("울산", "2,871", "3.2%", "증가")]
    joined, apart = 14.0, 24.0  # pdfminer joins a gap within twice the character width (10pt) into one piece

    def draw(pdf, index):
        pdf.setFont(_FONT, 10)
        pdf.drawString(34, 760, "지역별 실적은 다음과 같다.")
        for row, cells in enumerate(rows):
            gaps = (apart, apart, joined) if row == 4 else (joined, apart, apart)
            x, y = 44.0, 736 - row * 20
            for column, text in enumerate(cells):
                pdf.drawString(x, y, text)
                if column < len(gaps):
                    x += pdf.stringWidth(text, _FONT, 10) + gaps[column]
        pdf.drawString(34, 736 - len(rows) * 20 - 10, "이상.")

    return _korean_pdf(draw)


class TestBorderlessTableWithARowMergedDifferently:
    """When a row whose cells pdfminer merged differently comes in the middle of a borderless table, the table was cut above
    it and the two remaining rows, short of the minimum row count, became a paragraph in mixed-up order like
    '광주 5,955 10.8% 감소 대전 3,409 …' (`_split_merged_cells` only re-examined the rows above the table)."""

    def test_rows_below_a_differently_merged_row_stay_in_the_table(self):
        text = convert(_borderless_with_split_row_pdf(), "a.pdf", ConvertOptions(page_markers=False)).text

        assert "| 광주 | 5,955 | 10.8% | 감소 |\n| 대전 | 3,409 | 9.6% | 감소 |\n| 울산 | 2,871 | 3.2% | 증가 |" in text
        assert text.index("증가 |") < text.index("이상.")


def _tbody_zebra_pdf(first_line: list[str] | None = None) -> bytes:
    """Striped table as printed by Chrome (`tbody tr:nth-child(even)` — the header row and the first data row are unshaded,
    no ruling lines, 18pt rows with 10pt text, left-aligned cells). If `first_line` is given, its cell texts are written in
    place of the header row."""
    edges, row_height, top = [34.0, 66.0, 116.0, 159.0, 191.0], 18.0, 780.0
    rows = [first_line or ["번호", "지점", "금액", "상태"], ["1", "서울지점", "71,433", "보류"], ["2", "부산지점", "76,233", "완료"],
            ["3", "대구지점", "72,292", "보류"], ["4", "인천지점", "12,141", "진행"], ["5", "광주지점", "8,312", "완료"]]

    def draw(pdf, index):
        from reportlab.lib import colors

        pdf.setFont(_FONT, 10)
        pdf.drawString(34, top + 22, "표 3. 지점별 실적")
        pdf.setFillColor(colors.HexColor("#EEEEEE"))
        for row in (2, 4):
            for x0, x1 in zip(edges, edges[1:], strict=False):
                pdf.rect(x0, top - (row + 1) * row_height, x1 - x0, row_height, stroke=0, fill=1)
        pdf.setFillColor(colors.black)
        for row, cells in enumerate(rows):
            for x0, text in zip(edges, cells, strict=False):
                pdf.drawString(x0 + 7.5, top - row * row_height - 12, text)
        pdf.drawString(34, top - len(rows) * row_height - 24, "끝.")

    return _korean_pdf(draw)


class TestZebraTableUnderAnUnshadedHeaderAndFirstRow:
    """Striped table whose header row and first data row are both unshaded. Only one open band was accepted upwards, so
    when the header row followed above the first row at line spacing, neither the first row nor the header row was
    accepted — the header row was scattered and the first row became a mixed-up paragraph, worse than with no shading."""

    def test_unshaded_header_and_first_row_stay_in_the_table(self):
        text = convert(_tbody_zebra_pdf(), "a.pdf", ConvertOptions(page_markers=False)).text

        assert text == ("표 3. 지점별 실적\n\n| 번호 | 지점 | 금액 | 상태 |\n| --- | --- | --- | --- |\n| 1 | 서울지점 | 71,433 | 보류 |\n"
                        "| 2 | 부산지점 | 76,233 | 완료 |\n| 3 | 대구지점 | 72,292 | 보류 |\n| 4 | 인천지점 | 12,141 | 진행 |\n"
                        "| 5 | 광주지점 | 8,312 | 완료 |\n\n끝.")

    def test_line_with_a_number_above_the_first_row_is_not_taken_for_a_header(self):
        """If the band above the first row does not look like a header row (it has a numeric cell), it is not taken as one."""
        text = convert(_tbody_zebra_pdf(["0", "본점", "80,000", "보류"]), "a.pdf", ConvertOptions(page_markers=False)).text

        assert "| 번호" not in text and text.count("71,433") == 1
