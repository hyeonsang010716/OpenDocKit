"""Reading order of one-column PDF pages: a short centred caption and the paragraph below it, and the positions of tables and pictures."""

from io import BytesIO

import pytest

from opendockit import ConvertOptions, convert
from opendockit.extractors._pdf_chars import _drop_cap
from tests import builder
from tests.test_pdf import _FONT, _korean_pdf


def _story_pdf(make_story) -> bytes:
    """Build a one-column document with reportlab platypus. `make_story(styles)` returns the list of flowables. `styles` holds the paragraph styles
    for body text (body), headings (heading) and centred captions (caption)."""
    from reportlab.lib import colors
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.platypus import SimpleDocTemplate

    pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
    body = ParagraphStyle("body", fontName=_FONT, fontSize=10, leading=15, wordWrap="CJK")
    styles = {"body": body, "heading": ParagraphStyle("heading", fontName=_FONT, fontSize=14, leading=20),
              "caption": ParagraphStyle("caption", parent=body, alignment=1), "colors": colors}
    buffer = BytesIO()
    SimpleDocTemplate(buffer).build(make_story(styles))
    return buffer.getvalue()


def _figure_report(styles, paragraph_repeats: int) -> list:
    """Heading → paragraph → picture → centred caption → paragraph → heading → table → '끝.'"""
    from reportlab.platypus import Image, Paragraph, Table, TableStyle

    table = Table([["연도", "매출", "증가율"]] + [[str(2019 + i), f"{(i + 1) * 1000:,}", f"{10 + i}%"] for i in range(6)])
    table.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), _FONT, 9), ("GRID", (0, 0), (-1, -1), 0.5, styles["colors"].black)]))
    return [Paragraph("1. 개요", styles["heading"]),
            Paragraph("아래 그림은 연도별 매출 추이를 보여준다. " * 4, styles["body"]),
            Image(BytesIO(builder.png_bytes(600, 300, seed=3)), width=300, height=150),
            Paragraph("그림 1. 연도별 매출 추이", styles["caption"]),
            Paragraph("그림에서 보듯이 매출은 꾸준히 증가하였다. " * paragraph_repeats, styles["body"]),
            Paragraph("2. 세부 내역", styles["heading"]),
            table,
            Paragraph("끝.", styles["body"])]


class TestCentredCaptionOnOneColumnPage:
    """pdfminer's text box flow (boxes_flow) sent a short centred caption behind the left-aligned paragraph below it, and the positions of tables
    and pictures were pushed back along with it."""

    @pytest.mark.parametrize("paragraph_repeats", [1, 3])
    def test_caption_under_a_figure_comes_before_the_paragraph_below_it(self, paragraph_repeats):
        text = convert(_story_pdf(lambda styles: _figure_report(styles, paragraph_repeats)), "a.pdf").sections[0].text

        order = [text.index(part) for part in ("아래 그림은", "그림 1. 연도별", "그림에서 보듯이", "# 2. 세부 내역", "| 연도 |", "끝.")]
        assert order == sorted(order)
        assert "그림에서 보듯이 매출은 꾸준히 증가하였다." in text

    def test_caption_above_a_table_comes_before_the_table_and_the_closing_line(self):
        """As in a LibreOffice document, the centred caption above a table was pushed behind the '끝.' below the table."""
        from reportlab.platypus import Paragraph, Table, TableStyle

        def story(styles):
            table = Table([["연도", "매출", "증가율"]] + [[str(2019 + i), f"{(i + 1) * 1000:,}", f"{10 + i}%"] for i in range(3)],
                          colWidths=[140, 140, 140])
            table.setStyle(TableStyle([("FONT", (0, 0), (-1, -1), _FONT, 9),
                                       ("GRID", (0, 0), (-1, -1), 0.5, styles["colors"].black)]))
            return [Paragraph("1. 사업 실적", styles["heading"]),
                    Paragraph("올해 사업 실적을 아래와 같이 정리하였다. " * 3, styles["body"]),
                    Paragraph("2. 세부 내역", styles["heading"]),
                    Paragraph("표 1. 연도별 매출", styles["caption"]),
                    table,
                    Paragraph("끝.", styles["body"])]

        text = convert(_story_pdf(story), "a.pdf").sections[0].text

        order = [text.index(part) for part in ("# 2. 세부 내역", "표 1. 연도별 매출", "| 연도 |", "끝.")]
        assert order == sorted(order)

    def test_centred_lines_with_nothing_linking_them_to_the_left_aligned_text_stay_in_place(self):
        """A centred table title below a list → a picture frame drawn as a border only → a centred caption → a left-aligned subheading. With no
        line or picture linking the centred lines to the text on the left, the page looked like two columns and the subheading came out before the
        centred lines."""
        def draw(pdf, index):
            for row, item in enumerate(["합계 운영 계획 집행 예산", "계획 결과 일정 현황 부서", "지표 집행 결과 집행 지표"]):
                pdf.drawString(70, 475 - row * 16, f"{row + 1}.")
                pdf.drawString(88, 475 - row * 16, item)
            pdf.drawCentredString(297.6, 401, "<내용 대상>")
            pdf.rect(176.6, 278, 242, 98)
            pdf.setFont(_FONT, 9)
            pdf.drawCentredString(297.6, 262, "그림 1. 기간 기간 대상")
            pdf.setFont(_FONT, 14)
            pdf.drawString(60, 164, "3. 성과 담당")

        text = convert(_korean_pdf(draw), "a.pdf").sections[0].text

        order = [text.index(part) for part in ("1. 합계 운영", "<내용 대상>", "그림 1. 기간", "3. 성과 담당")]
        assert order == sorted(order)

    def test_caption_of_a_figure_in_the_right_column_is_not_put_into_the_left_column(self):
        """When the right column of a two-column page holds only a picture and its caption, the space beside the left-column lines below the
        caption is empty, but the picture is beside the left column, so this is a column gap."""
        from reportlab.lib.utils import ImageReader

        def draw(pdf, index):
            for line in range(24):
                pdf.drawString(57, 760 - line * 14, f"왼쪽 단의 {line + 1}번째 줄 본문이 이어집니다.")
            pdf.drawImage(ImageReader(BytesIO(builder.png_bytes(440, 300, seed=5))), 330, 600, width=200, height=150)
            pdf.drawCentredString(430, 585, "그림 1. 오른쪽 단 그림")

        text = convert(_korean_pdf(draw), "a.pdf").sections[0].text

        body = "\n".join(f"왼쪽 단의 {line + 1}번째 줄 본문이 이어집니다." for line in range(24))
        assert body in text


def _captioned_table_pairs(ruled_second: bool = True) -> bytes:
    """A page on which two groups of captioned side-by-side tables follow one below the other (two flex cells of a printed web page). There is a
    subheading above the second group."""
    from tests.test_pdf_table_columns import _grid

    pairs = [(750, "상반기", "하반기", [["월", "매출", "이익"], ["1월", "120", "11"], ["2월", "131", "13"], ["3월", "125", "10"]],
              [["월", "매출", "이익"], ["7월", "142", "15"], ["8월", "150", "17"], ["9월", "147", "14"]]),
             (620, "국내", "해외", [["지역", "매출", "비중"], ["서울", "520", "41%"], ["부산", "210", "17%"], ["대전", "95", "8%"]],
              [["국가", "매출", "비중"], ["미국", "230", "18%"], ["일본", "120", "10%"], ["베트남", "80", "6%"]])]

    def draw(pdf, index):
        pdf.setFont(_FONT, 15)
        pdf.drawString(60, 780, "반기별 실적 비교")
        pdf.drawString(60, 660, "지역별 비교")
        pdf.setFont(_FONT, 10)
        for top, left_caption, right_caption, left_rows, right_rows in pairs:
            pdf.drawString(60, top + 6, left_caption)
            pdf.drawString(300, top + 6, right_caption)
            _grid(pdf, top, 18, [60, 130, 200, 270], left_rows)
            _grid(pdf, top, 18, [300, 370, 440, 510], right_rows)

    return _korean_pdf(draw)


class TestCaptionsOverSideBySideTables:
    """When two groups of captioned side-by-side tables follow one below the other, pdfminer read the left captions and the right captions
    separately like columns, so a right caption went behind the next section's subheading and tables were placed after the wrong caption."""

    def test_caption_rows_stay_right_above_their_tables(self):
        result = convert(_captioned_table_pairs(), "a.pdf")

        text = result.text
        order = ["상반기", "하반기", "| 1월 |", "| 7월 |", "지역별 비교", "국내", "해외", "| 서울 |", "| 미국 |"]
        assert [text.index(word) for word in order] == sorted(text.index(word) for word in order)

    def test_label_value_block_right_above_a_table_is_not_a_caption_row(self):
        """The lower row of a 2×2 label/value block right above a table is not a caption row — the block's column-by-column reading order is left
        as it is."""
        from tests.test_pdf_table_columns import _grid

        def draw(pdf, index):
            pdf.setFont(_FONT, 10)
            for top, (label, value, other_label, other_value) in ((760, ("작성 부서", "인재개발팀", "작성일", "2026-09-15")),
                                                                  (745, ("승인자", "박본부장", "예산 총액", "48,500,000원"))):
                pdf.drawString(60, top, label)
                pdf.drawString(126, top, value)
                pdf.drawString(290, top, other_label)
                pdf.drawString(356, top, other_value)
            _grid(pdf, 732, 18, [60, 160, 260, 360, 460], [["구분", "과정명", "인원", "예산"], ["직무", "데이터 분석", "60", "9,000"],
                                                          ["공통", "법정 교육", "320", "5,600"]])

        result = convert(_korean_pdf(draw), "a.pdf")

        text = result.text
        assert text.index("인재개발팀") < text.index("승인자") < text.index("박본부장") < text.index("작성일") < text.index("예산 총액")


class TestCentredLinesOverTwoColumns:
    """On a two-column paper page whose right column is short and which has centred title and affiliation lines at the top, those lines were
    counted as right-column lines, so the column decision narrowed to only the height where the two columns run side by side and the section order
    became 1 → 4 → 2 → 3."""

    def test_short_right_column_under_centred_lines_comes_after_the_left_column(self):
        def draw(pdf, index):
            def at(x, top, size, text, centre=False):
                pdf.setFont(_FONT, size)
                (pdf.drawCentredString if centre else pdf.drawString)(x, 842 - top - size * 0.86, text)

            at(300, 76, 16, "물류 수요 예측 모델의 성능 비교", centre=True)
            at(300, 105, 9.5, "김연구, 이교수", centre=True)
            at(300, 131, 9.5, "한빛대학교 산업공학과, 미래대학교 경영학과", centre=True)
            at(88, 156, 9.5, "요약: 본 연구는 세 모델의 일별 물동량 예측 성능을 비교하였다. 실험 결과 변환기 모델의")
            at(88, 170, 9.5, "실험한 결과 변환기 모델의 오차가 가장 낮았다.")
            for title, top, count in (("1. 서론", 201, 7), ("2. 실험 방법", 343, 4), ("3. 결과 및 논의", 440, 8)):
                at(88, top, 13, title)
                for number in range(count):
                    at(88, top + 22 + number * 14, 9.5, f"{title[0]}절 본문: 물류 수요 예측은 인력 배치와 재고")
            at(308, 201, 13, "4. 결론")
            for number in range(3):
                at(308, 223 + number * 14, 9.5, "세 모델 모두 실무 적용이 가능한 수준이며")
            at(308, 265, 9.5, "권한다.")

        result = convert(_korean_pdf(draw), "a.pdf")

        headings = [line for line in result.text.splitlines() if line.startswith("## ")]
        assert headings == ["## 1. 서론", "## 2. 실험 방법", "## 3. 결과 및 논의", "## 4. 결론"]


def _hebrew_pdf() -> bytes:
    """A page with the Hebrew 'שלום' drawn in logical order (starting from the rightmost letter). The characters are defined by the ToUnicode of an
    Identity-H font."""
    import zlib

    from tests.test_pdf import _raw_pdf

    def stream(body: bytes) -> bytes:
        packed = zlib.compress(body)
        return b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(packed) + packed + b"\nendstream"

    glyphs = b"".join(b"BT /F1 12 Tf %d 700 Td <%04X> Tj ET\n" % (200 - index * 7, index + 1) for index in range(4))
    cmap = (b"/CIDInit /ProcSet findresource begin 12 dict begin begincmap /CMapName /T def "
            b"1 begincodespacerange <0000> <FFFF> endcodespacerange "
            b"4 beginbfchar <0001> <05E9> <0002> <05DC> <0003> <05D5> <0004> <05DD> endbfchar "
            b"endcmap CMapName currentdict /CMap defineresource pop end end")
    return _raw_pdf({1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                     3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R "
                        b"/Resources << /Font << /F1 6 0 R >> >> >>",
                     4: stream(glyphs), 5: stream(cmap),
                     6: b"<< /Type /Font /Subtype /Type0 /BaseFont /T /Encoding /Identity-H /ToUnicode 5 0 R "
                        b"/DescendantFonts [<< /Type /Font /Subtype /CIDFontType2 /BaseFont /T "
                        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> /DW 580 >>] >>"})


class TestSymbolsDrawnAfterTheirLine:
    """LibreOffice draws bullet symbols and Roman numerals in a fallback font, later than the rest of the line. pdfminer joins characters in
    drawing order, so '○ 5개 기관 선정' became '5○ 개 기관 선정' and 'Ⅰ. 사업 개요' became '. Ⅰ 사업 개요'."""

    @pytest.mark.parametrize(("symbol", "rest"), [("○", " 5개 기관 선정"), ("※", " 1인당 최대 500만원"), ("Ⅰ", ". 사업 개요")])
    def test_symbol_drawn_after_the_rest_of_its_line_is_read_first(self, symbol, rest):
        def draw(pdf, index):
            pdf.setFont(_FONT, 11)
            width = pdf.stringWidth(symbol, _FONT, 11)
            head = rest[:2]
            pdf.drawString(90 + width, 700, head)  # drawn in the order: start of the line → symbol → the rest
            pdf.drawString(90, 700, symbol)
            pdf.drawString(90 + width + pdf.stringWidth(head, _FONT, 11), 700, rest[2:])

        result = convert(_korean_pdf(draw), "a.pdf")

        assert f"{symbol}{rest}" in result.text

    _LINES = ["The quick brown fox jumps over the lazy dog", "and then it runs away into the deep dark forest",
              "where nobody will ever be able to find it again", "until the morning comes with a bright sun"]

    @pytest.mark.parametrize("letters_backwards", [False, True])
    def test_words_drawn_from_right_to_left_without_spaces_are_read_in_place(self, letters_backwards):
        """Words of a line drawn last to first with no space glyph: pdfminer took a short word and the word drawn after it,
        to its left, for one piece and joined them in drawing order ('comes awith bright'). The same for the letters of a
        word drawn last to first."""
        def draw(pdf, index):
            pdf.setFont("Helvetica", 11)
            for number, line in enumerate(self._LINES):
                x, placed = 72.0, []
                for word in line.split():
                    if letters_backwards:
                        placed.extend((x + pdf.stringWidth(word[:at], "Helvetica", 11), word[at]) for at in range(len(word)))
                    else:
                        placed.append((x, word))
                    x += pdf.stringWidth(word + " ", "Helvetica", 11)
                for left, text in reversed(placed):
                    pdf.drawString(left, 700 - 14 * number, text)

        result = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert result.text == "\n".join(self._LINES)

    def test_right_to_left_text_drawn_in_logical_order_is_not_reversed(self):
        """For right-to-left text drawn in logical order it is correct that the characters' x decreases. Re-placing them in x order would reverse
        the text."""
        result = convert(_hebrew_pdf(), "a.pdf")

        assert "שלום" in result.text


class TestEquationNumbers:
    """A centred equation and its equation number '(1)' at the right edge are lines beside each other, so an equation that had been read behind the
    paragraph below could not come back to its place, and the numbers were grouped separately and bunched at the end of the page."""

    def test_equations_and_their_numbers_stay_where_they_are_drawn(self):
        def draw(pdf, index):
            pdf.setFont(_FONT, 11)
            y = 760
            for text in ("본 절에서는 제안하는 손실 함수를 정의한다. 학습 데이터의 각 표본에 대하여 예측값과",
                         "정답 사이의 차이를 다음과 같이 계산한다."):
                pdf.drawString(60, y, text)
                y -= 16
            y -= 10
            pdf.drawString(250, y, "L = - Σ y log p")
            pdf.drawString(500, y, "(1)")
            y -= 26
            for text in ("여기서 y는 정답 분포, p는 모델의 예측 분포이다. 정규화 항을 더한 최종 목적 함수는", "다음과 같다."):
                pdf.drawString(60, y, text)
                y -= 16
            y -= 10
            pdf.drawString(240, y, "J = L + λ w²")
            pdf.drawString(500, y, "(2)")
            pdf.drawString(60, y - 26, "λ는 0.01로 설정하였다. 식 (2)를 최소화하도록 학습한다.")

        text = convert(_korean_pdf(draw), "a.pdf", ConvertOptions(page_markers=False)).text

        assert text.index("L = - Σ y log p (1)") < text.index("여기서 y는") < text.index("J = L + λ w² (2)") < text.index("λ는 0.01")


class TestShortColumnsUnderOneHeading:
    """Last page of a two-column paper: the left column has a few lines of a paragraph carried over from the previous page and the right column
    only the '참고문헌' subheading and its list, which falls short of the column decision, so the subheading was wedged in after the first line of the
    left column."""

    _LEFT = ["평균 3.2%p 향상되었으며, 특히 긴 문맥에서 성능 저", "하가 적었다. 실험은 NVIDIA A100 GPU 8장에서",
             "수행하였고 학습률은 2e-5, 배치 크기는 32로 설정", "하였다."]
    _RIGHT = ['[1] A. Vaswani et al., "Attention is all you need," NeurIPS,', "2017.",
              '[2] 박철수, "한국어 형태소 분석기 비교," 정보과학회논문지,', "45(3), pp. 123-130, 2018."]

    def _pdf(self) -> bytes:
        def draw(pdf, index):
            pdf.setFont(_FONT, 9.5)
            for number, text in enumerate(self._LEFT):
                pdf.drawString(57, 780 - number * 14.25, text)
            pdf.setFont(_FONT, 11)
            pdf.drawString(309, 782, "참고문헌")
            pdf.setFont(_FONT, 8)
            for number, text in enumerate(self._RIGHT):
                pdf.drawString(309, 757 - number * 12, text)

        return _korean_pdf(draw)

    def test_heading_of_the_right_column_comes_after_the_left_column(self):
        text = convert(self._pdf(), "a.pdf", ConvertOptions(page_markers=False)).text

        assert text.index("하였다.") < text.index("참고문헌") < text.index("[1] A. Vaswani")
        assert "\n".join(self._LEFT) in text


class TestSidebarBesideTheBody:
    """The left 30% sidebar of a résumé (name, contact details) has narrow lines and does not qualify for the two-column decision, and pdfminer
    read the sidebar after reading all of the body on the right, so the name came out at the end of the page."""

    _SIDE = [(20, "김민준"), (9.5, "백엔드 개발자"), (13, "연락처"), (9.5, "010-9876-5432"), (9.5, "minjun@example.com"),
             (13, "기술"), (9.5, "Python, Go, Kotlin"), (9.5, "PostgreSQL, Redis")]
    _MAIN = [(13, "경력"), (9.5, "(주)네오페이 — 시니어 백엔드 엔지니어 (2021.03 ~ 현재)"),
             (9.5, "결제 승인 시스템을 마이크로서비스로 전환하여 평균 응답 시간을 320ms에서 85ms로"),
             (9.5, "단축하였다. 일 거래 1,200만 건을 처리하는 정산 배치를 설계하고 운영하였다."), (13, "프로젝트"),
             (9.5, "실시간 이상거래 탐지 (2023)"), (9.5, "Kafka 스트림 기반 규칙 엔진을 구축하여 이상거래 탐지율을 18%p 높였다.")]

    # Top coordinates of the lines in the Chrome printout (from the top of the page). The body column starts a little above the sidebar
    _MAIN_TOPS = (59.8, 82.0, 97.8, 112.0, 127.8, 143.5, 157.8)
    _SIDE_TOPS = (67.0, 108.3, 130.3, 152.5, 168.3, 184.0, 206.0, 228.3)

    def _pdf(self, side) -> bytes:
        def draw(pdf, index):
            for (size, text), top in zip(self._MAIN, self._MAIN_TOPS, strict=True):
                pdf.setFont(_FONT, size)
                pdf.drawString(216, 842 - top - size * 0.85, text)
            for (size, text), top in zip(side, self._SIDE_TOPS, strict=True):
                pdf.setFont(_FONT, size)
                pdf.drawString(50, 842 - top - size * 0.85, text)

        return _korean_pdf(draw)

    def test_sidebar_with_the_name_is_read_before_the_body(self):
        text = convert(self._pdf(self._SIDE), "a.pdf", ConvertOptions(page_markers=False)).text

        assert text.index("김민준") < text.index("PostgreSQL, Redis") < text.index("경력") < text.index("이상거래 탐지율")

    def test_side_notes_without_the_page_title_stay_after_the_body(self):
        """When the sidebar does not hold the largest text on the page (margin notes), the order is the same as before."""
        notes = [(9.5, text) for _, text in self._SIDE]

        before = convert(self._pdf(notes), "a.pdf", ConvertOptions(page_markers=False)).text

        assert before.index("경력") < before.index("김민준")


class TestDropCaps:
    """A paragraph that opens with a drop cap: a large initial hanging beside its first lines. pdfminer put the initial in the
    first line with a space after it ('T he quick'), and its height kept the first line apart from the rest of the
    paragraph."""

    _LINES = ["he quick brown fox jumps over the", "lazy dog and keeps running far", "away from the hunters in the woods"]
    _NEXT = ["Next paragraph starts here and continues", "on another line for a while."]

    def _pdf(self, initial: str, lines: list[str], size: float = 36, baseline: float | None = None) -> bytes:
        def draw(pdf, index):
            pdf.setFont("Helvetica", size)
            pdf.drawString(72, 700 + 0.718 * 11 - 0.718 * size if baseline is None else baseline, initial)
            left = 72 + pdf.stringWidth(initial, "Helvetica", size) + 2
            pdf.setFont("Helvetica", 11)
            for number, text in enumerate(lines):
                pdf.drawString(left, 700 - 13 * number, text)
            for number, text in enumerate(self._NEXT):
                pdf.drawString(72, 640 - 13 * number, text)

        return _korean_pdf(draw)

    @pytest.mark.parametrize("baseline", [None, 676])
    def test_a_drop_cap_is_joined_to_its_word_and_paragraph(self, baseline):
        text = convert(self._pdf("T", self._LINES, baseline=baseline), "a.pdf", ConvertOptions(page_markers=False)).text

        assert text == "\n".join(["The quick brown fox jumps over the", *self._LINES[1:]]) + "\n\n" + "\n".join(self._NEXT)

    @pytest.mark.parametrize(("initial", "lines", "size", "baseline"), [
        ("A", ["General terms of the agreement apply", "to every order placed by the buyer"], 36, None),  # a capital follows
        ("B", ["grade awarded to the second team", "for the final round of the season"], 24, 700),  # sits on the line
        ("2", ["overview of the results we found", "in the survey of the last two years"], 36, None),  # a number
        ("T", ["he quick brown fox jumps over the", "lazy dog and keeps running far"], 15, None)])  # hardly larger
    def test_a_large_label_that_is_no_drop_cap_keeps_its_space(self, initial, lines, size, baseline):
        text = convert(self._pdf(initial, lines, size, baseline), "a.pdf", ConvertOptions(page_markers=False)).text

        assert f"{initial} {lines[0]}" in text

    @pytest.mark.parametrize("bad", [float("inf"), float("nan"), -float("inf")])
    def test_characters_without_a_finite_position_are_no_line_beside_an_initial(self, bad):
        initial = {"text": "T", "size": 36.0, "x0": 72.0, "x1": 94.0, "top": 100.0, "bottom": 136.0}
        beside = [{"text": text, "size": 11.0, "x0": 96.0 + 6 * index, "x1": 102.0 + 6 * index, "top": bad, "bottom": bad}
                  for index, text in enumerate("he quick")]

        assert _drop_cap([initial, *beside], "T he quick") is None
