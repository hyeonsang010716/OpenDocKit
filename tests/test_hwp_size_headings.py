"""Headings of Hangul documents that do not use the 개요 (outline) styles: paragraphs with an outline head (개요 번호) on another
style, and titles set by character size alone (`_hangul.SizeHeadings`).

In 26 real HWP and 8 real HWPX files every paragraph was in the 바탕글 style; chapter titles were 13 pt bold over a 10 pt body
(or 13 pt over 9 pt), and the outline paragraphs of a Hangul sample were 바탕글 paragraphs with an outline head. No heading came
out of any of them.
"""

import struct

from opendockit import convert
from opendockit.extractors.hwp import (
    TAG_CHAR_SHAPE,
    TAG_CTRL_HEADER,
    TAG_LIST_HEADER,
    TAG_PARA_CHAR_SHAPE,
    TAG_PARA_HEADER,
    TAG_PARA_SHAPE,
    TAG_PARA_TEXT,
    TAG_TABLE,
)
from tests import builder
from tests.test_hwp import _fake_hwp, _para_header, _para_shape


def _char_shape(size: int, bold: bool = False) -> bytes:
    """CHAR_SHAPE: font IDs (2×7) + ratio, spacing, relative size, offset (1×7 each) + base size (INT32, 1/100 pt) + attribute
    (bit 1 bold) + the rest of the record."""
    return b"\x00" * 42 + struct.pack("<iI", size, 2 if bold else 0) + b"\x00" * 26


# Character shapes 0: 10 pt (the body), 1: 13 pt bold, 2: 16 pt, 3: 11 pt, 4: 9 pt
_SIZES = b"".join(builder.hwp_record(TAG_CHAR_SHAPE, 0, _char_shape(size, bold))
                  for size, bold in ((1000, False), (1300, True), (1600, False), (1100, False), (900, False)))
_BODY = "본문은 열 포인트 글자로 길게 씁니다. " * 6
# A tab in HWP text is an inline control of 8 WCHARs (code 9, leader and width, code 9)
_TAB = (9, "\x00" * 4)


def _paragraph(text: str | tuple, *runs: tuple[int, int], level: int = 0, shape: int = 0) -> bytes:
    """A paragraph at `level` with its character shape runs ((position, character shape ID); one run of shape 0 by default)."""
    record = builder.hwp_record
    runs = runs or ((0, 0),)
    return (record(TAG_PARA_HEADER, level, _para_header(shape))
            + record(TAG_PARA_TEXT, level + 1, builder.hwp_text(*(text if isinstance(text, tuple) else (text,))))
            + record(TAG_PARA_CHAR_SHAPE, level + 1, b"".join(struct.pack("<II", *run) for run in runs)))


def _hwp(*paragraphs: bytes, docinfo: bytes = b"") -> bytes:
    return _fake_hwp(None, sections=[b"".join(paragraphs)], docinfo_extra=_SIZES + docinfo)


_HWPX_HEAD = (
    '<hh:charProperties itemCnt="5">'
    '<hh:charPr id="0" height="1000"/><hh:charPr id="1" height="1300"><hh:bold/></hh:charPr>'
    '<hh:charPr id="2" height="1600"/><hh:charPr id="3" height="1100"/><hh:charPr id="4" height="900"/>'
    "</hh:charProperties>"
)


def _hwpx_paragraph(text: str, *runs: tuple[int, str], shape: int = 0) -> str:
    """A paragraph whose runs are (character shape id, text); one run of shape 0 by default."""
    runs = runs or ((0, text),)
    return (f'<hp:p paraPrIDRef="{shape}" styleIDRef="0">'
            + "".join(f'<hp:run charPrIDRef="{shape_id}"><hp:t>{part}</hp:t></hp:run>' for shape_id, part in runs) + "</hp:p>")


def _hwpx(*paragraphs: str, head: str = "") -> bytes:
    return builder.zip_with(builder.hwpx_bytes("".join(paragraphs)), edit={
        "Contents/header.xml": lambda xml: xml.replace("</hh:refList>", _HWPX_HEAD + head + "</hh:refList>")})


class TestSizeHeadings:
    def test_hwp_title_in_a_larger_size_is_a_heading(self):
        data = _hwp(_paragraph("1. 사업 개요", (0, 1)), _paragraph(_BODY), _paragraph("추진 배경", (0, 2)), _paragraph(_BODY))

        text = convert(data, "a.hwp").text

        assert text.splitlines()[0] == "## 1. 사업 개요"
        assert "# 추진 배경" in text.splitlines()

    def test_hwpx_title_in_a_larger_size_is_a_heading(self):
        data = _hwpx(_hwpx_paragraph("1. 사업 개요", (1, "1. 사업 개요")), _hwpx_paragraph(_BODY),
                     _hwpx_paragraph("추진 배경", (2, "추진 배경")), _hwpx_paragraph(_BODY))

        text = convert(data, "a.hwpx").text

        assert text.splitlines()[0] == "## 1. 사업 개요"
        assert "# 추진 배경" in text.splitlines()

    def test_level_follows_the_ratio_to_the_body_size(self):
        """1.5 times the body or more is level 1, 1.3 level 2, 1.15 level 3; 11 pt over a 10 pt body is no heading."""
        data = _hwp(_paragraph("큰 제목", (0, 2)), _paragraph("중간 제목", (0, 1)), _paragraph("작은 제목", (0, 3)),
                    _paragraph(_BODY))

        lines = convert(data, "a.hwp").text.splitlines()

        assert lines[:5] == ["# 큰 제목", "", "## 중간 제목", "", "작은 제목"]

    def test_body_size_is_the_size_of_most_characters(self):
        """A document written in 13 pt with a few 10 pt lines has no headings in 13 pt."""
        body = _paragraph(_BODY, (0, 1))
        data = _hwp(_paragraph("1. 사업 개요", (0, 1)), body, body, _paragraph("주석", (0, 0)))

        assert not any(line.startswith("#") for line in convert(data, "a.hwp").text.splitlines())

    def test_paragraph_with_a_smaller_part_is_no_heading(self):
        """'3. 시험기간 : 시험 시작일로부터 X일' with only the label in 13 pt is a body line."""
        data = _hwp(_paragraph("3. 시험기간 : 시작일로부터", (0, 1), (7, 0)), _paragraph(_BODY))

        assert convert(data, "a.hwp").text.startswith("3. **시험기간** : 시작일로부터")   # the 13 pt label is bold

    def test_lines_that_only_look_large_are_no_headings(self):
        """A cover-page date, a table-of-contents line, the addressee line of a form and a sentence written large stay text."""
        large = ["2025.12.12", "202 .  .", ("1. 단위테스트", _TAB, "1"), ("2. 결함관리 내역 ", _TAB, " n+1"),
                 ("3. 운영", _TAB, "2-3"), "국민대학교 사업단장 귀하", "한국정보원 귀중", "목차는 다음 순서로 진행됩니다.",
                 "왜 필요한가?", "자세히 적어 주세요"]
        data = _hwp(*(_paragraph(line, (0, 2)) for line in large), _paragraph(_BODY))

        assert not any(line.startswith("#") for line in convert(data, "a.hwp").text.splitlines())

    def test_heading_with_a_tab_before_its_words_stays_a_heading(self):
        data = _hwp(_paragraph(("1.", _TAB, "사업 개요"), (0, 2)), _paragraph(_BODY))

        assert convert(data, "a.hwp").text.startswith("# 1. 사업 개요")

    def test_cell_text_in_a_larger_size_is_no_heading(self):
        record = builder.hwp_record
        cell = (record(TAG_LIST_HEADER, 2, builder.hwp_cell(0, 0)) + record(TAG_PARA_HEADER, 2, _para_header(0))
                + record(TAG_PARA_TEXT, 3, builder.hwp_text("표 제목")) + record(TAG_PARA_CHAR_SHAPE, 3, struct.pack("<II", 0, 2)))
        table = (record(TAG_PARA_HEADER, 0, _para_header(0)) + record(TAG_PARA_TEXT, 1, builder.hwp_text((11, "tbl ")))
                 + record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("tbl ")) + record(TAG_TABLE, 2, struct.pack("<IHH", 0, 1, 1) + b"\x00" * 10)
                 + cell)
        data = _hwp(table, _paragraph(_BODY))

        assert "#" not in convert(data, "a.hwp").text

    def test_document_with_outline_headings_keeps_those_only(self):
        """Where the author marked the structure with outline styles, a large line is not taken for a heading too."""
        data = _hwp(_paragraph("표지 제목", (0, 2)), _paragraph(_BODY),
                    builder.hwp_record(TAG_PARA_HEADER, 0, _para_header(0, 2)) + builder.hwp_record(
                        TAG_PARA_TEXT, 1, builder.hwp_text("추진 계획")))

        lines = convert(data, "a.hwp").text.splitlines()

        assert lines[0] == "표지 제목"
        assert "# 추진 계획" in lines

    def test_document_without_character_sizes_has_no_size_headings(self):
        data = _fake_hwp(None, sections=[_paragraph("제목") + _paragraph(_BODY)])

        assert convert(data, "a.hwp").text.startswith("제목")

    def test_runs_out_of_order_give_no_size(self):
        """Hangul writes the runs of a paragraph in order of position; runs out of order are not read for a size."""
        data = _hwp(_paragraph("사업 개요", (0, 2), (0, 2)), _paragraph(_BODY))

        assert convert(data, "a.hwp").text.startswith("사업 개요")


class TestOutlineHeads:
    """A paragraph with an outline head (개요 번호) is a heading whatever its style. A real Hangul file (examples/samples/
    numbered-outline.hwpx) puts outline heads on 바탕글 paragraphs; they came out as a numbered list."""

    def test_hwpx_outline_head_on_a_normal_paragraph_is_a_heading(self):
        head = ('<hh:numberings itemCnt="1"><hh:numbering id="1" start="1">'
                '<hh:paraHead level="1" numFormat="DIGIT">^1.</hh:paraHead>'
                '<hh:paraHead level="2" numFormat="HANGUL_SYLLABLE">^2.</hh:paraHead></hh:numbering></hh:numberings>'
                '<hh:paraProperties itemCnt="3"><hh:paraPr id="0"><hh:heading type="NONE" idRef="0" level="0"/></hh:paraPr>'
                '<hh:paraPr id="1"><hh:heading type="OUTLINE" idRef="0" level="0"/></hh:paraPr>'
                '<hh:paraPr id="2"><hh:heading type="OUTLINE" idRef="0" level="1"/></hh:paraPr></hh:paraProperties>')
        body = ('<hp:p paraPrIDRef="1" styleIDRef="0"><hp:run><hp:secPr outlineShapeIDRef="1"/><hp:t>추진 배경</hp:t></hp:run></hp:p>'
                + _hwpx_paragraph("현황", shape=2) + _hwpx_paragraph("본문입니다"))

        text = convert(_hwpx(body, head=head), "a.hwpx").text

        assert text == "# 1. 추진 배경\n\n## 가. 현황\n\n본문입니다"

    def test_hwp_outline_head_on_a_normal_paragraph_is_a_heading(self):
        from tests.test_hwp import _numbering, _section_definition
        record = builder.hwp_record
        docinfo = (record(TAG_PARA_SHAPE, 0, _para_shape()) + record(TAG_PARA_SHAPE, 0, _para_shape(1, 0))
                   + record(TAG_PARA_SHAPE, 0, _para_shape(1, 1)) + builder.hwp_record(7 + 0x10, 0, _numbering(("^1.", 0), ("^2.", 8))))
        section = (record(TAG_PARA_HEADER, 0, _para_header(1)) + record(TAG_PARA_TEXT, 1, builder.hwp_text((2, "secd"), "추진 배경"))
                   + record(TAG_CTRL_HEADER, 1, _section_definition(1))
                   + record(TAG_PARA_HEADER, 0, _para_header(2)) + record(TAG_PARA_TEXT, 1, builder.hwp_text("현황"))
                   + record(TAG_PARA_HEADER, 0, _para_header(0)) + record(TAG_PARA_TEXT, 1, builder.hwp_text("본문입니다")))

        text = convert(_fake_hwp(None, sections=[section], docinfo_extra=docinfo), "a.hwp").text

        assert text == "# 1. 추진 배경\n\n## 가. 현황\n\n본문입니다"

    def test_no_heading_in_a_note_or_text_box(self):
        """A note paragraph with an outline head or style gave `[^1]: # 1. 항목`, and a text box a heading inside the body."""
        head = ('<hh:numberings itemCnt="1"><hh:numbering id="1" start="1">'
                '<hh:paraHead level="1" numFormat="DIGIT">^1.</hh:paraHead></hh:numbering></hh:numberings>'
                '<hh:paraProperties itemCnt="2"><hh:paraPr id="0"><hh:heading type="NONE" idRef="0" level="0"/></hh:paraPr>'
                '<hh:paraPr id="1"><hh:heading type="OUTLINE" idRef="0" level="0"/></hh:paraPr></hh:paraProperties>')
        note = ("<hp:ctrl><hp:footNote><hp:subList>" + _hwpx_paragraph("메모 항목", shape=1)
                + '<hp:p paraPrIDRef="0" styleIDRef="2"><hp:run><hp:t>스타일 항목</hp:t></hp:run></hp:p>'
                + "</hp:subList></hp:footNote></hp:ctrl>")
        box = ('<hp:rect><hp:drawText><hp:subList>' + _hwpx_paragraph("상자 항목", shape=1)
               + "</hp:subList></hp:drawText></hp:rect>")
        body = f'<hp:p paraPrIDRef="0" styleIDRef="0"><hp:run><hp:t>본문</hp:t>{note}{box}</hp:run></hp:p>'

        text = convert(_hwpx(body, head=head), "a.hwpx").text

        assert "#" not in text
        assert "[^1]: 1. 메모 항목" in text and "1. 상자 항목" in text

    def test_long_outline_paragraph_stays_a_numbered_line(self):
        head = ('<hh:numberings itemCnt="1"><hh:numbering id="1" start="1">'
                '<hh:paraHead level="1" numFormat="DIGIT">^1.</hh:paraHead></hh:numbering></hh:numberings>'
                '<hh:paraProperties itemCnt="2"><hh:paraPr id="0"><hh:heading type="NONE" idRef="0" level="0"/></hh:paraPr>'
                '<hh:paraPr id="1"><hh:heading type="OUTLINE" idRef="0" level="0"/></hh:paraPr></hh:paraProperties>')
        long_text = "가" * 130

        text = convert(_hwpx(_hwpx_paragraph(long_text, shape=1), head=head), "a.hwpx").text

        assert text == f"1. {long_text}"

    def test_numbered_clause_sentences_stay_numbered_paragraphs(self):
        """Contracts and regulations number their clauses with outline heads. A clause that is a sentence stays a numbered
        paragraph, and does not keep the title set by size from being a heading (it is no outline heading of the document)."""
        head = ('<hh:numberings itemCnt="1"><hh:numbering id="1" start="1">'
                '<hh:paraHead level="1" numFormat="DIGIT">^1.</hh:paraHead>'
                '<hh:paraHead level="2" numFormat="HANGUL_SYLLABLE">^2.</hh:paraHead></hh:numbering></hh:numberings>'
                '<hh:paraProperties itemCnt="3"><hh:paraPr id="0"><hh:heading type="NONE" idRef="0" level="0"/></hh:paraPr>'
                '<hh:paraPr id="1"><hh:heading type="OUTLINE" idRef="0" level="0"/></hh:paraPr>'
                '<hh:paraPr id="2"><hh:heading type="OUTLINE" idRef="0" level="1"/></hh:paraPr></hh:paraProperties>')
        body = ('<hp:p paraPrIDRef="0" styleIDRef="0"><hp:run charPrIDRef="2"><hp:secPr outlineShapeIDRef="1"/>'
                '<hp:t>용역 계약서</hp:t></hp:run></hp:p>'
                + _hwpx_paragraph("갑은 을에게 계약금을 계약 체결일로부터 7일 이내에 지급한다.", shape=1)
                + _hwpx_paragraph("다만, 천재지변의 경우에는 그러하지 아니하다.", shape=2) + _hwpx_paragraph(_BODY))

        lines = convert(_hwpx(body, head=head), "a.hwpx").text.split("\n\n")

        assert lines[:2] == ["# 용역 계약서", "1. 갑은 을에게 계약금을 계약 체결일로부터 7일 이내에 지급한다.\n"
                                              "  - 가. 다만, 천재지변의 경우에는 그러하지 아니하다."]
