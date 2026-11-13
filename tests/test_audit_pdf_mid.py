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


def _small_tables_pdf(count: int, per_row: int) -> bytes:
    """One-page PDF with `count` ruled 2×2 tables of 60×30pt laid out `per_row` to a line, one word in every cell."""
    lines = -(-count // per_row)
    width, height = per_row * 70 + 40, lines * 40 + 40
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(width, height))
    pdf.setFont("Helvetica", 6)
    for number in range(count):
        x, y = 20 + (number % per_row) * 70, height - 50 - (number // per_row) * 40
        for step in range(3):
            pdf.line(x, y + step * 15, x + 60, y + step * 15)
            pdf.line(x + step * 30, y, x + step * 30, y + 30)
        for row in range(2):
            for column in range(2):
                pdf.drawString(x + column * 30 + 3, y + (1 - row) * 15 + 5, f"t{number}{'ab'[row]}{column}")
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


class TestManyTablesOnOnePage:
    """`_closed_at_open_ends`, `_read_sideways_cells` and `_join_soft_wraps` each listed every vertical rule or every
    character of the page again for every table, so a page of 800 small tables (a 51 KB file) took 31 seconds."""

    @staticmethod
    def _read(data: bytes) -> tuple[list[list[list[str | None]]], int]:
        """The cell text of every table of the page after the three steps, and the number of characters on the page."""
        with pdfplumber.open(BytesIO(data)) as document:
            page = document.pages[0]
            cells = []
            for table in page.find_tables(pdf_module._TABLE_SETTINGS):
                assert pdf_module._closed_at_open_ends(page, table) is table
                rows = table.extract(**pdf_module._CELL_TEXT)
                pdf_module._read_sideways_cells(table, rows)
                pdf_module._join_soft_wraps(table, rows)
                cells.append(rows)
            return cells, len(page.chars)

    def test_each_table_takes_only_its_own_part_of_the_page(self, monkeypatch):
        from pdfplumber.container import Container

        listed = [0]
        vertical_edges = Container.vertical_edges.fget

        def counting(page):
            listed[0] += 1
            return vertical_edges(page)

        monkeypatch.setattr(Container, "vertical_edges", property(counting))
        sideways, inside = _counted(monkeypatch, "_is_sideways"), _counted(monkeypatch, "_inside_any")

        cells, chars = self._read(_small_tables_pdf(120, 10))

        assert cells == [[[f"t{number}a0", f"t{number}a1"], [f"t{number}b0", f"t{number}b1"]] for number in range(120)]
        assert listed[0] == 1  # once per table before
        assert sideways[0] <= chars  # 120 times the number of characters before
        assert inside[0] <= chars * 20  # each table looks at its own line of 10 tables (120 times the characters before)

    def test_lists_built_for_the_tables_are_dropped_with_them(self, monkeypatch):
        """The lists are kept on the page object, which pdfplumber keeps for the whole document: they must not hold a page's
        characters after its tables were read, nor serve the characters of an earlier reading of the page."""
        with pdfplumber.open(BytesIO(_small_tables_pdf(6, 3))) as document:
            page = document.pages[0]

            assert len(pdf_module._real_tables(page)) == 6
            assert "_page_indexes" not in vars(page)

            first = pdf_module._text_by_middle(page)[1]
            page.close()  # releases the page's objects; reading it again parses them anew
            again = pdf_module._text_by_middle(page)[1]
            assert again == first and again[0] is not first[0] and any(char is again[0] for char in page.chars)

            def failing(page):
                raise RuntimeError

            monkeypatch.setattr(pdf_module, "_open_sided_tables", failing)
            with pytest.raises(RuntimeError):
                pdf_module._real_tables(page)
            assert "_page_indexes" not in vars(page)

    @pytest.mark.timing
    def test_page_of_many_small_tables_is_read_in_seconds(self):
        data = _small_tables_pdf(800, 20)
        with pdfplumber.open(BytesIO(data)) as document:
            page = document.pages[0]
            tables = page.find_tables(pdf_module._TABLE_SETTINGS)
            extracted = [table.extract(**pdf_module._CELL_TEXT) for table in tables]  # pdfplumber's own time is not measured

            started = time.perf_counter()
            for table, rows in zip(tables, extracted, strict=True):
                pdf_module._closed_at_open_ends(page, table)
                pdf_module._read_sideways_cells(table, rows)
                pdf_module._join_soft_wraps(table, rows)
            elapsed = time.perf_counter() - started

        assert len(tables) == 800 and elapsed < 3  # 9 seconds in these three steps before

    def test_tables_with_wrapped_cells_and_a_row_open_at_the_page_end_read_as_before(self):
        """Two tables on one page: wrapped and broken lines in the cells of the first, and a last row without a bottom
        border in the second (its vertical rules run on to where the page ends)."""
        def grid(pdf, columns, top, height, rows, *, closed=True):
            for line in range(len(rows) + (1 if closed else 0)):
                pdf.line(columns[0], top - line * height, columns[-1], top - line * height)
            for x in columns:
                for row in range(len(rows)):
                    pdf.line(x, top - row * height, x, top - (row + 1) * height)
            for row, values in enumerate(rows):
                for column, value in enumerate(values):
                    for index, text in enumerate(value.split("\n")):
                        pdf.drawString(columns[column] + 4, top - row * height - 12 - index * 12, text)

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=(400, 420))
        pdf.setFont("Helvetica", 10)
        pdf.drawString(40, 390, "Report of the two tables")
        grid(pdf, [40, 150, 360], 370, 40, [["Item", "Description"],
                                            ["Pump", "The pressure of the hydraulic pump is\nchecked every morning"],
                                            ["Belt", "Tension\nWear"]])
        pdf.drawString(40, 225, "Second table")
        grid(pdf, [40, 150, 360], 210, 30, [["Name", "Value"], ["Alpha", "10"], ["Beta", "20"], ["Gamma", "30"]], closed=False)
        pdf.showPage()
        pdf.save()

        result = convert(buffer.getvalue(), "a.pdf", ConvertOptions(page_markers=False))

        assert result.text == (
            "Report of the two tables\n\n| Item | Description |\n| --- | --- |\n"
            "| Pump | The pressure of the hydraulic pump is checked every morning |\n| Belt | Tension<br>Wear |\n\n"
            "Second table\n\n| Name | Value |\n| --- | --- |\n| Alpha | 10 |\n| Beta | 20 |\n| Gamma | 30 |")
