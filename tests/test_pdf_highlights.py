"""PDF highlighter: fill-only rectangles that cover text exactly (as LibreOffice draws them for each run or font span) are not table rules."""


import pytest

from opendockit import convert
from tests.test_pdf import _FONT, _korean_pdf


_SIZE = 11.0


def _highlighted_line(pdf, x: float, baseline: float, runs: list[tuple[str, bool]]) -> float:
    """Writes the (text, filled) items of `runs` one after another and draws, for each filled span, a yellow rectangle fitted exactly to the width
    of its text. Digit/Latin spans are filled lower than Hangul spans (LibreOffice uses a different height for each font span). Returns the x of
    the line end."""
    from reportlab.lib import colors
    from reportlab.pdfbase.pdfmetrics import stringWidth

    for text, marked in runs:
        width = stringWidth(text, _FONT, _SIZE)
        if marked:
            ascii_run = text.strip().isascii()
            height = _SIZE * (1.17 if ascii_run else 1.69)
            pdf.setFillColor(colors.HexColor("#FFFF00"))
            pdf.rect(x, baseline - _SIZE * (0.12 if ascii_run else 0.4), width, height, stroke=0, fill=1)
        pdf.setFillColor(colors.black)
        pdf.setFont(_FONT, _SIZE)
        pdf.drawString(x, baseline, text)
        x += width
    return x


def _highlighted_pdf(lines: list[list[tuple[str, bool]]]) -> bytes:
    def draw(pdf, index):
        pdf.setFont(_FONT, 12)
        pdf.drawString(72, 780, "2.3 SV효과")
        baseline = 750.0
        for runs in lines:
            _highlighted_line(pdf, 72, baseline, runs)
            baseline -= _SIZE * 2
        pdf.setFont(_FONT, _SIZE)
        pdf.drawString(72, baseline - 10, "다음 문단은 평범한 본문이다.")

    return _korean_pdf(draw)


class TestHighlightedParagraphs:
    def test_fully_highlighted_paragraph_stays_a_paragraph(self):
        """The whole paragraph was highlighted, but the edges of the rectangles, whose height differs between Hangul and digit spans, became rules and
        produced a table split into one cell per word, like '| 재생용기는 일반 | HDPE 80% + | … | 1 | k | g | 당 |' (a feedback document exported
        from LibreOffice)."""
        lines = [[("재생용기는 일반 ", True), ("HDPE 80%", True), (" + 재생 ", True), ("HDPE 20%", True), ("로 구성되어 있으며", True)],
                 [("일반 ", True), ("HDPE", True), ("의 경우 ", True), ("1kg", True), ("당 온실가스를 배출한다.", True)]]

        result = convert(_highlighted_pdf(lines), "highlight.pdf")

        assert "|" not in result.text
        assert "재생용기는 일반 HDPE 80% + 재생 HDPE 20%로 구성되어 있으며" in result.text
        assert "의 경우 1kg당 온실가스를 배출한다." in result.text

    def test_few_highlighted_words_are_not_repeated_as_a_table(self):
        """Only a few words in the sentences were highlighted, but those words were appended once more after the paragraph as the table '| 핵심 수치 |
        5 | 만 톤 |'."""
        lines = [[("감축 효과의 ", False), ("핵심 수치 ", True), ("5", True), ("만 톤", True), ("은 내년부터 반영한다.", False)],
                 [("자세한 산식은 ", False), ("부록 ", True), ("A", True), ("를 참조한다.", False)]]

        result = convert(_highlighted_pdf(lines), "highlight.pdf")

        assert "|" not in result.text
        assert "감축 효과의 핵심 수치 5만 톤은 내년부터 반영한다." in result.text
        assert result.text.count("핵심 수치") == 1

    def test_highlighted_text_inside_a_ruled_table_keeps_the_table(self):
        """Highlighted text inside a cell of a ruled table is still a table cell (the rules make the table even without the filled rectangle)."""
        def draw(pdf, index):
            from reportlab.lib import colors

            edges, tops = [72, 200, 400], [760, 738, 716]
            for y in tops:
                pdf.line(edges[0], y, edges[-1], y)
            for x in edges:
                pdf.line(x, tops[0], x, tops[-1])
            pdf.setFont(_FONT, 10)
            pdf.drawString(78, 745, "항목")
            pdf.drawString(206, 745, "값")
            pdf.drawString(78, 723, "감축량")
            pdf.setFillColor(colors.HexColor("#FFFF00"))
            pdf.rect(206, 721, 40, 12, stroke=0, fill=1)
            pdf.setFillColor(colors.black)
            pdf.drawString(206, 723, "5만 톤")

        result = convert(_korean_pdf(draw), "cell.pdf")

        assert "| 항목 | 값 |\n| --- | --- |\n| 감축량 | 5만 톤 |" in result.text

    def test_cell_shading_aligned_in_a_column_is_not_a_highlight(self):
        """In a striped table with only 1pt of cell padding, the shading of the widest cell fits its text exactly. When another row has shading
        with the same left and right ends, it is cell shading aligned to a column and is kept — removing it as a highlight lost the boundary of the
        '담당' column and the name dropped out, as in '| 부산 | 830 |'."""
        rows = [["지점", "매출", "담당"], ["서울 본점", "1,250", "김민수"], ["부산", "830", "이정훈"], ["대구 지점", "610", "박지영"],
                ["광주", "540", "최서연"]]

        def draw(pdf, index):
            from reportlab.lib import colors
            from reportlab.pdfbase.pdfmetrics import stringWidth

            widths = [max(stringWidth(row[column], _FONT, 10) for row in rows) + 2 for column in range(3)]
            pdf.setFont(_FONT, 10)
            for row_index, row in enumerate(rows):
                y, x = 760 - row_index * 14, 60.0
                for column, text in enumerate(row):
                    if row_index % 2 == 0:
                        pdf.setFillColor(colors.HexColor("#DDDDDD"))
                        pdf.rect(x, y - 3.5, widths[column], 14, stroke=0, fill=1)
                    pdf.setFillColor(colors.black)
                    pdf.drawString(x + 1, y, text)
                    x += widths[column]

        result = convert(_korean_pdf(draw), "striped.pdf")

        assert "| 부산 | 830 | 이정훈 |" in result.text


def _chrome_table(rows: list[list[str]], *, marked: tuple[int, int] | None = None, shaded_header: bool = False,
                  borders: bool = True) -> bytes:
    """Draws a table the way Chrome prints one: rules are filled rectangles 0.75pt wide, the cell padding is 0.75pt, and column widths fit the
    widest text. The text of the `marked` (row, column) cell gets a yellow highlight (mark); with `shaded_header` every cell of the header row gets
    a grey background."""
    rule, pad, size, height = 0.75, 0.75, 10.0, 15.0

    def draw(pdf, index):
        from reportlab.lib import colors
        from reportlab.pdfbase.pdfmetrics import stringWidth

        widths = [max(stringWidth(row[column], _FONT, size) for row in rows) + pad * 2 for column in range(len(rows[0]))]
        edges = [60.0]
        for width in widths:
            edges.append(edges[-1] + width + rule)
        pdf.setFont(_FONT, size)
        for row_index, row in enumerate(rows):
            top = 760 - row_index * (height + rule)
            for column, text in enumerate(row):
                x = edges[column] + rule
                if shaded_header and row_index == 0:
                    pdf.setFillColor(colors.HexColor("#CCCCCC"))
                    pdf.rect(x, top - height, widths[column], height, stroke=0, fill=1)
                if marked == (row_index, column):
                    pdf.setFillColor(colors.HexColor("#FFFF00"))
                    pdf.rect(x + pad, top - height + 1.5, stringWidth(text, _FONT, size), height - 3, stroke=0, fill=1)
                pdf.setFillColor(colors.black)
                pdf.drawString(x + pad, top - height + 4, text)
        if not borders:
            return
        pdf.setFillColor(colors.black)
        for row_index in range(len(rows) + 1):  # rule segments drawn separately for each cell, as Chrome does
            top = 760 - row_index * (height + rule)
            for column in range(len(widths)):
                pdf.rect(edges[column], top, widths[column] + rule * 2, rule, stroke=0, fill=1)
                if row_index < len(rows):
                    pdf.rect(edges[column], top - height - rule, rule, height + rule, stroke=0, fill=1)
            if row_index < len(rows):
                pdf.rect(edges[-1], top - height - rule, rule, height + rule, stroke=0, fill=1)

    return _korean_pdf(draw)


class TestChromeTables:
    def test_highlight_in_a_cell_keeps_the_thin_rules_beside_it(self):
        """Chrome draws rules as 0.75pt filled rectangles and the cell padding is 1px, so a highlight inside a cell is within 1.5pt of the vertical
        rule beside it. Removing text-less rectangles together with the highlight they touch removed that rule too, making one cell: '| 사과 확인 필요
        입고 | | |'."""
        rows = [["품목", "상태", "비고"], ["사과", "확인 필요", "입고"], ["배", "정상", "출고"], ["감", "정상", "보류"]]

        result = convert(_chrome_table(rows, marked=(1, 1)), "mark.pdf")

        assert "| 사과 | 확인 필요 | 입고 |" in result.text

    def test_shaded_header_row_fitted_to_its_text_stays_the_header(self):
        """In a table whose column widths fit the header text, filling only the header row's cells makes the cell rectangles fit the text exactly, so
        they looked like highlights. The header became the paragraph '부서명칭 인원수합계 비고사항' outside the table and the first data row became
        the header."""
        rows = [["부서명칭", "인원수합계", "비고사항"], ["인사", "12", "-"], ["재무", "8", "신설"], ["영업", "30", "-"]]

        result = convert(_chrome_table(rows, shaded_header=True), "header.pdf")

        assert "| 부서명칭 | 인원수합계 | 비고사항 |\n| --- | --- | --- |\n| 인사 | 12 | - |" in result.text

    def test_shaded_header_of_a_borderless_table_stays_a_table_row(self):
        """In a borderless table too, the header row is not dissolved into a paragraph. Known accepted gap: the filled header row and the unruled
        data rows still come out as two tables (the same happens for a table with wide cell padding, which has nothing to do with highlights)."""
        rows = [["부서명칭", "인원수합계", "비고사항"], ["인사", "12", "-"], ["재무", "8", "신설"], ["영업", "30", "-"]]

        result = convert(_chrome_table(rows, shaded_header=True, borders=False), "header.pdf")

        assert "| 부서명칭 | 인원수합계 | 비고사항 |" in result.text
        assert "| 재무 | 8 | 신설 |" in result.text

    def test_same_highlighted_line_repeated_stays_a_paragraph(self):
        """When a highlighted line is repeated with the same text, the filled spans of each line line up in columns with the words of the lines above
        and below. Looking at columns among the filled words took them for a row of cells, producing the table '| 이것은 여러 런으로 나뉜 형광펜 문단
        | HDPE 80% 1kg |'."""
        runs = [("이것은 여러 런으로 나뉜 형광펜 문단 ", True), ("HDPE 80%", True), (" ", True), ("1kg", True)]

        result = convert(_highlighted_pdf([runs, runs, runs]), "repeated.pdf")

        assert "|" not in result.text
        assert result.text.count("이것은 여러 런으로 나뉜 형광펜 문단 HDPE 80% 1kg") == 3


@pytest.mark.timing
def test_many_painted_squares_beside_one_huge_letter_take_linear_time():
    """With one very large letter on the page, the band of characters selected for each rectangle is twice that letter's size, so on a page with
    40,000 small filled rectangles the highlight filtering alone took more than 10 seconds."""
    from io import BytesIO
    import random
    import time

    import pdfplumber
    from reportlab.pdfgen import canvas

    from opendockit.extractors.pdf import _without_highlights

    buffer = BytesIO()
    page = canvas.Canvas(buffer, pagesize=(600, 800))
    page.setFont("Helvetica", 250)
    page.drawString(10, 550, "A")
    page.setFont("Helvetica", 6)
    for y in range(20, 540, 7):
        page.drawString(10, y, "x" * 140)
    shuffle = random.Random(1)
    page.setFillColorRGB(0.9, 0.9, 0.2)
    for _ in range(20000):
        page.rect(shuffle.uniform(0, 590), shuffle.uniform(0, 540), 4, 4, stroke=0, fill=1)
    page.save()
    with pdfplumber.open(BytesIO(buffer.getvalue())) as document:
        first = document.pages[0]
        objects = (first.chars, first.rects)  # parsing time is not measured

        started = time.perf_counter()
        _without_highlights(first)
        elapsed = time.perf_counter() - started

    assert objects[0] and elapsed < 1.5


def _word_lines_pdf(lines: list[list[tuple[str, float, bool]]]) -> bytes:
    """Writes the (word, x, filled) items of each line at their positions and fills a filled word yellow, fitted exactly to its text width (one
    rectangle per word, like Chrome's mark)."""
    def draw(pdf, index):
        from reportlab.lib import colors
        from reportlab.pdfbase.pdfmetrics import stringWidth

        baseline = 760.0
        for words in lines:
            for text, x, marked in words:
                if marked:
                    pdf.setFillColor(colors.HexColor("#FFFF00"))
                    pdf.rect(x, baseline - 3, stringWidth(text, _FONT, _SIZE), _SIZE + 3, stroke=0, fill=1)
                pdf.setFillColor(colors.black)
                pdf.setFont(_FONT, _SIZE)
                pdf.drawString(x, baseline, text)
            baseline -= _SIZE * 1.6

    return _korean_pdf(draw)


def _spaced(words: list[str], *, marked: set[int] = frozenset(), at: dict[int, float] | None = None) -> list[tuple[str, float, bool]]:
    """(word, x, filled) for `words` written with spaces from the left at 72. A word in `at` starts at that x (the gap before it is stretched, as
    in justified text)."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont

    pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
    x, placed = 72.0, []
    for index, word in enumerate(words):
        x = (at or {}).get(index, x)
        placed.append((word, x, index in marked))
        x += pdfmetrics.stringWidth(word + " ", _FONT, _SIZE)
    return placed


def _aligned(words: str, marked: set[int], below: list[tuple[str, float, bool]]) -> list[tuple[str, float, bool]]:
    """Writes `words` with spaces, but aligns the last filled word to the start of the nearest word after it on the `below` line."""
    natural = _spaced(words.split())
    last = max(marked)
    return _spaced(words.split(), marked=marked, at={last: min(x for _, x, _ in below if x >= natural[last][1])})


def _remaining_rects(data: bytes) -> int:
    from io import BytesIO

    import pdfplumber

    from opendockit.extractors.pdf import _without_highlights

    with pdfplumber.open(BytesIO(data)) as document:
        return len(_without_highlights(document.pages[0]).rects)


class TestScatteredHighlightsInAParagraph:
    def test_marked_words_that_happen_to_line_up_with_the_next_line_are_highlights(self):
        """In a paragraph with a few words highlighted by Chrome's mark, when two filled words on one line happened to line up with the left margin
        and with the start of a word on the line below respectively, the line was taken for a filled table row and the highlights were kept. When a
        few such lines came together their edges became rules, producing a fake table like '| 중소기업 | 10월 |', and the filled words dropped out of
        the sentences. A line with unfilled words mixed in on the same line is not a table row."""
        below = _spaced("이번 사업은 중소기업 소상공인 청년 창업자를 대상으로 하며 신청이 가능하다".split())
        last = _spaced("선정 결과는 12월에 발표되며 지원 금액은 업체당 최대 5천만 원이다".split())
        lines = [_spaced("지역 경제 활성화를 위해 총 규모의 예산을 편성했다 세부 기준은".split()),
                 _aligned("정부는 올해 하반기 지원 사업을 추진한다 관계자는 밝혔다", {0, 3}, below), below,
                 _aligned("관계자는 2024년 대비 예산이 17% 늘었다고 밝혔다 신청 기간은", {0, 4}, last), last]

        data = _word_lines_pdf(lines)

        assert _remaining_rects(data) == 0
        result = convert(data, "marks.pdf")
        assert "|" not in result.text
        assert "정부는 올해 하반기 지원 사업을 추진한다" in result.text

    def test_runs_of_one_fully_highlighted_line_are_highlights(self):
        """A paragraph line highlighted in full can still have several runs (LibreOffice's Hangul and digit spans), and each run can line up in a
        column with words of the lines above and below. Abutting runs are one continuous highlight, not cells."""
        from reportlab.pdfbase.pdfmetrics import stringWidth

        below = _spaced("이번 사업은 중소기업 소상공인 청년 창업자를 대상으로 하며".split())
        runs, x = [], 72.0
        for text in ("정부는 올해 ", "2024", "년 하반기 지원 사업을"):
            runs.append((text, x, True))
            x += stringWidth(text, _FONT, _SIZE)
        lines = [_spaced("지역 경제 활성화를 위해 총 규모의 예산을 편성했다".split()), runs, below]

        assert _remaining_rects(_word_lines_pdf(lines)) == 0

    def test_short_highlights_at_line_starts_with_equal_widths_are_highlights(self):
        """Hangul characters all have the same width, so short highlights at the start of paragraph lines ('고', '다') had the same left and right
        ends on two lines. Different text filled at the same position was taken for cell shading aligned to a column and kept, producing a fake
        table like '| 고 | 31 |'. When there is unfilled text on the same line, it is not column shading."""
        lines = [_spaced("고 31 하반기 지역 경제 활성화를 위해 총 규모의 예산을".split(), marked={0, 1}),
                 _spaced("이번 사업은 중소기업 소상공인 청년 창업자를 대상으로 하며".split()),
                 _spaced("다 12 결과는 12월에 발표되며 지원 금액은 업체당 최대".split(), marked={0, 1})]

        assert _remaining_rects(_word_lines_pdf(lines)) == 0

    def test_highlights_in_cells_of_a_ruled_table_do_not_become_rules(self):
        """In a ruled table, when the text of every cell in two rows is filled to its text width, the Hangul cells ('장비', '홍보') have the same
        width and looked like cell shading aligned to a column, and the rows looked like filled table rows, so the highlights were kept. Their edges
        became vertical rules and the columns shifted, as in '| | 장비 | 150 |'."""
        rows = [["항목", "1차년도", "2차년도"], ["인건비", "300", "320"], ["장비", "150", "50"], ["교육", "80", "90"], ["홍보", "100", "110"]]

        def draw(pdf, index):
            from reportlab.lib import colors
            from reportlab.pdfbase.pdfmetrics import stringWidth

            edges, tops = [72, 180, 288, 396], [760 - row * 20 for row in range(len(rows) + 1)]
            for y in tops:
                pdf.line(edges[0], y, edges[-1], y)
            for x in edges:
                pdf.line(x, tops[0], x, tops[-1])
            for row, cells in enumerate(rows):
                for column, text in enumerate(cells):
                    x, baseline = edges[column] + 4, tops[row] - 14
                    if cells[0] in ("장비", "홍보"):
                        pdf.setFillColor(colors.HexColor("#FFFF00"))
                        pdf.rect(x, baseline - 3, stringWidth(text, _FONT, 10), 13, stroke=0, fill=1)
                    pdf.setFillColor(colors.black)
                    pdf.setFont(_FONT, 10)
                    pdf.drawString(x, baseline, text)

        data = _korean_pdf(draw)

        assert _remaining_rects(data) == 0
        result = convert(data, "ruled.pdf")
        assert "| 항목 | 1차년도 | 2차년도 |\n| --- | --- | --- |\n| 인건비 | 300 | 320 |\n| 장비 | 150 | 50 |" in result.text
