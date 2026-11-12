"""In HWP/HWPX a numbered paragraph holding only a table or a text box and no text also counts its number (the same as a paragraph
holding only a picture —
`test_hwp_heads_and_shapes.py::TestHeadOfAParagraphThatStartsWithAPicture::test_hwpx_paragraph_of_only_a_picture_keeps_its_number_but_gets_no_heading_mark`).

It was not counted, so '1. 추진 배경 / 2. [표] / 3. 추진 일정' ([표] = a table) on the Hangul (HWP) screen became
'1. 추진 배경 / [표] / 2. 추진 일정', with the later number moved up.
"""


from opendockit import convert
from tests import builder
from tests.test_hwp import (
    TAG_CTRL_HEADER,
    TAG_LIST_HEADER,
    TAG_PARA_HEADER,
    TAG_PARA_TEXT,
    _fake_hwp,
    _para_header,
)
from tests.test_hwp_heads_and_shapes import (
    _HWP_HEADS,
    _HWPX_HEADS,
    _hwp_paragraph,
    _hwp_table_control,
    _hwpx_paragraph,
)


_TABLE = "1. 추진 배경\n\n2.\n\n| 목표 | 20% |\n| --- | --- |\n\n3. 추진 일정"
_BOX = "1. 추진 배경\n\n2.\n\n요약 상자\n\n3. 추진 일정"


def _hwpx(inner: str) -> bytes:
    body = _hwpx_paragraph("추진 배경", 1) + f'<hp:p paraPrIDRef="1"><hp:run>{inner}</hp:run></hp:p>' + _hwpx_paragraph("추진 일정", 1)
    return builder.hwpx_with_picture(body, header_extra=_HWPX_HEADS)


class TestHwpxParagraphOfOnlyAnObject:
    def test_paragraph_of_only_a_table_keeps_its_number(self):
        table = ('<hp:tbl rowCnt="1" colCnt="2"><hp:tr>' + builder._hwpx_cell(0, 0, "목표") + builder._hwpx_cell(0, 1, "20%")
                 + "</hp:tr></hp:tbl>")

        result = convert(_hwpx(table), "a.hwpx")

        assert result.text == _TABLE

    def test_paragraph_of_only_a_text_box_keeps_its_number(self):
        box = "<hp:rect><hp:drawText><hp:subList>" + _hwpx_paragraph("요약 상자") + "</hp:subList></hp:drawText></hp:rect>"

        result = convert(_hwpx(box), "a.hwpx")

        assert result.text == _BOX

    def test_paragraph_without_a_head_of_only_a_table_gets_no_mark(self):
        table = ('<hp:tbl rowCnt="1" colCnt="2"><hp:tr>' + builder._hwpx_cell(0, 0, "목표") + builder._hwpx_cell(0, 1, "20%")
                 + "</hp:tr></hp:tbl>")
        body = _hwpx_paragraph("앞 문단") + f'<hp:p paraPrIDRef="0"><hp:run>{table}</hp:run></hp:p>' + _hwpx_paragraph("추진 배경", 1)

        result = convert(builder.hwpx_with_picture(body, header_extra=_HWPX_HEADS), "a.hwpx")

        assert result.text == "앞 문단\n\n| 목표 | 20% |\n| --- | --- |\n\n1. 추진 배경"


class TestHwpParagraphOfOnlyAnObject:
    def test_paragraph_of_only_a_table_keeps_its_number(self, monkeypatch):
        record = builder.hwp_record
        section = (_hwp_paragraph("추진 배경", 1)
                   + record(TAG_PARA_HEADER, 0, _para_header(1)) + record(TAG_PARA_TEXT, 1, builder.hwp_text((11, "tbl ")))
                   + _hwp_table_control(1, "목표", "20%")
                   + _hwp_paragraph("추진 일정", 1))
        data = _fake_hwp(monkeypatch, sections=[section], docinfo_extra=_HWP_HEADS)

        result = convert(data, "a.hwp")

        assert result.text == _TABLE

    def test_paragraph_of_only_a_text_box_keeps_its_number(self, monkeypatch):
        record = builder.hwp_record
        section = (_hwp_paragraph("추진 배경", 1)
                   + record(TAG_PARA_HEADER, 0, _para_header(1)) + record(TAG_PARA_TEXT, 1, builder.hwp_text((11, "gso ")))
                   + record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("gso ")) + record(TAG_LIST_HEADER, 2, b"\x00" * 8)
                   + record(TAG_PARA_HEADER, 2, builder.hwp_para_header()) + record(TAG_PARA_TEXT, 3, builder.hwp_text("요약 상자"))
                   + _hwp_paragraph("추진 일정", 1))
        data = _fake_hwp(monkeypatch, sections=[section], docinfo_extra=_HWP_HEADS)

        result = convert(data, "a.hwp")

        assert result.text == _BOX
