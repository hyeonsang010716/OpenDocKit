"""Bold and italic in Hangul documents (HWP character shape attribute bits 1 and 0, HWPX hh:bold / hh:italic), written as
`**bold**` / `*italic*` under `keep_emphasis` by the shared rules (`_inline.resolve_inline`), as for DOCX. They were dropped: a
form's '3. **시험기간** : 시험 시작일로부터 X일' read as plain text, while the same document saved as DOCX kept them."""

import struct

from opendockit import ConvertOptions, convert
from opendockit.extractors.hwp import TAG_CHAR_SHAPE
from tests import builder
from tests.test_hwp import _fake_hwp
from tests.test_hwp_size_headings import _BODY, _char_shape, _paragraph


_HEAD = ('<hh:charProperties itemCnt="3"><hh:charPr id="0" height="1000"/>'
         '<hh:charPr id="1" height="1000"><hh:bold/></hh:charPr><hh:charPr id="2" height="1000"><hh:italic/></hh:charPr>'
         "</hh:charProperties>")


def _hwpx(body: str, head: str = _HEAD) -> bytes:
    return builder.zip_with(builder.hwpx_bytes(body), edit={
        "Contents/header.xml": lambda xml: xml.replace("</hh:refList>", head + "</hh:refList>")})


def _runs(*runs: tuple[int, str]) -> str:
    return "<hp:p>" + "".join(f'<hp:run charPrIDRef="{shape}"><hp:t>{text}</hp:t></hp:run>' for shape, text in runs) + "</hp:p>"


class TestHwpxEmphasis:
    def test_bold_and_italic_runs_are_marked(self):
        text = convert(_hwpx(_runs((1, "시험기간"), (0, " : 시작일로부터 "), (1, "X"), (0, "일, "), (2, "참고"), (0, " 사항"))),
                       "a.hwpx").text

        # One bold character glued to its word stays plain (the shared rule, `_inline._drop_emphasis_inside_words`)
        assert text == "**시험기간** : 시작일로부터 X일, *참고* 사항"

    def test_paragraph_bold_throughout_is_not_marked(self):
        assert convert(_hwpx(_runs((1, "전체가 "), (1, "굵은 문단"))), "a.hwpx").text == "전체가 굵은 문단"

    def test_without_keep_emphasis_the_text_is_plain(self):
        data = _hwpx(_runs((1, "굵게"), (0, " 보통")))

        assert convert(data, "a.hwpx", options=ConvertOptions(keep_emphasis=False)).text == "굵게 보통"

    def test_emphasis_and_link_in_one_paragraph(self):
        body = ('<hp:p><hp:run charPrIDRef="1"><hp:t>필수</hp:t></hp:run><hp:run charPrIDRef="0"><hp:t> 항목은 </hp:t>'
                '<hp:ctrl><hp:fieldBegin id="7" type="HYPERLINK"><hp:parameters><hp:stringParam name="Command">'
                "https\\://a.example/;1;0;0;</hp:stringParam></hp:parameters></hp:fieldBegin></hp:ctrl><hp:t>안내</hp:t>"
                '<hp:ctrl><hp:fieldEnd beginIDRef="7"/></hp:ctrl><hp:t>를 본다</hp:t></hp:run></hp:p>')

        assert convert(_hwpx(body), "a.hwpx").text == "**필수** 항목은 [안내](https://a.example/)를 본다"

    def test_table_cell_keeps_emphasis_inside_its_text(self):
        cell = ('<hp:tc><hp:subList>' + _runs((0, "성적서("), (1, "V"), (0, ") 발급")) + "</hp:subList>"
                '<hp:cellAddr colAddr="0" rowAddr="0"/><hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>')
        cell2 = cell.replace('colAddr="0"', 'colAddr="1"')
        body = f'<hp:p><hp:run><hp:tbl rowCnt="1" colCnt="2"><hp:tr>{cell}{cell2}</hp:tr></hp:tbl></hp:run></hp:p>'

        assert "| 성적서(**V**) 발급 |" in convert(_hwpx(body), "a.hwpx").text


class TestHwpEmphasis:
    def test_bold_run_is_marked(self):
        data = _fake_hwp(None, sections=[_paragraph("시험기간 : 시작일로부터", (0, 1), (4, 0))], docinfo_extra=b"".join(
            builder.hwp_record(TAG_CHAR_SHAPE, 0, _char_shape(1000, bold)) for bold in (False, True)))

        assert convert(data, "a.hwp").text == "**시험기간** : 시작일로부터"

    def test_italic_run_is_marked(self):
        italic = b"\x00" * 42 + struct.pack("<iI", 1000, 1) + b"\x00" * 26
        data = _fake_hwp(None, sections=[_paragraph("참고 사항", (0, 1), (2, 0))], docinfo_extra=(
            builder.hwp_record(TAG_CHAR_SHAPE, 0, _char_shape(1000)) + builder.hwp_record(TAG_CHAR_SHAPE, 0, italic)))

        assert convert(data, "a.hwp").text == "*참고* 사항"

    def test_heading_is_written_without_emphasis(self):
        """A size heading that is bold in part (`**1.** 사업 개요`) is written as a plain heading."""
        docinfo = b"".join(builder.hwp_record(TAG_CHAR_SHAPE, 0, _char_shape(size, bold))
                           for size, bold in ((1000, False), (1600, True), (1600, False)))
        data = _fake_hwp(None, sections=[_paragraph("1. 사업 개요", (0, 1), (2, 2)), _paragraph(_BODY)], docinfo_extra=docinfo)

        assert convert(data, "a.hwp").text.startswith("# 1. 사업 개요\n")

    def test_spaced_bold_title_is_joined_in_its_heading_too(self):
        """A bold title written with spaced syllables (`보  도  자  료`) and a bold word in it is a heading without emphasis, joined."""
        docinfo = b"".join(builder.hwp_record(TAG_CHAR_SHAPE, 0, _char_shape(size, bold))
                           for size, bold in ((1000, False), (2000, True), (2000, False)))
        data = _fake_hwp(None, sections=[_paragraph("보  도  자  료", (0, 1), (3, 2)), _paragraph(_BODY)], docinfo_extra=docinfo)

        assert convert(data, "a.hwp").text.startswith("# 보도자료\n")
