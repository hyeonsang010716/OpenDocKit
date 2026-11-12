"""Right-aligned amounts in PDF forms, the title position at the top of a page, and borderless tables with justified labels."""

import pytest

from opendockit import ConvertOptions, convert
from tests.test_pdf import _FONT, _korean_pdf


_PAYSLIP = [("기본급", "3,000,000"), ("식대", "200,000"), ("연장근로수당", "450,000"), ("직책수당", "100,000")]
_DEDUCTIONS = [("국민연금", "135,000"), ("건강보험", "106,350"), ("소득세", "84,620")]


def _payslip(amount_right: float):
    """Payslip: a large centred title, one row with name and department, items on the left and amounts whose right edge is
    aligned at `amount_right` (no ruling lines)."""
    def draw(pdf, index):
        pdf.setFont(_FONT, 16)
        pdf.drawString(230, 790, "2025년 3월 급여명세서")
        pdf.setFont(_FONT, 10)
        pdf.drawString(60, 760, "성명: 홍길동")
        pdf.drawString(300, 760, "부서: 개발팀")
        y = 720.0
        for title, rows in (("지급 항목", _PAYSLIP), ("공제 항목", _DEDUCTIONS)):
            pdf.setFont(_FONT, 11)
            pdf.drawString(60, y, title)
            pdf.setFont(_FONT, 10)
            y -= 22
            for item, amount in rows:
                pdf.drawString(60, y, item)
                pdf.drawRightString(amount_right, y, amount)
                y -= 18
            y -= 10
        pdf.setFont(_FONT, 11)
        pdf.drawString(60, y, "실지급액")
        pdf.drawRightString(amount_right, y, "3,424,030")

    return _korean_pdf(draw)


class TestFarRightAmounts:
    """When an item and its amount aligned at the right edge of the page were more than 20% of the page width apart, items
    were grouped with items and amounts with amounts, losing which amount belonged to which item."""

    @pytest.mark.parametrize("amount_right", [260, 535])
    def test_each_item_keeps_its_amount_and_the_title_comes_first(self, amount_right):
        result = convert(_payslip(amount_right), "a.pdf", ConvertOptions(page_markers=False))

        lines = result.text.splitlines()
        for item, amount in [*_PAYSLIP, *_DEDUCTIONS, ("실지급액", "3,424,030")]:
            assert f"{item} {amount}" in lines
        assert result.text.lstrip("# ").startswith("2025년 3월 급여명세서")

    @pytest.mark.parametrize("header", ["금액", "금액(원)"])
    def test_amount_column_with_a_header_word_stays_beside_its_items(self, header):
        """A borderless two-column table with a '항목 | 금액(원)' header row was not seen as a right-aligned number column
        because the header is not a number, so the whole amount column fell behind the next section (the same table without
        a header was already paired before)."""
        rows = [("항목", header), ("인건비", "3,000,000"), ("재료비", "1,200,000"), ("장비비", "800,000"), ("합계", "5,000,000")]

        def draw(pdf, index):
            pdf.drawString(60, 780, "1. 소요 예산")
            for number, (item, amount) in enumerate(rows):
                pdf.drawString(60, 750 - number * 18, item)
                pdf.drawRightString(300, 750 - number * 18, amount)
            pdf.drawString(60, 660, "2. 추진 일정")

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        lines = result.text.splitlines()
        for item, amount in rows:
            assert f"{item} {amount}" in lines
        assert result.text.index("합계") < result.text.index("2. 추진 일정")

    def test_right_aligned_values_with_units_stay_beside_their_items(self):
        """The right-aligned values of a product specification table ('220V', '1500W', '60L/일', '52dB', '45kg', '2년') have
        units that were not in the list of numeric value units, so items were grouped with items and values with values."""
        rows = [("정격 전압", "220V"), ("소비 전력", "1.5kW"), ("제습 능력", "60L/일"), ("탱크 용량", "8"), ("소음", "52dB"),
                ("중량", "45kg"), ("설치 면적", "0.5㎡"), ("보증 기간", "2년"), ("교체 주기", "6개월")]

        def draw(pdf, index):
            pdf.drawString(72, 780, "모델 HX-3000 산업용 제습기의 주요 사양은 다음과 같다.")
            for number, (item, value) in enumerate(rows):
                pdf.drawString(72, 750 - number * 20, item)
                pdf.drawRightString(523, 750 - number * 20, value)
            pdf.drawString(72, 550, "※ 사양은 성능 개선을 위해 예고 없이 변경될 수 있습니다.")

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        lines = result.text.splitlines()
        for item, value in rows:
            assert f"{item} {value}" in lines

    def test_two_side_by_side_text_columns_are_not_paired(self):
        """Two forms placed side by side (the right column is left-aligned text) are still not paired, as before."""
        left = ["신청인 정보", "성명", "생년월일", "연락처"]
        right = ["대리인 정보", "대리인 성명", "관계", "대리인 연락처"]

        def draw(pdf, index):
            y = 760.0
            for first, second in zip(left, right, strict=True):
                pdf.drawString(60, y, first)
                pdf.drawString(320, y, second)
                y -= 20

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert "성명 대리인 성명" not in result.text


class TestDistributedLabelsInABorderlessTable:
    """When the labels in the first column of a borderless table with horizontal lines only are stretched by justification
    ('운영      경비'), the two words became two cells, adding a column, and the header row came off the table and became one
    paragraph per word."""

    def test_label_spread_over_its_cell_stays_one_cell_under_the_header(self):
        rows = [(["운영", "경비"], 12000, 9000), (["시설", "투자"], 50000, 31000), (["인력", "양성"], 8000, 7600),
                (["홍보", "활동"], 5000, 1200)]

        def draw(pdf, index):
            pdf.setFont(_FONT, 14)
            pdf.drawString(60, 790, "예산 집행 현황")
            pdf.setFont(_FONT, 10)
            for x, title in [(70, "구분"), (200, "예산"), (280, "집행"), (360, "잔액"), (440, "집행률")]:
                pdf.drawString(x, 750, title)
            pdf.line(60, 763, 535, 763)
            pdf.line(60, 744, 535, 744)
            y = 728.0
            for (first, second), budget, spent in rows:
                pdf.drawString(65, y, first)
                pdf.drawRightString(160, y, second)
                for right, value in [(250, f"{budget:,}"), (330, f"{spent:,}"), (410, f"{budget - spent:,}"),
                                     (480, f"{spent * 100 / budget:.1f}%")]:
                    pdf.drawRightString(right, y, value)
                y -= 18
            pdf.line(60, y + 10, 535, y + 10)

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert "| 구분 | 예산 | 집행 | 잔액 | 집행률 |\n| --- |" in result.text
        assert "| 운영 경비 | 12,000 | 9,000 | 3,000 | 75.0% |" in result.text
        assert "| 홍보 활동 | 5,000 | 1,200 | 3,800 | 24.0% |" in result.text
