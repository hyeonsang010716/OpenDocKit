"""PDF defects from the audit of table and picture analysis: steps that scanned a whole page once per cell, table, picture
or rectangle took time quadratic in the page's content, inside one page where the time budget cannot interrupt. The results
are the same as before; only how the characters, rules and pictures are found changed."""

from io import BytesIO
import time

import pdfplumber
import pytest
from reportlab.pdfgen import canvas

from opendockit import ConvertOptions, convert
from opendockit.extractors import _pdf_cell_text, _pdf_figures, _pdf_model, _pdf_ruled_tables
from opendockit.extractors import pdf as pdf_module
from tests.pdf_modules import replace_everywhere
from tests.timing import allowed


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

    def counting(*args, **kwargs):
        calls[0] += 1
        return original(*args, **kwargs)

    original = replace_everywhere(monkeypatch, name, counting)
    return calls


class TestSidewaysCells:
    """`_read_sideways_cells` scanned the rotated characters and then every character of the page once per cell, so a table
    laid sideways on one page (100 rows of 20 columns, a 10 KB file) spent 23 seconds there."""

    @staticmethod
    def _read(data: bytes) -> tuple[list[list[str | None]], int]:
        with pdfplumber.open(BytesIO(data)) as document:
            page = document.pages[0]
            table = max(page.find_tables(_pdf_ruled_tables._TABLE_SETTINGS), key=lambda found: len(found.cells))
            rows = table.extract(**_pdf_cell_text._CELL_TEXT)
            _pdf_cell_text._read_sideways_cells(table, rows)
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

        assert cells[99][19] == "r99c19" and elapsed < allowed(5)  # 23 seconds in `_read_sideways_cells` alone before

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
            for table in page.find_tables(_pdf_ruled_tables._TABLE_SETTINGS):
                assert _pdf_ruled_tables._closed_at_open_ends(page, table) is table
                rows = table.extract(**_pdf_cell_text._CELL_TEXT)
                _pdf_cell_text._read_sideways_cells(table, rows)
                _pdf_cell_text._join_soft_wraps(table, rows)
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

            first = _pdf_ruled_tables._text_by_middle(page)[1]
            page.close()  # releases the page's objects; reading it again parses them anew
            again = _pdf_ruled_tables._text_by_middle(page)[1]
            assert again == first and again[0] is not first[0] and any(char is again[0] for char in page.chars)

            def failing(page):
                raise RuntimeError

            monkeypatch.setattr(_pdf_ruled_tables, "_open_sided_tables", failing)
            with pytest.raises(RuntimeError):
                pdf_module._real_tables(page)
            assert "_page_indexes" not in vars(page)

    @pytest.mark.timing
    def test_page_of_many_small_tables_is_read_in_seconds(self):
        data = _small_tables_pdf(800, 20)
        with pdfplumber.open(BytesIO(data)) as document:
            page = document.pages[0]
            tables = page.find_tables(_pdf_ruled_tables._TABLE_SETTINGS)
            extracted = [table.extract(**_pdf_cell_text._CELL_TEXT) for table in tables]  # pdfplumber's own time is not measured

            started = time.perf_counter()
            for table, rows in zip(tables, extracted, strict=True):
                _pdf_ruled_tables._closed_at_open_ends(page, table)
                _pdf_cell_text._read_sideways_cells(table, rows)
                _pdf_cell_text._join_soft_wraps(table, rows)
            elapsed = time.perf_counter() - started

        assert len(tables) == 800 and elapsed < allowed(3)  # 9 seconds in these three steps before

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


def _vertical_strips_pdf(strips: int) -> bytes:
    """One-page PDF (3000×800pt) whose picture is drawn as `strips` vertical strips of the full page height, under 30 lines
    of text."""
    from reportlab.lib.utils import ImageReader

    from tests.builder import png_bytes

    width, height = 3000.0, 800.0
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(width, height))
    picture = ImageReader(BytesIO(png_bytes(64, 64, seed=3)))
    for strip in range(strips):
        pdf.drawImage(picture, strip * width / strips, 0, width=width / strips, height=height)
    pdf.setFont("Helvetica", 10)
    for line in range(30):
        pdf.drawString(50, 700 - line * 14, f"plain text line number {line} with words in it for the layer")
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


class _CountedBox(tuple):
    """A box that counts how many times a coordinate is read from any box of this kind."""

    reads = 0

    def __getitem__(self, index):
        _CountedBox.reads += 1
        return super().__getitem__(index)


class TestPictureDrawnAsManyStrips:
    """`_tiled_background` compared every pair of pictures that share a top, `_is_strips` every pair of pieces of a group
    and `_union_area` every box with every band, so a page whose picture is drawn as 3,000 vertical strips (a 12 KB file)
    took 10 seconds."""

    def test_strips_are_compared_with_their_neighbours_only(self, monkeypatch):
        abuts, encloses = _counted(monkeypatch, "_abuts"), _counted(monkeypatch, "_encloses")

        result = convert(_vertical_strips_pdf(600), "a.pdf", ConvertOptions(page_markers=False))

        assert result.text.count("plain text line number") == 30 and len(result.images) == 1  # one background, as before
        assert abuts[0] <= 600 * 10  # 600 × 599 / 2 before
        assert encloses[0] <= 600 * 40  # 600 × 600 before

    def test_union_area_of_many_stacked_strips_is_swept_once(self):
        strips = [_CountedBox((0.0, top * 2.0, 600.0, top * 2.0 + 2.0)) for top in range(400)]
        _CountedBox.reads = 0

        area = _pdf_figures._union_area(strips)

        assert area == 600.0 * 800.0
        assert _CountedBox.reads <= 400 * 20  # 800 bands × 400 boxes before

    def test_groups_and_areas_are_the_ones_found_by_comparing_everything(self):
        """The groups of abutting pictures, the strip shape of a group and the area of a union are the same as when every
        pair of boxes (every box for every band) is compared, on layouts of strips, grids, pieces laid inside strips and
        scattered boxes."""
        import random

        def abutting_groups(boxes):
            groups = [{index} for index in range(len(boxes))]
            for index, box in enumerate(boxes):
                for other in range(index + 1, len(boxes)):
                    if _pdf_figures._abuts(box, boxes[other]) and groups[index] is not groups[other]:
                        groups[index] |= groups[other]
                        for member in groups[other]:
                            groups[member] = groups[index]
            return sorted({tuple(sorted(group)) for group in groups})

        def union_area(boxes):
            edges = sorted({edge for box in boxes for edge in (box[1], box[3])})
            area = 0.0
            for low, high in zip(edges, edges[1:], strict=False):
                covered, reach = 0.0, float("-inf")
                for x0, x1 in sorted((box[0], box[2]) for box in boxes if box[1] <= low and box[3] >= high):
                    covered += max(0.0, x1 - max(x0, reach))
                    reach = max(reach, x1)
                area += covered * (high - low)
            return area

        def is_strips(shapes):
            encloses = _pdf_model._encloses
            outer = [tile for tile in shapes if not any(encloses(other, tile) and not encloses(tile, other) for other in shapes)]
            x0, top = min(tile[0] for tile in outer), min(tile[1] for tile in outer)
            x1, bottom = max(tile[2] for tile in outer), max(tile[3] for tile in outer)
            gap = _pdf_figures._TILE_GAP
            return (all(tile[0] - x0 <= gap and x1 - tile[2] <= gap for tile in outer)
                    or all(tile[1] - top <= gap and bottom - tile[3] <= gap for tile in outer))

        def expected(boxes, view):
            view_area = (view[2] - view[0]) * (view[3] - view[1])
            if sum(_pdf_model._box_area(box[:4]) for box in boxes) < view_area * _pdf_figures._BACKGROUND_RATIO:
                return boxes
            result = []
            for group in abutting_groups(boxes):
                tiles = [boxes[index] for index in group]
                shapes = [tile[:4] for tile in tiles]
                if len(tiles) > 1 and is_strips(shapes) and union_area(shapes) >= view_area * _pdf_figures._BACKGROUND_RATIO:
                    result.append(tuple(pick(tile[side] for tile in tiles) for side, pick in enumerate((min, min, max, max, max))))
                else:
                    result.extend(tiles)
            return result

        random.seed(7)
        width, height = 600.0, 800.0
        merged = 0
        for _ in range(400):
            count, slack = random.randrange(2, 40), [0.0, 0.0, 0.5, 1.5, 2.0, 2.5, 3.0, -1.0]
            layout = random.randrange(5)
            if layout == 0:  # horizontal strips, with gaps or overlaps between some
                boxes = [(0.0, row * height / count, width, (row + 1) * height / count - random.choice(slack), 1.0)
                         for row in range(count)]
            elif layout == 1:  # vertical strips
                boxes = [(column * width / count, 0.0, (column + 1) * width / count - random.choice(slack), height, 2.0)
                         for column in range(count)]
            elif layout == 2:  # a grid
                rows, columns = random.randrange(1, 7), random.randrange(1, 7)
                boxes = [(column * width / columns, row * height / rows, (column + 1) * width / columns - random.choice(slack),
                          (row + 1) * height / rows - random.choice(slack), 1.0) for row in range(rows) for column in range(columns)]
            elif layout == 3:  # strips with pieces laid inside them or just around them
                boxes = [(0.0, row * height / count, width, (row + 1) * height / count, 1.0) for row in range(count)]
                boxes += [(random.choice([-1.5, -0.5, 0.0, 0.5, 1.5, 10.0]), row * height / count + random.choice([0.0, 0.5, 1.5]),
                           width / random.choice([1, 2]), (row + 1) * height / count - random.choice([-1.5, -0.5, 0.0, 1.5]), 1.0)
                          for row in range(count) if random.random() < 0.5]
            else:  # boxes scattered on a 20pt lattice
                boxes = []
                for _ in range(count):
                    x0, top = random.randrange(30) * 20.0, random.randrange(40) * 20.0
                    boxes.append((x0, top, x0 + random.randrange(1, 12) * 20.0, top + random.randrange(1, 12) * 20.0, 1.0))
            random.shuffle(boxes)
            shapes = [box[:4] for box in boxes]

            found = _pdf_figures._tiled_background(boxes, (0.0, 0.0, width, height))

            assert sorted(found) == sorted(expected(boxes, (0.0, 0.0, width, height)))
            assert _pdf_figures._is_strips(shapes) == is_strips(shapes)
            assert _pdf_figures._union_area(shapes) == union_area(shapes)
            merged += len(found) < len(boxes)
        assert merged > 20

    @pytest.mark.timing
    def test_page_of_thousands_of_strips_is_grouped_in_seconds(self):
        width, height, strips = 3000.0, 800.0, 6000
        boxes = [(strip * width / strips, 0.0, (strip + 1) * width / strips, height, 1.0) for strip in range(strips)]

        started = time.perf_counter()
        found = _pdf_figures._tiled_background(boxes, (0.0, 0.0, width, height))
        elapsed = time.perf_counter() - started

        assert found == [(0.0, 0.0, width, height, 1.0)] and elapsed < allowed(3)  # 12 seconds before

    def test_scan_stored_as_strips_and_photos_side_by_side_read_as_before(self):
        from tests import builder

        lines = ["Minutes of the regular meeting", "The budget for the next year was approved as proposed."]
        scan = convert(builder.pdf_scanned_in_strips([lines]), "scan.pdf", ConvertOptions(page_markers=False))
        photos = convert(builder.pdf_scanned_in_strips([lines], strips=4, gap=12), "a.pdf", ConvertOptions(page_markers=False))

        assert scan.text == "Minutes of the regular meeting\nThe budget for the next year was approved as proposed."
        assert len(scan.images) == 1 and len(scan.warnings) == 2  # the strips are one background: the page is sent whole
        assert photos.text == ("[Image]\n\nMinutes of the regular meeting\nThe budget for the next year was approved as proposed."
                               "\n\n[Image]\n\n[Image]\n\n[Image]")
        assert len(photos.images) == 4 and photos.warnings == []


def _filled_line_pdf(count: int) -> bytes:
    """One-page PDF with one line of `count` characters (Courier, 6pt apart) and a borderless filled rectangle under each."""
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(count * 6 + 40, 200))
    pdf.setFont("Courier", 10)
    pdf.setFillColorRGB(1, 1, 0)
    for index in range(count):
        pdf.rect(20 + index * 6, 96, 5.5, 12, stroke=0, fill=1)
    pdf.setFillColorRGB(0, 0, 0)
    for start in range(0, count, 50):
        pdf.drawString(20 + start * 6, 100, ("abcdefghi " * 5)[:min(50, count - start)])
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


class _CountedChar(dict):
    """A character that counts how many times a value is read from any character of this kind."""

    reads = 0

    def __getitem__(self, key):
        _CountedChar.reads += 1
        return super().__getitem__(key)


class TestManyFilledRectanglesOnOneLine:
    """`_without_highlights` went through every character of the line once to find the characters of each filled
    rectangle and once more to look for unfilled text beside it, so a line of 4,000 characters with a rectangle under each
    (a 15 KB file) took 8 seconds."""

    def test_each_rectangle_looks_only_at_the_characters_it_covers(self):
        with pdfplumber.open(BytesIO(_filled_line_pdf(600))) as document:
            page = document.pages[0]
            page.chars[:] = [_CountedChar(char) for char in page.chars]
            rects = len(page.rects)
            _CountedChar.reads = 0

            kept = pdf_module._without_highlights(page)

            assert rects == 600 and len(kept.rects) == 0  # all of them are highlights, as before
            assert _CountedChar.reads <= 600 * 60  # more than 600 × 600 × 3 before

    @pytest.mark.timing
    def test_line_of_thousands_of_filled_rectangles_is_read_in_seconds(self):
        with pdfplumber.open(BytesIO(_filled_line_pdf(8000))) as document:
            page = document.pages[0]
            characters = len(page.chars) + len(page.rects)  # parsing time is not measured

            started = time.perf_counter()
            pdf_module._without_highlights(page)
            elapsed = time.perf_counter() - started

        assert characters == 16000 and elapsed < allowed(3)  # 8 seconds before

    def test_highlighted_words_and_shaded_cells_read_as_before(self):
        """Highlights over words of a paragraph are not rules (no table of words); the shading of table cells stays."""
        buffer = BytesIO()
        pdf = canvas.Canvas(buffer, pagesize=(500, 400))
        pdf.setFont("Courier", 10)
        pdf.setFillColorRGB(1, 1, 0)
        pdf.rect(40 + 4 * 6, 347.5, 9 * 6, 11, stroke=0, fill=1)  # 'container'
        pdf.rect(40 + 21 * 6, 347.5, 3 * 6, 11, stroke=0, fill=1)  # 'HDP'
        pdf.rect(40 + 24 * 6, 347.5, 4 * 6, 11, stroke=0, fill=1)  # 'E 80' in a second run
        pdf.rect(40, 333.5, 2 * 6, 11, stroke=0, fill=1)  # 'of' at the start of the second line
        pdf.setFillColorRGB(0.8, 0.8, 0.8)
        for row in range(3):
            for column in range(3):
                if row == 0 or column == 0:
                    pdf.rect(40 + column * 120, 270 - row * 16, 120, 16, stroke=0, fill=1)
        pdf.setFillColorRGB(0, 0, 0)
        pdf.drawString(40, 350, "The container is 70% HDPE 80% recycled resin by weight")
        pdf.drawString(40, 336, "of the material and is collected after use.")
        for row, values in enumerate([["Item", "2023", "2024"], ["Bottles", "1,200", "1,350"], ["Caps", "300", "420"]]):
            for column, value in enumerate(values):
                pdf.drawString(44 + column * 120, 274 - row * 16, value)
        pdf.showPage()
        pdf.save()

        result = convert(buffer.getvalue(), "a.pdf", ConvertOptions(page_markers=False))

        assert result.text == ("The container is 70% HDPE 80% recycled resin by weight\nof the material and is collected after use.\n\n"
                               "| Item | 2023 | 2024 |\n| --- | --- | --- |\n| Bottles | 1,200 | 1,350 |\n| Caps | 300 | 420 |")


def _striped_table_pdf(rows: int) -> bytes:
    """One-page PDF with a table of `rows` rows and three columns whose every other row is shaded cell by cell (filled
    rectangles without a border, no rules)."""
    edges = [40, 160, 280, 400]
    height = rows * 14 + 80
    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=(500, height))
    pdf.setFont("Helvetica", 8)
    for row in range(rows):
        top = height - 40 - row * 14
        if row % 2 == 0:
            pdf.setFillColorRGB(0.85, 0.85, 0.9)
            for left, right in zip(edges, edges[1:], strict=False):
                pdf.rect(left, top - 14, right - left, 14, stroke=0, fill=1)
        pdf.setFillColorRGB(0, 0, 0)
        for column, left in enumerate(edges[:-1]):
            pdf.drawString(left + 5, top - 10, f"row{row}" if column == 0 else str(row * 7 + column))
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


class TestLongStripedTable:
    """`_grown_shaded_table` measured every shaded row again and searched the rows joined so far for each of them at every
    step, which grew with the cube of the number of shaded rows (5 seconds on a striped table of 2,000 rows)."""

    def test_fragments_are_measured_once(self, monkeypatch):
        from pdfplumber.table import Table

        measured = [0]
        bbox = Table.bbox.fget

        def counting(table):
            measured[0] += 1
            return bbox(table)

        monkeypatch.setattr(Table, "bbox", property(counting))
        with pdfplumber.open(BytesIO(_striped_table_pdf(400))) as document:
            page = document.pages[0]
            fragments = page.find_tables(_pdf_ruled_tables._TABLE_SETTINGS)
            measured[0] = 0

            (table,) = _pdf_ruled_tables._with_shaded_rows(page, fragments)

            assert len(fragments) == 200 and len(table.rows) == 400
            assert measured[0] <= 200 * 10  # 200 × 200 / 2 before

    def test_striped_table_reads_as_before(self):
        result = convert(_striped_table_pdf(7), "a.pdf", ConvertOptions(page_markers=False))

        assert result.text == ("| row0 | 1 | 2 |\n| --- | --- | --- |\n| row1 | 8 | 9 |\n| row2 | 15 | 16 |\n| row3 | 22 | 23 |\n"
                               "| row4 | 29 | 30 |\n| row5 | 36 | 37 |\n| row6 | 43 | 44 |")
