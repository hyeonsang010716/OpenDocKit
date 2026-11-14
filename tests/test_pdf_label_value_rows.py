"""PDF 'label value' two columns: rows whose value wraps in a group with long labels (over 15% of the page width)."""

import pytest

from opendockit import ConvertOptions, convert
from tests.test_pdf import _FONT, _korean_pdf


class TestLongLabelsWithWrappedValues:
    """In a two-column table with long labels ('정부지원연구개발비', over 15% of the page width), as in R&D forms, when a value wrapped at the cell
    width the wrapped value line was not accepted, and the label group and the value group were split apart wholesale."""

    _ROWS = [("과제명", ["클라우드 기반 제조 데이터 분석 플랫폼 개발"]), ("주관연구개발기관", ["(주)스마트데이터랩"]),
             ("공동연구개발기관", ["한국과학기술연구원, 부산대학교 산학협력단, (주)미래정밀 등 각 기관은", "세부과제별 책임자를 둔다"]),
             ("총 연구개발기간", ["2026. 4. 1. ~ 2028. 12. 31. (33개월)"]),
             ("정부지원연구개발비", ["1,800,000천원 (1차년도 600,000천원, 2차년도 700,000천원,", "3차년도 500,000천원)"]),
             ("기관부담연구개발비", ["450,000천원"])]

    def test_long_labels_keep_their_wrapped_values(self):
        def draw(pdf, index):
            pdf.setFont(_FONT, 14)
            pdf.drawString(60, 782, "연구개발과제 요약")
            pdf.setFont(_FONT, 10.5)
            y = 752
            for label, lines in self._ROWS:
                pdf.drawString(62, y, label)
                for line in lines:
                    pdf.drawString(200, y, line)
                    y -= 14
                y -= 6
            pdf.setFont(_FONT, 14)
            pdf.drawString(60, y - 20, "1. 연구 목표")

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        for label, lines in self._ROWS:
            assert "\n".join([f"{label} {lines[0]}", *lines[1:]]) in result.text
        assert result.text.index("기관부담연구개발비 450,000천원") < result.text.index("1. 연구 목표")


class TestLabelValueRowsBesideAnotherBlock:
    """Invoice head: when a 'label value' block sits beside the company information block on the left with a different line pitch, so that the two
    are staggered, the left lines got mixed into the same rows, breaking the pairs and splitting the label group from the value group. Lines of
    another column that end left of the label column are left out of the decision."""

    _PAIRS = [("청구번호", "INV-2026-0931"), ("청구일", "2026-09-30"), ("납부기한", "2026-10-15"), ("고객번호", "C-10442")]

    def _invoice(self, left_lines):
        def draw(pdf, index):
            pdf.setFont(_FONT, 20)
            pdf.drawString(57, 770, "청구서")
            pdf.setFont(_FONT, 9.5)
            for number, text in enumerate(left_lines):
                pdf.drawString(57, 740 - number * 13, text)
            for number, (label, value) in enumerate(self._PAIRS):
                pdf.drawString(346, 737 - number * 17.4, label)
                pdf.drawRightString(496, 737 - number * 17.4, value)
            pdf.drawString(57, 640, "청구 대상: (주)미래물산 재무팀 귀중")

        return convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

    def test_pairs_beside_a_left_address_block_stay_paired(self):
        result = self._invoice(["(주)한빛클라우드", "서울특별시 강남구 테헤란로 123, 7층", "사업자등록번호 123-45-67890",
                                "billing@hanbit.example"])

        for label, value in self._PAIRS:
            assert f"{label} {value}" in result.text
        assert "(주)한빛클라우드\n서울특별시 강남구 테헤란로 123, 7층" in result.text

    def test_pairs_without_a_left_block_stay_paired(self):
        result = self._invoice(["(주)한빛클라우드"])

        for label, value in self._PAIRS:
            assert f"{label} {value}" in result.text

    @pytest.mark.parametrize("offset", [0.7, 1.5])
    def test_pairs_whose_first_rows_line_up_with_the_left_block_stay_paired(self, offset):
        """When the first line of the left block is at the same height as the first pair, that row has three pieces and cannot start as a pair, so
        only the lower pairs staggered against the left lines were joined and the upper '등록번호' and '상호' were split into a label group and a
        value group (a quotation printed from a browser). A pair that sits at a height between two lines of the left block must not be grouped as
        'centred cells beside a two-line cell' (`_with_centred_wrapped_cells`) into a three-column table either."""
        pairs = [("등록번호", "123-45-67890"), ("상호", "한빛테크(주)"), ("대표자", "김대표"), ("주소", "서울 강남구 테헤란로 123"),
                 ("연락처", "02-1234-5678")]
        left = ["(주)가나다상사 귀하", "견적일자: 2025. 9. 30.", "유효기간: 견적일로부터 30일", "", "아래와 같이 견적합니다."]

        def draw(pdf, index):
            pdf.setFont(_FONT, 12)
            for number, text in enumerate(left):
                pdf.drawString(35, 710 - number * 14.3, text)
            for number, (label, value) in enumerate(pairs):
                pdf.drawString(308, 710 - offset - number * 15.75, label)
                pdf.drawString(390, 710 - offset - number * 15.75, value)

        text = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False)).text

        for label, value in pairs:
            assert f"{label} {value}" in text
        assert "(주)가나다상사 귀하\n견적일자: 2025. 9. 30." in text

    def test_rows_of_three_short_columns_are_not_split_into_a_pair_and_a_left_line(self):
        """A three-column layout with a left piece on every row is not a left block — the two columns are not joined on their own."""
        rows = [("1분기", "매출", "1,200"), ("2분기", "매출", "1,350")]

        def draw(pdf, index):
            pdf.setFont(_FONT, 10)
            for number, row in enumerate(rows):
                for x, cell in zip((60, 300, 360), row, strict=True):
                    pdf.drawString(x, 700 - number * 15, cell)

        text = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False)).text

        assert "매출 1,200" not in text


def _invoice_with_totals(item_rows: int = 4) -> bytes:
    """A reportlab invoice: a striped item table (no rules, only a bottom border), a totals block at the right below it, and bank account and
    contact lines below that."""
    from io import BytesIO

    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
    base = ParagraphStyle("base", fontName=_FONT, fontSize=9.5, leading=13)
    items = [("컴퓨팅 인스턴스", "9/1~9/30", "720시간", "152", "109,440"), ("블록 스토리지", "9/1~9/30", "2,000GB", "120", "240,000"),
             ("데이터 전송", "9/1~9/30", "1,350GB", "110", "148,500"), ("관리형 데이터베이스", "9/1~9/30", "1대", "385,000", "385,000"),
             ("기술 지원", "9월", "1식", "100,000", "100,000"), ("백업 보관", "9/1~9/30", "500GB", "40", "20,000")][:item_rows]
    rows = [["No", "서비스", "기간", "수량", "단가(원)", "금액(원)"], *([str(number), *item] for number, item in enumerate(items, 1))]
    style = [("FONT", (0, 0), (-1, -1), _FONT, 9), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E79")),
             ("TEXTCOLOR", (0, 0), (-1, 0), colors.white), ("ALIGN", (3, 1), (-1, -1), "RIGHT"),
             ("LINEBELOW", (0, -1), (-1, -1), 0.8, colors.black),
             *(("BACKGROUND", (0, row), (-1, row), colors.HexColor("#EEF2F7")) for row in range(2, len(rows), 2))]
    table = Table(rows, colWidths=[10 * mm, 58 * mm, 24 * mm, 22 * mm, 26 * mm, 30 * mm], style=TableStyle(style))
    totals = Table([["공급가액", "1,002,940"], ["부가세 (10%)", "100,294"], ["할인 (약정 5%)", "-50,147"], ["청구 금액", "1,053,087원"]],
                   colWidths=[35 * mm, 35 * mm], hAlign="RIGHT",
                   style=[("FONT", (0, 0), (-1, -1), _FONT, 9.5), ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                          ("LINEABOVE", (0, -1), (-1, -1), 0.8, colors.black), ("FONT", (0, -1), (-1, -1), _FONT, 11)])
    buffer = BytesIO()
    SimpleDocTemplate(buffer, pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=18 * mm).build(
        [Paragraph("청구 대상: (주)미래물산 재무팀 귀중", base), Spacer(1, 8), table, Spacer(1, 10), totals, Spacer(1, 14),
         Paragraph("입금 계좌: 한빛은행 123-456-789012", base), Paragraph("문의: 고객센터 1588-0000", base)])
    return buffer.getvalue()


class TestTotalsBelowAnItemTable:
    """The totals block at the right, under the bottom border of an invoice's item table (no rules), was absorbed as rows of the item table, and
    the '청구 금액' line went after the account and contact lines. When a table-wide horizontal rule is the only one among the data rows and the rows
    below it leave the first column empty and fill at most half of the columns, it is the table's bottom border."""

    def test_totals_block_under_the_bottom_rule_is_not_a_table_row(self):
        result = convert(_invoice_with_totals(), "a.pdf", ConvertOptions(page_markers=False))

        text = result.text
        assert "| 4 | 관리형 데이터베이스 | 9/1~9/30 | 1대 | 385,000 | 385,000 |" in text
        assert "| 공급가액" not in text and "| | | | 공급가액" not in text
        assert "공급가액 1,002,940" in text
        assert text.index("공급가액") < text.index("청구 금액") < text.index("입금 계좌")


class TestLeftBlockIsNotAWrappedCell:
    """A quotation printed from a browser (line positions exactly as in the real file): the right-hand '상호' pair sits at a height between two lines
    of the left block ('견적일자', '유효기간'), so it was grouped as vertically centred cells beside a two-line cell and became the three-column table
    '| (주)가나다상사 귀하 | 등록번호 | 123-45-67890 |'. A centred cell must be within 20% of the line height of the middle of the two lines (this
    block is 4pt off)."""

    def test_lines_of_two_blocks_with_different_line_pitches_are_not_a_table(self):
        from opendockit.extractors._pdf_aligned_tables import _split_line_table_runs
        from opendockit.extractors._pdf_model import _Line

        placed = [(70.5, 34.5, 128.9, "(주)가나다상사 귀하"), (84.7, 34.5, 143.8, "견적일자: 2025. 9. 30."),
                  (99.0, 34.5, 172.4, "유효기간: 견적일로부터 30일"), (127.5, 34.5, 147.3, "아래와 같이 견적합니다."),
                  (72.0, 308.2, 349.8, "등록번호"), (87.7, 308.2, 329.0, "상호"), (103.5, 308.2, 339.4, "대표자"),
                  (119.2, 308.2, 329.0, "주소"), (135.0, 308.2, 339.4, "연락처"), (72.0, 378.8, 452.2, "123-45-67890"),
                  (87.7, 378.8, 438.3, "한빛테크(주)"), (103.5, 378.8, 409.9, "김대표"), (119.2, 378.8, 498.8, "서울 강남구 테헤란로 123"),
                  (135.0, 378.8, 450.8, "02-1234-5678")]
        lines = [_Line(top=top, bottom=top + 12, x0=x0, x1=x1, text=text, size=12.0) for top, x0, x1, text in placed]

        runs = _split_line_table_runs(lines, 612 * 0.25)

        assert runs == []


class TestLabelGridWithASpanningRow:
    """When a value row spanning three cells ('참석자 | name list') sits inside the borderless label/value grid of a minutes head (일시 | value | 장소
    | value), the run of table-shaped rows was broken; the bold label column was grouped on its own and '일시·참석자·작성자' all fell behind the
    values."""

    @pytest.mark.parametrize("last", [("작성자", "박과장", "승인", "김부장"), ("안건", "1. 차세대 시스템 구축 일정 검토")])
    def test_labels_stay_on_their_rows(self, last):
        rows = [("일시", "2025. 3. 10.(월) 14:00~16:00", "장소", "본관 3층 대회의실"),
                ("참석자", "김부장, 이차장, 박과장, 최대리, 정주임 (5명)"), last]

        def draw(pdf, index):
            pdf.setFont(_FONT, 16)
            pdf.drawCentredString(297, 790, "회 의 록")
            for number, row in enumerate(rows):
                top = 750 - number * 24
                pdf.setFont(_FONT, 10)
                for x, text in zip((120, 340, 420), row[1:], strict=False):
                    pdf.drawString(x, top, text)
            for number, row in enumerate(rows):  # draw the bold label column separately, as Chrome does
                pdf.setFont(_FONT, 10)
                pdf.drawString(40, 750 - number * 24, row[0])
            pdf.drawString(40, 660, "논의 내용")

        text = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False)).text

        assert "일시 2025. 3. 10.(월) 14:00~16:00 장소 본관 3층 대회의실\n참석자 김부장, 이차장, 박과장, 최대리, 정주임 (5명)\n" in text
        assert " ".join(last) in text


class TestShortLabelsWithLongerValues:
    """A layout with short labels on the left (history dates, schedule times, side headings) and wide or multi-line values on the right. When wide
    values made up half of the group or more, or the second line of a value was in smaller type (the speaker), the group was discarded and all the
    labels were read before all the values."""

    @staticmethod
    def _draw_rows(rows, label_size=10.0, value_x=160, pitch=14, gap=8, sizes=(10.0, 10.0)):
        def draw(pdf, index):
            pdf.setFont(_FONT, 14)
            pdf.drawString(60, 790, "추진 경과")
            y = 760
            for label, lines in rows:
                pdf.setFont(_FONT, label_size)
                pdf.drawString(62, y, label)
                for number, line in enumerate(lines):
                    pdf.setFont(_FONT, sizes[min(number, 1)])
                    pdf.drawString(value_x, y, line)
                    y -= pitch
                y -= gap
            pdf.setFont(_FONT, 10)
            pdf.drawString(60, y - 20, "이상의 경과를 바탕으로 다음 단계를 준비한다.")

        return _korean_pdf(draw)

    def test_history_timeline_with_wide_values_keeps_each_date_with_its_entry(self):
        rows = [("2023. 3.", ["현장 실사 및 문제점 도출 (외부 컨설팅사 참여)"]),
                ("2023. 9.", ["경영진 보고 및 추진 방향 확정, 전담 TF 구성(팀장 포함 7명). TF 는 생산기술팀과",
                              "정보전략팀 인원으로 구성하며 매주 진행 상황을 점검한다."]),
                ("2024. 2.", ["시범 라인 선정(사출 3라인)"]),
                ("2024. 6.", ["시범 적용 결과 OEE 9%p 개선 확인, 전사 확대 결정. 확대 적용 범위와 예산은",
                              "하반기 이사회에서 의결한다."])]

        result = convert(self._draw_rows(rows, value_x=150, pitch=15, gap=5), "a.pdf", ConvertOptions(page_markers=False))

        for label, lines in rows:
            assert "\n".join([f"{label} {lines[0]}", *lines[1:]]) in result.text

    def test_program_schedule_with_a_smaller_second_line_keeps_each_time_with_its_session(self):
        rows = [("09:30 - 10:00", ["등록 및 네트워킹"]), ("10:00 - 10:20", ["개회사", "한국제조혁신협회 회장 김혁신"]),
                ("10:20 - 11:00", ["기조연설: 제조 AI 의 현재와 미래", "서울대학교 산업공학과 교수 이연구"]),
                ("11:40 - 13:00", ["점심 식사"]), ("13:00 - 14:30", ["패널 토론", "좌장: 최토론 / 패널: 4인"])]

        result = convert(self._draw_rows(rows, label_size=11, value_x=220, pitch=15, gap=12, sizes=(11, 9)), "a.pdf",
                         ConvertOptions(page_markers=False))

        for label, lines in rows:
            assert "\n".join([f"{label} {lines[0]}", *lines[1:]]) in result.text

    def test_side_headings_stay_before_their_paragraphs(self):
        """A side heading in slightly larger type than the body (12/10) is still grouped when it is on the same row. Known accepted gap: a much
        larger side heading (14/10) is off by more than the row tolerance in its top coordinate and becomes a different row, so the headings come
        first as before (docs/decisions/pdf-rows.md)."""
        rows = [("개요", ["본 프로젝트는 물류센터의 재고 관리 시스템을 클라우드 기반으로 전환하여",
                        "실시간 재고 가시성을 확보하는 것을 목표로 한다. 현재 일 1회 배치",
                        "갱신으로 인해 결품률이 4.2%에 이른다."]),
                ("범위", ["입고, 출고, 재고 실사 세 개 업무를 대상으로 하며, 기존 ERP 와의",
                        "연동 인터페이스 12종을 새로 개발한다. 운송 관리 시스템은 이번 범위에서",
                        "제외한다."]),
                ("일정", ["2025년 1월 착수하여 6월 시범 운영, 9월 전면 적용을 목표로 한다.",
                        "시범 운영은 평택 센터에서 진행한다."])]

        result = convert(self._draw_rows(rows, label_size=12, value_x=170, pitch=15, gap=20), "a.pdf",
                         ConvertOptions(page_markers=False))

        for label, lines in rows:
            assert "\n".join([f"{label} {lines[0]}", *lines[1:]]) in result.text

    def test_line_numbers_beside_wide_lines_are_not_label_value_rows(self):
        """The numbers of a line-numbered document (contract, draft legislation) are not labels — a number is not attached in front of the body
        text on each line and stays separate as before."""
        lines = ["제1조(목적) 이 계약은 갑과 을 사이의 소프트웨어 개발 용역에 관한 권리와 의무를 정한다.",
                 "제2조(정의) 이 계약에서 사용하는 용어의 뜻은 다음과 같다. 다만 따로 정한 것은 그에 따른다.",
                 "제3조(계약 기간) 계약 기간은 계약 체결일부터 2025년 12월 31일까지로 한다.",
                 "제4조(대금) 계약 금액은 금 일억 원으로 하며 부가가치세를 포함한다."]

        def draw(pdf, index):
            pdf.setFont(_FONT, 10)
            for number, line in enumerate(lines):
                y = 760 - number * 30
                pdf.drawString(40, y, f"{(number + 1) * 5:02d}")
                pdf.drawString(80, y, line)
                pdf.drawString(80, y - 15, "계속되는 본문 줄이 여기에 이어진다. 줄 번호는 다섯 줄마다 찍는다.")

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert not any(line.startswith(("05 제1조", "10 제2조")) for line in result.text.splitlines())


class TestLabelsCenteredOnTheirValues:
    """In a borderless two-column table whose labels are centred vertically in the cell (valign=middle), a label is not on the same row as the
    first line of its value, so no 'label value' pair was formed. All the labels were read before all the values (Chrome), or one value was placed
    after the wrong label (LibreOffice)."""

    _ROWS = [("지원 대상", ["중소기업 기본법에 따른 중소기업으로서 공고일 현재 사업을 영위하고",
                         "최근 3년간 수출 실적이 있는 기업"]),
             ("제외 대상", ["휴업 또는 폐업 중인 기업, 금융기관 등으로부터 채무불이행으로",
                         "규제 중인 기업, 동일 사업으로 다른 기관의 지원을 이미 받고 있는",
                         "기업"]),
             ("지원 금액", ["기업당 최대 5천만원 이내에서 총사업비의 70% 까지 지원하며,",
                         "나머지 30% 는 기업이 현금으로 부담하여야 합니다"])]

    def _centered(self, pitch, padding):
        def draw(pdf, index):
            pdf.setFont(_FONT, 14)
            pdf.drawString(60, 790, "신청 자격 안내")
            pdf.setFont(_FONT, 10)
            y = 760
            for label, lines in self._ROWS:
                middle = y - pitch * (len(lines) - 1) / 2
                pdf.drawString(62, middle, label)
                for line in lines:
                    pdf.drawString(140, y, line)
                    y -= pitch
                y -= padding
            pdf.drawString(60, y - 20, "자세한 내용은 공고문을 참고하십시오.")

        return _korean_pdf(draw)

    @pytest.mark.parametrize(("pitch", "padding"), [(12, 3), (12, 16)])
    def test_each_label_comes_before_its_own_value(self, pitch, padding):
        result = convert(self._centered(pitch, padding), "a.pdf", ConvertOptions(page_markers=False))

        text = result.text
        for label, lines in self._ROWS:
            assert text.index(label) < text.index(lines[0])
        for (label, _), (_, previous) in zip(self._ROWS[1:], self._ROWS, strict=False):
            assert text.index(previous[-1]) < text.index(label)

    @pytest.mark.parametrize("single", [0, 1, 3])
    def test_value_of_one_line_at_any_place_keeps_each_label_before_its_value(self, single):
        """The label of a one-line value is on the same row as the value line, which is a normal pair. When that pair was the first item, the group
        was rejected on the grounds that the row right above continues the value column, so all three labels were read before all the values (in
        the middle or at the end the group accepted the row and it was fine)."""
        rows = [("목적", ["고용량 전고체 배터리 셀의 양산 공정을 확립하여", "전기차 주행거리를 현재 대비 30% 이상",
                          "늘리는 것을 목표로 한다"]),
                ("기간", ["2026년 1월부터 2028년 12월까지", "총 36개월"]),
                ("예산", ["총 45억원 규모로 정부 출연금 30억원과", "참여 기업 부담금 15억원으로 구성되며",
                          "민간 15억 중 현물은 5억 이내"])]
        rows.insert(single, ("과제명", ["차세대 전고체 배터리 셀 양산 기술 개발"]))

        def draw(pdf, index):
            pdf.setFont(_FONT, 14)
            pdf.drawString(60, 790, "과제 개요")
            pdf.setFont(_FONT, 10)
            y = 760
            for label, lines in rows:
                pdf.drawString(62, y - 12 * (len(lines) - 1) / 2, label)
                for line in lines:
                    pdf.drawString(140, y, line)
                    y -= 12
                y -= 10
            pdf.drawString(60, y - 20, "세부 내용은 붙임을 참고하십시오.")

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        text = result.text
        for label, lines in rows:
            assert text.index(label) < text.index(lines[0])
        for (label, _), (_, previous) in zip(rows[1:], rows, strict=False):
            assert text.index(previous[-1]) < text.index(label)

    def test_short_lines_beside_an_evenly_spaced_paragraph_are_not_paired(self):
        """Short lines (margin notes) beside evenly spaced body text are not taken for labels, because there is no gap between value chunks."""
        body = [f"본문 {number}번째 줄은 같은 간격으로 이어지는 문단의 일부이다." for number in range(1, 7)]

        def draw(pdf, index):
            pdf.setFont(_FONT, 10)
            for number, line in enumerate(body):
                pdf.drawString(140, 760 - number * 12, line)
            pdf.drawString(62, 748, "메모 하나")
            pdf.drawString(62, 712, "메모 둘")

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert "메모 하나 본문" not in result.text and "메모 둘 본문" not in result.text

    def test_label_wrapped_over_two_lines_is_not_split_into_two_labels(self):
        """A label wrapped over two lines ('정성 / 효과') is not two labels — it is not dealt out one line per value line."""
        def draw(pdf, index):
            pdf.setFont(_FONT, 10.5)
            pdf.drawString(62, 760, "정성")
            pdf.drawString(62, 747.3, "효과")
            pdf.setFont(_FONT, 12)
            pdf.drawString(105, 765, "구매 이력 추적 가능, 감사 대응 용이")
            pdf.drawString(105, 743.5, "현업 만족도 향상")

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert "정성 구매 이력" not in result.text and "효과 현업" not in result.text

    def test_short_lines_in_the_left_column_of_two_column_text_are_not_labels(self):
        """Short lines in the left column of a two-column layout are not taken for labels, because the right column continues below the group (a
        generated two-column document)."""
        def draw(pdf, index):
            pdf.setFont(_FONT, 10)
            y = 760
            for number in range(1, 11):
                pdf.drawString(300, y, f"오른쪽 단 {number}번째 줄은 문단을 이루는 본문이다.")
                y -= 12 + (8 if number in (2, 4) else 0)
            pdf.drawString(62, 754, "[P012] 제목 12")
            pdf.drawString(62, 722, "[P013] 제목 13")
            pdf.drawString(62, 696, "왼쪽 단의 본문 문단이 여기서 다시 시작하여 이어진다.")

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert "제목 12 오른쪽" not in result.text and "제목 13 오른쪽" not in result.text


class TestLongRunsOfLinesForCentredLabels:
    """Looking for labels centred on value blocks walked from every row of a long paragraph (one value column) to its end,
    and a group of thousands of wrapped value lines gathered its pairs again for every line: both took time with the
    square of the lines (a paragraph of 6,000 lines: 29 seconds)."""

    @staticmethod
    def _rows(shape: str, count: int):
        from opendockit.extractors._pdf_model import _Line, _visual_rows

        lines = []
        for number in range(count):
            top = 20.0 + number * 12
            if shape == "paragraph":
                lines.append(_Line(top=top, bottom=top + 10, x0=100.0, x1=160.0, text=f"line {number}", size=10.0))
            else:  # two labels, and the second value wrapped over all the other lines
                if number < 2:
                    lines.append(_Line(top=top, bottom=top + 10, x0=60.0, x1=90.0, text=f"항목 {number}", size=10.0))
                lines.append(_Line(top=top, bottom=top + 10, x0=200.0, x1=260.0, text=f"내용 {number}", size=10.0))
        return _visual_rows(lines)

    @pytest.mark.timing
    @pytest.mark.parametrize("shape", ["paragraph", "wrapped"])
    def test_label_value_rows_of_thousands_of_lines_take_linear_time(self, shape):
        import time

        from opendockit.extractors._pdf_label_values import _label_value_rows

        count = {"paragraph": 4000, "wrapped": 8000}[shape]
        rows = self._rows(shape, count)

        started = time.perf_counter()
        found = _label_value_rows(rows, 600.0)
        elapsed = time.perf_counter() - started

        assert elapsed < 1.5  # 5.4 and 4.4 seconds before
        if shape == "wrapped":
            assert [len(row) for row in found] == [2, count - 1 + 1]

    @pytest.mark.parametrize("seed", range(6))
    def test_the_kept_walks_give_the_groups_of_the_full_walk(self, seed):
        """`_centered_label_rows` with what it learned kept (`reach`) against the full walk on random rows: labels beside
        value blocks, with offsets, sizes and gaps that make the walk stop at many places."""
        import random

        from opendockit.extractors._pdf_label_values import _centered_label_rows
        from opendockit.extractors._pdf_model import _Line, _visual_rows

        rng = random.Random(seed)
        for _ in range(200):
            lines, top = [], 0.0
            for block in range(rng.randint(1, 8)):
                size, tops = rng.choice([1, 2, 3, 4]), []
                for line in range(size):
                    top += rng.choice([12.0, 12.0, 11.5]) if line else rng.choice([20.0, 24.0, 12.0, 30.0])
                    tops.append(top)
                    x0 = rng.choice([200.0, 200.0, 200.0, 201.0, 205.0])
                    lines.append(_Line(top=top, bottom=top + 10, x0=x0, x1=x0 + rng.choice([60.0, 200.0]),
                                       text=f"값 {block} {line}", size=10.0))
                if rng.random() < 0.9:
                    middle = (tops[0] + tops[-1]) / 2 + rng.choice([0.0, 0.0, 6.0, -6.0, 1.0])
                    x0 = rng.choice([60.0, 60.0, 61.0, 70.0, 210.0])
                    lines.append(_Line(top=middle, bottom=middle + 10, x0=x0, x1=x0 + rng.choice([20.0, 30.0, 160.0]),
                                       text=rng.choice(["성명", "주소", "1", "연락 처"]), size=rng.choice([10.0, 10.0, 12.0])))
            rows = _visual_rows(lines)
            reach: dict = {}
            for index in range(len(rows)):
                assert _centered_label_rows(rows, index, 150.0, reach) == _centered_label_rows(rows, index, 150.0)


class TestLabelWithAnEdgeThatIsNotANumber:
    """A label whose right edge is NaN is dropped from its own row when the lines of another column are taken out, so the search
    for where its row starts found nothing and `next` raised StopIteration."""

    def test_no_group_instead_of_an_error(self):
        from opendockit.extractors._pdf_label_values import _without_left_block
        from opendockit.extractors._pdf_model import _Line

        row = [_Line(top=0, bottom=10, x0=10, x1=200, text="wide", size=10),
               _Line(top=0, bottom=10, x0=100, x1=float("nan"), text="label", size=10),
               _Line(top=0, bottom=10, x0=300, x1=360, text="value", size=10)]
        below = [_Line(top=12, bottom=22, x0=100, x1=140, text="label 2", size=10),
                 _Line(top=12, bottom=22, x0=300, x1=360, text="value 2", size=10)]

        assert _without_left_block([row, below], 0, 80, 600) is None
