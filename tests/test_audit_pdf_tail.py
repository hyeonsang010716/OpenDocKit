"""PDF audit of the borderless-table, row-joining and repeated-line code: lost cell lines and super-linear time."""

from io import BytesIO
from itertools import product
from random import Random
import re
import time

import pytest

from opendockit import ConvertOptions, convert
from opendockit.extractors import pdf


def _pdf(draw, pages: int = 1, size: tuple[float, float] = (595, 842)) -> bytes:
    """A PDF whose pages (`size` points) are drawn with `draw(canvas, page index from 0)`."""
    from reportlab.pdfgen import canvas

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=size)
    for index in range(pages):
        draw(pdf, index)
        pdf.showPage()
    pdf.save()
    return buffer.getvalue()


class TestJoinedCellLinesAtThePageTop:
    """A borderless table at the top of every page whose first data row has a cell wrapped to two lines beside vertically
    centred cells: the wrapped cell is one joined line in the table, and its original lines were not among the lines
    protected from the repeated-line rule, so 'Quarterly revenue' was removed on every page and the table fell apart."""

    @staticmethod
    def _draw(pdf, page):
        pdf.setFont("Helvetica", 10)
        y = 700
        for x, text in ((60, "Item"), (220, "Count"), (330, "Note")):
            pdf.drawString(x, y, text)
        y -= 20
        pdf.drawString(60, y + 6, "Quarterly revenue")
        pdf.drawString(60, y - 6, "(domestic only)")
        for x, text in ((220, f"1{page}0"), (330, f"ok{page}")):
            pdf.drawString(x, y, text)
        y -= 26
        for index in range(4):
            for x, text in ((60, f"row{page}{index}"), (220, f"{page}{index}5"), (330, f"n{page}{index}")):
                pdf.drawString(x, y, text)
            y -= 16
        y -= 30
        for index in range(6):
            pdf.drawString(60, y - index * 14, f"Body paragraph line {page}-{index} with enough words to be a wide line of text here")

    @staticmethod
    def _table(page: int) -> str:
        rows = [("Item", "Count", "Note"), ("---", "---", "---"), ("Quarterly revenue<br>(domestic only)", f"1{page}0", f"ok{page}"),
                *((f"row{page}{index}", f"{page}{index}5", f"n{page}{index}") for index in range(4))]
        return "\n".join(f"| {' | '.join(row)} |" for row in rows)

    def test_one_page_is_the_reference(self):
        result = convert(_pdf(self._draw), "a.pdf", ConvertOptions(page_markers=False))

        assert self._table(0) in result.text

    def test_wrapped_first_cell_survives_on_every_page(self):
        result = convert(_pdf(self._draw, pages=3), "a.pdf", ConvertOptions(page_markers=False))

        for page in range(3):
            assert self._table(page) in result.text
        assert result.text.count("Quarterly revenue") == 3


class TestJoinedCellLinesOnATwoColumnPage:
    """A borderless table under two columns of text, with a vertically centred row whose cells wrap to different numbers of
    lines: the row is one table row of joined cells, but the search inside each page column did not count the original
    lines of those cells as table lines and made a second table out of the lines of the three left cells."""

    @staticmethod
    def _draw(pdf, page):
        pdf.setFont("Helvetica", 10)
        y = 780
        for index in range(8):
            pdf.drawString(60, y, f"Left column body line {index} runs on and on to here")
            pdf.drawString(315, y, f"Right column body line {index} runs on and on too")
            y -= 14
        y -= 30
        starts = (60, 130, 200, 330, 430)
        for x, text in zip(starts, ("Alpha", "Beta", "Gamma", "Delta", "Eps"), strict=True):
            pdf.drawString(x, y, text)
        y -= 22
        for index in range(4):
            for x, text in zip(starts, ("aa", "bb", "cc"), strict=False):
                pdf.drawString(x, y - 12 * index, f"{text}{index}")
        pdf.drawString(starts[3], y - 18, "dd")
        pdf.drawString(starts[4], y - 12, "ee1")
        pdf.drawString(starts[4], y - 24, "ee2")
        y -= 58
        for index in range(2):
            for x, text in zip(starts, ("r", "s", "t", "u", "v"), strict=True):
                pdf.drawString(x, y, f"{text}{index}")
            y -= 22

    def test_lines_of_a_joined_row_are_not_a_second_table(self):
        result = convert(_pdf(self._draw), "a.pdf", ConvertOptions(page_markers=False))

        table = ("| Alpha | Beta | Gamma | Delta | Eps |\n| --- | --- | --- | --- | --- |\n"
                 "| aa0<br>aa1<br>aa2<br>aa3 | bb0<br>bb1<br>bb2<br>bb3 | cc0<br>cc1<br>cc2<br>cc3 | dd | ee1<br>ee2 |\n"
                 "| r0 | s0 | t0 | u0 | v0 |\n| r1 | s1 | t1 | u1 | v1 |")
        assert table in result.text
        assert result.text.count("aa1") == 1
        assert result.text.count("\n| --- ") == 1
        assert result.text.rstrip().endswith(table)


def _wrapped_lines_pdf(count: int, rows_below: int) -> bytes:
    """A borderless table (header, one row) with `count` one-word lines at line spacing under the first cell, in the gap
    before the second header, and `rows_below` more table rows under them."""
    height = 200 + count * 4.2

    def draw(pdf, page):
        pdf.setFont("Helvetica", 4)
        y = height - 50
        for x, text in ((50, "aa"), (200, "bb"), (300, "cc")):
            pdf.drawString(x, y, text)
        y -= 7
        for x, text in ((50, "name here long text"), (200, "v"), (300, "w")):
            pdf.drawString(x, y, text)
        for _ in range(count):
            y -= 4.2
            pdf.drawString(60, y, "wrap")
        for index in range(rows_below):
            y -= 7
            for x, text in ((50, f"row{index}"), (200, "v"), (300, "w")):
                pdf.drawString(x, y, text)

    return _pdf(draw, size=(600, height))


class TestManyWrappedLinesUnderOneRow:
    """Every further wrapped line under a table row measured the whole row again (its bottom and the column of each of its
    cells, each of which looks at the other cells), so the time grew with the cube of the number of lines: 600 one-word
    lines in a 3.8 KB file took 10 seconds."""

    @staticmethod
    def _counted(monkeypatch) -> list[int]:
        calls = [0]
        column_of = pdf._column_of

        def counting(*args):
            calls[0] += 1
            return column_of(*args)

        monkeypatch.setattr(pdf, "_column_of", counting)
        return calls

    def test_wrapped_lines_of_a_cell_are_not_measured_again_for_every_line(self, monkeypatch):
        calls = self._counted(monkeypatch)

        result = convert(_wrapped_lines_pdf(200, rows_below=3), "a.pdf", ConvertOptions(page_markers=False))

        assert result.text.startswith("| aa | bb | cc |\n| --- | --- | --- |\n| name here long text" + "<br>wrap" * 200 + " | v | w |\n"
                                      "| row0 | v | w |\n| row1 | v | w |\n| row2 | v | w |")
        assert calls[0] < 2_000  # 21,353 when the row was measured again for every line

    def test_lines_that_end_the_page_without_a_row_below_stay_lines(self, monkeypatch):
        calls = self._counted(monkeypatch)

        result = convert(_wrapped_lines_pdf(200, rows_below=0), "a.pdf", ConvertOptions(page_markers=False))

        assert result.text == "aa\n\nname here long text\n\nbb\n\nv\n\ncc\n\nw\n\n" + "\n".join(["wrap"] * 200)
        assert calls[0] < 2_000  # 62,121 when the row was measured again for every line

    def test_columns_of_a_growing_row_are_those_of_column_of(self):
        """The columns kept for a growing row equal `_column_of` asked afresh for every cell, whatever the cells are:
        text and numbers at header edges, inside headers, in the gaps between them, and empty-width cells."""
        random = Random(20261113)
        moved = 0
        for _ in range(400):
            starts = sorted(random.sample(range(40, 520, 10), random.randint(3, 6)))
            header = [pdf._Line(100, 110, x, x + random.choice((8, 20, 45)), "head", 10) for x in starts]
            edges = [edge for cell in header for edge in (cell.x0, cell.x1)]
            row: list = []
            known: dict = {}
            for _ in range(random.randint(1, 14)):
                x0 = random.choice([random.choice(edges) + random.choice((-30, -12, -3, 0, 3, 12, 30)), random.uniform(30, 560),
                                    *([random.choice(row).x1] * 2 if row else [])])  # also right where another cell ends
                width = random.choice((0, 0, 4, 15, 40, 90))
                x1 = random.choice([x0 + width, random.choice(edges)]) if random.random() < 0.3 else x0 + width
                row.append(pdf._Line(120, 130, min(x0, x1), max(x0, x1), random.choice(("word", "12", "3,400", "note a")), 10))
                expected = [pdf._column_of(cell, header, row) for cell in row]
                assert pdf._cell_columns(row, header, known) == expected
                moved += sum(column != pdf._header_column(cell, header) for cell, column in zip(row, expected, strict=True))
        assert moved > 200  # the gap rule (a cell sent to the column on its right) is exercised

    @pytest.mark.timing
    def test_six_hundred_wrapped_lines_take_no_longer_than_reading_them(self):
        started = time.perf_counter()

        result = convert(_wrapped_lines_pdf(600, rows_below=0), "a.pdf", ConvertOptions(page_markers=False))

        assert result.text.count("wrap") == 600
        assert time.perf_counter() - started < 5.0  # 10 seconds before; about half a second now


def _same_verdicts(pattern, reference: str, tokens: tuple[str, ...], length: int, *, search: bool) -> bool:
    """Whether `pattern` gives the same yes/no as the `reference` pattern on every text made of up to `length` `tokens`."""
    old = re.compile(reference, pattern.flags)
    for count in range(length + 1):
        for parts in product(tokens, repeat=count):
            text = "".join(parts)
            if bool(pattern.search(text) if search else pattern.match(text)) != bool(old.search(text) if search else old.match(text)):
                return False
    return True


class TestLongDotRunInsideALine:
    """The dot-leader patterns were tried from every dot of a run, each time to the end of the run and back, so a piece
    with a long run of dots that does not end the text took time with the square of the run: a 1.4 KB file with 20,000
    dots and a word after them took 18 seconds."""

    _TOKENS = (".", "·", "…", "‥", " ", "a", "1", "\n", ".....", "……", "abcdefghijkl")

    def test_leader_patterns_judge_as_before(self):
        assert _same_verdicts(pdf._LEADER_END, r"(?:[.·∙・]{5,}|[…‥]{2,})$", self._TOKENS, 5, search=True)
        assert _same_verdicts(pdf._LEADER_TAIL, r"(?:[.·∙・]{5,}|[…‥]{2,})\s*\S{1,12}$", self._TOKENS, 5, search=True)

    def test_contents_line_keeps_its_page_number(self):
        def draw(canvas, page):
            canvas.setFont("Helvetica", 10)
            for index, (title, number) in enumerate((("1. Introduction", "3"), ("2. Method", "12"), ("3. Results", "27"))):
                text = f"{title} {'.' * 40}"
                canvas.drawString(60, 700 - index * 16, text)
                canvas.drawString(60 + canvas.stringWidth(text, "Helvetica", 10) + 15, 700 - index * 16, number)

        result = convert(_pdf(draw), "a.pdf", ConvertOptions(page_markers=False))

        assert [line for line in result.text.splitlines() if line] == ["1. Introduction … 3", "2. Method … 12", "3. Results … 27"]

    @pytest.mark.timing
    def test_long_dot_run_before_a_word_is_read_in_linear_time(self):
        dots = 20_000

        def draw(canvas, page):
            canvas.setFont("Helvetica", 2)
            canvas.drawString(50, 200, "left")
            canvas.drawString(150, 200, "." * dots + " " + "y" * 20)
            canvas.drawString(50, 150, "body text")

        started = time.perf_counter()
        result = convert(_pdf(draw, size=(dots * 0.556 + 400, 300)), "a.pdf", ConvertOptions(page_markers=False))

        assert "y" * 20 in result.text and "body text" in result.text
        assert time.perf_counter() - started < 5.0  # 12 to 18 seconds before; 0.2 seconds now

