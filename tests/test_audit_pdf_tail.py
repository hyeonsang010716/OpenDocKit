"""PDF audit of the borderless-table, row-joining and repeated-line code: lost cell lines and super-linear time."""

from io import BytesIO

from opendockit import ConvertOptions, convert


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
