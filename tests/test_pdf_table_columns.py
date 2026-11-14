"""Columns of borderless PDF tables: rows where pdfminer merged cells across a narrow gap into one piece, and the header row of a table continued
on the next page."""

import pytest

from opendockit import ConvertOptions, convert
from tests.test_pdf import _FONT, _korean_pdf


def _table_pdf(rows, widths, style=(), pages=1, above="부서별 인원 현황", below="이상."):
    """A PDF with a reportlab table (no rules) drawn on it. With narrow column widths the gap between cells is only 6pt + 6pt."""
    def draw(pdf, index):
        from reportlab.platypus import Table, TableStyle

        pdf.setFont(_FONT, 10)
        pdf.drawString(72, 780, above)
        table = Table(rows, colWidths=widths)
        table.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), _FONT, 10), *style]))
        _, height = table.wrapOn(pdf, 0, 0)
        table.drawOn(pdf, 72, 760 - height)
        pdf.drawString(72, 740 - height, below)

    return _korean_pdf(draw, pages=pages)


class TestNarrowColumnGaps:
    """In a table whose gaps between cells are about one font size (12pt), pdfminer (char_margin 2.0) merged neighbouring cells into one piece
    ('120 118', '정원 현원 결원 비고'), so numbers went into the wrong column and the header row came loose from the table."""

    _ROWS = [["부서", "정원", "현원", "결원", "비고"], ["경영지원본부", "45", "42", "3", "채용 진행 중"],
             ["연구개발본부", "120", "118", "2", "-"], ["생산본부", "310", "298", "12", "하반기 공채"],
             ["영업본부", "85", "85", "0", "-"]]

    def test_cells_merged_into_one_piece_across_a_narrow_column_gap_go_to_their_own_columns(self):
        data = _table_pdf(self._ROWS, [90, 32, 32, 32, 120], [("ALIGN", (1, 1), (3, -1), "RIGHT")])

        result = convert(data, "a.pdf", ConvertOptions(page_markers=False))

        assert ("| 부서 | 정원 | 현원 | 결원 | 비고 |\n| --- | --- | --- | --- | --- |\n"
                "| 경영지원본부 | 45 | 42 | 3 | 채용 진행 중 |\n| 연구개발본부 | 120 | 118 | 2 | - |\n"
                "| 생산본부 | 310 | 298 | 12 | 하반기 공채 |\n| 영업본부 | 85 | 85 | 0 | - |") in result.text

    def test_amount_and_note_merged_into_one_piece_are_split_into_their_columns(self):
        """A left-aligned note right after a right-aligned amount leaves a 12pt gap between cells, so '4,890,000 육아휴직' became one piece."""
        rows = [["사번", "성명", "부서", "기본급", "비고"], ["E1000", "정우성", "개발팀", "4,890,000", "육아휴직"],
                ["E1001", "이영희", "재무팀", "2,810,000", "파견"], ["E1002", "정우성", "재무팀", "5,790,000", ""],
                ["E1003", "최수진", "개발팀", "4,880,000", "파견"]]
        data = _table_pdf(rows, [50, 55, 60, 80, 60], [("ALIGN", (3, 1), (3, -1), "RIGHT")])

        result = convert(data, "a.pdf", ConvertOptions(page_markers=False))

        assert "| E1000 | 정우성 | 개발팀 | 4,890,000 | 육아휴직 |" in result.text
        assert "| E1002 | 정우성 | 재무팀 | 5,790,000 | |" in result.text

    def test_right_aligned_numbers_next_to_a_left_aligned_note_column(self):
        """Between a right-aligned number cell and a left-aligned '비고' (note) cell of a Chrome table there is only the cell padding (0.75x the font
        size, no space character), so '18 2023 신규' became one cell and the header became the single cell '2024 비고'."""
        rows = [("지역", "2021", "2022", "2023", "2024", "비고"), ("서울", "120", "135", "", "160", "2023 미집계"),
                ("부산", "", "45", "52", "60", ""), ("대구", "30", "", "", "41", "사업 중단"), ("인천", "55", "58", "61", "", ""),
                ("광주", "", "", "12", "18", "2023 신규")]

        def draw(pdf, index):
            pdf.drawString(60, 790, "지역별 사업 실적 (단위: 건)")
            for number, (region, *years, note) in enumerate(rows):
                top = 760 - number * 20
                pdf.drawString(60, top, region)
                for right, value in zip((180, 250, 320, 390), years, strict=True):
                    pdf.drawRightString(right, top, value)
                pdf.drawString(397.5, top, note)

        text = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False)).text

        assert "| 지역 | 2021 | 2022 | 2023 | 2024 | 비고 |" in text
        assert "| 서울 | 120 | 135 | | 160 | 2023 미집계 |" in text and "| 광주 | | | 12 | 18 | 2023 신규 |" in text

    def test_justified_prose_with_wide_word_spaces_is_left_alone(self):
        """Short lines whose word gaps were stretched wider than the font size by justification (two narrow columns) are not split between words
        even when they sit right above a table. Splitting would make every word a cell, turning the lines into the table's header row or into a
        table."""
        lines = [["정부는", "이번", "정책을", "통해"], ["지역", "경제를", "살리고", "새로운"], ["일자리를", "늘리며", "예산도", "더"],
                 ["늘린다는", "계획을", "오늘", "밝혔다"]]

        def draw(pdf, index):
            from reportlab.platypus import Table, TableStyle

            y = 780.0
            for words in lines:
                for left in (72, 300):
                    width = sum(pdf.stringWidth(word, _FONT, 10) for word in words)
                    space = (135 - width) / (len(words) - 1)  # justified to a 135pt width: word gaps open up beyond 1em
                    x = float(left)
                    for word in words:
                        pdf.drawString(x, y, word)
                        x += pdf.stringWidth(word, _FONT, 10) + space
                y -= 14
            table = Table(self._ROWS, colWidths=[90, 32, 32, 32, 120])
            table.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), _FONT, 10), ("ALIGN", (1, 1), (3, -1), "RIGHT")]))
            _, height = table.wrapOn(pdf, 0, 0)
            table.drawOn(pdf, 72, y - height)

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert "| 부서 | 정원 | 현원 | 결원 | 비고 |" in result.text
        assert "| 연구개발본부 | 120 | 118 | 2 | - |" in result.text
        for words in lines:
            assert result.text.count(" ".join(words)) == 2

    def test_wide_word_space_inside_one_cell_is_not_a_new_column(self):
        """A cell whose word gap was stretched by justification inside the cell ('루멘 추천   (성능이') does not create a column that exists only in
        that row."""
        def draw(pdf, index):
            columns = [72, 190, 300, 410]
            rows = [["루멘 추천", "레이 추천", "최고 비주얼", "동적인 씬"], ["라이트맵 없이", "고사양 PC", "레이트 활성", "훌륭한 조명"],
                    ["동적인 씬", "하이엔드", "좋습니다", "구현합니다"], ["할 경우", "비주얼", "권장", "가능"]]
            for row_index, row in enumerate(rows):
                y = 760 - row_index * 16
                for column, value in zip(columns, row, strict=True):
                    # the ideographic space (U+3000) in the first cell plays the word gap stretched by justification (gap 12.5pt, 1.25x the font size)
                    pdf.drawString(column, y, "루멘 추천 \u3000(성능이" if value == "루멘 추천" else value)

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert "| 루멘 추천 (성능이 | 레이 추천 | 최고 비주얼 | 동적인 씬 |" in result.text


def _long_table_pdf(ruled: bool, repeat_header: bool = False, rows: int = 70) -> bytes:
    """A table spanning several pages (SimpleDocTemplate breaks the pages). Without `repeat_header` there is no header row from the second page on."""
    from io import BytesIO

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Table, TableStyle

    pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
    names = ["김민수", "이영희", "박지훈", "최수진", "정우성"]
    data = [["사번", "성명", "부서", "입사일", "기본급"]]
    data += [[f"E{1000 + index}", names[index % 5], ["영업팀", "개발팀", "인사팀"][index % 3], f"20{10 + index % 15}.0{index % 9 + 1}.01",
              f"{(280 + index * 7) * 10000:,}"] for index in range(rows)]
    style = [("FONT", (0, 0), (-1, -1), _FONT, 9), ("ALIGN", (4, 1), (4, -1), "RIGHT")]
    style.append(("GRID", (0, 0), (-1, -1), 0.5, colors.black) if ruled else ("LINEBELOW", (0, 0), (-1, 0), 0.8, colors.black))
    table = Table(data, colWidths=[60, 60, 60, 80, 90], repeatRows=1 if repeat_header else 0)
    table.setStyle(TableStyle(style))
    buffer = BytesIO()
    SimpleDocTemplate(buffer, pagesize=A4).build([Paragraph("임직원 급여 기준표", ParagraphStyle("t", fontName=_FONT, fontSize=14)),
                                                 table])
    return buffer.getvalue()


class TestTableContinuedOnTheNextPage:
    """A table continued on the next page without repeating its header got the first data row of the second page as its Markdown header. A table
    that starts at the very top of a page with nothing in between and has the same columns (count and horizontal positions) as the table at the
    bottom of the previous page reuses that table's header."""

    def test_header_cell_on_two_lines_is_measured_without_its_line_break(self):
        """The length bound of a header cell (`_HEADER_CELL_MAX_CHARS`, line breaks excluded) counted the line break of a
        ruled cell's text, so a 30-character header cell set on two lines was too long and the header was not carried."""
        columns, height = [60.0, 120.0, 330.0, 420.0], 30.0

        def row(pdf, top, cells):
            pdf.line(columns[0], top, columns[-1], top)
            pdf.line(columns[0], top - height, columns[-1], top - height)
            for x in columns:
                pdf.line(x, top, x, top - height)
            for x, text in zip(columns, cells, strict=False):
                for line, part in enumerate(text.split("\n")):
                    pdf.drawString(x + 5, top - 12 - line * 11, part)

        def draw(pdf, index):
            top = 780.0
            rows = ([["번호", "구분\n(unit: KRW thousand, FY2024)", "비고"], *([str(n), f"항목 {n}", "확인"] for n in range(1, 5))]
                    if index == 0 else [[str(n), f"항목 {n}", "확인"] for n in range(5, 8)])
            for cells in rows:
                row(pdf, top, cells)
                top -= height

        sections = convert(_korean_pdf(draw, pages=2), "a.pdf", ConvertOptions(page_markers=False)).sections

        assert sections[1].text.startswith("| 번호 | 구분<br>(unit: KRW thousand, FY2024) | 비고 |\n| --- | --- | --- |\n"
                                           "| 5 | 항목 5 | 확인 |")

    @pytest.mark.parametrize("ruled", [True, False], ids=["ruled", "borderless"])
    def test_table_continued_without_its_header_reuses_the_header_of_the_previous_page(self, ruled):
        result = convert(_long_table_pdf(ruled), "a.pdf")

        assert len(result.sections) >= 2
        for section in result.sections:
            assert section.text.count("| 사번 | 성명 | 부서 | 입사일 | 기본급 |\n| --- |") == 1, section.text[:300]
        assert "E1069" in result.sections[-1].text

    @pytest.mark.parametrize("ruled", [True, False], ids=["ruled", "borderless"])
    def test_table_that_repeats_its_header_is_left_as_it_is(self, ruled):
        result = convert(_long_table_pdf(ruled, repeat_header=True), "a.pdf")

        for section in result.sections:
            assert section.text.count("사번") == 1

    def test_new_table_with_its_own_header_at_the_top_of_the_next_page_keeps_its_header(self):
        """When a different table with the same column layout starts at the top of the next page with its own header, the previous table's header
        is not put on top of it."""
        def draw(pdf, index):
            from reportlab.lib import colors
            from reportlab.platypus import Table, TableStyle

            header = ["구분", "상반기", "하반기"] if index == 0 else ["항목", "1분기", "2분기"]
            rows = [header, *[[f"항목{index}-{row}", f"{row * 120:,}", f"{row * 95:,}"] for row in range(1, 30)]]
            table = Table(rows, colWidths=[120, 80, 80])
            table.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), _FONT, 10), ("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
            _, height = table.wrapOn(pdf, 0, 0)
            table.drawOn(pdf, 72, 70 if index == 0 else 800 - height)

        result = convert(_korean_pdf(draw, pages=2), "a.pdf")

        assert "| 항목 | 1분기 | 2분기 |\n| --- |" in result.sections[1].text
        assert "상반기" not in result.sections[1].text

    def test_page_filling_frames_of_slides_do_not_get_the_previous_slide_as_a_header(self):
        """Frame boxes that fill the page on every slide (a layout detected as a ruled table) are not a continued table even when their column
        layout matches. The previous page's body text was put on top as a header."""
        def draw(pdf, index):
            from reportlab.lib import colors
            from reportlab.platypus import Table, TableStyle

            body = f"{index + 1}장 배운 내용\n지금까지 저희는 스터디에서 여러 가지를 배웠습니다\n- 소스 코드 저장과 협업 관리"
            rows = [[body, ""], [f"{index + 1}장 정리 내용입니다", f"{index + 1}장 화면 설명입니다"]]
            table = Table(rows, colWidths=[300, 150], rowHeights=[380, 380])
            table.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), _FONT, 10), ("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
            table.wrapOn(pdf, 0, 0)
            table.drawOn(pdf, 72, 40)

        result = convert(_korean_pdf(draw, pages=3), "a.pdf", ConvertOptions(extract_images=False))

        assert "1장 배운 내용" not in result.sections[1].text
        assert "2장 배운 내용" not in result.sections[2].text

    def test_slide_frames_repeating_their_section_title_down_the_page_do_not_get_a_header(self):
        """A slide frame whose first cell (the section title) is merged vertically, so that the same text also appears in the data rows, does not
        carry the header over."""
        def draw(pdf, index):
            from reportlab.lib import colors
            from reportlab.platypus import Table, TableStyle

            section = "01. 기업이 도입에 실패하는 이유" if index < 2 else "02. 도입 전략 수립 방법"
            rows = [[section, "", f"{index + 1}쪽 제목 질문입니다"], ["", "", "용어 설명"], ["", "", f"{index + 1}쪽 본문 설명"]]
            table = Table(rows, colWidths=[150, 20, 280], rowHeights=[60, 300, 380])
            table.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), _FONT, 10), ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                                       ("SPAN", (0, 0), (0, -1))]))
            table.wrapOn(pdf, 0, 0)
            table.drawOn(pdf, 72, 40)

        result = convert(_korean_pdf(draw, pages=3), "a.pdf", ConvertOptions(extract_images=False))

        assert "1쪽 제목 질문입니다" not in result.sections[1].text
        assert "2쪽 제목 질문입니다" not in result.sections[2].text


def _row_split_at_the_page_end_pdf(repeat_header: bool = False) -> bytes:
    """A row split at the page end as in Chrome printing: the last row of page 1 has only vertical lines and no bottom border (the first part of
    the cell text), and the first row of page 2 has no top border (the rest of the long cell text). With `repeat_header` the header row is repeated
    on page 2 (thead)."""
    columns = [40, 70, 130, 470, 540]
    header = ["번호", "과제명", "추진 내용", "예산"]

    def row(pdf, top, height, cells, closed_top=True, closed_bottom=True):
        if closed_top:
            pdf.line(columns[0], top, columns[-1], top)
        if closed_bottom:
            pdf.line(columns[0], top - height, columns[-1], top - height)
        for x in columns:
            pdf.line(x, top, x, top - height)
        for x, lines in zip(columns, cells, strict=False):
            for number, text in enumerate(lines.split("\n") if lines else ()):
                pdf.drawString(x + 4, top - 14 - number * 14, text)

    def draw(pdf, index):
        if index == 0:
            pdf.drawString(40, 800, "과제별 추진 내용")
            top = 790.0
            row(pdf, top, 20, header)
            top -= 20
            for number in range(1, 5):
                row(pdf, top, 20, [str(number), f"과제 {number}", "후속 조치 및 성과 점검", f"{number * 50}"])
                top -= 20
            row(pdf, top, 48, ["5", "과제 5-가", "지역 거점 기관과 협력하여 수요 조사를\n실시하고 그 결과를 바탕으로", "500"],
                closed_bottom=False)
        else:
            top = 800.0
            if repeat_header:
                row(pdf, top, 20, header)
                top -= 20
            row(pdf, top, 34, ["", "", "맞춤형 지원 방안을 마련한다.\n끝까지 추진한다.", ""], closed_top=repeat_header)
            top -= 34
            for number in range(6, 9):
                row(pdf, top, 20, [str(number), f"과제 {number}", "후속 조치 및 성과 점검", f"{number * 50}"])
                top -= 20

    return _korean_pdf(draw, pages=2)


def _ruled_table(pdf, rows: list[list[str]], top: float, widths: list[float]) -> None:
    """A ruled table drawn downwards from `top` (row height 20pt, 10pt text)."""
    from reportlab.lib import colors
    from reportlab.platypus import Table, TableStyle

    table = Table(rows, colWidths=widths, rowHeights=20)
    table.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), _FONT, 10), ("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
    _, height = table.wrapOn(pdf, 0, 0)
    table.drawOn(pdf, 60, top - height)


class TestHeaderCarriedOverWithYearsAndForms:
    """Mistakes from treating year/date cells as numeric values when deciding the header of a continued table, and from taking the first row of a
    label/value form grid as a header."""

    @pytest.mark.parametrize("header", [["과목", "주석", "2024.12.31", "2023.12.31"], ["과목", "주석", "2024", "2023"]])
    def test_header_with_years_or_dates_is_carried_to_the_next_page(self, header):
        """The header '과목 | 주석 | 2024.12.31 | 2023.12.31' of a financial statement looked like numeric values, so the first data row of the second
        page became the header."""
        def draw(pdf, index):
            if index == 0:
                rows = [header, *[[f"계정{row}", str(row), f"{row * 1234:,}", f"{row * 1111:,}"] for row in range(1, 37)]]
                _ruled_table(pdf, rows, 800, [170, 60, 120, 125])
            else:
                rows = [[f"계정{row}", str(row), f"{row * 987:,}", f"{row * 876:,}"] for row in range(40, 50)]
                _ruled_table(pdf, rows, 800, [170, 60, 120, 125])
                pdf.drawString(60, 560, "※ 위 금액은 백만원 단위입니다.")

        result = convert(_korean_pdf(draw, pages=2), "a.pdf")

        assert f"| {' | '.join(header)} |\n| --- |" in result.sections[1].text
        assert "| 계정40 | 40 |" in result.sections[1].text

    def test_table_with_its_own_year_header_at_the_top_of_the_next_page_keeps_it(self):
        """When a different table at the top of the next page starts with '성과 지표 | 2023 | 2024 | 2025', the previous table's text header is not
        put on top of it."""
        def draw(pdf, index):
            if index == 0:
                rows = [["구분", "담당 부서", "추진 내용", "비고"], *[[f"과제{row}", "기획조정실", "제도 개선 검토", "계속"]
                                                             for row in range(1, 37)]]
                _ruled_table(pdf, rows, 800, [120, 120, 120, 115])
            else:
                rows = [["성과 지표", "2023", "2024", "2025"], ["이용자 수", "1,200", "1,500", "1,900"],
                        ["만족도", "78.5", "81.2", "84.0"], ["처리 건수", "3,400", "4,100", "5,000"]]
                _ruled_table(pdf, rows, 800, [120, 120, 120, 115])
                pdf.drawString(60, 700, "※ 2025년은 목표치임")

        result = convert(_korean_pdf(draw, pages=2), "a.pdf")

        assert "| 성과 지표 | 2023 | 2024 | 2025 |\n| --- |" in result.sections[1].text
        assert "담당 부서" not in result.sections[1].text

    def test_first_row_of_a_label_value_form_grid_is_not_carried_to_the_next_page(self):
        """In an application form filled with a 'label | value' ruled grid on every page, the first row of page 1 ('성명 | 홍길동') was put on the
        page 2 table as its header."""
        def draw(pdf, index):
            if index == 0:
                rows = [["성명", "홍길동"], ["생년월일", "1990년 1월 1일"], ["연락처", "010-1234-5678"],
                        ["주소", "서울특별시 종로구 세종대로 1"], *[[f"학력 {row}", f"한국대학교 {row}학년 수료"] for row in range(1, 33)]]
            else:
                rows = [["경력 회사명", "(주)가나다 소프트"], ["근무 기간", "2015년 3월 ~ 2020년 2월"], ["담당 업무", "백엔드 개발"],
                        ["자격증", "정보처리기사"], ["어학", "TOEIC 900"]]
            _ruled_table(pdf, rows, 800, [120, 355])

        result = convert(_korean_pdf(draw, pages=2), "a.pdf")

        assert "홍길동" not in result.sections[1].text
        assert result.sections[1].text.lstrip().startswith("| 경력 회사명 | (주)가나다 소프트 |")

    def test_two_column_table_of_amounts_still_gets_its_header_on_the_next_page(self):
        """A two-column table of amounts ('항목 | 금액') still carries its header over as before."""
        def draw(pdf, index):
            start = 1 if index == 0 else 40
            rows = [[f"항목{row}", f"{row * 1234:,}"] for row in range(start, start + 36 - index * 26)]
            _ruled_table(pdf, [["항목", "금액"], *rows] if index == 0 else rows, 800, [200, 200])

        result = convert(_korean_pdf(draw, pages=2), "a.pdf")

        assert "| 항목 | 금액 |\n| --- |" in result.sections[1].text


class TestCentredHeadersOverLeftAlignedCells:
    """Narrow centred headers over wide columns with left-aligned cell text. The centre of the cell text lies left of where the header starts, so
    the text went one column too early and the row collapsed like '손익 매출액 | |', or the header row came loose from the table."""

    def test_left_aligned_items_under_a_centred_header_stay_in_its_column(self):
        rows = [["구분", "항목", "2023", "2024"], ["손익", "매출액", "1,456", "1,690"], ["", "영업이익", "120", "(15)"],
                ["", "당기순이익", "88", "△32"], ["재무상태", "자산총계", "2,350", "2,410"]]
        style = [("ALIGN", (0, 0), (-1, 0), "CENTER"), ("ALIGN", (2, 1), (-1, -1), "RIGHT")]

        result = convert(_table_pdf(rows, [100, 140, 70, 70], style, above="연도별 재무 요약"), "a.pdf",
                         ConvertOptions(page_markers=False))

        assert ("| 구분 | 항목 | 2023 | 2024 |\n| --- | --- | --- | --- |\n| 손익 | 매출액 | 1,456 | 1,690 |\n"
                "| | 영업이익 | 120 | (15) |\n| | 당기순이익 | 88 | △32 |\n| 재무상태 | 자산총계 | 2,350 | 2,410 |") in result.text

    def test_header_row_of_centred_titles_stays_with_its_table(self):
        rows = [["부서", "담당 업무", "담당자", "연락처"], ["경영지원팀", "예산 편성 및 집행 관리", "김민수", "내선 1201"],
                ["정보화팀", "전산 장비 유지보수 총괄", "이영희", "내선 1302"], ["홍보팀", "보도자료 작성 및 배포", "박지훈", "내선 1403"]]

        result = convert(_table_pdf(rows, [100, 170, 100, 100], [("ALIGN", (0, 0), (-1, 0), "CENTER")], above="부서별 담당 업무"),
                         "a.pdf", ConvertOptions(page_markers=False))

        assert ("| 부서 | 담당 업무 | 담당자 | 연락처 |\n| --- | --- | --- | --- |\n"
                "| 경영지원팀 | 예산 편성 및 집행 관리 | 김민수 | 내선 1201 |") in result.text

    def test_left_aligned_note_numbers_under_a_centred_header_stay_in_its_column(self):
        """The '주석' (note) column of a financial statement: left-aligned numbers ('4,5', '6') under a narrow centred header lie left of where the
        header starts, so they went into the account column; the row fell outside the table's criteria, the table split in two, and the second
        table got a data row as its header."""
        rows = [["과목", "주석", "제 12 기", "제 11 기"], ["자산", "", "", ""],
                ["I. 유동자산", "", "8,234,567,890", "7,012,345,678"], ["현금및현금성자산", "4,5", "1,234,567,890", "987,654,321"],
                ["매출채권", "6", "3,500,000,000", "3,100,000,000"], ["대손충당금", "", "(35,000,000)", "(31,000,000)"],
                ["재고자산", "7", "3,535,000,000", "2,955,691,357"], ["매입채무", "10", "3,000,000,000", "2,900,000,000"]]
        style = [("ALIGN", (1, 0), (-1, 0), "CENTER"), ("ALIGN", (2, 1), (-1, -1), "RIGHT")]

        result = convert(_table_pdf(rows, [150, 90, 110, 110], style, above="재무상태표"), "a.pdf",
                         ConvertOptions(page_markers=False))

        assert ("| 과목 | 주석 | 제 12 기 | 제 11 기 |\n| --- | --- | --- | --- |\n| 자산 | | | |\n"
                "| I. 유동자산 | | 8,234,567,890 | 7,012,345,678 |\n| 현금및현금성자산 | 4,5 | 1,234,567,890 | 987,654,321 |\n"
                "| 매출채권 | 6 | 3,500,000,000 | 3,100,000,000 |\n| 대손충당금 | | (35,000,000) | (31,000,000) |\n"
                "| 재고자산 | 7 | 3,535,000,000 | 2,955,691,357 |\n| 매입채무 | 10 | 3,000,000,000 | 2,900,000,000 |") in result.text

    @pytest.mark.parametrize("ruled", [True, False])
    @pytest.mark.parametrize("notes", [("확정", "변경", "미정"), ("", "변경", "")])
    def test_cells_in_the_gaps_before_every_centred_header(self, ruled, notes):
        """Left-aligned cell text that sits only in the gaps before the centred headers ('내용', '비고') (Chrome-printed tables with horizontal rules,
        borderless tables). Short text of the '비고' column was counted into the '내용' column by header position alone, which left the '내용' cell
        text in the '구분' column, and short cell text overlapped the first column's text because its centre was closer to the header on its left —
        the header row dropped out as a paragraph and, when there were empty cells, the table scattered column by column."""
        rows = [["구분", "내용", "비고"], ["일시", "2026. 5. 12.(화) 14:00~16:00", notes[0]],
                ["장소", "정부서울청사 별관 3층 회의실", notes[1]], ["참석", "관계부처 국장급 12명", notes[2]]]
        style = [("ALIGN", (0, 0), (-1, 0), "CENTER")]
        if ruled:
            style += [("LINEABOVE", (0, 0), (-1, -1), 0.5, (0, 0, 0)), ("LINEBELOW", (0, -1), (-1, -1), 0.5, (0, 0, 0))]

        result = convert(_table_pdf(rows, [50, 360, 110], style, above="본문 문단이 표 앞에 옵니다.", below="끝."), "a.pdf",
                         ConvertOptions(page_markers=False))

        body = "\n".join(f"| {row[0]} | {row[1]} | {row[2]} |".replace("|  |", "| |") for row in rows[1:])
        assert result.text == f"본문 문단이 표 앞에 옵니다.\n\n| 구분 | 내용 | 비고 |\n| --- | --- | --- |\n{body}\n\n끝."

    def test_wrapped_line_of_a_cell_in_the_gap_before_a_centred_header_stays_in_the_cell(self):
        """A wrapped line of cell text ('회의실') that sits in the gap before a header is also in the first column by header position, so it did not
        join the cell above; the table broke off and the rows below scattered into paragraphs (a table with horizontal rules drawn by LibreOffice).
        The column of a wrapped line is the column of the cell above that it overlaps horizontally."""
        placed = [(78, [(90, "회의 개요는 다음과 같다.")]), (111, [(151, "구분"), (295, "내용"), (439, "비고")]),
                  (142, [(95.5, "일시"), (239.5, "2026. 5. 12.(화)")]), (158, [(239.5, "14:00~16:00")]),
                  (188.5, [(95.5, "장소"), (239.5, "정부서울청사 별관 3층 대"), (400, "변경 가능")]), (210, [(239.5, "회의실")]),
                  (241, [(95.5, "참석"), (239.5, "관계부처 국장급 12명")]), (272, [(90, "끝.")])]

        def draw(pdf, index):
            pdf.setFont(_FONT, 11)
            for top, cells in placed:
                for left, text in cells:
                    pdf.drawString(left, 832.5 - top, text)

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert ("| 구분 | 내용 | 비고 |\n| --- | --- | --- |\n| 일시 | 2026. 5. 12.(화)<br>14:00~16:00 | |\n"
                "| 장소 | 정부서울청사 별관 3층 대<br>회의실 | 변경 가능 |\n| 참석 | 관계부처 국장급 12명 | |\n\n끝.") in result.text


class TestRowSplitAcrossPages:
    """The next-page piece of a row split at the page end is a top row with only one cell filled, so it was taken for a table title and became a
    paragraph (the long '상세 내용' cell of a requirements specification). The top row of a ruled table continued from the previous page is a title
    only when it is a horizontally merged cell (`merged_titles_only` of `markdown.render_table`)."""

    @pytest.mark.parametrize("rows_before", [1, 36])
    def test_part_of_a_row_carried_to_the_next_page_stays_a_table_row(self, rows_before):
        header = ["ID", "요구사항명", "상세 내용", "우선순위"]

        def draw(pdf, index):
            if index == 0:
                rows = [header, *[[f"REQ-{row:03d}", f"기능 {row}", "조회 화면에서 조건을 입력한다", "상"]
                                  for row in range(1, rows_before + 1)]]
                pdf.drawString(60, 810, "요구사항 목록")
                _ruled_table(pdf, rows, 800 - (36 - rows_before) * 20, [90, 90, 200, 95])
            else:
                rows = [["", "", "있어야 한다.", ""], ["REQ-100", "기능 100", "결과를 내려받을 수 있어야 한다", "중"],
                        ["REQ-101", "기능 101", "이력을 조회할 수 있어야 한다", "하"]]
                _ruled_table(pdf, rows, 800, [90, 90, 200, 95])

        result = convert(_korean_pdf(draw, pages=2), "a.pdf")

        second = result.sections[1].text
        assert any(line.startswith("|") and "있어야 한다." in line for line in second.splitlines())
        assert "| REQ-100 | 기능 100 |" in second

    @pytest.mark.parametrize("repeat_header", [False, True])
    def test_row_whose_cells_are_all_open_at_the_page_end_stays_in_the_table(self, repeat_header):
        """Chrome printing draws every cell of a row split at the page end without a bottom border. That row failed to become cells, so its first
        part ('5 과제 5-가 …') dropped out of the table as a paragraph, and the rest on the next page, having no top border, became a paragraph too,
        so the header was not carried over to the next page's table."""
        sections = convert(_row_split_at_the_page_end_pdf(repeat_header), "a.pdf", ConvertOptions(page_markers=False)).sections

        assert sections[0].text.endswith("| 4 | 과제 4 | 후속 조치 및 성과 점검 | 200 |\n"
                                         "| 5 | 과제 5-가 | 지역 거점 기관과 협력하여 수요 조사를<br>실시하고 그 결과를 바탕으로 | 500 |")
        assert sections[1].text == ("| 번호 | 과제명 | 추진 내용 | 예산 |\n| --- | --- | --- | --- |\n"
                                    "| | | 맞춤형 지원 방안을 마련한다.<br>끝까지 추진한다. | |\n"
                                    "| 6 | 과제 6 | 후속 조치 및 성과 점검 | 300 |\n| 7 | 과제 7 | 후속 조치 및 성과 점검 | 350 |\n"
                                    "| 8 | 과제 8 | 후속 조치 및 성과 점검 | 400 |")

    def test_merged_top_row_of_a_table_continued_on_the_next_page_is_still_a_title(self):
        """A top row made of a single horizontally merged cell is still a paragraph above the table (a title), as before."""
        from reportlab.lib import colors
        from reportlab.platypus import Table, TableStyle

        def draw(pdf, index):
            if index == 0:
                rows = [["ID", "요구사항명", "상세 내용", "우선순위"], ["REQ-001", "기능 1", "조회 화면", "상"]]
                _ruled_table(pdf, rows, 80, [90, 90, 200, 95])
            else:
                table = Table([["2. 비기능 요구사항", "", "", ""], ["REQ-100", "기능 100", "응답 시간 3초 이내", "중"],
                               ["REQ-101", "기능 101", "동시 사용자 500명", "하"]], colWidths=[90, 90, 200, 95], rowHeights=20)
                table.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), _FONT, 10), ("SPAN", (0, 0), (-1, 0)),
                                           ("GRID", (0, 0), (-1, -1), 0.5, colors.black)]))
                table.wrapOn(pdf, 0, 0)
                table.drawOn(pdf, 60, 740)

        result = convert(_korean_pdf(draw, pages=2), "a.pdf")

        assert "2. 비기능 요구사항\n\n| REQ-100 |" in result.sections[1].text


def _grid(pdf, top: float, height: float, columns: list[float], rows: list[list[str]]) -> float:
    """A table ruled downwards from `top` with the same column edges (`columns`) on every row. Returns the bottom edge."""
    for index in range(len(rows) + 1):
        pdf.line(columns[0], top - index * height, columns[-1], top - index * height)
    for index, row in enumerate(rows):
        for x in columns:
            pdf.line(x, top - index * height, x, top - (index + 1) * height)
        for x, value in zip(columns, row, strict=False):
            pdf.drawString(x + 5, top - (index + 1) * height + 8, value)
    return top - len(rows) * height


class TestAbuttingTablesWithDifferentColumns:
    """When two ruled tables with different column structures abut (the '경비 내역' table right below the '인적 사항' grid of an application form),
    pdfplumber read them as one grid with both tables' column edges merged, so empty columns crept in and the lower table's header became a data
    row. Row groups (at least 2 rows each) whose column edges do not contain each other are split into two tables."""

    personal = [["성명", "김철수", "소속", "재무팀"], ["직급", "과장", "연락처", "010-1234-5678"],
                ["출장지", "부산 해운대", "기간", "2025.10.13~10.15"]]
    costs = [["구분", "내역", "단가", "수량", "금액", "비고"], ["교통비", "KTX 왕복", "59,800", "2", "119,600", "법인카드"],
             ["숙박비", "호텔 2박", "120,000", "2", "240,000", ""], ["식비", "일비", "25,000", "3", "75,000", "현금"]]

    def test_two_abutting_tables_with_different_column_edges_are_two_tables(self):
        def draw(pdf, index):
            pdf.drawString(60, 790, "출장 신청서")
            bottom = _grid(pdf, 760, 24, [60, 130, 300, 370, 535], self.personal)
            bottom = _grid(pdf, bottom, 24, [60, 120, 230, 300, 360, 440, 535], self.costs)
            pdf.drawString(60, bottom - 30, "위와 같이 출장을 신청합니다.")

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "| 성명 | 김철수 | 소속 | 재무팀 |\n| --- | --- | --- | --- |\n| 직급 | 과장 |" in result.text
        assert "| 구분 | 내역 | 단가 | 수량 | 금액 | 비고 |\n| --- | --- | --- | --- | --- | --- |\n| 교통비 |" in result.text

    def test_table_with_a_merged_header_row_is_not_split(self):
        """A table whose top header row groups columns (its edges are a subset of those of the rows below) is one table."""
        def draw(pdf, index):
            bottom = _grid(pdf, 760, 24, [60, 300, 535], [["상반기", "하반기"]])
            _grid(pdf, bottom, 24, [60, 180, 300, 420, 535], [["1분기", "2분기", "3분기", "4분기"], ["10", "20", "30", "40"],
                                                            ["11", "21", "31", "41"]])

        result = convert(_korean_pdf(draw), "a.pdf")

        assert sum(1 for line in result.text.splitlines() if line.startswith("| ---")) == 1
        assert "| 상반기 1분기 | 상반기 2분기 | 하반기 3분기 | 하반기 4분기 |" in result.text

    def test_merged_cells_at_the_outer_edges_keep_one_table(self):
        """The right column merged over data rows 1-3 and the left column over rows 3-5: every row has the same outer
        boundaries, so the table is not split in two and the left merged cell ('채소') is kept on its rows."""
        owner = [["h0", "h1", "h2", "h3"], ["a", "b", "c", "N"], ["d", "e", "f", "N"], ["V", "g", "i", "N"],
                 ["V", "j", "k", "l"], ["V", "m", "n", "o"]]
        text = {"h0": "구분", "h1": "품목", "h2": "금액", "h3": "비고", "a": "과일", "b": "사과", "c": "1,000", "N": "공통 비고",
                "d": "과일", "e": "배", "f": "2,000", "V": "채소", "g": "오이", "i": "3,000", "j": "당근", "k": "4,000",
                "l": "국산", "m": "무", "n": "5,000", "o": "수입"}
        xs = [60, 160, 260, 360, 460]
        ys = [740 - 18 * index for index in range(7)]

        def draw(pdf, index):
            pdf.drawString(72, 780, "본문 단락입니다.")
            for row in range(7):
                for column in range(4):
                    if 0 < row < 6 and owner[row - 1][column] == owner[row][column]:
                        continue
                    pdf.line(xs[column], ys[row], xs[column + 1], ys[row])
            for column in range(5):
                for row in range(6):
                    if 0 < column < 4 and owner[row][column - 1] == owner[row][column]:
                        continue
                    pdf.line(xs[column], ys[row], xs[column], ys[row + 1])
            for key, value in text.items():
                places = [(row, column) for row in range(6) for column in range(4) if owner[row][column] == key]
                first, last = places[0][0], places[-1][0]
                pdf.drawString(xs[places[0][1]] + 4, (ys[first] + ys[last + 1]) / 2 - 3, value)
            pdf.drawString(72, ys[-1] - 40, "표 아래 본문입니다.")

        result = convert(_korean_pdf(draw), "a.pdf")

        assert sum(1 for line in result.text.splitlines() if line.startswith("| ---")) == 1
        assert "| 채소 | 오이 | 3,000 | 공통 비고 |\n| 채소 | 당근 | 4,000 | 국산 |\n| 채소 | 무 | 5,000 | 수입 |" in result.text


def _three_rule_statement(header_lines: list[tuple[float, list[tuple[float, str]]]], header_height: float = 30.0) -> bytes:
    """A three-rule table (financial statement) with horizontal rules only at the top, below the header and at the bottom. `header_lines` is a list
    of (distance from the top rule to the baseline, [(centre x, text)]) — a one-line cell beside two-line cells sits at their vertical centre. Data
    cells are left-aligned in the first column and right-aligned in the rest."""
    rows = [["Ⅰ. 매출액", "23", "152,340", "138,920", "13,420"], ["Ⅱ. 매출원가", "24", "(118,210)", "(106,450)", "(11,760)"],
            ["Ⅲ. 매출총이익", "", "34,130", "32,470", "1,660"], ["Ⅳ. 영업이익", "", "12,260", "12,360", "(100)"]]
    rights = [0, 270, 360, 445, 525]

    def draw(pdf, index):
        pdf.setFont(_FONT, 16)
        pdf.drawCentredString(297, 760, "손익계산서")
        pdf.setFont(_FONT, 10)
        pdf.drawRightString(530, 726, "(단위: 백만원)")
        top = 722.0
        pdf.line(65, top, 530, top)
        pdf.line(65, top - header_height, 530, top - header_height)
        pdf.setFont(_FONT, 9)
        for offset, line in header_lines:
            for centre, text in line:
                pdf.drawCentredString(centre, top - offset, text)
        y = top - header_height - 15
        for row in rows:
            pdf.drawString(71, y, row[0])
            for right, text in zip(rights[1:], row[1:], strict=True):
                pdf.drawRightString(right, y, text)
            y -= 17
        pdf.line(65, y + 10, 530, y + 10)
        pdf.drawString(65, y - 8, "※ 괄호 안의 금액은 음수를 뜻한다.")

    return _korean_pdf(draw)


class TestTwoLineHeaderBetweenRules:
    """The two-line header of a three-rule table ('2025년 / (제12기)') has a different height on each line, so it became separate paragraphs and the
    first data row became the header."""

    def test_header_lines_between_the_top_rules_become_one_header_row(self):
        data = _three_rule_statement([(12, [(330, "2025년"), (415, "2024년")]),
                                      (18, [(153, "과목"), (260, "주석"), (490, "증감")]),
                                      (24, [(330, "(제12기)"), (415, "(제11기)")])])

        result = convert(data, "a.pdf", ConvertOptions(page_markers=False))

        assert ("| 과목 | 주석 | 2025년 (제12기) | 2024년 (제11기) | 증감 |\n| --- | --- | --- | --- | --- |\n"
                "| Ⅰ. 매출액 | 23 | 152,340 | 138,920 | 13,420 |") in result.text

    def test_one_line_header_between_rules_is_unchanged(self):
        data = _three_rule_statement([(13, [(153, "과목"), (260, "주석"), (330, "2025년"), (415, "2024년"), (490, "증감")])],
                                     header_height=18)

        result = convert(data, "a.pdf", ConvertOptions(page_markers=False))

        assert ("| 과목 | 주석 | 2025년 | 2024년 | 증감 |\n| --- | --- | --- | --- | --- |\n"
                "| Ⅰ. 매출액 | 23 | 152,340 | 138,920 | 13,420 |") in result.text


def _grouped_header_statement(group_texts: tuple[str, str] = ("2023년", "2024년"), full_middle_rule: bool = True) -> bytes:
    """A three-rule table (rules at the top, below the header and at the bottom) with a two-tier header: beside '구분' (centred over the height of two
    lines), '2023년' and '2024년' each span the two columns '남성·여성'. Without `full_middle_rule` the rule below the header skips the '구분' column
    (Chrome's border for a rowspan cell)."""
    edges = [80.0, 160.0, 245.0, 330.0, 415.0, 500.0]
    rows = [("20대", "45.2", "51.0", "47.8", "53.3"), ("30대", "38.1", "40.4", "39.9", "42.0"), ("40대", "29.7", "31.2", "30.5", "33.6"),
            ("50대 이상", "15.4", "18.9", "17.0", "20.1")]

    def draw(pdf, index):
        def centre(column):
            return (edges[column] + edges[column + 1]) / 2

        pdf.setFont(_FONT, 10)
        pdf.drawString(80, 760, "〈표 2〉 연령대별 이용률 (단위: %)")
        top = 745.0
        pdf.line(edges[0], top, edges[-1], top)
        pdf.line(edges[0] if full_middle_rule else edges[1], top - 38, edges[-1], top - 38)
        pdf.drawCentredString(centre(0), top - 23, "구분")
        pdf.drawCentredString(edges[2], top - 13, group_texts[0])
        pdf.drawCentredString(edges[4], top - 13, group_texts[1])
        for column, text in zip(range(1, 5), ("남성", "여성", "남성", "여성"), strict=True):
            pdf.drawCentredString(centre(column), top - 32, text)
        y = top - 52
        for row in rows:
            for column, text in enumerate(row):
                pdf.drawCentredString(centre(column), y, text)
            y -= 19
        pdf.line(edges[0], y + 12, edges[-1], y + 12)
        pdf.drawString(80, y - 6, "자료: 통계청, 「2024 디지털 이용 실태조사」")

    return _korean_pdf(draw)


class TestGroupedHeaderBetweenRules:
    """In the two-tier header of a three-rule table ('2023년' spanning the two columns '남성·여성'), the upper cell does not overlap the cells below
    horizontally, so it was not joined to each of them. The header became separate paragraphs and the first data row ('20대') became the header."""

    _TABLE = ("| 구분 | 2023년 남성 | 2023년 여성 | 2024년 남성 | 2024년 여성 |\n| --- | --- | --- | --- | --- |\n"
              "| 20대 | 45.2 | 51.0 | 47.8 | 53.3 |")

    def test_group_header_over_two_columns_is_joined_to_each_of_them(self):
        text = convert(_grouped_header_statement(), "a.pdf", ConvertOptions(page_markers=False)).text

        assert self._TABLE in text
        assert "| 50대 이상 | 15.4 | 18.9 | 17.0 | 20.1 |" in text

    def test_middle_rule_that_skips_the_first_column_still_closes_the_header(self):
        text = convert(_grouped_header_statement(full_middle_rule=False), "a.pdf", ConvertOptions(page_markers=False)).text

        assert self._TABLE in text

    def test_group_header_wider_than_the_columns_below_is_joined_to_each_of_them(self):
        text = convert(_grouped_header_statement(("2023년 남녀 이용률", "2024년 남녀 이용률")), "a.pdf",
                       ConvertOptions(page_markers=False)).text

        assert "| 구분 | 2023년 남녀 이용률 남성 | 2023년 남녀 이용률 여성 | 2024년 남녀 이용률 남성 | 2024년 남녀 이용률 여성 |" in text


def _gapped_table(gap: float, rows: list[tuple[str, ...]]) -> str:
    """Page text of a borderless table (below a title) whose columns are spaced by the longest cell text + `gap` pt. An empty cell is an empty
    string."""
    def draw(pdf, index):
        pdf.setFont(_FONT, 12)
        pdf.drawString(50, 782, "가. 주요 성과")
        pdf.setFont(_FONT, 10)
        widths = [max(pdf.stringWidth(row[column], _FONT, 10) for row in rows) + gap for column in range(len(rows[0]))]
        y = 752.0
        for row in rows:
            x = 50.0
            for width, text in zip(widths, row, strict=True):
                pdf.drawString(x, y, text)
                x += width
            y -= 16

    return convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False)).text


class TestBorderlessTableWithNarrowGaps:
    """In a borderless table whose column gaps are 1 to 1.6 times the font size, pdfminer (char_margin 2.0) merged neighbouring cells into one
    piece, so the number of pieces differed from row to row and the table was not found. With empty cells the text boxes were grouped column by
    column and the rows got mixed up ('김철수 100 / 95 / 95.0% / 고객 … 80 / …')."""

    _ROWS = [("과제명", "담당", "목표", "실적", "달성률"), ("차세대 ERP 구축", "김철수", "100", "95", "95.0%"),
             ("고객 데이터 플랫폼", "이영희", "80", "", "-"), ("AI 상담 챗봇 도입", "박민수", "60", "70", "116.7%"),
             ("전자결재 개편", "", "40", "40", "100.0%")]

    def test_table_with_empty_cells_and_narrow_gaps_keeps_its_rows(self):
        text = _gapped_table(16, self._ROWS)

        assert ("| 과제명 | 담당 | 목표 | 실적 | 달성률 |\n| --- | --- | --- | --- | --- |\n| 차세대 ERP 구축 | 김철수 | 100 | 95 | 95.0% |\n"
                "| 고객 데이터 플랫폼 | 이영희 | 80 | | - |\n| AI 상담 챗봇 도입 | 박민수 | 60 | 70 | 116.7% |\n"
                "| 전자결재 개편 | | 40 | 40 | 100.0% |") in text

    def test_full_table_with_narrow_gaps_is_a_table(self):
        rows = [*self._ROWS[:2], ("고객 데이터 플랫폼", "이영희", "80", "88", "110.0%"), self._ROWS[3],
                ("전자결재 개편", "최지훈", "40", "40", "100.0%")]

        text = _gapped_table(12, rows)

        assert "| 과제명 | 담당 | 목표 | 실적 | 달성률 |" in text and "| 전자결재 개편 | 최지훈 | 40 | 40 | 100.0% |" in text

    def test_table_with_wide_gaps_is_unchanged(self):
        assert "| 전자결재 개편 | | 40 | 40 | 100.0% |" in _gapped_table(20, self._ROWS)

    def test_cells_a_little_apart_without_space_glyphs_are_split(self):
        """Chrome leaves the cell padding (0.75x the font size) empty, without a space character. Between words inside a cell there is a space
        character."""
        text = _gapped_table(7, self._ROWS)

        assert "| 고객 데이터 플랫폼 | 이영희 | 80 | | - |" in text and "| 전자결재 개편 | | 40 | 40 | 100.0% |" in text

    def test_first_cell_wrapped_beside_vertically_centred_cells_stays_in_its_row(self):
        """When the first cell has two lines, the vertically centred text of the other cells sits at a height between the first cell's two lines,
        which made three rows by top coordinate."""
        rows = [(("과제명",), "담당", "목표", "실적"), (("차세대 ERP 구축", "(1단계 분석 설계)"), "김철수", "100", "95"),
                (("고객 데이터 플랫폼",), "이영희", "80", "88"), (("AI 상담 챗봇 도입 및", "콜센터 연계"), "박민수", "60", ""),
                (("전자결재 개편",), "", "40", "40")]

        def draw(pdf, index):
            pdf.setFont(_FONT, 12)
            pdf.drawString(50, 782, "가. 주요 성과")
            pdf.setFont(_FONT, 10)
            top = 760.0
            for first, *cells in rows:
                height = 6 + 14 * len(first)
                for number, text in enumerate(first):
                    pdf.drawString(50, top - 13 - number * 14, text)
                for x, text in zip((160, 220, 270), cells, strict=True):
                    pdf.drawString(x, top - height / 2 - 4, text)
                top -= height

        text = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False)).text

        assert ("| 과제명 | 담당 | 목표 | 실적 |\n| --- | --- | --- | --- |\n| 차세대 ERP 구축<br>(1단계 분석 설계) | 김철수 | 100 | 95 |\n"
                "| 고객 데이터 플랫폼 | 이영희 | 80 | 88 |\n| AI 상담 챗봇 도입 및<br>콜센터 연계 | 박민수 | 60 | |\n"
                "| 전자결재 개편 | | 40 | 40 |") in text
        assert text.count("콜센터 연계") == 1


def _group_split_across_pages_pdf(tall: bool = False) -> bytes:
    """A two-page PDF in which the '그룹' cell of a ruled table's first column (4 rows merged vertically) is split at the page end: page 1 has 2 rows
    without the cell's bottom border, and page 2 repeats the header and then has 2 rows with an empty cell that has no top border (rowspan in
    Chrome printing). With `tall` the cell text on page 1 has four lines and overflows the bottom edge of the table (Chrome draws all of a merged
    cell's text on the first page)."""
    columns, height = [60.0, 160.0, 260.0, 360.0], 22.0

    def row_lines(pdf, top, cells, first_open=False, last_open=False):
        pdf.line(columns[1] if first_open else columns[0], top, columns[-1], top)
        pdf.line(columns[1] if last_open else columns[0], top - height, columns[-1], top - height)
        for x in columns:
            pdf.line(x, top, x, top - height)
        for x, text in zip(columns, cells, strict=False):
            if text:
                pdf.drawString(x + 5, top - 15, text)

    def draw(pdf, index):
        top = 780.0
        row_lines(pdf, top, ["그룹", "과제", "예산"])
        top -= height
        if index == 0:
            for number in range(1, 5):
                row_lines(pdf, top, ["그룹1 담당: 팀1" if number == 1 else "", f"과제 {number}", f"{number * 1111:,}"],
                          first_open=number > 1, last_open=number < 4)
                top -= height
            for number in (5, 6):
                row_lines(pdf, top, ["그룹2 담당: 팀2" if number == 5 and not tall else "", f"과제 {number}",
                                     f"{number * 1111:,}"], first_open=number > 5, last_open=True)
                if tall and number == 5:
                    for line, text in enumerate(("그룹2", "담당:", "팀2", "(공통)")):
                        pdf.drawString(columns[0] + 5, top - 15 - line * 14, text)
                top -= height
        else:
            for number in (7, 8):
                row_lines(pdf, top, ["", f"과제 {number}", f"{number * 1111:,}"], first_open=True, last_open=number < 8)
                top -= height
            for number in (9, 10, 11):
                row_lines(pdf, top, ["그룹3 담당: 팀3" if number == 9 else "", f"과제 {number}", f"{number * 1111:,}"],
                          first_open=number > 9, last_open=number < 11)
                top -= height

    return _korean_pdf(draw, pages=2)


class TestMergedCellOpenAtThePageEnd:
    """A vertically merged cell split at the page end (the first column, '그룹') has no bottom border on that page, so it failed to become a cell.
    Its text ('그룹2 담당: 팀2') dropped out of the table as a paragraph and the first cell of those rows was empty."""

    def test_open_merged_cell_at_the_bottom_of_the_page_keeps_its_text_in_the_table(self):
        page = convert(_group_split_across_pages_pdf(), "a.pdf", ConvertOptions(page_markers=False)).sections[0].text

        assert "| 그룹2 담당: 팀2 | 과제 5 | 5,555 |\n| 그룹2 담당: 팀2 | 과제 6 | 6,666 |" in page
        assert page.count("그룹2") == 2

    def test_rows_of_the_cell_continued_on_the_next_page_get_its_text(self):
        page = convert(_group_split_across_pages_pdf(), "a.pdf", ConvertOptions(page_markers=False)).sections[1].text

        assert "| 그룹2 담당: 팀2 | 과제 7 | 7,777 |\n| 그룹2 담당: 팀2 | 과제 8 | 8,888 |" in page
        assert "| 그룹3 담당: 팀3 | 과제 9 | 9,999 |" in page

    def test_text_of_the_open_cell_running_below_the_table_stays_whole_in_the_cell(self):
        """When the merged cell's text is longer than the last rows of the page and overflows the bottom edge of the table, the added bottom line
        cut the text: the cell held only the first part and the rest ('팀2 (공통)') became a paragraph outside the table."""
        sections = convert(_group_split_across_pages_pdf(tall=True), "a.pdf", ConvertOptions(page_markers=False)).sections

        cell = "그룹2<br>담당:<br>팀2<br>(공통)"
        assert f"| {cell} | 과제 5 | 5,555 |\n| {cell} | 과제 6 | 6,666 |" in sections[0].text
        assert sections[0].text.count("팀2") == 2
        assert f"| {cell} | 과제 7 | 7,777 |" in sections[1].text

    @pytest.mark.parametrize(("values", "repeated"), [("dates", False), ("dates", True), ("pending", False), ("pending", True),
                                                      ("amounts", False)])
    def test_rows_continued_without_a_header_get_the_open_cell_whatever_their_values(self, values, repeated):
        """With a date column ('2024.07.15') or a first row whose value is text ('미정') the first rows of page 2 looked like
        a header of their own, and the rest of the merged cell lost its text. Without a repeated header the top row of page 2
        also showed its first cell empty: the top row of a continued ruled table was passed with every cell lacking a top
        border as a covered cell, which dropped the text carried into it."""
        remaining = 2
        columns, height = [60.0, 160.0, 260.0, 360.0], 22.0

        def value(number):
            return (f"2024.{number:02d}.15" if values == "dates" else "미정" if number == 7 and values == "pending"
                    else f"{number * 1111:,}")

        def row_lines(pdf, top, cells, first_open=False, last_open=False):
            pdf.line(columns[1] if first_open else columns[0], top, columns[-1], top)
            pdf.line(columns[1] if last_open else columns[0], top - height, columns[-1], top - height)
            for x in columns:
                pdf.line(x, top, x, top - height)
            for x, text in zip(columns, cells, strict=False):
                if text:
                    pdf.drawString(x + 5, top - 15, text)

        def draw(pdf, index):
            top = 780.0
            if index == 0 or repeated:
                row_lines(pdf, top, ["그룹", "과제", "기한"])
                top -= height
            if index == 0:
                for number in range(1, 5):
                    row_lines(pdf, top, ["그룹1 담당: 팀1" if number == 1 else "", f"과제 {number}", value(number)],
                              first_open=number > 1, last_open=number < 4)
                    top -= height
                for number in (5, 6):
                    row_lines(pdf, top, ["그룹2 담당: 팀2" if number == 5 else "", f"과제 {number}", value(number)],
                              first_open=number > 5, last_open=True)
                    top -= height
            else:
                rest = list(range(7, 7 + remaining))
                for number in rest:
                    row_lines(pdf, top, ["", f"과제 {number}", value(number)], first_open=True, last_open=number < rest[-1])
                    top -= height
                for number in range(rest[-1] + 1, rest[-1] + 4):
                    row_lines(pdf, top, ["그룹3 담당: 팀3" if number == rest[-1] + 1 else "", f"과제 {number}", value(number)],
                              first_open=number > rest[-1] + 1, last_open=number < rest[-1] + 3)
                    top -= height

        page = convert(_korean_pdf(draw, pages=2), "a.pdf", ConvertOptions(page_markers=False)).sections[1].text

        for number in range(7, 7 + remaining):
            assert f"| 그룹2 담당: 팀2 | 과제 {number} | {value(number)} |" in page
        assert f"| 그룹3 담당: 팀3 | 과제 {7 + remaining} | {value(7 + remaining)} |" in page

    def test_new_table_with_its_own_header_does_not_get_the_open_cell(self):
        """A different table with the same columns at the top of the next page, starting with its own header (no numbers,
        numbers in the row below) and a blank corner cell, got the open cell's text '그룹2 담당: 팀2' in that corner."""
        columns, height = [60.0, 160.0, 260.0, 360.0], 22.0

        def row_lines(pdf, top, cells, first_open=False, last_open=False):
            pdf.line(columns[1] if first_open else columns[0], top, columns[-1], top)
            pdf.line(columns[1] if last_open else columns[0], top - height, columns[-1], top - height)
            for x in columns:
                pdf.line(x, top, x, top - height)
            for x, text in zip(columns, cells, strict=False):
                if text:
                    pdf.drawString(x + 5, top - 15, text)

        def draw(pdf, index):
            top = 780.0
            if index == 0:
                row_lines(pdf, top, ["그룹", "과제", "예산"])
                top -= height
                for number in range(1, 5):
                    row_lines(pdf, top, ["그룹1 담당: 팀1" if number == 1 else "", f"과제 {number}", f"{number * 1111:,}"],
                              first_open=number > 1, last_open=number < 4)
                    top -= height
                for number in (5, 6):
                    row_lines(pdf, top, ["그룹2 담당: 팀2" if number == 5 else "", f"과제 {number}", f"{number * 1111:,}"],
                              first_open=number > 5, last_open=True)
                    top -= height
            else:
                for cells in (["", "지출 항목", "금액"], ["교통", "택시", "12,000"], ["식비", "점심", "9,000"],
                              ["숙박", "호텔", "80,000"]):
                    row_lines(pdf, top, cells)
                    top -= height

        sections = convert(_korean_pdf(draw, pages=2), "a.pdf", ConvertOptions(page_markers=False)).sections

        assert sections[1].text.startswith("| | 지출 항목 | 금액 |\n| --- | --- | --- |\n| 교통 | 택시 | 12,000 |")
        assert "그룹2" not in sections[1].text


def _wide_first_column_pdf(centred: bool = False) -> bytes:
    """The shape of a Chrome borderless table: the '과제명' cell, wider than 1/3 of the page width, wraps to two lines (line pitch 15.3pt — the gap is
    a little wider than half the line height; the gap between rows is 10.6pt), and the gap between the right-aligned amount and the ratio cell is
    5pt (0.5x the font size, no space character). With `centred` the other cells sit at the vertical centre of the two-line cell."""
    rows = [(("차세대 통합 정보시스템 구축 및 데이터", "이관 사업"), "정보화팀", "1,250", "65%"),
            (("민원 처리",), "민원팀", "300", "90%"),
            (("재난 안전 통신망 연계 고도화 및 노후 장", "비 교체"), "안전관리과", "2,100", "30%"),
            (("홈페이지 개편",), "홍보팀", "150", "100%")]

    def draw(pdf, index):
        pdf.setFont(_FONT, 12)
        pdf.drawString(60, 790, "주요 과제별 추진 현황")
        pdf.setFont(_FONT, 10)
        top = 760.0
        for x, title in [(60, "과제명"), (290, "담당부서"), (480, "진척률")]:
            pdf.drawString(x, top, title)
        pdf.drawRightString(470, top, "예산(백만원)")
        top -= 20.6
        for first, team, amount, ratio in rows:
            for number, text in enumerate(first):
                pdf.drawString(60, top - number * 15.3, text)
            middle = top - (len(first) - 1) * 7.65 if centred else top
            pdf.drawString(290, middle, team)
            pdf.drawRightString(470, middle, amount)
            pdf.drawString(475, middle, ratio)
            top -= 20.6 + (len(first) - 1) * 15.3

    return _korean_pdf(draw)


class TestWideWrappedFirstColumn:
    """A Chrome borderless table whose first column, 45% of the page width, wraps to two lines was cut at every row with a wide cell, and the rows
    got mixed column by column. The wrapped line became a row of its own because its gap differed from the gap between rows by less than 0.6x the
    line height, and the right-aligned amount and the ratio became one cell ('1,250 65%')."""

    @pytest.mark.parametrize("centred", [False, True])
    def test_wrapped_wide_first_cells_stay_in_their_rows(self, centred):
        text = convert(_wide_first_column_pdf(centred), "a.pdf", ConvertOptions(page_markers=False)).text

        assert ("| 과제명 | 담당부서 | 예산(백만원) | 진척률 |\n| --- | --- | --- | --- |\n"
                "| 차세대 통합 정보시스템 구축 및 데이터<br>이관 사업 | 정보화팀 | 1,250 | 65% |\n"
                "| 민원 처리 | 민원팀 | 300 | 90% |\n"
                "| 재난 안전 통신망 연계 고도화 및 노후 장<br>비 교체 | 안전관리과 | 2,100 | 30% |\n"
                "| 홈페이지 개편 | 홍보팀 | 150 | 100% |") in text

    def test_wide_line_crossing_into_the_next_column_does_not_join_the_table(self):
        """A wide cell is accepted only when it fits inside its own column. A body line that reaches into the next header cell is a paragraph below
        the table."""
        def draw(pdf, index):
            for row, top in enumerate((760, 740, 720, 700)):
                for x, text in [(60, f"항목{row}"), (290, f"부서{row}"), (420, f"{row * 100}")]:
                    pdf.drawString(x, top, text)
            pdf.drawString(60, 680, "이 문장은 표 아래에 이어 쓴 본문으로 다음 열의 머리글 자리까지 길게 뻗어 나간다")
            pdf.drawString(290, 680, "끝")

        text = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False)).text

        assert "| 항목3 | 부서3 | 300 |" in text and "| 이 문장은" not in text


class TestBorderlessTableWithBlankCorner:
    """In a borderless table whose first-column header is left blank (visitors per quarter), the header has one piece fewer than the data rows, so
    the header dropped out as one paragraph per cell and the first data row became the header."""

    _ROWS = [("온라인", "12,300", "15,800", "14,200", "18,900"), ("오프라인", "8,100", "7,600", "9,300", "10,200"),
             ("모바일 앱", "20,500", "22,100", "25,700", "27,400"), ("합계", "40,900", "45,500", "49,200", "56,500")]

    def _pdf(self, header, header_lefts=None):
        def draw(pdf, index):
            pdf.drawString(72, 790, "분기별 방문자 수")
            for number, (right, title) in enumerate(zip((220, 292, 364, 436), header, strict=True)):
                if header_lefts:
                    pdf.drawString(header_lefts[number], 760, title)
                else:
                    pdf.drawRightString(right, 760, title)
            for number, (name, *values) in enumerate(self._ROWS):
                pdf.drawString(80, 736 - number * 24, name)
                for right, value in zip((220, 292, 364, 436), values, strict=True):
                    pdf.drawRightString(right, 736 - number * 24, value)
            pdf.drawString(72, 620, "모바일 앱 방문자가 꾸준히 증가하였다.")

        return _korean_pdf(draw)

    def test_header_row_without_its_first_cell_stays_the_header(self):
        text = convert(self._pdf(("1분기", "2분기", "3분기", "4분기")), "a.pdf", ConvertOptions(page_markers=False)).text

        assert ("| | 1분기 | 2분기 | 3분기 | 4분기 |\n| --- | --- | --- | --- | --- |\n| 온라인 | 12,300 | 15,800 | 14,200 | 18,900 |"
                in text)
        assert "| 합계 | 40,900 | 45,500 | 49,200 | 56,500 |" in text

    def test_row_above_out_of_line_with_the_columns_is_not_a_header(self):
        """A row above that is not lined up with the columns is not attached as a header (the first data row is the header, as before)."""
        pdf = self._pdf(("가", "나", "다", "라"), header_lefts=(140, 228, 300, 380))

        text = convert(pdf, "a.pdf", ConvertOptions(page_markers=False)).text

        assert "| 온라인 | 12,300 | 15,800 | 14,200 | 18,900 |\n| --- |" in text


class TestBorderlessTwoLineHeader:
    """When the header cells of a borderless table have two lines ('판매량 / (대)', '전년 동기 / 대비 증감률'), the second line became the first data
    row, producing a '| | (대) | (백만원) |' row and leaving only half of each column name."""

    _COLUMNS = [72, 170, 260, 350, 450]
    _HEADS = [["지역"], ["판매량", "(대)"], ["매출액", "(백만원)"], ["전년 동기", "대비 증감률"], ["시장", "점유율"]]
    _DATA = [("서울", "1,230", "45,600", "+12.3%", "31.2%"), ("경기", "980", "35,100", "+8.1%", "24.9%"),
             ("부산", "450", "16,200", "-2.4%", "11.4%"), ("대구", "310", "11,000", "+0.5%", "7.9%")]

    def _pdf(self, header_pitch: float, first_row_gap: float) -> bytes:
        def draw(pdf, index):
            pdf.drawString(72, 790, "2024년 상반기 지역별 판매 실적은 아래와 같다.")
            y = 760.0
            for x, lines in zip(self._COLUMNS, self._HEADS, strict=True):
                for number, text in enumerate(lines):
                    pdf.drawString(x, y - number * header_pitch, text)
            y -= first_row_gap
            for row in self._DATA:
                for x, text in zip(self._COLUMNS, row, strict=True):
                    pdf.drawString(x, y, text)
                y -= 16
            pdf.drawString(72, y - 10, "※ 점유율은 당사 추정치임")

        return _korean_pdf(draw)

    def test_second_header_line_joins_the_header_cells(self):
        result = convert(self._pdf(12, 32), "a.pdf", ConvertOptions(page_markers=False))

        lines = result.text.splitlines()
        assert "| 지역 | 판매량 (대) | 매출액 (백만원) | 전년 동기 대비 증감률 | 시장 점유율 |" in lines
        assert "| 서울 | 1,230 | 45,600 | +12.3% | 31.2% |" in lines

    def test_row_at_the_same_pitch_as_the_data_rows_stays_a_row(self):
        """A line below the header at the same pitch as the data rows is a row, not a wrapped header line (unchanged from before)."""
        result = convert(self._pdf(16, 32), "a.pdf", ConvertOptions(page_markers=False))

        assert "| 지역 | 판매량 | 매출액 | 전년 동기 | 시장 |" in result.text.splitlines()


class TestRowsOfASplitTableAreNotTakenAgain:
    """The lines of a table found by splitting narrow cell gaps (`_split_line_table_runs`) are split-off pieces, so the original lines were missing
    from the list of lines already used. When the table right below widened upwards it took those rows again, and '기여도 | 8700 | 판매량' appeared
    twice, in both tables (LibreOffice PDF)."""

    def test_rows_of_the_table_above_appear_once(self):
        above = [("구분", "수량", "비고"), ("판매량가나다라", "8020", "재생용기"), ("기여도가나다라", "8700", "판매량"),
                 ("체계가나다라마", "2580", "효과")]
        below = [("원료", "5", "반영"), ("제품", "7", "투입량"), ("결과", "9", "검토"), ("방법", "2", "시장")]

        def draw(pdf, index):
            pdf.drawString(60, 790, "현황")
            for number, row in enumerate(above + below):
                for x, text in zip((60, 140, 175), row, strict=True):
                    pdf.drawString(x, 760 - number * 20, text)
            pdf.drawString(60, 580, "이상.")

        text = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False)).text

        assert text.count("기여도가나다라") == 1 and text.count("체계가나다라마") == 1
        assert "| 체계가나다라마 | 2580 | 효과 |" in text and "| 원료 | 5 | 반영 |" in text


class TestOneLetterHeaderCell:
    """In a Chrome three-rule table the gap between the cells of '값 | 단위' is 1.3em, so pdfminer merges them into the single piece '값 단위'. Merely
    because a one-letter piece would result, the line was taken for letter-spaced text ('구    분') and not split, so the header became a paragraph
    outside the table and the first data row became the header."""

    def test_header_with_a_one_letter_cell_stays_the_header(self):
        rows = [("항목 이름", "값", "단위", "비고"), ("기여도 산정 재생용기", "31", "체계", "산식"), ("산정 결과 시장평균", "8", "kg", "보상"),
                ("감축 기준값 산정", "66", "방법", "산식"), ("판매량 감축", "95", "산식", "2023년")]

        def draw(pdf, index):
            pdf.drawString(60, 790, "표 1. 산정 항목")
            pdf.line(60, 775, 330, 775)
            pdf.line(60, 752, 330, 752)
            for number, row in enumerate(rows):
                for x, text in zip((62, 190, 213, 270), row, strict=True):
                    pdf.drawString(x, 760 - number * 20 - (4 if number else 0), text)
            pdf.line(60, 660, 330, 660)
            pdf.drawString(60, 640, "이상.")

        text = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False)).text

        assert "| 항목 이름 | 값 | 단위 | 비고 |\n| --- | --- | --- | --- |\n| 기여도 산정 재생용기 | 31 | 체계 | 산식 |" in text


class TestHeaderMergedWiderThanACell:
    """When the gaps between the header cells of a borderless table are narrower than two characters, pdfminer merges '1분기 2분기 3분기 4분기' into
    one line. That line exceeded the maximum table cell width (25% of the page width) and was not split, so the header became a paragraph outside the
    table and the first data row moved up into the header's place (19 of 42 Chrome borderless and three-rule tables). The line is split when every
    piece is within the maximum cell width (the same criterion as `_split_line_table_runs`)."""

    _NAMES = [("연도", "효과"), ("재생", "사업"), ("안전", "재생"), ("절감", "사업"), ("달성", "효과")]

    def _pdf(self, header: list[str]) -> bytes:
        def draw(pdf, index):
            xs = [89, 342, 392, 442, 492]
            pdf.setFont(_FONT, 11)
            pdf.drawString(84, 700, "아래 표는 사업별 비용과 배출량, 감축량을 정리한 것이다.")
            pdf.setFont(_FONT, 12)
            for x, text in zip(xs, header, strict=True):
                pdf.drawString(x, 670, text)
            for row, (name, kind) in enumerate(self._NAMES, start=1):
                y = 670 - 19 * row
                pdf.setFont(_FONT, 12)
                pdf.drawString(xs[0], y, name)
                pdf.drawString(xs[1], y, kind)
                pdf.setFont("Helvetica", 12)
                for column, x in enumerate(xs[2:]):
                    pdf.drawString(x, y, f"{(row * 7 + column) * 1_111 + 10_000:,}")
            pdf.setFont(_FONT, 11)
            pdf.drawString(84, 670 - 19 * 6 - 30, "표 아래 문단이다.")

        return _korean_pdf(draw)

    def test_header_merged_across_narrow_column_gaps_stays_the_table_header(self):
        data = self._pdf(["구분", "1분기", "2분기", "3분기", "4분기"])

        text = convert(data, "a.pdf", ConvertOptions(page_markers=False)).text

        assert "| 구분 | 1분기 | 2분기 | 3분기 | 4분기 |\n| --- | --- | --- | --- | --- |\n| 연도 | 효과 | 17,777 |" in text
        assert text.startswith("아래 표는 사업별 비용과 배출량, 감축량을 정리한 것이다.\n\n| 구분 |")

    def test_header_whose_cells_are_already_apart_is_unchanged(self):
        data = self._pdf(["구분", "사업", "비용", "배출량", "감축"])

        text = convert(data, "a.pdf", ConvertOptions(page_markers=False)).text

        assert "| 구분 | 사업 | 비용 | 배출량 | 감축 |\n| --- | --- | --- | --- | --- |\n| 연도 | 효과 | 17,777 |" in text


class TestVeryWideBorderlessRows:
    """Faint rows of 1,000 one-point letters every 4pt form a borderless 'table' of 1,000 columns; looking up the column of
    each piece scanned every header cell, so the table step took time with cells × columns."""

    @staticmethod
    def _rows_pdf(rows: int) -> bytes:
        from tests.test_audit_pdf_head import _flate, _one_page

        parts = [b"BT /F1 1 Tf 0 g "]
        for piece in range(rows * 1000):
            parts.append(b"1 0 0 1 %d %d Tm (x) Tj " % (10 + (piece % 1000) * 4, 20 + (piece // 1000) * 3))
        parts.append(b"ET")
        return _one_page(_flate(b"".join(parts)), mediabox=b"[0 0 4600 600]")

    @pytest.mark.timing
    def test_column_lookups_of_a_very_wide_table_are_not_quadratic(self, monkeypatch):
        import time

        import opendockit.extractors.pdf as pdf_module

        extract, spent = pdf_module._extract_aligned_tables, [0.0]

        def timed(page):
            started = time.perf_counter()
            try:
                extract(page)
            finally:
                spent[0] += time.perf_counter() - started

        monkeypatch.setattr(pdf_module, "_extract_aligned_tables", timed)
        text = convert(self._rows_pdf(10), "a.pdf", ConvertOptions(max_chars=None)).text

        assert text.count("x") == 10_000
        assert spent[0] < 2.0  # 5 seconds before

    @pytest.mark.parametrize("seed", range(4))
    def test_the_header_index_gives_the_columns_of_the_scans(self, seed, monkeypatch):
        """`_HeaderIndex` against the scans it replaces, on random wide headers with many equal edges and distances (the
        first header at the smallest distance wins), overlapping cells and pieces in the gaps."""
        import random

        from opendockit.extractors._pdf_model import _Line
        import opendockit.extractors._pdf_table_rows as table_rows

        rng = random.Random(seed)

        def line(x0, width, text):
            return _Line(top=0.0, bottom=10.0, x0=x0, x1=x0 + width, text=text, size=10.0)

        def columns(header, pieces, row):
            return [(table_rows._header_column(piece, header), table_rows._gap_column(piece, header, row),
                     table_rows._column_of(piece, header, row)) for piece in pieces]

        for _ in range(150):
            edges = [rng.choice([0.0, 0.3, 2.5, 5.0, 7.5]) + 6 * rng.randint(0, 40) for _ in range(12)]
            header = [line(rng.choice(edges) if rng.random() < 0.5 else rng.uniform(0, 300),
                           rng.choice([0, 1, 2.5, 5, 30, rng.uniform(0, 80)]), rng.choice(["ab", "12"]))
                      for _ in range(rng.choice([17, 40, 120]))]
            pieces = [line(rng.choice(header).x0 + rng.choice([0, 2.5, -2.5, 1, 3, 0.1 + 0.2]) if rng.random() < 0.7
                           else rng.uniform(-10, 310), rng.choice([0, 1, 4, 20, rng.uniform(0, 100)]), rng.choice(["ab", "12", "가"]))
                      for _ in range(12)]
            row = sorted(rng.sample(pieces, rng.randint(0, len(pieces))), key=lambda piece: piece.x0)

            indexed = columns(header, pieces, row)
            monkeypatch.setattr(table_rows, "_INDEXED_HEADER_CELLS", 10_000)
            scanned = columns(header, pieces, row)
            monkeypatch.setattr(table_rows, "_INDEXED_HEADER_CELLS", 16)
            table_rows._drop_header_indexes()

            assert indexed == scanned
