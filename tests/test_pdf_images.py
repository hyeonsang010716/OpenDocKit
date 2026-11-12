"""PDF picture positions: the visible area of a clipped picture, pictures inside ruled-table cells, multi-column photo grids."""

from io import BytesIO

import pytest

from opendockit import ImageKind, convert
from tests import builder
from tests.test_pdf import _korean_pdf


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
