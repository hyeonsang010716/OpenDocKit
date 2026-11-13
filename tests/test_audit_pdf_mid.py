"""PDF defects from the audit of table and picture analysis: steps that scanned a whole page once per cell, table, picture
or rectangle took time quadratic in the page's content, inside one page where the time budget cannot interrupt. The results
are the same as before; only how the characters, rules and pictures are found changed."""

from io import BytesIO
import time

import pdfplumber
import pytest
from reportlab.pdfgen import canvas

from opendockit import ConvertOptions, convert
from opendockit.extractors import pdf as pdf_module


def _sideways_table_pdf(rows: int, columns: int, *, upright: tuple[int, int] | None = None) -> bytes:
    """One-page PDF with a ruled table of `rows` × `columns` cells of 30pt whose text ('r0c0') is drawn rotated 90 degrees.
    The cell `upright` (row, column) also holds a horizontal word."""
    width, height = columns * 30 + 40, rows * 30 + 40
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(width, height))
    pdf.setFont("Helvetica", 6)
    for row in range(rows + 1):
        pdf.line(20, 20 + row * 30, 20 + columns * 30, 20 + row * 30)
    for column in range(columns + 1):
        pdf.line(20 + column * 30, 20, 20 + column * 30, 20 + rows * 30)
    for row in range(rows):
        for column in range(columns):
            pdf.saveState()
            pdf.translate(20 + column * 30 + 15, 20 + (rows - 1 - row) * 30 + 4)
            pdf.rotate(90)
            pdf.drawString(0, 0, f"r{row}c{column}")
            pdf.restoreState()
    if upright is not None:
        pdf.drawString(20 + upright[1] * 30 + 18, 20 + (rows - 1 - upright[0]) * 30 + 12, "ab")
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def _counted(monkeypatch, name: str) -> list[int]:
    """Count the calls of the PDF extractor's function `name` (the returned one-item list holds the count)."""
    calls = [0]
    original = getattr(pdf_module, name)

    def counting(*args, **kwargs):
        calls[0] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(pdf_module, name, counting)
    return calls


class TestSidewaysCells:
    """`_read_sideways_cells` scanned the rotated characters and then every character of the page once per cell, so a table
    laid sideways on one page (100 rows of 20 columns, a 10 KB file) spent 23 seconds there."""

    @staticmethod
    def _read(data: bytes) -> tuple[list[list[str | None]], int]:
        with pdfplumber.open(BytesIO(data)) as document:
            page = document.pages[0]
            table = max(page.find_tables(pdf_module._TABLE_SETTINGS), key=lambda found: len(found.cells))
            rows = table.extract(**pdf_module._CELL_TEXT)
            pdf_module._read_sideways_cells(table, rows)
            return rows, len(page.chars)

    @pytest.mark.parametrize(("rows", "columns"), [(40, 10), (1, 300), (300, 1)])
    def test_each_cell_looks_only_at_its_own_band(self, monkeypatch, rows, columns):
        calls = _counted(monkeypatch, "_inside_any")

        cells, chars = self._read(_sideways_table_pdf(rows, columns))

        assert cells == [[f"r{row}c{column}" for column in range(columns)] for row in range(rows)]
        assert calls[0] <= chars * 30  # it was the number of cells (300 or 400) times the number of characters and more

    @pytest.mark.timing
    def test_large_sideways_table_is_read_in_seconds(self):
        data = _sideways_table_pdf(100, 20)

        started = time.perf_counter()
        cells, _ = self._read(data)
        elapsed = time.perf_counter() - started

        assert cells[99][19] == "r99c19" and elapsed < 5  # 23 seconds in `_read_sideways_cells` alone before

    def test_sideways_table_reads_as_before(self):
        """A cell with a horizontal word mixed in keeps the text pdfplumber read (no order to join two directions in)."""
        result = convert(_sideways_table_pdf(3, 4, upright=(1, 2)), "a.pdf", ConvertOptions(page_markers=False))

        assert result.text == ("| r0c0 | r0c1 | r0c2 | r0c3 |\n| --- | --- | --- | --- |\n| r1c0 | r1c1 | ab 2c1r | r1c3 |\n"
                               "| r2c0 | r2c1 | r2c2 | r2c3 |")
