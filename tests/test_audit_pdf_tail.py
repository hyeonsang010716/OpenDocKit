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
