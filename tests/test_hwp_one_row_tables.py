"""A table of one row in the top-level body of a Hangul document is a line of text (`SizeHeadings.row`): the bar an
attachment opens with, the time of release beside the date. In 59 Korean press releases 126 of 219 tables were of one
row and none held data; each came out as a table header over no rows (`evals/README.md`)."""

import struct

from opendockit import convert
from opendockit.extractors.hwp import TAG_CTRL_HEADER, TAG_LIST_HEADER, TAG_PARA_CHAR_SHAPE, TAG_PARA_HEADER, TAG_PARA_TEXT, TAG_TABLE
from tests import builder
from tests.test_hwp import _para_header
from tests.test_hwp_size_headings import _BODY, _hwp, _hwpx, _hwpx_paragraph, _paragraph


def _hwp_row(*cells: list[tuple[str, int]], rows: int = 1) -> bytes:
    """A top-level table whose `cells` stand side by side, `rows` rows of them; each cell is its paragraphs as (text,
    character shape id)."""
    record = builder.hwp_record
    columns = len(cells) // rows
    body = b"".join(
        record(TAG_LIST_HEADER, 2, builder.hwp_cell(index // columns, index % columns))
        + b"".join(record(TAG_PARA_HEADER, 2, _para_header(0)) + record(TAG_PARA_TEXT, 3, builder.hwp_text(text))
                   + record(TAG_PARA_CHAR_SHAPE, 3, struct.pack("<II", 0, shape)) for text, shape in cell)
        for index, cell in enumerate(cells))
    return (record(TAG_PARA_HEADER, 0, _para_header(0)) + record(TAG_PARA_TEXT, 1, builder.hwp_text((11, "tbl ")))
            + record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("tbl ")) + record(TAG_TABLE, 2, struct.pack("<IHH", 0, rows, columns) + b"\x00" * 10)
            + body)


def _hwpx_row(*cells: list[tuple[str, int]], rows: int = 1) -> str:
    columns = len(cells) // rows
    written = "".join(
        ("<hp:tr>" if index % columns == 0 else "") + "<hp:tc><hp:subList>"
        + "".join(_hwpx_paragraph(text, (shape, text)) for text, shape in cell)
        + f'</hp:subList><hp:cellAddr colAddr="{index % columns}" rowAddr="{index // columns}"/></hp:tc>'
        + ("</hp:tr>" if index % columns == columns - 1 else "")
        for index, cell in enumerate(cells))
    return (f'<hp:p paraPrIDRef="0" styleIDRef="0"><hp:run charPrIDRef="0"><hp:tbl rowCnt="{rows}" colCnt="{columns}">{written}'
            "</hp:tbl></hp:run></hp:p>")


def _both(*cells: list[tuple[str, int]], rows: int = 1) -> tuple[str, str]:
    return (convert(_hwp(_paragraph(_BODY), _hwp_row(*cells, rows=rows), _paragraph(_BODY)), "a.hwp").text,
            convert(_hwpx(_hwpx_paragraph(_BODY), _hwpx_row(*cells, rows=rows), _hwpx_paragraph(_BODY)), "a.hwpx").text)


class TestOneRowTables:
    def test_the_cells_of_one_row_are_one_line(self):
        for text in _both([("보도시점", 0)], [("배포 즉시 보도 가능", 0)], [("배포", 0)], [("2026. 11. 20.(금)", 0)]):
            assert text.split("\n\n")[1] == "보도시점 배포 즉시 보도 가능 배포 2026. 11. 20.(금)"
            assert "|" not in text and "#" not in text

    def test_the_bar_of_an_attachment_is_a_heading_whatever_its_size(self):
        for label in ("붙임", "붙 임", "참고 2", "별첨1"):
            for text in _both([(label, 3)], [], [("행사 개요", 3)]):
                assert text.split("\n\n")[1] == f"## {label} 행사 개요"

    def test_a_bar_with_a_cell_that_could_be_no_heading_is_a_heading_still(self):
        """A cell of one figure between the label and the title, as some bars have: the label decides, not each cell."""
        for text in _both([("붙임2", 3)], [("1", 3)], [("업무협약서 전문", 3)]):
            assert text.split("\n\n")[1] == "## 붙임2 1 업무협약서 전문"

    def test_a_label_alone_or_another_first_cell_is_no_attachment_bar(self):
        for text in (*_both([("붙임", 3)], []), *_both([("붙임 자료는 다음과 같다", 3)], [("행사 개요", 3)])):
            assert "#" not in text

    def test_a_row_set_large_is_a_heading_by_its_size(self):
        """16 pt beside a body of 10 pt, as the title of a box is (`TestTitlesInBoxes`)."""
        for text in _both([("2026년", 2)], [("산지관리 추진 계획", 2)]):
            assert text.split("\n\n")[1] == "# 2026년 산지관리 추진 계획"

    def test_a_row_with_one_cell_set_small_is_no_heading(self):
        for text in _both([("산지관리 추진 계획", 2)], [("산림청", 0)]):
            assert text.split("\n\n")[1] == "산지관리 추진 계획 산림청"

    def test_a_cell_of_several_paragraphs_keeps_the_table(self):
        """Two texts set side by side are not one line."""
        for text in _both([("왼쪽 첫 줄", 0), ("왼쪽 둘째 줄", 0)], [("오른쪽", 0)]):
            assert text.split("\n\n")[1] == "| 왼쪽 첫 줄<br>왼쪽 둘째 줄 | 오른쪽 |\n| --- | --- |"

    def test_a_table_of_two_rows_is_a_table(self):
        for text in _both([("붙임", 3)], [("행사 개요", 3)], [("일시", 0)], [("11월 20일", 0)], rows=2):
            assert text.split("\n\n")[1] == "| 붙임 | 행사 개요 |\n| --- | --- |\n| 일시 | 11월 20일 |"
