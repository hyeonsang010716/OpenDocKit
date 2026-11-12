"""PDF defects from review round 46: a borderless table whose right-aligned numeric header was merged with the neighbouring cell and whose last
column has a value in only one row, a lone subtotal row in an otherwise empty last column, tables of two or three rows mixing a wrapped cell and a
wide cell, and a centred formula row whose cells wrap to different numbers of lines."""

import pytest

from opendockit import ConvertOptions, convert
from tests.test_pdf import _FONT, _korean_pdf


def _story_pdf(story) -> bytes:
    from io import BytesIO

    from reportlab.lib.pagesizes import A4
    from reportlab.platypus import SimpleDocTemplate

    buffer = BytesIO()
    SimpleDocTemplate(buffer, pagesize=A4).build(story)
    return buffer.getvalue()


def _style(size: float = 10, leading: float = 14, alignment: int = 0):
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
    return ParagraphStyle(f"k{size}{alignment}", fontName=_FONT, fontSize=size, leading=leading, alignment=alignment)


def _table(rows: list[list], widths: list[float], commands: list[tuple] = ()):
    from reportlab.platypus import Table, TableStyle

    table = Table(rows, colWidths=widths)
    table.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), _FONT, 9), *commands]))
    return table


def _text(story) -> str:
    return convert(_story_pdf(story), "a.pdf", ConvertOptions(page_markers=False)).text


def _cost_story(rows: list[list[str]], commands: list[tuple] = ()) -> list:
    """The heading '월별 비용', a borderless 5-column cost table (품목 | 1월 | 2월 | 3월 | 비고), a paragraph below the table and the next section."""
    from reportlab.platypus import Paragraph

    heading = _style(14, 18)
    body = _style()
    table = _table([["품목", "1월", "2월", "3월", "비고"], *rows], [80, 60, 60, 60, 100], commands)
    return [Paragraph("월별 비용", heading), table, Paragraph("비고란은 담당자가 작성한다.", body),
            Paragraph("다음 절", heading), Paragraph("본문.", body)]


_COSTS = [["원재료", "1,200", "1,150", "1,300", ""], ["부재료", "300", "340", "310", ""],
          ["포장재", "220", "210", "230", ""]]


class TestRightAlignedHeaderMergedWithANearlyEmptyColumn:
    """In a borderless table whose numeric columns are right-aligned including their headers, pdfminer merged '3월 비고' into one piece, and with a
    value in only one row of the 비고 (note) column the table ended above that row, so the values of 운송비 and 보험료 and '2월 미집행' scattered
    behind the body of the next section."""

    def test_rows_below_stay_in_the_table_with_their_values(self):
        rows = [*_COSTS, ["운송비", "400", "", "420", "2월 미집행"], ["보험료", "50", "50", "50", ""]]

        text = _text(_cost_story(rows, [("ALIGN", (1, 0), (3, -1), "RIGHT")]))

        assert "| 품목 | 1월 | 2월 | 3월 | 비고 |" in text
        assert "| 운송비 | 400 | | 420 | 2월 미집행 |" in text
        assert "| 보험료 | 50 | 50 | 50 | |" in text
        assert text.index("보험료") < text.index("비고란은") < text.index("다음 절")


class TestLoneSubtotalInAnOtherwiseEmptyLastColumn:
    """A row with a single subtotal in the last column (비고), which is empty in every other row, cut the table, so the values of the rows below and
    the subtotal scattered behind the next section (`_is_lone_value_row` of round 45 looked for a cell of the same column in the rows above and
    below, which always failed in an empty column)."""

    @pytest.mark.parametrize("lone", ["3,550", "소계 3,550"])
    @pytest.mark.parametrize("commands", [[], [("ALIGN", (1, 0), (3, -1), "RIGHT")]])
    def test_subtotal_row_keeps_the_table_whole(self, lone, commands):
        rows = [_COSTS[0], ["부재료", "300", "340", "310", "단가 인상"], _COSTS[2], ["", "", "", "", lone],
                ["운송비", "400", "410", "420", ""], ["보험료", "50", "50", "50", ""]]

        text = _text(_cost_story(rows, commands))

        assert f"| | | | | {lone} |" in text
        assert "| 운송비 | 400 | 410 | 420 | |" in text
        assert "| 보험료 | 50 | 50 | 50 | |" in text
        assert sum(line.startswith("| --- ") for line in text.split("\n")) == 1
        assert text.index("보험료") < text.index("비고란은") < text.index("다음 절")


_LONG_TASK = "고객 데이터 이관 및 정합성 검증 작업, 레거시 시스템 종료 포함"


def _task_story(tasks: list[str], valign: str) -> list:
    """A borderless 4-column task table (번호 | 과제명 | 담당 | 기한). The 과제명 cell is 250pt wide, so a long task name wraps to two lines."""
    from reportlab.platypus import Paragraph

    cell = _style(9, 11)
    owners, dues = ["박지영", "이준호", "최유나"], ["11/30", "12/15", "12/31"]
    rows = [["번호", "과제명", "담당", "기한"]]
    rows += [[str(number), Paragraph(task, cell), owners[number - 1], dues[number - 1]]
             for number, task in enumerate(tasks, 1)]
    table = _table(rows, [40, 250, 70, 60], [("VALIGN", (0, 0), (-1, -1), valign)])
    return [Paragraph("4분기 과제 목록", _style(14, 18)), table, Paragraph("위 일정은 변경될 수 있음.", _style())]


class TestSmallTableWithAWrappedCellAndAWideCell:
    """In a borderless task table of two or three rows, when the task name wraps to two lines in one row and exceeds 25% of the page width in
    another, the rows holding only short cells fell short of the minimum row count, so the table was not found and scattered column by column (the
    담당 and 기한 columns went behind the paragraph below the table)."""

    @pytest.mark.parametrize("valign", ["TOP", "MIDDLE", "BOTTOM"])
    def test_rows_keep_their_owner_and_due_date(self, valign):
        tasks = [_LONG_TASK, "모바일 앱 결제 모듈 개편", "사내 위키 문서 정리와 검색 기능 개선, 권한 체계 재설계"]

        text = _text(_task_story(tasks, valign))

        assert "| 번호 | 과제명 | 담당 | 기한 |" in text
        assert "| 2 | 모바일 앱 결제 모듈 개편 | 이준호 | 12/15 |" in text
        assert "| 3 | 사내 위키 문서 정리와 검색 기능 개선, 권한 체계 재설계 | 최유나 | 12/31 |" in text
        assert "박지영 | 11/30 |" in text
        assert text.index("최유나") < text.index("위 일정은")

    @pytest.mark.parametrize("valign", ["TOP", "MIDDLE", "BOTTOM"])
    def test_two_row_table_with_a_wrapped_first_row(self, valign):
        tasks = [_LONG_TASK, "모바일 앱 결제 모듈 개편"]

        text = _text(_task_story(tasks, valign))

        assert "| 2 | 모바일 앱 결제 모듈 개편 | 이준호 | 12/15 |" in text
        assert "박지영 | 11/30 |" in text
        assert text.index("이준호") < text.index("위 일정은")

    def test_item_lines_of_an_official_letter_stay_lines(self):
        """The item lines of an official letter ('가. 일    시  2024. …') are not a table even though the letter-spaced item name splits into two
        pieces and looks like three cells (they became a table when rows mixed with a wide value cell were counted toward the minimum row count)."""
        items = [("가. 일    시", "2024. 11. 12.(화) 14:00"), ("나. 장    소", "본사 대강당 (서울시 중구 세종대로 100)"),
                 ("다. 참 석 자", "협력업체 대표 또는 임원 1명"),
                 ("라. 신청 방법", "11. 5.(화)까지 이메일 회신 (partner@hansang.co.kr)")]

        def draw(pdf, index):
            pdf.drawString(60, 760, "2. 당사는 협력업체와의 상생 협력을 위하여 아래와 같이 간담회를 개최하오니 참석하여 주시기 바랍니다.")
            pdf.drawCentredString(297, 736, "- 아    래 -")
            for number, (label, value) in enumerate(items):
                pdf.drawString(80, 714 - number * 18, label)
                pdf.drawString(170, 714 - number * 18, value)
            pdf.drawString(60, 630, "붙임  1. 참가 신청서 1부.  끝.")

        text = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False)).text

        assert "| --- " not in text
        assert "나. 장 소 본사 대강당 (서울시 중구 세종대로 100)" in text

    def test_top_of_a_table_cut_at_a_row_merged_differently_is_not_taken(self):
        """In a chronology where only the first part forms a table, out of rows with a wide value cell plus one short row, when a row with two
        cells merged into one piece ('1688') follows, the part above it was taken as a table first, which blocked the path that splits the merged
        piece and finds the whole table again (in the real PDF the rows below 1688 came loose)."""
        rows = [("연도", "사건", "리스크 관리적 의의"),
                ("1750", "함무라비 해상 대부", "최초의 성문 조건부 계약, 리스크 분담의 법적 체계화"),
                ("1347", "제노바 해상보험 계약", "근대 보험의 효시, 상인 사이 위험 나눔의 제도화 시작"),
                ("1654", "확률론 서신 교환", "수학적 정량화 기반"),
                ("1688", "로이드 커피하우스 개업 그리고 설립과", "보험 시장의 탄생"),
                ("1929", "뉴욕 증시 대폭락", "금융 규제 재정립"), ("1933", "글래스-스티걸법", "국가적 관리 확립"),
                ("1952", "포트폴리오 이론", "수익과 위험의 관계")]

        def draw(pdf, index):
            pdf.drawString(62, 780, "아래 표는 주요 사건과 그 의의를 시계열로 정리한 것이다.")
            for number, row in enumerate(rows):
                for x, value in zip((70, 140, 330), row, strict=True):
                    pdf.drawString(x, 740 - number * 28, value)
            pdf.drawString(62, 740 - len(rows) * 28, "이상.")

        text = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False)).text

        assert sum(line.startswith("| --- ") for line in text.split("\n")) == 1
        assert "| 1688 | 로이드 커피하우스 개업 그리고 설립과 보험 시장의 탄생 | |" in text
        assert "| 1952 | 포트폴리오 이론 | 수익과 위험의 관계 |" in text

    def test_wrapped_rows_of_a_top_aligned_table_are_not_cut_at_a_second_line(self):
        """In a top-aligned table (no header) whose cells each wrap to two lines, a fragment that took the wrapped second line as its header was
        accepted as a table, so the row above it scattered into one paragraph per cell (the table of risk management policy steps in the real PDF).
        Such a table stays as row-by-row lines, as before."""
        from reportlab.platypus import Paragraph

        cell = _style(9, 11)
        rows = [["1단계", "경영 맥락의 이해", "비전과 미션, 전략 목표 파악, 내외부 환경 분석(조직 문화, 역량, 시장, 규제, 경쟁 구도)",
                 "ISO 31000 프레임워크"],
                ["2단계", "이해관계자 분석", "주주, 이사회, 규제기관, 채권자, 고객 등의 리스크 인식과 기대 수준 파악 및 정책 반영",
                 "한국거래소 공시 규정, 금융감독원 지침"],
                ["3단계", "리스크 허용 한도와 한계 설정", "전략 목표 달성을 위해 감수 가능한 리스크 유형과 수준을 구체적 수치로 명시",
                 "내부 경영 판단 (예: 환율 영향을 연간 매출의 3% 이내로 제한)"],
                ["4단계", "정책 문서화와 승인", "이사회 또는 리스크위원회의 공식 승인, 전사적 공유, 매년 또는 환경 변화 시 갱신",
                 "통상 20~50페이지 분량의 공식 문서"]]
        table = _table([[Paragraph(text, cell) for text in row] for row in rows], [30, 70, 210, 160])

        text = _text([Paragraph("리스크 관리 정책 수립은 일반적으로 다음의 단계를 거칩니다.", _style()), table,
                      Paragraph("4.1.2 리스크 등록부 작성과 관리", _style())])

        assert "3단 한도와 한계 전략 목표 달성을 위해 감수 가능한 리스크 내부 경영 판단 (예: 환율 영향을" in text
        assert "| --- " not in text


class TestCentredFormulaRowWithCellsWrappedToDifferentHeights:
    """Formula table: a centred row whose cells wrap to different numbers of lines ('열분해유 Oil 함량 / 기존 원유 Oil 함량 x …') split the table in
    two, and the wrapped pieces ('함량', 'eco-cost') went to other columns. There must be one table, the formula must be one row, and the pieces must
    be in their own columns."""

    @pytest.mark.parametrize("framed", [False, True])
    def test_formula_stays_one_table_with_pieces_in_their_columns(self, framed):
        from reportlab.lib import colors
        from reportlab.platypus import Paragraph

        cell = _style(9, 11, alignment=1)
        head = ["(자사SV기준", "/", "Baseline)", "x", "Proxy", "x", "제공량", "x", "기여도", "-", "보상"]
        row = ["열분해유 Oil 함량", "/", "기존 원유 Oil 함량", "x", "Oil Scarcity eco-cost", "x", "폐타이어 열분해유 도입처리량",
               "x", "기여도", "-", "보상"]
        frame = [("BOX", (0, 0), (-1, -1), 0.5, colors.black), ("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.black)]
        table = _table([head, [Paragraph(text, cell) for text in row]], [95, 25, 70, 20, 45, 20, 55, 20, 50, 20, 40],
                       [("VALIGN", (0, 0), (-1, -1), "MIDDLE"), ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                        *(frame if framed else [])])

        text = _text([Paragraph("성과측정식", _style(14, 18)), table, Paragraph("1. Baseline", _style(14, 18)),
                      Paragraph("본문.", _style())])

        assert sum(line.startswith("| --- ") for line in text.split("\n")) == 1
        rows = [line for line in text.split("\n") if line.startswith("| ")]
        cells = [[cell.strip() for cell in line.split("|")[1:-1]] for line in rows if not line.startswith("| --- ")]
        columns = ["".join(row[index] for row in cells if index < len(row)).replace("<br>", "").replace(" ", "")
                   for index in range(len(cells[0]))]
        assert len(cells) == 2
        assert "열분해유Oil함량" in columns[0]
        assert "기존원유Oil함량" in columns[2]
        assert "OilScarcityeco-cost" in columns[4]
        assert "폐타이어열분해유도입처리량" in columns[6]

    @pytest.mark.parametrize("data_rows", [2, 3])
    def test_rows_shifted_by_fonts_are_not_one_centred_row(self, data_rows):
        """A small table whose numeric cells sit 4.5pt higher because of the font keeps each row separate even though the lines are staggered (even
        when a group with only one two-line column arises)."""
        rows = [["직급", "기본급", "직책수당", "연차일수"], ["사원", "3,200,000", "0", "15"], ["대리", "3,900,000", "100,000", "16"],
                ["과장", "4,700,000", "200,000", "18"]][:data_rows + 1]

        def draw(pdf, index):
            pdf.setFont(_FONT, 11)
            pdf.drawString(72, 760, "직급별 급여 기준은 다음과 같다.")
            for number, row in enumerate(rows):
                for x, value in zip((95, 203, 311, 419), row, strict=True):
                    pdf.drawString(x, 720 - number * 16 + (4.5 if number and x != 95 else 0.0), value)

        text = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False)).text

        assert "<br>" not in text
        assert "| 대리 | 3,900,000 | 100,000 | 16 |" in text
