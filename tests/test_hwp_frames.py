"""A table of the top-level body of a Hangul document that only frames text — no row of it holds content in more than
one cell, no cell of it is drawn as a box, and none holds a table — is one column of text (`_hangul.is_frame`).

Hangul authors draw the box around a summary as a table of three rows and columns: the label (`< 요 약 >`) sits across the
top line of the box, between two cells that hold nothing but a piece of that line, above one cell with the text. 380
tables in 91 of 2,180 HWPX files are built so. Drawn as the table it is, the box was an HTML table with empty cells
around its label; Hancom's PDF of the same document reads a label and paragraphs.
"""

from html import escape
import struct

import pytest

from opendockit import convert
from opendockit.extractors import _hangul
from opendockit.extractors.hwp import TAG_BORDER_FILL, TAG_CTRL_HEADER, TAG_LIST_HEADER, TAG_PARA_HEADER, TAG_PARA_TEXT, TAG_TABLE
from tests import builder
from tests.test_hwp import _fake_hwp


# Border fills by the sides they draw (left, right, top, bottom): 1 none, 2 all four, then the pieces of a summary box
_FILLS = {"none": 1, "box": 2, "bottom": 3, "top-left": 4, "top-right": 5, "open-top": 6, "white-box": 7}
_SIDES = {"none": "", "box": "lrtb", "bottom": "b", "top-left": "tl", "top-right": "tr", "open-top": "lrb", "white-box": "lrtb"}
# (row, column, row span, column span, text, border fill): the box around a summary, its label across the top line
_SUMMARY = [(0, 0, 1, 1, "", "bottom"), (0, 1, 2, 1, "< 요 약 >", "none"), (0, 2, 1, 1, "", "bottom"),
            (1, 0, 1, 1, "", "top-left"), (1, 2, 1, 1, "", "top-right"), (2, 0, 1, 3, "◇ 수출 1조 달러 돌파", "open-top")]
_SUMMARY_TEXT = "앞 문단\n\n< 요 약 >\n\n◇ 수출 1조 달러 돌파\n\n뒤 문단"
_SUMMARY_TABLE = ('앞 문단\n\n<table>\n<tr><th></th><th rowspan="2">< 요 약 ></th></tr>\n<tr><td></td></tr>\n'
                  '<tr><td colspan="2">◇ 수출 1조 달러 돌파</td></tr>\n</table>\n\n뒤 문단')


def _hwpx(cells, rows: int, columns: int, *, fills: bool = True, caption: str = "") -> bytes:
    def border(name: str, sides: str, side: str) -> str:
        kind = "SOLID" if side[0] in sides else "NONE"
        colour = "#FFFFFF" if name == "white-box" else "#000000"
        return f'<hh:{side}Border type="{kind}" width="0.12 mm" color="{colour}"/>'

    header = "<hh:borderFills>" + "".join(
        f'<hh:borderFill id="{number}">' + "".join(border(name, _SIDES[name], side) for side in ("left", "right", "top", "bottom"))
        + "</hh:borderFill>" for name, number in _FILLS.items()) + "</hh:borderFills>"
    table = "".join(
        f'<hp:tr><hp:tc borderFillIDRef="{_FILLS[fill]}"><hp:subList>'
        + "".join(f"<hp:p><hp:run><hp:t>{escape(line)}</hp:t></hp:run></hp:p>" for line in text.split("\n"))
        + f'</hp:subList><hp:cellAddr colAddr="{column}" rowAddr="{row}"/>'
        f'<hp:cellSpan colSpan="{column_span}" rowSpan="{row_span}"/></hp:tc></hp:tr>'
        for row, column, row_span, column_span, text, fill in cells)
    caption = f"<hp:caption><hp:subList><hp:p><hp:run><hp:t>{caption}</hp:t></hp:run></hp:p></hp:subList></hp:caption>" if caption else ""
    body = ("<hp:p><hp:run><hp:t>앞 문단</hp:t></hp:run></hp:p>"
            f'<hp:p><hp:run><hp:tbl rowCnt="{rows}" colCnt="{columns}">{caption}{table}</hp:tbl></hp:run></hp:p>'
            "<hp:p><hp:run><hp:t>뒤 문단</hp:t></hp:run></hp:p>")
    data = builder.hwpx_bytes(body)
    return builder.zip_with(data, edit={"Contents/header.xml": lambda xml: xml.replace("<hh:refList>", "<hh:refList>" + header)}) \
        if fills else data


def _hwp(monkeypatch, cells, rows: int, columns: int, *, fills: bool = True) -> bytes:
    record = builder.hwp_record

    def fill(name: str) -> bytes:   # attribute, then the left, right, top and bottom borders: type, width, colour
        colour = b"\xff\xff\xff\x00" if name == "white-box" else b"\x00\x00\x00\x00"
        return b"\x00\x00" + b"".join(bytes([side in _SIDES[name], 1]) + colour for side in "lrtb") + b"\x00" * 8

    def paragraph(level: int, text) -> bytes:
        return record(TAG_PARA_HEADER, level, builder.hwp_para_header()) + record(TAG_PARA_TEXT, level + 1, builder.hwp_text(text))

    table = b"".join(
        record(TAG_LIST_HEADER, 2, builder.hwp_cell(row, column, row_span=row_span, col_span=column_span)[:32]
               + struct.pack("<H", _FILLS[name] if fills else 0))
        + b"".join(paragraph(2, line) for line in text.split("\n"))
        for row, column, row_span, column_span, text, name in cells)
    section = (paragraph(0, "앞 문단") + paragraph(0, (11, "tbl ")) + record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("tbl "))
               + record(TAG_TABLE, 2, struct.pack("<IHH", 0, rows, columns) + b"\x00" * 10) + table + paragraph(0, "뒤 문단"))
    docinfo = b"".join(record(TAG_BORDER_FILL, 0, fill(name)) for name in _FILLS) if fills else b""
    return _fake_hwp(monkeypatch, sections=[section], docinfo_extra=docinfo)


class TestSummaryBox:
    def test_hwpx(self):
        assert convert(_hwpx(_SUMMARY, 3, 3), "a.hwpx").text == _SUMMARY_TEXT

    def test_hwp(self, monkeypatch):
        assert convert(_hwp(monkeypatch, _SUMMARY, 3, 3), "a.hwp").text == _SUMMARY_TEXT

    def test_the_lines_of_its_text_stay_lines(self):
        cells = [*_SUMMARY[:5], (2, 0, 1, 3, "◇ 현황\n- 반도체\n◇ 성장세", "open-top")]

        assert convert(_hwpx(cells, 3, 3), "a.hwpx").text == "앞 문단\n\n< 요 약 >\n\n◇ 현황\n- 반도체\n◇ 성장세\n\n뒤 문단"

    def test_no_paragraph_of_it_becomes_a_heading_by_its_size(self):
        """A frame is unfolded and nothing else: its label is no heading, as it was none while the frame was a table."""
        text = convert(_hwpx(_SUMMARY, 3, 3), "a.hwpx").text

        assert "#" not in text

    def test_label_spanning_rows_is_written_once(self):
        """A label beside nothing, over three rows: drawn as a table and unfolded as one column, it was written on each row."""
        cells = [(0, 0, 1, 1, "", "none"), (0, 1, 3, 1, "가정에서는", "none"), (1, 0, 1, 1, "", "none"), (2, 0, 1, 1, "", "none")]

        assert convert(_hwpx(cells, 3, 2), "a.hwpx").text == "앞 문단\n\n가정에서는\n\n뒤 문단"


class TestTablesThatStayTables:
    def test_two_texts_in_a_row(self):
        cells = [(0, 0, 1, 1, "기관명", "none"), (0, 1, 1, 1, "한국정보원", "none"), (1, 0, 1, 2, "대전광역시", "none")]

        assert "<tr><th>기관명</th><th>한국정보원</th></tr>" in convert(_hwpx(cells, 2, 2), "a.hwpx").text

    def test_text_beside_a_cell_that_spans_its_row(self):
        """One text to a row by where the cells start, two by what they cover: a label over three rows beside three lines."""
        cells = [(0, 0, 3, 1, "비고", "none"), (0, 1, 1, 1, "", "none"), (1, 1, 1, 1, "첫째", "none"), (2, 1, 1, 1, "둘째", "none")]

        text = convert(_hwpx(cells, 3, 2), "a.hwpx").text

        assert "<table>" in text or "| 비고 |" in text

    # A form to fill in: an empty cell with a border on all four sides is a blank, `성명 | ____`
    _FORM = [(0, 0, 1, 1, "성명", "box"), (0, 1, 1, 1, "", "box"), (1, 0, 1, 1, "", "box"), (1, 1, 1, 1, "서명", "box")]
    _FORM_TEXT = "앞 문단\n\n| 성명 | |\n| --- | --- |\n| | 서명 |\n\n뒤 문단"

    def test_blank_field_of_a_form(self):
        assert convert(_hwpx(self._FORM, 2, 2), "a.hwpx").text == self._FORM_TEXT

    def test_hwp_blank_field_of_a_form(self, monkeypatch):
        assert convert(_hwp(monkeypatch, self._FORM, 2, 2), "a.hwp").text == self._FORM_TEXT

    def test_the_same_cells_without_their_boxes_are_a_frame(self):
        cells = [(row, column, row_span, column_span, text, "none") for row, column, row_span, column_span, text, _ in self._FORM]

        assert convert(_hwpx(cells, 2, 2), "a.hwpx").text == "앞 문단\n\n성명\n\n서명\n\n뒤 문단"

    def test_label_drawn_as_a_box_of_its_own(self):
        """A cell boxed on all four sides is a cell on paper, with content as without: the PDF of the same document shows a
        ruled cell there, and the table is left a table."""
        cells = [(0, 0, 1, 1, "", "none"), (0, 1, 2, 1, "일 러 두 기", "box"), (0, 2, 1, 1, "", "none"),
                 (1, 0, 1, 1, "", "none"), (1, 2, 1, 1, "", "none"), (2, 0, 1, 3, "□ 작성 목적", "none")]

        assert "<table>" in convert(_hwpx(cells, 3, 3), "a.hwpx").text

    def test_hwp_label_drawn_as_a_box_of_its_own(self, monkeypatch):
        cells = [(0, 0, 1, 1, "", "none"), (0, 1, 2, 1, "일 러 두 기", "box"), (0, 2, 1, 1, "", "none"),
                 (1, 0, 1, 1, "", "none"), (1, 2, 1, 1, "", "none"), (2, 0, 1, 3, "□ 작성 목적", "none")]

        assert "<table>" in convert(_hwp(monkeypatch, cells, 3, 3), "a.hwp").text

    def test_a_white_border_is_no_border(self):
        """White lines are not seen on paper: cells boxed in white are pieces of the frame."""
        cells = [(0, 0, 1, 1, "성명", "white-box"), (0, 1, 1, 1, "", "white-box"), (1, 0, 1, 1, "", "white-box"),
                 (1, 1, 1, 1, "서명", "white-box")]

        assert convert(_hwpx(cells, 2, 2), "a.hwpx").text == "앞 문단\n\n성명\n\n서명\n\n뒤 문단"

    def test_borders_that_are_not_known(self):
        """Without the border fills of the header nothing says the empty cells are no blanks: the table is kept."""
        assert convert(_hwpx(_SUMMARY, 3, 3, fills=False), "a.hwpx").text == _SUMMARY_TABLE

    def test_hwp_borders_that_are_not_known(self, monkeypatch):
        assert convert(_hwp(monkeypatch, _SUMMARY, 3, 3, fills=False), "a.hwp").text == _SUMMARY_TABLE

    def test_table_with_a_caption(self):
        assert "<table>" in convert(_hwpx(_SUMMARY, 3, 3, caption="표 1. 요약"), "a.hwpx").text

    def test_picture_beside_a_text(self):
        """A picture is content: a picture beside a text is two things side by side."""
        cells = [(0, 0, 1, 2, "수출 1조 달러 달성 4개국", "none"), (1, 0, 1, 1, "", "none"), (1, 1, 1, 1, "국토면적", "none")]
        data = builder.zip_with(_hwpx(cells, 2, 2), edit={"Contents/section0.xml": lambda xml: xml.replace(
            "<hp:t></hp:t>", "<hp:t></hp:t><hp:pic/>", 1)})

        assert "<table>" in convert(data, "a.hwpx").text

    def test_table_without_text(self):
        cells = [(row, column, 1, 1, "", "none") for row in range(2) for column in range(2)]

        assert convert(_hwpx(cells, 2, 2), "a.hwpx").text == "앞 문단\n\n뒤 문단"


class TestWhatIsRead:
    def test_frame_that_holds_a_table_is_left_as_it_is(self):
        inner = ('<hp:tbl rowCnt="2" colCnt="2">' + "".join(
            f"<hp:tr>{builder._hwpx_cell(row, 0, left)}{builder._hwpx_cell(row, 1, right)}</hp:tr>"
            for row, (left, right) in enumerate((("구분", "값"), ("수출", "1조")))) + "</hp:tbl>")
        data = builder.zip_with(_hwpx(_SUMMARY, 3, 3), edit={"Contents/section0.xml": lambda xml: xml.replace(
            "<hp:t>◇ 수출 1조 달러 돌파</hp:t>", f"<hp:t>◇ 수출 1조 달러 돌파</hp:t>{inner}")})

        text = convert(data, "a.hwpx").text

        assert '<th rowspan="2">< 요 약 ></th>' in text and "<table><tr><th>구분</th><th>값</th></tr>" in text

    def test_hwp_and_hwpx_read_alike(self, monkeypatch):
        cells = [(0, 0, 1, 1, "", "bottom"), (0, 1, 2, 1, "< 참고 >", "none"), (0, 2, 1, 1, "", "bottom"),
                 (1, 0, 1, 1, "", "top-left"), (1, 2, 1, 1, "", "top-right"), (2, 0, 1, 3, "※ 신고수리일 기준\n※ 잠정치", "open-top")]

        assert convert(_hwp(monkeypatch, cells, 3, 3), "a.hwp").text == convert(_hwpx(cells, 3, 3), "a.hwpx").text \
            == "앞 문단\n\n< 참고 >\n\n※ 신고수리일 기준\n※ 잠정치\n\n뒤 문단"


class TestIsFrame:
    """`is_frame` on the cells themselves: (row, rows spanned, content, the cell), and whether an empty cell is boxed."""

    @pytest.mark.parametrize(("cells", "boxed", "frame"), [
        ([(0, 2, True, "open"), (0, 1, False, "open"), (2, 1, True, "open")], {"open": False}, True),
        ([(0, 2, True, "label"), (0, 1, False, "open"), (2, 1, True, "open")], {"open": False, "label": True}, False),  # a cell with content drawn as a box
        ([(0, 2, True, "label"), (0, 1, False, "open")], {"open": False}, False),         # ... or with borders not known
        ([(0, 1, True, "a"), (0, 1, True, "b")], {}, False),                              # two texts in a row
        ([(0, 3, True, "a"), (1, 1, True, "b")], {}, False),                              # ... by the rows a cell covers
        ([(0, 1, True, "a"), (1, 1, False, "blank")], {"a": False, "blank": True}, False),  # a blank to fill in
        ([(0, 1, True, "a"), (1, 1, False, "unknown")], {"a": False, "unknown": None}, False),  # borders not known
        ([(0, 1, False, "open"), (1, 1, False, "open")], {"open": False}, False),         # no content
        ([(0, 1, True, "a"), None, (1, 1, True, "b")], {}, False),                        # a cell without an address
        ([(0, 0, True, "a")], {}, False),                                                 # a span of no rows
        ([(0, 70_000, True, "a")], {}, False),                                            # ... or of more rows than a frame has
        ([(row, 1, True, "a") for row in range(100)], {"a": False}, True),
        ([(row, 1, True, "a") for row in range(101)], {}, False),                         # more cells than a frame has
    ])
    def test_cases(self, cells, boxed, frame):
        assert _hangul.is_frame(cells, boxed.get) is frame

    def test_cells_are_read_only_as_far_as_the_answer_needs(self):
        """The header of nearly every table holds two texts: the question costs two cells and asks for no border."""
        seen = []

        def cells():
            for cell in [(0, 1, True, "a"), (0, 1, True, "b"), (1, 1, False, "c"), (1, 1, True, "d")]:
                seen.append(cell[3])
                yield cell

        assert _hangul.is_frame(cells(), lambda cell: pytest.fail("no border is asked for")) is False
        assert seen == ["a", "b"]
