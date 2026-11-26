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

    from opendockit.extractors._pdf_table_filters import _without_highlights

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

    assert objects[0] and elapsed < 4


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

    from opendockit.extractors._pdf_table_filters import _without_highlights

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


class TestManyHighlightedLines:
    """Telling the cells of a filled table row from highlights looked at every word of the page for every highlighted line,
    so a tall page of 4,000 lines, each with two touching highlights, took 15 seconds."""

    @staticmethod
    def _page(lines: int) -> bytes:
        from io import BytesIO

        from reportlab.pdfgen import canvas

        height = lines * 12 + 100
        buffer = BytesIO()
        page = canvas.Canvas(buffer, pagesize=(600, height))
        page.setFont("Helvetica", 10)
        first, second = page.stringWidth("ab", "Helvetica", 10), page.stringWidth("ab cd", "Helvetica", 10)
        for line in range(lines):
            y = height - 50 - line * 12
            page.setFillColorRGB(1, 1, 0)
            page.rect(40, y - 2, first, 10.5, stroke=0, fill=1)
            page.rect(40 + first, y - 2, second - first, 10.5, stroke=0, fill=1)
            page.setFillColorRGB(0, 0, 0)
            page.drawString(40, y, "ab cd ef gh ij kl mn op qr st")
        page.save()
        return buffer.getvalue()

    @pytest.mark.timing
    def test_each_line_looks_only_at_the_words_near_it(self):
        """The highlights are told apart in less time than pdfplumber takes to read the page (0.4 of it; 7 times it before).
        Measured against that reading on the same machine: a bound of 4 seconds failed on a slow Windows runner, and four
        times the lines against the lines, tried instead, left too little room on a Linux runner."""
        from io import BytesIO

        import pdfplumber

        from opendockit.extractors._pdf_table_filters import _without_highlights
        from tests.timing import fastest

        data = self._page(4000)

        def read() -> None:
            with pdfplumber.open(BytesIO(data)) as document:
                document.pages[0].chars, document.pages[0].rects

        reading = fastest(read)
        with pdfplumber.open(BytesIO(data)) as document:
            page = document.pages[0]
            objects = (page.chars, page.rects)  # parsing time is not measured
            left = _without_highlights(page)
            assert len(objects[1]) == 8000 and len(left.rects) == 0  # the highlights of every line are removed
            assert fastest(lambda: _without_highlights(page)) < 1.5 * reading


def _table_with_shaded_header(line_shading: bool, between: bool = False) -> bytes:
    """A ruled table of three columns whose header cells are shaded; the last header cell holds four lines. With
    `line_shading`, every header line is shaded once more inside its cell in the cell's colour, as Word shades a paragraph
    inside a shaded cell; with `between`, a white rectangle is drawn over the header cell before that line shading."""
    from io import BytesIO

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    columns = [72.0, 180.0, 300.0, 420.0]
    header = [["Jurisdiction"], ["Permitted"], ["Foreign", "Ownership", "Reporting", "Requirements"]]
    rows = [["Argentina", "Yes", "None"], ["Australia", "Yes", "To the agency"], ["Austria", "No", "None"]]
    top, line, row_height = 100.0, 13.0, 20.0
    header_height = 4 * line + 6
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    y = lambda top_y: A4[1] - top_y  # noqa: E731

    pdf.setFillColorRGB(1, 0.949, 0.8)
    for left, right in zip(columns, columns[1:], strict=False):
        pdf.rect(left, y(top + header_height), right - left, header_height, stroke=0, fill=1)
    if between:
        pdf.setFillColorRGB(1, 1, 1)
        pdf.rect(columns[2] + 2, y(top + header_height - 2), 116, header_height - 4, stroke=0, fill=1)
        pdf.setFillColorRGB(1, 0.949, 0.8)
    if line_shading:
        for column, cell in enumerate(header):
            for index in range(len(cell)):
                pdf.rect(columns[column] + 5, y(top + 3 + (index + 1) * line), columns[column + 1] - columns[column] - 10, line,
                         stroke=0, fill=1)
    pdf.setFillColorRGB(0, 0, 0)
    pdf.setFont("Helvetica", 10)
    for column, cell in enumerate(header):
        for index, text in enumerate(cell):
            pdf.drawString(columns[column] + 6, y(top + 3 + (index + 1) * line - 3), text)
    for number, row in enumerate(rows):
        for column, text in enumerate(row):
            pdf.drawString(columns[column] + 6, y(top + header_height + (number + 1) * row_height - 6), text)
    bottom = top + header_height + len(rows) * row_height
    for x in columns:
        pdf.line(x, y(top), x, y(bottom))
    for rule in [top, top + header_height, *(top + header_height + (n + 1) * row_height for n in range(len(rows)))]:
        pdf.line(columns[0], y(rule), columns[-1], y(rule))
    pdf.save()
    return buffer.getvalue()


class TestShadingInsideShadedCells:
    """Line shading drawn in a shaded cell's own colour is invisible and draws no rule (`_without_nested_fills`)."""

    def test_a_header_cell_shaded_again_line_by_line_stays_one_cell(self):
        text = convert(_table_with_shaded_header(line_shading=True), "a.pdf").text

        header = next(line for line in text.splitlines() if line.startswith("| Jurisdiction"))
        assert header.count("|") == 4 and "Foreign<br>Ownership" in header and "Requirements" in header
        assert "| Argentina | Yes | None |" in text
        assert text.count("Jurisdiction") == 1 and text.count("Requirements") == 1

    def test_it_reads_as_the_same_table_without_the_line_shading(self):
        with_lines = convert(_table_with_shaded_header(line_shading=True), "a.pdf").text

        assert with_lines == convert(_table_with_shaded_header(line_shading=False), "a.pdf").text

    def test_line_shading_over_a_rectangle_of_another_colour_is_kept(self):
        """A white rectangle drawn between the cell shading and the line shading makes the line shading's edges show."""
        from io import BytesIO

        import pdfplumber

        from opendockit.extractors._pdf_table_filters import _without_nested_fills

        with pdfplumber.open(BytesIO(_table_with_shaded_header(line_shading=True, between=True))) as pdf:
            page = pdf.pages[0]
            kept = _without_nested_fills(page)
            inner = [rect for rect in kept.rects if rect["x0"] > 420 - 116 and rect["height"] < 14]

        assert len(inner) == 4

    @pytest.mark.timing
    @pytest.mark.parametrize("shape", ["tall bars", "nested in two colours", "many in one band", "crossing"])
    def test_crowded_pages_take_linear_time(self, shape):
        """Each rectangle looks only at the rectangles near it in drawing order or overlapping it: looking at every fill of
        the height bands it covers, and at every rectangle of those bands for each outer one, a histogram of 4,000 tall bars
        took 14 seconds, 500 nested rectangles of two alternating colours 24 and 8,000 small ones in one band over a background
        of their colour 14. Rectangles that all overlap each other (bars across bars) stop at `_NESTED_FILL_WORK`."""
        from io import BytesIO

        import pdfplumber
        from reportlab.pdfgen import canvas

        from opendockit.extractors._pdf_table_filters import _without_nested_fills
        from tests.timing import fastest

        def page_pdf(count: int) -> bytes:
            buffer = BytesIO()
            if shape == "tall bars":
                pdf = canvas.Canvas(buffer, pagesize=(count * 2 + 100, 700))
                pdf.setFillColorRGB(0.2, 0.4, 0.7)
                for index in range(count):
                    pdf.rect(40 + 2 * index, 60, 1.5, 20 + index * 37 % 430, stroke=0, fill=1)
            elif shape == "nested in two colours":
                pdf = canvas.Canvas(buffer, pagesize=(count * 2 + 100, count * 2 + 100))
                for index in range(count):
                    pdf.setFillColorRGB(*((0.9, 0.9, 0.9) if index % 2 else (0.2, 0.2, 0.2)))
                    pdf.rect(10 + index, 10 + index, count * 2 + 80 - 2 * index, count * 2 + 80 - 2 * index, stroke=0, fill=1)
            elif shape == "crossing":
                pdf = canvas.Canvas(buffer, pagesize=(count * 2 + 100, count * 2 + 100))
                for index in range(count):
                    pdf.setFillColorRGB(0.5, 0.5, 0.5)
                    pdf.rect(10 + 2 * index, 10, 1.5, count * 2 + 50, stroke=0, fill=1)
                    pdf.setFillColorRGB(0.1, 0.5, 0.9)
                    pdf.rect(10, 10 + 2 * index, count * 2 + 50, 1.5, stroke=0, fill=1)
            else:
                pdf = canvas.Canvas(buffer, pagesize=(count * 2 + 100, 200))
                pdf.setFillColorRGB(0.9, 0.9, 0.9)
                pdf.rect(0, 0, count * 2 + 100, 200, stroke=0, fill=1)
                for index in range(count):
                    pdf.setFillColorRGB(*((0.9, 0.9, 0.9) if index % 2 else (0.2, 0.2, 0.2)))
                    pdf.rect(10 + 2 * index, 100, 1.5, 8, stroke=0, fill=1)
            pdf.save()
            return buffer.getvalue()

        def seconds(count: int) -> float:
            with pdfplumber.open(BytesIO(page_pdf(count))) as document:
                page = document.pages[0]
                page.rects  # noqa: B018 — pdfplumber's own reading is not measured
                return fastest(lambda: _without_nested_fills(page), runs=10)  # a millisecond or so: one slow run is not enough

        small, large = (seconds(250), seconds(1_000)) if shape == "nested in two colours" else (seconds(1_000), seconds(4_000))
        assert large < small * 8

    def test_nested_shading_is_found_on_a_crowded_page_as_well(self):
        """The line shading inside a shaded cell is found among hundreds of other fills, and a fill of the same colour beside
        it (not around it) does not hide it."""
        from io import BytesIO

        import pdfplumber
        from reportlab.pdfgen import canvas

        from opendockit.extractors._pdf_table_filters import _without_nested_fills

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=(800, 800))
        for row in range(20):
            for column in range(20):
                pdf.setFillColorRGB(*((0.85, 0.85, 0.85) if (row + column) % 2 else (0.6, 0.8, 0.9)))
                pdf.rect(20 + 30 * column, 20 + 30 * row, 30, 30, stroke=0, fill=1)
                if row == column:
                    pdf.rect(23 + 30 * column, 30 + 30 * row, 24, 10, stroke=0, fill=1)
        pdf.save()
        with pdfplumber.open(BytesIO(buffer.getvalue())) as document:
            page = document.pages[0]
            kept = _without_nested_fills(page)
            assert len(page.rects) == 420 and len(kept.rects) == 400
            assert not any(rect["height"] == 10 for rect in kept.rects)


def _bar_chart(values: list[int], *, hanging: bool = False, label: str | None = None, stacked: bool = False,
               labelled: bool = False) -> bytes:
    """A bar chart drawn as vector shapes: an axis with a gridline every 30pt, one filled bar per value standing on it (or
    hanging from it), the value above (below) each bar, and the category under the axis; with `label`, a caption in a ruled box under the chart whose
    first cell holds the figure label. With `stacked`, a second segment of another colour stands on each bar; with `labelled`,
    the first bar holds its value inside it."""
    from io import BytesIO

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    base = 500.0
    pdf.setFont("Helvetica", 9)
    pdf.drawString(72, 760, "The chart below shows the number of visitors in each quarter of the year.")
    pdf.line(90, base, 90 + 60 * len(values), base)
    pdf.setStrokeColorRGB(0.8, 0.8, 0.8)
    for step in range(1, 6):
        pdf.line(90, base - 30 * step if hanging else base + 30 * step, 90 + 60 * len(values), base - 30 * step if hanging else base + 30 * step)
    pdf.setStrokeColorRGB(0, 0, 0)
    for index, value in enumerate(values):
        x = 100 + index * 60
        pdf.setFillColorRGB(0, 0.377, 0.663)
        pdf.rect(x, base - value if hanging else base, 30, value, stroke=0, fill=1)
        if stacked:
            pdf.setFillColorRGB(0.9, 0.5, 0.1)
            pdf.rect(x, base + value, 30, 20, stroke=0, fill=1)
        pdf.setFillColorRGB(1, 1, 1) if labelled and not index else pdf.setFillColorRGB(0, 0, 0)
        inside = labelled and not index
        pdf.drawString(x + 8, base + value / 2 if inside else base - value - 12 if hanging else base + value + (24 if stacked else 4),
                       str(value))
        pdf.setFillColorRGB(0, 0, 0)
        pdf.drawString(x + 4, base + 8 if hanging else base - 14, f"Q{index + 1}")
    if label:
        pdf.rect(100, 380, 70, 20)
        pdf.rect(170, 380, 300, 20)
        pdf.drawString(106, 386, label)
        pdf.drawString(176, 386, "Visitors per quarter of the year")
    pdf.save()
    return buffer.getvalue()


def _striped_table() -> bytes:
    """A ruled table whose rows are shaded in turn, each row's shading as wide as the table and holding the row's text."""
    from io import BytesIO

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    rows = [["Region", "Visitors", "Change"], ["North", "1,200", "+4%"], ["South", "980", "-2%"], ["East", "1,430", "+9%"],
            ["West", "760", "+1%"]]
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setFont("Helvetica", 10)
    for number, row in enumerate(rows):
        top = 700 - number * 20
        if number % 2:
            pdf.setFillColorRGB(0.85, 0.9, 1)
            pdf.rect(100, top - 20, 300, 20, stroke=0, fill=1)
        pdf.setFillColorRGB(0, 0, 0)
        for column, text in enumerate(row):
            pdf.drawString(106 + column * 100, top - 14, text)
    for x in (100, 200, 300, 400):
        pdf.line(x, 700, x, 700 - 20 * len(rows))
    for number in range(len(rows) + 1):
        pdf.line(100, 700 - number * 20, 400, 700 - number * 20)
    pdf.save()
    return buffer.getvalue()


def _sideways_bar_chart(values: list[int]) -> bytes:
    """A bar chart with its bars drawn sideways from a vertical axis, a vertical gridline every 30pt, each value after its bar
    and each category before the axis."""
    from io import BytesIO

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    pdf.setFont("Helvetica", 9)
    pdf.drawString(72, 760, "The chart below shows the number of visitors in each region of the country.")
    axis, top = 150.0, 700.0
    pdf.line(axis, top, axis, top - 40 * len(values))
    pdf.setStrokeColorRGB(0.8, 0.8, 0.8)
    for step in range(1, 6):
        pdf.line(axis + 30 * step, top, axis + 30 * step, top - 40 * len(values))
    for index, value in enumerate(values):
        y = top - 40 * index - 30
        pdf.setFillColorRGB(0, 0.377, 0.663)
        pdf.rect(axis, y, value, 20, stroke=0, fill=1)
        pdf.setFillColorRGB(0, 0, 0)
        pdf.drawString(axis + value + 4, y + 6, str(value))
        pdf.drawString(axis - 40, y + 6, f"Area {index + 1}")
    pdf.save()
    return buffer.getvalue()


class TestBarCharts:
    """The bars of a vector bar chart draw no rules (`_without_chart_bars`)."""

    def test_bars_drawn_sideways_are_not_a_table(self):
        text = convert(_sideways_bar_chart([120, 80, 150]), "a.pdf").text

        assert "|" not in text and "Area 2" in text and "150" in text

    def test_bars_with_segments_stacked_on_them_are_not_a_table(self):
        assert "|" not in convert(_bar_chart([120, 80, 150, 60], stacked=True), "a.pdf").text

    def test_a_bar_holding_its_value_is_still_a_bar(self):
        assert "|" not in convert(_bar_chart([120, 80, 150, 60], labelled=True), "a.pdf").text

    def test_a_chart_labelled_above_its_bars_at_the_left_margin_is_read(self):
        """The characters of a bar's own width were looked up in a list whose name the loop after it took for 'width':
        a chart at the left margin with its values above the bars failed the whole PDF as corrupted."""
        from io import BytesIO

        from reportlab.pdfgen import canvas

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=(595, 842))
        pdf.setFont("Helvetica", 10)
        for line in range(20):
            pdf.drawString(200, 782 - 14 * line, f"Body line {line} of the report text beside the chart.")
        for index, value in enumerate([60, 90, 40, 120]):
            pdf.setFillColorRGB(0.2, 0.4, 0.7)
            pdf.rect(30 + 35 * index, 300, 20, value, stroke=0, fill=1)
            pdf.setFillColorRGB(0, 0, 0)
            pdf.drawString(33 + 35 * index, 304 + value, str(value))
        pdf.save()

        text = convert(buffer.getvalue(), "a.pdf").text

        assert "Body line 19" in text and "120" in text and "|" not in text

    @pytest.mark.timing
    def test_a_page_of_thousands_of_bars_is_read_in_linear_time(self):
        """A segment is looked up among the bars of its own edges: compared with every bar, 4,000 bars and 4,000 other fills
        took 2 seconds, four times as many sixteen times as long."""
        from io import BytesIO

        import pdfplumber
        from reportlab.pdfgen import canvas

        from opendockit.extractors._pdf_table_filters import _without_chart_bars
        from tests.timing import fastest

        def bars_pdf(count: int) -> bytes:
            buffer = BytesIO()
            pdf = canvas.Canvas(buffer, pagesize=(20 * count / 100 + 100, 4000))
            for index in range(count):
                x, row = 10 + 20 * (index % (count // 100)), index // (count // 100)
                pdf.setFillColorRGB(0, 0.4, 0.7)
                pdf.rect(x, 10 + 38 * row, 8, 5 + index % 7, stroke=0, fill=1)
                pdf.setFillColorRGB(0.9, 0.5, 0.1)
                pdf.rect(x + 9, 10 + 38 * row + 30, 6, 3, stroke=0, fill=1)
            pdf.save()
            return buffer.getvalue()

        def seconds(count: int) -> float:
            with pdfplumber.open(BytesIO(bars_pdf(count))) as document:
                page = document.pages[0]
                page.rects  # noqa: B018 — pdfplumber's own reading is not measured
                return fastest(lambda: _without_chart_bars(page))

        small, large = seconds(1_000), seconds(4_000)
        assert large < small * 8

    @pytest.mark.timing
    def test_tall_bars_over_many_characters_take_linear_time(self):
        """A bar looks only at the characters of its own width or of its own height: looking at those of every height band
        it covers, 4,000 bars 1,000pt tall with ten characters beside each took 4.7 seconds, four times as many sixteen times
        as long."""
        from io import BytesIO

        import pdfplumber
        from reportlab.pdfgen import canvas

        from opendockit.extractors._pdf_table_filters import _without_chart_bars
        from tests.timing import fastest

        def bars_pdf(count: int) -> bytes:
            buffer = BytesIO()
            pdf = canvas.Canvas(buffer, pagesize=(count * 5 + 100, 1400))
            pdf.setFont("Helvetica", 4)
            for index in range(count):
                pdf.setFillColorRGB(0, 0.4, 0.7)
                pdf.rect(10 + 5 * index, 10, 3, 1000 + index % 300, stroke=0, fill=1)
                pdf.setFillColorRGB(0, 0, 0)
                for line in range(10):
                    pdf.drawString(10 + 5 * index + 3.6, 20 + line * 90, "1")
            pdf.save()
            return buffer.getvalue()

        def seconds(count: int) -> float:
            with pdfplumber.open(BytesIO(bars_pdf(count))) as document:
                page = document.pages[0]
                page.rects, page.chars  # noqa: B018 — pdfplumber's own reading is not measured
                return fastest(lambda: _without_chart_bars(page))

        small, large = seconds(500), seconds(2_000)
        assert large < small * 8

    def test_a_bar_chart_is_not_a_table(self):
        text = convert(_bar_chart([120, 80, 150, 60]), "a.pdf").text

        assert "|" not in text
        assert "150" in text and "Q3" in text

    def test_bars_hanging_from_the_axis_are_not_a_table(self):
        assert "|" not in convert(_bar_chart([40, 90, 25], hanging=True), "a.pdf").text

    def test_a_table_with_shaded_rows_stays_a_table(self):
        text = convert(_striped_table(), "a.pdf").text

        assert "| Region | Visitors | Change |" in text
        assert "| South | 980 | -2% |" in text

    def test_a_figure_label_in_a_box_is_its_caption(self):
        text = convert(_bar_chart([120, 80, 150], label="Diagram 4"), "a.pdf").text

        assert "|" not in text
        assert "Diagram 4" in text and "Visitors per quarter of the year" in text


def _table_with_open_header() -> bytes:
    """A ruled table of seven columns under a header of two levels drawn without a top border or outer sides: 'No.', 'Party'
    and 'Change' are merged over both levels, 'Provisional' and 'Official' over two sub-columns each, and only the rules
    between the merged cells reach up to the first level."""
    from io import BytesIO

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    columns = [60, 100, 220, 280, 340, 400, 460, 520]
    first, second, body = 92, 118, 155
    rows = [["1", "North Party", "1,652", "28,008", "1,652", "28,008", "0"],
            ["2", "South Party", "1,649", "23,679", "1,623", "23,939", "+260"],
            ["3", "East Party", "715", "9,407", "680", "9,952", "+545"]]
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    y = lambda top: A4[1] - top  # noqa: E731
    pdf.setFont("Helvetica", 9)
    for x, text in ((64, "No."), (104, "Party"), (224, "Provisional"), (344, "Official"), (464, "Change")):
        pdf.drawString(x, y(first + 16), text)
    for x in (224, 344):
        pdf.drawString(x, y(second + 22), "Areas")
        pdf.drawString(x + 60, y(second + 22), "Candidates")
    for x in (100, 220, 340, 460):
        pdf.line(x, y(first), x, y(second))
    pdf.line(220, y(second), 460, y(second))
    for x in columns[1:-1]:
        pdf.line(x, y(second), x, y(body))
    bottom = body + 16 * len(rows)
    for x in columns:
        pdf.line(x, y(body), x, y(bottom))
    for number, row in enumerate(rows):
        pdf.line(columns[0], y(body + 16 * number), columns[-1], y(body + 16 * number))
        for column, text in enumerate(row):
            pdf.drawString(columns[column] + 4, y(body + 16 * number + 11), text)
    pdf.line(columns[0], y(bottom), columns[-1], y(bottom))
    pdf.save()
    return buffer.getvalue()


class TestHeaderWithoutTopBorder:
    """A two-level header drawn without a top border or outer sides belongs to its table (`_closed_above_open_header`)."""

    def test_the_first_header_level_is_a_row_of_the_table(self):
        text = convert(_table_with_open_header(), "a.pdf").text

        table = [line for line in text.splitlines() if line.startswith("<tr>")]
        assert table[:2] == ['<tr><th rowspan="2">No.</th><th rowspan="2">Party</th><th colspan="2">Provisional</th>'
                             '<th colspan="2">Official</th><th rowspan="2">Change</th></tr>',
                             "<tr><td>Areas</td><td>Candidates</td><td>Areas</td><td>Candidates</td></tr>"]
        assert ("<tr><td>2</td><td>South Party</td><td>1,649</td><td>23,679</td><td>1,623</td><td>23,939</td>"
                "<td>+260</td></tr>") in table
        assert not any(line.startswith(("No.", "Provisional")) for line in text.splitlines())
