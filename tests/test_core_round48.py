"""The time for repeated `![` in MD picture syntax, foreignObject labels in inline SVG, code fences in MD with Windows line breaks."""

import base64
import io
import time

from PIL import Image
import pytest

from opendockit import convert


def _png_base64() -> str:
    buffer = io.BytesIO()
    Image.new("RGB", (100, 100), (200, 0, 0)).save(buffer, "PNG")
    return base64.b64encode(buffer.getvalue()).decode()


class TestRepeatedPictureOpenersTakeLinearTime:
    """The alt text part searched for `]` to the end of the line at every `![`, so one line of repeated `![` without `]` + a data:
    URI took quadratic time (20KB 3.3 seconds, 80KB 2 minutes)."""

    @pytest.mark.timing
    def test_line_of_unclosed_picture_openers_takes_linear_time(self):
        data = ("![" * 40_000 + " data:image/png;base64,AAAA\n").encode()
        started = time.perf_counter()

        result = convert(data, "a.md")

        assert time.perf_counter() - started < 2.0
        assert result.text.startswith("![![")

    def test_picture_after_unclosed_openers_on_the_same_line_is_still_a_picture(self):
        data = f"![ 열고 안 닫음 ![차트](data:image/png;base64,{_png_base64()}) 끝".encode()

        result = convert(data, "a.md")

        assert len(result.images) == 1
        assert "base64," not in result.text
        assert "끝" in result.text


def _mermaid_node(label: str) -> str:
    return (f'<g class="node"><rect/><g class="label"><foreignObject width="80" height="24">'
            f'<div xmlns="http://www.w3.org/1999/xhtml"><span class="nodeLabel"><p>{label}</p></span></div>'
            f"</foreignObject></g></g>")


class TestForeignObjectLabelsInInlineSvg:
    """Only `<text>` was counted as a label, so diagram text written as HTML inside `<foreignObject>`, as Mermaid (htmlLabels) and
    draw.io do, vanished whole."""

    def test_flow_diagram_with_html_labels_keeps_its_text(self):
        nodes = "".join(_mermaid_node(label) for label in ["고객 주문 접수", "재고 확인", "결제 승인", "배송 시작"])
        html = f"<html><body><h1>주문 흐름</h1><svg id='mermaid-1'>{nodes}</svg><p>끝.</p></body></html>"

        result = convert(html.encode(), "a.html")

        assert "고객 주문 접수 재고 확인 결제 승인 배송 시작" in result.text
        assert result.text.endswith("끝.")

    def test_chart_whose_html_labels_are_mostly_numbers_is_still_dropped(self):
        labels = ["매출 추이"] + [str(value * 100) for value in range(8)] + [f"{month}월" for month in range(1, 13)]
        nodes = "".join(_mermaid_node(label) for label in labels)
        html = f"<p>가</p><svg>{nodes}</svg><p>나</p>"

        result = convert(html.encode(), "a.html")

        assert result.text == "가\n\n나"

    def test_text_and_html_labels_are_kept_in_document_order(self):
        html = f"<p>가</p><svg><text>시작</text>{_mermaid_node('중간 단계')}<text>종료</text></svg><p>나</p>"

        result = convert(html.encode(), "a.html")

        assert "시작 중간 단계 종료" in result.text


class TestCodeFenceWithWindowsLineBreaks:
    """Because of the `\\r` at the end of the closing fence line, the code block was not recognized only in MD saved with CRLF, so
    the data: URI picture example inside the block was extracted as a picture (with LF and CR the example stayed as is)."""

    @pytest.mark.parametrize("line_break", ["\n", "\r\n", "\r"])
    def test_data_uri_picture_inside_a_code_fence_stays_as_code(self, line_break):
        markdown = f"# 제목\n\n예시 코드:\n\n```md\n![그림](data:image/png;base64,{_png_base64()})\n```\n\n끝 문단\n"
        data = markdown.replace("\n", line_break).encode()

        result = convert(data, "a.md")

        assert result.images == []
        assert "![그림](data:image/png;base64," in result.text
        assert result.text.endswith("끝 문단")
