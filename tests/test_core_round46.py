"""The text of an inline SVG diagram (flowchart, architecture drawing) with more than ten labels."""

from opendockit import convert


_STEPS = ["고객 주문 접수", "재고 확인", "출고 지시", "배송 업체 배정", "배송 완료 통보", "정산",
          "반품 접수", "검수", "환불 처리", "CS 기록", "월말 보고", "종료"]


def _svg(labels: list[str]) -> str:
    return "<svg>" + "".join(f'<text x="10" y="{20 * index}">{label}</text>' for index, label in enumerate(labels)) + "</svg>"


class TestDiagramWithManyLabels:
    """An SVG with more than ten labels was taken for a chart and removed, so the text of a flowchart made of twelve step names
    vanished whole. It is kept when at least half of the labels are words."""

    def test_flow_diagram_with_twelve_word_labels_keeps_its_text(self):
        html = f"<h1>물류 프로세스</h1>{_svg(_STEPS)}<p>위 흐름을 따른다.</p>"

        result = convert(html.encode(), "a.html")

        assert "고객 주문 접수 재고 확인" in result.text
        assert "월말 보고 종료" in result.text

    def test_chart_whose_labels_are_mostly_numbers_is_still_dropped(self):
        labels = ["매출 추이"] + [str(value * 100) for value in range(8)] + [f"{month}월" for month in range(1, 13)]
        html = f"<p>가</p>{_svg(labels)}<p>나</p>"

        result = convert(html.encode(), "a.html")

        assert result.text == "가\n\n나"

    def test_svg_with_a_huge_number_of_word_labels_is_dropped(self):
        html = f"<p>가</p>{_svg([f'노드 이름 {index}' for index in range(1000)])}<p>나</p>"

        result = convert(html.encode(), "a.html")

        assert result.text == "가\n\n나"
