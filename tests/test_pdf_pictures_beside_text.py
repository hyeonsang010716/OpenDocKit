"""Reading order of text beside a PDF picture: text right of a picture, the caption under it, where a picture goes in its column."""


from opendockit import ConvertOptions, convert
from tests.test_pdf import _FONT, _korean_pdf
from tests.test_pdf_images import _image


_HEIGHT = 841.89


def _write(pdf, lines) -> None:
    """Write a list of (x, top coordinate of the glyphs, size, text), positioned by the top coordinate."""
    for x, top, size, text in lines:
        pdf.setFont(_FONT, size)
        pdf.drawString(x, _HEIGHT - top - 0.74 * size, text)


class TestTextBesideAPicture:
    """Picture on the left, text on the right (a site report). pdfminer first read the subheading at the left margin and
    the first-column text of the table as one column, so the text right of the picture was pushed behind the subheading
    below ('사진 대장'). The step that lifts displaced lines did not move lines that have a picture beside them."""

    _TEXT = [(58, 56, 15.7, "현장 보고"), (58, 294, 12.3, "사진 대장"), (82, 325, 10.5, "번호"), (62, 370, 10.5, "1"),
             (62, 439, 10.5, "2"), (62, 514, 10.5, "3"), (205, 88.5, 10.5, "1구역"),
             (205, 101.2, 10.5, "배관 누수 발견. 길이 약 30cm 균열이 있으며 즉시 교체가 필요하다."),
             (205, 114, 10.5, "담당: 시설팀 박과장."), (205, 191.2, 10.5, "2구역"), (205, 204, 10.5, "이상 없음. 다음 점검일 2026-11-15."),
             (231, 325, 10.5, "사진"), (445, 325, 10.5, "설명"), (361, 370, 10.5, "전기실 배전반"), (361, 439, 10.5, "옥상 방수층"),
             (361, 507, 10.5, "비상계단 유도등"), (130, 544, 10.5, "(보수 후)"),
             (58, 574, 10.5, "위 점검 결과에 따른 보수 공사는 11월 중 완료할 예정이다.")]

    def test_text_right_of_a_picture_comes_before_the_heading_below(self):
        def draw(pdf, index):
            _write(pdf, self._TEXT)
            pdf.drawImage(_image(270, 180), 58, _HEIGHT - 179, 135, 90)
            pdf.drawImage(_image(270, 180, seed=5), 58, _HEIGHT - 282, 135, 90)
            for y in (322, 345, 413, 481, 564):
                pdf.line(58, _HEIGHT - y, 554, _HEIGHT - y)
            for x in (58, 127, 357, 554):
                pdf.line(x, _HEIGHT - 322, x, _HEIGHT - 564)

        text = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False)).text

        order = [text.index(part) for part in ("# 현장 보고", "1구역", "배관 누수", "2구역", "이상 없음", "사진 대장", "| 번호 |")]
        assert order == sorted(order)
        assert text.index("[Image]") < text.index("1구역")
        assert text.index("배관 누수") < text.rindex("[Image]") < text.index("사진 대장")


class TestCaptionUnderAPictureBesideText:
    """Slide: a bullet list on the left, a picture on the right with a centred caption under it, and number cards side by
    side in one row below. pdfminer sent the caption behind the card row, and the picture was inserted before the
    'first in reading order' paragraph of the same column (a card), landing among the cards."""

    def test_picture_and_its_caption_come_before_the_cards_below(self):
        def draw(pdf, index):
            lines = [(22.5, 12.5, 22, "03. 시장 현황 및 기회"), (22.5, 68.8, 13, "• 국내 클라우드 시장 연평균 18% 성장"),
                     (22.5, 89.8, 13, "• 공공 부문 클라우드 전환 의무화"), (22.5, 110, 13, "• 중소 SaaS 기업 경쟁 심화"),
                     (22.5, 131, 13, "• 보안 인증(CSAP) 획득 기업 우위"), (465.4, 261.7, 9, "[그림 3] 시장 규모 추이"),
                     (22.5, 404.2, 9, "출처: 한국클라우드산업협회")]
            for x, label, value in ((72.9, "시장 규모", "8.2조"), (237.2, "성장률", "18%"), (387.9, "공공 비중", "27%"),
                                    (545.4, "경쟁사 수", "142개")):
                lines += [(x, 303.7, 12, label), (x, 317.5, 20, value)]
            _write(pdf, lines)
            pdf.drawImage(_image(570, 390), 390, _HEIGHT - 262, 285 * 0.7, 195)

        text = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False)).text

        order = [text.index(part) for part in ("보안 인증", "[Image]", "[그림 3]", "시장 규모", "경쟁사 수", "출처:")]
        assert order == sorted(order)
