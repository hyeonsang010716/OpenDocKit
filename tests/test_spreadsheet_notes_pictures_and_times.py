"""Excel cell comments, the cell a floating picture sits on, time formats.

- Cell comments (review remarks) were dropped without a warning. Like DOCX and PPTX comments they go at the end of the sheet as
  `> Comment(author, cell E5, on "…"): text` lines.
- On a sheet with a picture on every row (a product photo list) the pictures followed the table only as `[Image]`, so it was
  impossible to tell which row a picture belonged to.
- `h:mm`, `h:mm AM/PM` and `h"시" mm"분"` cells came out as `18:03:40` with seconds attached, and `[h]"시간" mm"분"` as `36:00`
  without its units.
"""

from datetime import datetime, time, timedelta
from io import BytesIO
import struct
from time import perf_counter

import openpyxl
from openpyxl.comments import Comment
import pytest

from opendockit import convert
from opendockit.extractors._sheet_format import format_cell
from opendockit.extractors.xls import extract_xls
from opendockit.models import ConvertOptions
from tests import builder
from tests.test_spreadsheet import _anchor, _xlsx_with_drawing


_THREADED = "http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments"
_THREADED_RELATIONSHIPS = "http://schemas.microsoft.com/office/2017/10/relationships"


@pytest.fixture(params=[False, True], ids=["full-load", "streamed"])
def streamed(request, monkeypatch):
    """Read the same XLSX through both paths, full load and streaming (same as the fixture of the same name in
    test_spreadsheet.py)."""
    from opendockit.extractors import spreadsheet

    if request.param:
        monkeypatch.setattr(spreadsheet, "_MAX_MERGED_CELLS", -1)
    return request.param


def _report_with_comments(comments: dict[str, tuple[str, str]]) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for row in [["월", "매출", "비고"], ["1월", 100, ""], ["2월", 120, "할인"]]:
        sheet.append(row)
    for coordinate, (text, author) in comments.items():
        sheet[coordinate].comment = Comment(text, author)
    buffer = BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _with_threaded_comments(data: bytes, comments: str, people: str) -> bytes:
    """Attach Excel 365 threaded comments (threadedComments) and the person list (persons) to a workbook written by openpyxl."""
    def sheet_relationships(xml: str) -> str:
        return xml.replace("</Relationships>", f'<Relationship Id="rIdT" Type="{_THREADED_RELATIONSHIPS}/threadedComment" '
                                               'Target="../threadedComments/threadedComment1.xml"/></Relationships>')

    def workbook_relationships(xml: str) -> str:
        return xml.replace("</Relationships>", f'<Relationship Id="rIdP" Type="{_THREADED_RELATIONSHIPS}/person" '
                                               'Target="persons/person.xml"/></Relationships>')

    return builder.zip_with(data, edit={"xl/worksheets/_rels/sheet1.xml.rels": sheet_relationships,
                                        "xl/_rels/workbook.xml.rels": workbook_relationships},
                            add={"xl/threadedComments/threadedComment1.xml":
                                 f'<ThreadedComments xmlns="{_THREADED}">{comments}</ThreadedComments>',
                                 "xl/persons/person.xml": f'<personList xmlns="{_THREADED}">{people}</personList>'})


class TestXlsxCellComments:
    def test_comment_is_written_after_the_table_with_its_cell_and_row(self, streamed):
        data = _report_with_comments({"B3": ("검토자:\n3월 할인 행사 영향", "검토자")})

        result = convert(data, "a.xlsx")

        assert result.text == ("## Sheet: Sheet\n\n| 월 | 매출 | 비고 |\n| --- | --- | --- |\n| 1월 | 100 | |\n| 2월 | 120 | 할인 |\n\n"
                               '> Comment(검토자, cell B3, on "2월: 120"): 3월 할인 행사 영향')
        assert result.warnings == []

    def test_comments_follow_the_cell_order(self, streamed):
        data = _report_with_comments({"C3": ("둘째", "을"), "A2": ("첫째", "갑")})

        result = convert(data, "a.xlsx")

        assert result.text.endswith('> Comment(갑, cell A2, on "1월"): 첫째\n> Comment(을, cell C3, on "2월: 할인"): 둘째')

    def test_threaded_comment_with_a_reply_replaces_its_legacy_copy(self, streamed):
        legacy = "[Threaded comment]\n\nYour version of Excel allows you to read this threaded comment.\n\nComment:\n    단가 확인"
        comments = ('<threadedComment ref="B2" personId="{P1}" id="{C1}"><text>단가 확인 부탁</text></threadedComment>'
                    '<threadedComment ref="B2" personId="{P2}" id="{C2}" parentId="{C1}"><text>확인했습니다</text></threadedComment>')
        people = '<person displayName="김검토" id="{P1}"/><person displayName="이담당" id="{P2}"/>'
        data = _with_threaded_comments(_report_with_comments({"B2": (legacy, "tc={C1}")}), comments, people)

        result = convert(data, "a.xlsx")

        assert result.text.endswith('| 2월 | 120 | 할인 |\n\n> Comment(김검토, cell B2, on "1월: 100"): 단가 확인 부탁\n'
                                    "> Reply(이담당): 확인했습니다")
        assert "Threaded comment" not in result.text

    def test_sheet_without_comments_is_unchanged(self, streamed):
        result = convert(_report_with_comments({}), "a.xlsx")

        assert result.text.endswith("| 2월 | 120 | 할인 |")

    def test_comments_count_toward_the_character_limit(self, streamed):
        data = _report_with_comments({"A2": ("가" * 200, "갑")})

        result = convert(data, "a.xlsx", ConvertOptions(max_chars=100))

        assert result.exceeded


def _xls_note(row: int, column: int, author: str) -> bytes:
    """NOTE record (BIFF8): row, column, options, object id (the 1 of `builder.xls_object`) + author + one trailing byte."""
    return builder.xls_record(0x001C, struct.pack("<4HH", row, column, 0, 1, len(author)) + b"\x01"
                              + author.encode("utf-16-le") + b"\x00")


class TestXlsCellComments:
    def test_comment_is_written_after_the_table_with_its_cell(self):
        rows = [["항목", "값"], ["가", "1"]]
        data = builder.xls_bytes([("Sheet1", rows, builder.xls_text_box("검토 의견", object_type=0x19) + _xls_note(1, 1, "검토자"))])

        extraction = extract_xls(data, ConvertOptions())

        assert extraction.sections[0].text == ('| 항목 | 값 |\n| --- | --- |\n| 가 | 1 |\n\n'
                                               '> Comment(검토자, cell B2, on "가: 1"): 검토 의견')


def _picture_at(row: int, column: int, relationship_id: str) -> str:
    picture = ('<xdr:pic><xdr:nvPicPr><xdr:cNvPr id="3" name="Picture 1"/><xdr:cNvPicPr/></xdr:nvPicPr><xdr:blipFill>'
               f'<a:blip r:embed="{relationship_id}"/></xdr:blipFill><xdr:spPr/></xdr:pic>')
    return _anchor(row, picture).replace("<xdr:col>3</xdr:col>", f"<xdr:col>{column}</xdr:col>", 1)


class TestFloatingPictureCells:
    def test_each_picture_is_labeled_with_its_cell_and_row_when_a_sheet_has_several(self, streamed):
        """The table of `_xlsx_with_drawing` is `항목 | 값`, `가 | 1` (rows 1 and 2). The pictures are on row 2 (0-based 1) column C
        and row 3 column D."""
        media = {"image1.png": builder.png_bytes(seed=3), "image2.png": builder.png_bytes(seed=4)}
        data = _xlsx_with_drawing(_picture_at(2, 3, "rId2") + _picture_at(1, 2, "rId1"), media)

        result = convert(data, "a.xlsx")

        assert result.text.endswith("| 가 | 1 |\n\n(cell C2: 가)\n\n[Image]\n\n(cell D3)\n\n[Image]")

    def test_single_picture_is_not_labeled(self, streamed):
        data = _xlsx_with_drawing(_picture_at(1, 2, "rId1"), {"image1.png": builder.png_bytes(seed=3)})

        result = convert(data, "a.xlsx")

        assert result.text.endswith("| 가 | 1 |\n\n[Image]")

    def test_picture_repeated_on_every_row_is_not_labeled(self, streamed):
        """The same picture (an icon on every row) is removed as a repeated picture. No label is attached, so that a bare cell
        address is not left behind."""
        anchors = "".join(_picture_at(row, 2, "rId1") for row in range(4))
        data = _xlsx_with_drawing(anchors, {"image1.png": builder.png_bytes(seed=3)})

        result = convert(data, "a.xlsx")

        assert "cell C" not in result.text

    @pytest.mark.timing
    def test_many_anchors_take_linear_time(self, monkeypatch):
        """For every picture the whole list of placed pictures was counted again (`list.count`), taking the square of the anchor
        count (40s for 200,000 anchors). Only the streaming path is checked — a full load is mostly openpyxl's time finding the
        pictures. 12 times the anchors must take a little over 12 times as long (over 30 times if quadratic)."""
        from opendockit.extractors import spreadsheet

        monkeypatch.setattr(spreadsheet, "_MAX_MERGED_CELLS", -1)

        def seconds(anchors: int) -> float:
            data = _xlsx_with_drawing("".join(_picture_at(row % 2, 2, "rId1") for row in range(anchors)),
                                      {"image1.png": builder.png_bytes(seed=3)})
            started = perf_counter()
            convert(data, "a.xlsx")
            return perf_counter() - started

        small, large = seconds(5_000), seconds(60_000)

        assert large < small * 20


class TestTimeFormats:
    @pytest.mark.parametrize(("value", "number_format", "expected"), [
        (datetime(1900, 1, 1, 18, 3, 40), "h:mm", "18:03"),
        (time(9, 5, 59), "hh:mm", "09:05"),
        (time(18, 3, 40), "h:mm AM/PM", "6:03 PM"),
        (time(0, 30), "h:mm AM/PM", "12:30 AM"),
        (time(14, 30, 45), "[$-412]AM/PM h:mm", "오후 2:30"),
        (time(9, 30), "[$-ko-KR]AM/PM h:mm:ss", "오전 9:30:00"),
        (time(14, 30, 45), 'h"시" mm"분"', "14시 30분"),
        (time(14, 30, 45), "h:mm:ss", "14:30:45"),
        (time(0, 1, 43, 680000), "mm:ss", "00:01:44"),
    ])
    def test_time_of_day_follows_the_format(self, value, number_format, expected):
        result = format_cell(value, number_format)

        assert result == expected

    @pytest.mark.parametrize(("value", "number_format", "expected"), [
        (datetime(2024, 3, 5, 14, 30, 45), "yyyy-mm-dd hh:mm", "2024-03-05 14:30"),
        (datetime(2024, 3, 5, 14, 30, 45), "yyyy-mm-dd hh:mm:ss", "2024-03-05 14:30:45"),
        (datetime(2024, 1, 1), "yyyy-mm-dd hh:mm", "2024-01-01 00:00"),
    ])
    def test_date_and_time_show_seconds_only_when_the_format_does(self, value, number_format, expected):
        result = format_cell(value, number_format)

        assert result == expected

    @pytest.mark.parametrize(("value", "number_format", "expected"), [
        (timedelta(hours=36), '[h]"시간" mm"분"', "36시간 00분"),
        (timedelta(hours=26), "[h]:mm", "26:00"),
        (timedelta(minutes=90, seconds=3), "[mm]:ss", "90:03"),
    ])
    def test_elapsed_time_keeps_its_unit_letters(self, value, number_format, expected):
        result = format_cell(value, number_format)

        assert result == expected

    def test_xlsx_cells(self):
        workbook = openpyxl.Workbook()
        sheet = workbook.active
        sheet.append(["항목", "시각"])
        sheet.append(["출근", time(9, 5, 12)])
        sheet["B2"].number_format = "[$-412]AM/PM h:mm"
        buffer = BytesIO()
        workbook.save(buffer)

        result = convert(buffer.getvalue(), "a.xlsx")

        assert "| 출근 | 오전 9:05 |" in result.text
