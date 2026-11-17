"""PDF picture positions: the visible area of a clipped picture, pictures inside ruled-table cells, multi-column photo grids."""

from io import BytesIO

import pytest

from opendockit import ImageKind, convert
from tests import builder
from tests.test_pdf import _korean_pdf
from tests.timing import Fastest


def _image(width: int, height: int, seed: int = 3):
    from reportlab.lib.utils import ImageReader

    return ImageReader(BytesIO(builder.png_bytes(width, height, seed=seed)))


class TestClippedImages:
    """In a PDF that places a photo large and clips it to a rectangle so only the visible part is drawn (browser
    `object-fit: cover` / `overflow: hidden`, Office cropping), the picture box was taken from the original placement box, so
    neighbouring body text was mixed into the picture and cut off, and when the original covered the page it was filtered
    out as a background and the photo disappeared without a warning. The picture box is the part that overlaps the
    clipping rectangle."""

    @staticmethod
    def _draw(placement: tuple[float, float, float, float], even_odd: bool = False):
        def draw(pdf, index):
            pdf.setFont("HYSMyeongJo-Medium", 14)
            pdf.drawString(60, 782, "3. 현장 사진")
            pdf.saveState()
            path = pdf.beginPath()
            path.rect(60, 582, 200, 180)
            pdf.clipPath(path, stroke=0, fill=0, fillMode=1 if even_odd else 0)
            pdf.drawImage(_image(480, 620), *placement)
            pdf.restoreState()
            pdf.setFont("HYSMyeongJo-Medium", 10)
            pdf.drawString(60, 567, "사진 1. 정문 전경")
            for row in range(15):
                pdf.drawString(60, 532 - row * 16, f"본문 {row + 1}행: 현장 점검 결과 특이 사항 없음.")

        return draw

    @pytest.mark.parametrize("even_odd", [False, True])
    def test_picture_is_cut_to_its_clipping_rectangle(self, even_odd):
        result = convert(_korean_pdf(self._draw((60, 142, 480, 620), even_odd)), "a.pdf")

        assert [image.kind for image in result.images] == [ImageKind.FIGURE]
        assert abs(result.images[0].width / result.images[0].height - 200 / 180) < 0.02  # the original placement is 480 × 620
        assert result.text.index("3. 현장 사진") < result.text.index("[Image]") < result.text.index("사진 1. 정문 전경")

    def test_clipped_picture_whose_placement_covers_the_page_is_not_taken_for_a_background(self):
        result = convert(_korean_pdf(self._draw((0, 0, 595.28, 841.89))), "a.pdf")

        assert [image.kind for image in result.images] == [ImageKind.FIGURE]
        assert result.text.index("3. 현장 사진") < result.text.index("[Image]") < result.text.index("사진 1. 정문 전경")


class TestPicturesInTableCells:
    """For photos inside ruled-table cells (photo registers, product lists) the cell was left empty and the photos were
    gathered after the table, so it was impossible to tell which row a photo belonged to. As in the other formats, the
    picture placeholder goes inside the cell."""

    rows = [("1", "무선 마우스", "25,000"), ("2", "기계식 키보드", "89,000"), ("3", "27인치 모니터", "320,000")]
    more = [("9", "무선 헤드셋", "120,000"), ("10", "웹캠", "45,000")]  # rows of the table continued on the next page

    def _draw(self, continued: bool = False):
        xs = [60, 110, 230, 400, 535]

        def draw(pdf, index):
            pdf.setFont("HYSMyeongJo-Medium", 14)
            if index == 0:
                pdf.drawString(60, 782, "제품 사진 목록")
            pdf.setFont("HYSMyeongJo-Medium", 10)
            top = 762 if index == 0 else 800
            y = top
            pdf.line(xs[0], top, xs[-1], top)
            if index == 0:
                for x, title in zip(xs, ["번호", "사진", "제품명", "단가(원)"], strict=False):
                    pdf.drawString(x + 5, top - 15, title)
                y = top - 22
                pdf.line(xs[0], y, xs[-1], y)
            for number, (order, name, price) in enumerate(self.rows if not continued or index == 0 else self.more):
                pdf.drawString(xs[0] + 5, y - 45, order)
                pdf.drawImage(_image(300, 300, seed=number), xs[1] + 20, y - 85, 80, 80)
                pdf.drawString(xs[2] + 5, y - 45, name)
                pdf.drawString(xs[3] + 5, y - 45, price)
                y -= 90
                pdf.line(xs[0], y, xs[-1], y)
            for x in xs:
                pdf.line(x, top, x, y)
            if index == 0 and continued:
                for row in range(5):  # fill in more rows so the table runs to the end of the page
                    pdf.drawString(xs[0] + 5, y - 15 - row * 20, f"{row + 4}")
                    pdf.drawString(xs[2] + 5, y - 15 - row * 20, f"상품 {row + 4}")
                    pdf.drawString(xs[3] + 5, y - 15 - row * 20, "1,000")
                    pdf.line(xs[0], y - 22 - row * 20, xs[-1], y - 22 - row * 20)
                for x in xs:
                    pdf.line(x, y, x, y - 22 - 4 * 20)
            else:
                pdf.drawString(60, y - 30, "표 아래 문단입니다.")

        return draw

    def test_picture_inside_a_cell_is_placed_in_that_cell(self):
        result = convert(_korean_pdf(self._draw()), "a.pdf")

        assert len(result.images) == 3
        for order, name, price in self.rows:
            assert f"| {order} | [Image] | {name} | {price} |" in result.text
        assert result.text.count("[Image]") == 3

    def test_picture_in_a_cell_of_a_table_continued_from_the_previous_page_is_placed_in_that_cell(self):
        """A table that carries over the header row of the previous page's table (`_carry_table_headers`) gains one row but
        still puts each picture in the cell of its own row."""
        result = convert(_korean_pdf(self._draw(continued=True), pages=2), "a.pdf")

        second = result.text.split("<!-- Page 2 -->")[1]
        assert "| 번호 | 사진 | 제품명 | 단가(원) |" in second
        for order, name, price in self.more:
            assert f"| {order} | [Image] | {name} | {price} |" in second

    def test_picture_between_lines_of_a_cell_keeps_its_place_among_them(self):
        """A picture placed between the lines of a cell (a diagram inside the '산정 로직' cell of a review request) is put
        between the cell's lines at that height."""
        def draw(pdf, index):
            pdf.setFont("HYSMyeongJo-Medium", 10)
            for y in (780, 760, 500):
                pdf.line(60, y, 535, y)
            for x in (60, 160, 535):
                pdf.line(x, 780, x, 500)
            pdf.drawString(65, 766, "구분")
            pdf.drawString(165, 766, "내용")
            pdf.drawString(65, 640, "산정 로직")
            pdf.drawString(165, 745, "1) 위 설명 첫째 줄")
            pdf.drawString(165, 731, "2) 위 설명 둘째 줄")
            pdf.drawImage(_image(300, 200), 170, 560, 240, 160)
            pdf.drawString(165, 540, "(출처: 아래 설명 줄)")

        result = convert(_korean_pdf(draw), "a.pdf")

        assert "| 산정 로직 | 1) 위 설명 첫째 줄<br>2) 위 설명 둘째 줄<br>[Image]<br>(출처: 아래 설명 줄) |" in result.text


class TestPictureGrid:
    """In a photo grid of four or more columns (product guides, product line-ups) the caption under a photo is wider than
    half the photo width and was taken for 'body text of another column' of the neighbouring photo, so photos and captions
    were mixed up (with 2 or 3 columns each row gave its photos → its captions). Three or more pictures side by side at
    the same height are kept together as one row."""

    @pytest.mark.parametrize("columns, rows", [(2, 3), (3, 3), (4, 2), (4, 3), (5, 4)])
    def test_each_row_of_pictures_comes_before_its_captions(self, columns, rows):
        width = (595.28 - 100) / columns
        height = width * 0.75

        def draw(pdf, index):
            pdf.setFont("HYSMyeongJo-Medium", 14)
            pdf.drawString(50, 792, "신상품 안내")
            pdf.setFont("HYSMyeongJo-Medium", 9)
            for number in range(columns * rows):
                x = 50 + number % columns * width
                y = 762 - height - number // columns * (height + 40)
                pdf.drawImage(_image(300, 225, seed=number), x + 5, y, width - 10, height)
                pdf.drawString(x + 5, y - 12, f"상품{number + 1:02d} {(number + 1) * 1100:,}원")

        result = convert(_korean_pdf(draw), "a.pdf")

        tokens = [token for token in result.text.split() if token == "[Image]" or token.startswith("상품")]
        assert tokens == [token for row in range(rows) for token in
                          ["[Image]"] * columns + [f"상품{row * columns + column + 1:02d}" for column in range(columns)]]


class TestManyPicturesInRuledCells:
    """Every ruled cell holding a picture scanned all the characters of the page for its lines, and every cell all the
    pictures of the page: a ruled grid with a picture and a label in each of 3,600 cells spent 30 seconds there."""

    @staticmethod
    def _grid(size: int) -> tuple[list[tuple], list[dict], list[tuple]]:
        """A `size` × `size` grid of 50pt cells, each with a picture, a two-line label and a character on the cell edge."""
        boxes, chars, figures = [], [], []
        for row in range(size):
            for column in range(size):
                x, y = 20.0 + column * 50, 20.0 + row * 50
                boxes.append((x, y, x + 50, y + 50))
                figures.append((x + 4, y + 4, x + 46, y + 30, 1.0))
                chars += [{"text": "a", "x0": x + 3, "x1": x + 7, "top": y + 32 + line * 8, "bottom": y + 38 + line * 8}
                          for line in range(2)]
                chars.append({"text": "b", "x0": x - 2, "x1": x + 2, "top": y + 40, "bottom": y + 46})
        return boxes, chars, figures

    @staticmethod
    def _table(boxes: list[tuple], chars: list[dict], figures: list[tuple]) -> object:
        from types import SimpleNamespace

        from opendockit.extractors import _pdf_ruled_tables

        lookups = _pdf_ruled_tables._TableLookups(SimpleNamespace(chars=chars, horizontal_edges=[]), figures)
        return _pdf_ruled_tables._ruled_table((0.0, 0.0, 1.0, 1.0), [["x"] * len(boxes)], boxes, lookups)

    @pytest.mark.timing
    def test_cell_lines_take_linear_time(self):
        spent = []
        for size in (20, 40):
            timer = Fastest()
            for _ in timer:
                boxes, chars, figures = self._grid(size)
                with timer:
                    table = self._table(boxes, chars, figures)
                assert len(table.cell_lines) == size * size and table.cell_lines[0] == (52.0, 60.0)
            spent.append(timer.seconds)
        small, large = spent

        assert large < 8 * small + 0.5  # 16 times as long when every cell scanned the whole page

    def test_huge_cell_is_read(self):
        """A ruled cell 1e21pt wide beside a table: the grid cells it covers are more than `sys.maxsize`, and `len()` of that
        range raised OverflowError, which rejected the whole document."""
        from tests.test_pdf import _raw_pdf

        content = "\n".join(["0.5 w", *(f"{x} {y} 100 40 re S" for x in (100, 200, 300) for y in (300, 340, 380)),
                             "100 420 1000000000000000000000 40 re S",
                             *(f"BT /F1 8 Tf {x + 4} {y + 15} Td (c{x}{y}) Tj ET" for x in (100, 200, 300) for y in (300, 340, 380))])
        data = _raw_pdf({
            1: b"<< /Type /Catalog /Pages 2 0 R >>", 2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            3: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            5: b"<< /Length %d >>stream\n%s\nendstream" % (len(content), content.encode())})

        assert "| c100380 | c200380 | c300380 |" in convert(data, "a.pdf").text

    def test_regions_of_huge_extent_are_answered(self):
        """The page indexes give every item for a region of more grid cells or bands than they look through, however many."""
        from opendockit.extractors._pdf_model import _ObjectBands, _PointGrid

        grid = _PointGrid([(1.0, 1.0)], lambda item: item)
        bands = _ObjectBands({"char": [{"top": 1.0, "bottom": 2.0}]})

        assert grid.within(0, 0, 1e21, 10) == [(1.0, 1.0)] and grid.within(0, 0, 10, 1e300) == [(1.0, 1.0)]
        assert bands.overlapping((0, 0, 10, 1e21)) is None

    @pytest.mark.parametrize("seed", range(6))
    def test_cell_lines_are_those_of_scanning_the_page(self, monkeypatch, seed):
        import random

        from opendockit.extractors import _pdf_model

        rng = random.Random(seed)
        odd = [float("nan"), float("inf"), -float("inf"), 1e12]
        boxes = []
        for _ in range(120):
            x, y = rng.uniform(0, 900), rng.uniform(0, 900)
            width, height = rng.choice([(50.0, 40.0), (8.0, 300.0), (700.0, 12.0), (-20.0, 30.0), (5000.0, 5000.0)])
            boxes.append(tuple(rng.choice([value, value, value, value, *odd]) for value in (x, y, x + width, y + height)))
        figures, chars = [], []
        for _ in range(150):
            x, y = rng.uniform(-20, 920), rng.uniform(-20, 920)
            width, height = rng.choice([(30.0, 20.0), (2.0, 2.0), (-10.0, 20.0), (20.0, -10.0), (400.0, 60.0)])
            figures.append((*(rng.choice([value, value, value, *odd]) for value in (x, y, x + width, y + height)), 1.0))
        for _ in range(3000):
            x, y = rng.uniform(-20, 920), rng.uniform(-20, 920)
            chars.append({"text": rng.choice(["a", "b", " "]), "x0": rng.choice([x, x, x, *odd]), "x1": x + rng.choice([4.0, -4.0]),
                          "top": rng.choice([y, y, y, *odd]), "bottom": y + rng.choice([6.0, -6.0])})

        indexed = self._table(boxes, chars, figures).cell_lines
        with monkeypatch.context() as patched:
            patched.setattr(_pdf_model._PointGrid, "within", lambda self, *box: self._items)
            scanned = self._table(boxes, chars, figures).cell_lines

        assert indexed == scanned and len(indexed) > 20
