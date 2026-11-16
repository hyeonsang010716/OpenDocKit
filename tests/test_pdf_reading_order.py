"""Reading order of one-column PDF pages: a short centred caption and the paragraph below it, and the positions of tables and pictures."""

from io import BytesIO

import pytest

from opendockit import ConvertOptions, convert
from opendockit.extractors._pdf_chars import _drop_cap, _initial_is_a_word
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

    @pytest.mark.parametrize(("state", "matrix", "expected"), [
        (b"/F1 -11 Tf", b"1 0 0 1 400 700 Tm", "Totalamountduetoday"),   # a negative font size: turned by 180° without a matrix
        (b"/F1 11 Tf -100 Tz", b"1 0 0 1 400 700 Tm", "Totalamountduetoday"),  # negative horizontal scaling: mirrored
        (b"/F1 11 Tf", b"-1 0 0 -1 400 700 Tm", "Totalamountduetoday"),  # the same turn written in the matrix
        (b"/F1 -11 Tf", b"-1 0 0 -1 72 700 Tm", "Total amount due today"),  # both at once: ordinary text running rightwards
    ])
    def test_text_running_leftwards_without_a_turned_matrix_is_not_reversed(self, state, matrix, expected):
        """Text turned upside down by a negative font size, or mirrored by negative horizontal scaling, runs right to left
        on the page under an upright text matrix. With its words set apart by kerning instead of space glyphs it was taken
        for words drawn last to first and laid in x order: 'yadot eud tnuoma latoT'. Text turned twice (a negative size in
        a turned matrix) runs rightwards and keeps its word gaps."""
        from tests.test_pdf import _raw_pdf

        content = b"BT " + state + b" " + matrix + b" [(Total) -278 (amount) -278 (due) -278 (today)] TJ ET"
        data = _raw_pdf({1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                         3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 "
                            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >> >> >> >>",
                         4: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content)})

        result = convert(data, "a.pdf", ConvertOptions(page_markers=False))

        assert result.text == expected

    # A font whose '~' has no width, as a zero-width mark has in an embedded font.
    _NO_WIDTH_FONT = (b"<< /Type /Font /Subtype /TrueType /BaseFont /Custom /Encoding /WinAnsiEncoding /FirstChar 32 /LastChar 255"
                      b" /Widths [%s] /FontDescriptor << /Type /FontDescriptor /FontName /Custom /Flags 32 /Ascent 900"
                      b" /Descent -200 /FontBBox [0 -200 1000 900] >> >>"
                      % b" ".join(b"0" if code == 0x7E else b"556" for code in range(32, 256)))

    def _letters_drawn_backwards(self, mark: bytes, as_pdf: bool = False) -> str | bytes:
        """Text of a page (or the page itself) whose line 'Total amount due today' is drawn letter by letter, last to first,
        followed by `mark` (operators drawing one more glyph before the line)."""
        from reportlab.pdfbase.pdfmetrics import stringWidth

        from tests.test_pdf import _raw_pdf

        x, placed = 72.0, []
        for word in "Total amount due today".split():
            placed.extend((x + stringWidth(word[:at], "Helvetica", 11), word[at]) for at in range(len(word)))
            x += stringWidth(word + " ", "Helvetica", 11)
        content = b"BT /F1 11 Tf " + b" ".join(b"1 0 0 1 %.2f 700 Tm (%s) Tj" % (left, letter.encode()) for left, letter in reversed(placed))
        content += b" " + mark + b" ET"
        data = _raw_pdf({1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                         3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 "
                            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >> /F2 "
                            + self._NO_WIDTH_FONT + b" >> >> >>",
                         4: b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content)})
        return data if as_pdf else convert(data, "a.pdf", ConvertOptions(page_markers=False)).text

    @pytest.mark.parametrize(("mark", "expected"), [
        (b"1 0 0 1 66 700 Tm (\\177) Tj", "• Total amount due today"),  # the list bullet of reportlab: no width in pdfminer
        (b"/F2 11 Tf 1 0 0 1 66 700 Tm (~) Tj", "~ Total amount due today"),  # a glyph whose font gives it the width 0
    ])
    def test_a_glyph_without_advance_does_not_keep_its_line_in_drawing_order(self, mark, expected):
        """A glyph of no width moves the pen nowhere, so it tells no direction. It was taken for text running leftwards,
        and the line it stood on — ordinary text whose letters were drawn last to first — stayed in drawing order:
        'yadoteudtnuomalatoT•'."""
        assert self._letters_drawn_backwards(mark) == expected

    @pytest.mark.parametrize(("mark", "laid_in_x_order"), [(b"1 0 0 1 60 700 Tm (*) Tj", True), (b"0 1 -1 0 66 700 Tm (*) Tj", False)])
    def test_a_piece_holding_a_glyph_turned_by_a_quarter_is_left_in_drawing_order(self, mark, laid_in_x_order):
        """A glyph turned by 90° does not run along x: a piece holding one is not laid in x order, as it was not before
        the direction was told by the advance. pdfminer puts such a glyph on a line of its own on the pages tried, so the
        piece is put together here from the characters of the page."""
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTChar, LTTextLineHorizontal

        from opendockit.extractors._pdf_pieces import _late_symbols_in_place

        def chars(node):
            return [node] if isinstance(node, LTChar) else [char for child in getattr(node, "_objs", []) for char in chars(child)]

        page = next(iter(extract_pages(BytesIO(self._letters_drawn_backwards(mark, as_pdf=True)))))
        drawn = sorted(chars(page), key=lambda char: -char.x0)  # last letter first, the mark at the end
        assert len(drawn) == 20
        piece = LTTextLineHorizontal(0.1)
        for char in drawn:
            piece.add(char)

        relaid = _late_symbols_in_place(piece, 0.1)

        assert (relaid is not None) == laid_in_x_order
        if relaid is not None:
            assert relaid.get_text().strip() == "* Total amount due today"


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


# Sentence openers set as drop caps, by what the initial is: the first letter of the first word, or a word of its own
# (TestDropCaps.test_how_often_the_letters_alone_misjudge_a_sentence_opener).
_WORDS_BEGINNING_WITH_A = """
    After a long day|Although the weather|All the children|As the sun rose|At the end of|An old man|And then there|
    Among the many|About a year ago|Another way of|Any attempt to|Are we alone|Around the world|Again and again|Also known as|
    Always the first|Across the river|According to the|Above the town|Almost every day|Already the leaves|Along the coast|
    Alone in the house|Anyone who has|Anything can happen|Ancient maps show|Art has always|Air travel was|Age is only|
    Animals of every|Autumn came early|August was hot|April showers bring|America was changing|Americans love their|
    Alice was beginning|Arthur had never|Adam looked up|Anna opened the|Actually it was|Additionally the report|
    Admittedly the plan|Apparently no one|Afterwards they went|Amid the chaos|Against all odds|Ahead of the|Apart from the|
    Aside from that|Asked about the|Asking for help|Away from home|Able to walk|Advances in medicine|Agriculture remains the|
    Architecture is frozen|Attention to detail|Authors often say|Average incomes rose|Awareness of the|Access to water|
    Adults and children|Africa is a|Australia has long|Algorithms now decide|Apples were cheap|Armed with a|Army units moved|
    Aunt Mary came|Awards were given|Afternoon light fell|Ask anyone who|Anger is a|Angels we have|Answers to these|
    Anybody can learn|Applications are due|Areas of the|Artists have long|Aspects of this""".replace("\n    ", "").strip().split("|")
_A_AND_A_WORD = """
    A long time ago|A few years ago|A man walked into|A new study shows|A small village lay|A great deal of|A good friend once|
    A year ago I|A single candle|A century ago the|A young woman|A little girl|A woman stood|A boy and his|A day in the|
    A week before the|A month later|A decade of|A number of|A lot has changed|A couple of|A group of|A recent report|
    A growing number|A common mistake|A simple question|A typical day|A large crowd|A big part of|A strange thing|A dark cloud|
    A cold wind|A quiet morning|A light rain|A soft knock|A loud bang|A sudden noise|A sharp turn|A deep breath|A low hum|
    A high wall|A thin line|A thick fog|A tall man|A short walk|A hundred years|A thousand miles|A million people|
    A friend of mine|A letter arrived|A dog barked|A child is|A story is|A book can|A house stood|A river runs|A person who|
    A family of|A storm was|A fire broke|A door opened|A voice called|A bird sang|A train passed|A few miles|A good book|
    A team of|A survey of|A majority of|A key question|A second look|A third of|A quarter of|A half century|A brief history|
    A closer look|A funny thing|A wise man|A rich merchant|A poor farmer|A king once|A unique feature|A useful rule|
    A university town|A one in a|A round table|A cross word|A lone wolf|A part of me|A way out|A head of state|A while ago|
    A piece of|A rose is|A gain of ten|A bout of flu|A like mind|A live wire|A broad smile|A far cry|A field of|A foot of snow|
    A top official|A side effect|A sleep study|A wake was held|A go at it|A mid sized firm""".replace("\n    ", "").strip().split("|")
_WORDS_BEGINNING_WITH_I = """
    In the beginning|It was a|If you have|Is there a|Indeed it was|Imagine a world|Instead of going|Its walls were|
    Into the woods|Inside the house|India is a|Italy has long|Ireland was then|Iron was first|Ice covered the|Ideas are cheap|
    Images of the|Important as it|Increasingly the public|Initially the plan|Interestingly the same|Immediately after the|
    Ironically it was|Israel and its|Island life is|Innovation is the|Industry leaders say|Investors were nervous|
    Information is power|Illness kept him|Inflation has been|Interest rates rose|Insects are the|International trade has|
    Individuals who have|Improvements in the|Issues of this|Items on the|Isolated from the|Ignoring the advice|
    Illustrated by the|Impressed by the|Infants learn to|Injuries are common|Ink was made|Inspired by the|Intelligence is not|
    Introduced in the|Invented in the|Irrigation made the|Identity is a|Idle hands are""".replace("\n    ", "").strip().split("|")
_I_AND_A_WORD = """
    I was born in|I am writing to|I have always|I had a dream|I will never|I remember the day|I think that|I never thought|
    I know a|I would like|I could not|I should have|I can still|I do not|I did not|I love the|I want to|I met him|I first saw|
    I still recall|I always wanted|I often wonder|I once knew|I used to|I grew up|I got up|I came to|I went to|I saw the|
    I heard a|I felt the|I found myself|I believe that|I suppose it|I wonder if|I wish to|I hope you|I mean to|I must admit|
    I may be|I might have|I shall never|I said nothing|I told him|I took the|I made a|I like to|I need to|I see no|I feel that|
    I live in|I lived there|I work in|I write this|I wrote the|I read the|I learned to|I spent the|I decided to|I started to|
    I began to|I woke up|I walked home|I stood there|I sat down|I looked up|I turned to|I tried to|I thought of|I knew then|
    I asked him|I enjoy the|I owe a|I understand that|I expected to|I ate the|I opened the|I arrived late|I entered the|
    I also remember|I only wish|I too have|I just wanted|I recently read|I confess that|I doubt that|I guess it""".replace("\n    ", "").strip().split("|")
_WORDS_BEGINNING_WITH_O = """
    On the first|Of all the|One day a|Once upon a|Our story begins|Only a few|Over the years|Often the best|Other people say|
    Out of the|Old houses have|Open the door|Originally built as|October was cold|Obviously this is|Ordinary people do""".replace("\n    ", "").strip().split("|")
_O_AND_A_WORD = """
    O come all ye|O my soul|O for a muse""".replace("\n    ", "").strip().split("|")


class TestDropCaps:
    """A paragraph that opens with a drop cap: a large initial hanging beside its first lines. pdfminer put the initial in the
    first line with a space after it ('T he quick'), and its height kept the first line apart from the rest of the
    paragraph."""

    _LINES = ["he quick brown fox jumps over the", "lazy dog and keeps running far", "away from the hunters in the woods"]
    _NEXT = ["Next paragraph starts here and continues", "on another line for a while."]

    def _pdf(self, initial: str, lines: list[str], size: float | None = None, baseline: float | None = None, *, rows: int = 2,
             gap: float = 2, space: str | None = None, spaces: bool = True, leading: float = 13) -> bytes:
        """A large `initial` and the 11pt `lines` beside it, `leading` apart, the first on the baseline 700. By default the
        initial is a drop cap over `rows` lines: it stands on the baseline of the last of them and its top is level with the
        capitals of the first. `space` writes a space glyph after the initial ('big') or before the text ('small');
        without `spaces` every word is placed by itself and the page has no space glyph at all."""
        def draw(pdf, index):
            height = (0.718 * 11 + leading * (rows - 1)) / 0.718 if size is None else size
            pdf.setFont("Helvetica", height)
            pdf.drawString(72, 700 - leading * (rows - 1) if baseline is None else baseline, initial + (" " if space == "big" else ""))
            left = 72 + pdf.stringWidth(initial, "Helvetica", height) + gap
            pdf.setFont("Helvetica", 11)
            for number, text in enumerate([*lines, *self._NEXT]):
                x, y = (left, 700 - leading * number) if number < len(lines) else (72, 640 - 13 * (number - len(lines)))
                text = " " + text if space == "small" and number == 0 else text
                for word in [text] if spaces else text.split():
                    pdf.drawString(x, y, word)
                    x += pdf.stringWidth(word + " ", "Helvetica", 11)

        return _korean_pdf(draw)

    @pytest.mark.parametrize("rows", [2, 3])
    def test_a_drop_cap_is_joined_to_its_word_and_paragraph(self, rows):
        text = convert(self._pdf("T", self._LINES, rows=rows), "a.pdf", ConvertOptions(page_markers=False)).text

        assert text == "\n".join(["The quick brown fox jumps over the", *self._LINES[1:]]) + "\n\n" + "\n".join(self._NEXT)

    _OPTION = ["the second option, which costs less", "but takes two more weeks to deliver", "and has no warranty at all"]

    @pytest.mark.parametrize(("initial", "lines", "options"), [
        ("A", ["General terms of the agreement apply", "to every order placed by the buyer"], {}),  # a capital follows
        ("2", ["overview of the results we found", "in the survey of the last two years"], {}),  # a number
        ("Q", ["how do I reset my password?"], {"size": 28, "baseline": 700 + 0.718 * (11 - 28)}),  # beside one line only
        ("B", _OPTION, {"size": 40, "baseline": 678.3}),  # centred beside three lines: it stands on none of them
        ("B", _OPTION, {"size": 14, "baseline": 698}),  # a little larger, on the first line: not dropped
        ("B", _OPTION, {"size": 60, "baseline": 687}),  # on the second line, but rising far above the first
        ("B", _OPTION, {"gap": 24})])  # in the margin, away from the text
    def test_a_large_label_that_is_no_drop_cap_keeps_its_space(self, initial, lines, options):
        text = convert(self._pdf(initial, lines, **options), "a.pdf", ConvertOptions(page_markers=False)).text

        assert f"{initial} {lines[0]}" in text

    _STEP = ["ress and hold the button until", "the light goes out and then", "let go of it and wait for", "the unit to start again"]

    @pytest.mark.parametrize(("options", "joined"), [
        ({"size": 19.8, "baseline": 689, "leading": 11}, False),  # 1.8 times the text, on the second line of text set solid
        ({"size": 22.6, "baseline": 689, "leading": 11}, True),  # the same place at 2.05 times
        ({"rows": 4, "leading": 13}, True),  # beside four lines: 5.9 times
        ({"rows": 4, "leading": 15}, False),  # beside four lines set wider apart: 6.7 times
    ])
    def test_a_letter_under_twice_or_over_six_times_the_text_is_no_drop_cap(self, options, joined):
        """A letter that stands on a line beside it like a drop cap, but is less than twice or more than six times the size
        of the text: an option or step letter ('D' + 'press and hold the button' was read 'Dpress and hold the button').
        Without a bound on the size, the baselines alone joined 21 of 2,000 such labels that had kept their space before."""
        text = convert(self._pdf("P", self._STEP, **options), "a.pdf", ConvertOptions(page_markers=False)).text

        assert text.startswith("Press and hold the button until\n" if joined else "P ress and hold the button until\n")

    def test_a_space_glyph_at_the_start_of_a_later_line_does_not_part_the_initial_from_its_word(self):
        """Only a space between the initial and the first line is the space after a one-letter word."""
        lines = [self._LINES[0], " " + self._LINES[1], self._LINES[2]]
        text = convert(self._pdf("T", lines), "a.pdf", ConvertOptions(page_markers=False)).text

        assert text.startswith("The quick brown fox jumps over the\nlazy dog")

    _TABLE = ["round table was set for the", "guests who came from far away", "to see the old king once more"]

    @pytest.mark.parametrize("space", ["big", "small"])
    def test_an_initial_followed_by_a_space_glyph_is_a_word(self, space):
        """'A' + ' round table': the space of a one-letter word is in the content stream, after the large letter or before
        the small text. The letters alone read 'Around'."""
        text = convert(self._pdf("A", self._TABLE, space=space), "a.pdf", ConvertOptions(page_markers=False)).text

        assert text == "\n".join(["A round table was set for the", *self._TABLE[1:]]) + "\n\n" + "\n".join(self._NEXT)

    @pytest.mark.parametrize(("initial", "first"), [("A", "long time ago in a small village"), ("I", "was born in the year 1632"),
                                                    ("I", "am the youngest of three"), ("A", "unique feature of the town")])
    def test_an_initial_that_is_a_word_keeps_its_space(self, initial, first):
        """Joined like any drop cap they were 'Along time ago' and 'Iwas born'. The page has no space glyph to tell: the
        letters decide. The first line still belongs to its paragraph."""
        lines = [first, *self._LINES[1:]]
        text = convert(self._pdf(initial, lines, spaces=False), "a.pdf", ConvertOptions(page_markers=False)).text

        assert text == "\n".join([f"{initial} {first}", *lines[1:]]) + "\n\n" + "\n".join(self._NEXT)

    @pytest.mark.parametrize(("initial", "first"), [
        ("A", "fter the rain had stopped"), ("A", "s the sun rose over"), ("I", "n the beginning there was"), ("I", "t was a dark"),
        ("A", "unt Mary came to stay"),  # 'A' is not written before a vowel
        ("A", "bout a year ago we"), ("I", "magine a world without"),  # the rest of a common word, although it could be a word
        ("O", "nce upon a time"), ("O", "ur story begins in")])
    def test_an_initial_of_a_word_beginning_with_a_or_i_is_joined(self, initial, first):
        text = convert(self._pdf(initial, [first, *self._LINES[1:]], spaces=False), "a.pdf", ConvertOptions(page_markers=False)).text

        assert text.startswith(f"{initial}{first}\n")

    def test_how_often_the_letters_alone_misjudge_a_sentence_opener(self):
        """The figures recorded for the rule that decides without a space glyph on the page (`_initial_is_a_word`)."""
        openers = {"A": (_WORDS_BEGINNING_WITH_A, _A_AND_A_WORD), "I": (_WORDS_BEGINNING_WITH_I, _I_AND_A_WORD),
                   "O": (_WORDS_BEGINNING_WITH_O, _O_AND_A_WORD)}
        split = {letter: [opener for opener in joined if _initial_is_a_word(letter, opener[1:])] for letter, (joined, _) in openers.items()}
        glued = {letter: [opener for opener in words if not _initial_is_a_word(letter, opener[2:])] for letter, (_, words) in openers.items()}

        assert sum(len(joined) + len(words) for joined, words in openers.values()) == 346
        assert [opener.split()[0] for opener in split["A"]] == ["Along", "Alone", "Alice", "Adam", "Ahead", "Apart", "Aside", "Asking",
                                                                "Away", "Agriculture", "Awareness", "Awards", "Aspects"]
        assert glued["A"] == ["A round table", "A cross word", "A gain of ten", "A bout of flu", "A mid sized firm"]
        assert [opener.split()[0] for opener in split["I"]] == ["Italy", "Ireland", "Isolated"]
        assert glued["I"] == [] and split["O"] == [] and glued["O"] == _O_AND_A_WORD

    @pytest.mark.parametrize("bad", [float("nan"), -float("inf")])
    def test_characters_without_a_finite_position_are_no_line_beside_an_initial(self, bad):
        """A character at a position that is not a number stands nowhere: it is neither the initial nor the first line, and
        the drop cap beside real lines is still read."""
        initial = {"text": "T", "size": 29.1, "x0": 72.0, "x1": 90.0, "top": 131.0, "bottom": 160.1}
        beside = [{"text": text, "size": 11.0, "x0": 92.0 + 6 * index, "x1": 98.0 + 6 * index, "top": 133.0 + 13 * row, "bottom": 144.0 + 13 * row}
                  for row in range(2) for index, text in enumerate("he quick")]
        nowhere = {"text": "x", "size": 11.0, "x0": bad, "x1": bad, "top": bad, "bottom": bad}

        assert _drop_cap([nowhere, initial, *beside], "T he quick") == ("The quick", 133.0, 144.0)


def _helvetica_story(paragraphs: list[str]) -> bytes:
    """A reportlab platypus document of `paragraphs` in 11pt Helvetica (inline markup such as `<sub>` allowed)."""
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, SimpleDocTemplate

    style = ParagraphStyle("body", fontName="Helvetica", fontSize=11, leading=14)
    buffer = BytesIO()
    SimpleDocTemplate(buffer).build([Paragraph(text, style) for text in paragraphs])
    return buffer.getvalue()


def _drawn_line(runs: list[tuple[str, float, float]]) -> bytes:
    """A page with one line drawn run by run in Helvetica: (text, size, how far its baseline is lowered in pt), each run
    starting where the previous one ends (an empty text leaves `size` pt blank), between a line above and a line below."""
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer)
    pdf.setFont("Helvetica", 11)
    pdf.drawString(72, 714, "The line above the formula is ordinary text.")
    x = 72.0
    for text, size, drop in runs:
        if not text:
            x += size
            continue
        pdf.setFont("Helvetica", size)
        pdf.drawString(x, 700 - drop, text)
        x += stringWidth(text, "Helvetica", size)
    pdf.setFont("Helvetica", 11)
    pdf.drawString(72, 686, "The line below the formula is ordinary text.")
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


class TestSubscriptsOnALineOfTheirOwn:
    """pdfminer keeps two characters on one line only when their boxes overlap by half the smaller height, so a subscript
    lowered by about 0.42 em or more (reportlab's `<sub>`) became a line of its own: the sentence was split in three around
    it and the subscript was put out of order ('Water is H', 'O in chemistry.', '2')."""

    def test_subscript_set_into_its_line_is_read_in_place(self):
        data = _helvetica_story(["First paragraph line is here.", "Water is H<sub>2</sub>O in chemistry.", "Third line."])

        text = convert(data, "a.pdf", ConvertOptions(page_markers=False)).text

        assert text == "First paragraph line is here.\nWater is H_2O in chemistry.\nThird line."

    def test_subscripts_followed_by_a_space_are_read_in_place(self):
        data = _helvetica_story(["Carbon dioxide CO<sub>2</sub> and methane CH<sub>4</sub> are gases.",
                                 "Index x<sub>i</sub> runs from one to n."])

        text = convert(data, "a.pdf", ConvertOptions(page_markers=False)).text

        assert "Carbon dioxide CO_2 and methane CH_4 are gases." in text
        assert "Index x_i runs from one to n." in text

    def test_subscript_under_a_superscript_is_left_alone(self):
        """A subscript with a superscript stacked over it is a formula's: joining the subscript alone would split the
        formula's pieces up differently."""
        from reportlab.pdfbase.pdfmetrics import stringWidth
        from reportlab.pdfgen import canvas

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer)
        pdf.setFont("Helvetica", 11)
        pdf.drawString(72, 700, "Sum of x")
        x = 72 + stringWidth("Sum of x", "Helvetica", 11)
        pdf.setFont("Helvetica", 8)
        pdf.drawString(x, 695, "j")
        pdf.drawString(x, 709, "j")  # the superscript as wide as the subscript, so the next piece touches both
        pdf.setFont("Helvetica", 11)
        pdf.drawString(x + stringWidth("j", "Helvetica", 8), 700, " over all rows.")
        pdf.showPage()
        pdf.save()

        text = convert(buffer.getvalue(), "a.pdf", ConvertOptions(page_markers=False)).text

        assert "x_j" not in text
        assert "Sum of x over all rows." in text  # as before subscripts were joined

    def test_short_lower_piece_fitting_the_gap_is_a_subscript(self):
        text = convert(_drawn_line([("Total", 11, 0), ("note", 8, 5), ("100", 11, 0)]), "a.pdf",
                       ConvertOptions(page_markers=False)).text

        assert "\nTotal_{note}100\n" in text

    @pytest.mark.parametrize(("runs", "kept"), [
        ([("Total", 11, 0), ("note", 8, 5), ("", 6, 0), ("100", 11, 0)], "note"),  # a blank after the small text: not one gap
        ([("Total", 11, 0), ("note", 8, 9), ("100", 11, 0)], "note"),         # lowered almost a line: the line below
        ([("Total", 11, 0), ("note", 9.5, 5.5), ("100", 11, 0)], "note"),     # nearly as large as the line: not a script
        ([("Total", 11, 0), ("ab-cd", 8, 5), ("100", 11, 0)], "ab-cd"),       # not a short word: not a script
    ])
    def test_lower_piece_that_does_not_fit_the_gap_is_not_joined(self, runs, kept):
        """Each run differs from the subscript of `test_short_lower_piece_fitting_the_gap_is_a_subscript` in one way."""
        text = convert(_drawn_line(runs), "a.pdf", ConvertOptions(page_markers=False)).text

        assert f"_{kept}" not in text and f"_{{{kept}}}" not in text
        assert kept in text
