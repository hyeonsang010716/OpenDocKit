"""Headings of Hangul documents that do not use the 개요 (outline) styles: paragraphs with an outline head (개요 번호) on another
style, and titles set by character size alone (`_hangul.SizeHeadings`).

In 26 real HWP and 8 real HWPX files every paragraph was in the 바탕글 style; chapter titles were 13 pt bold over a 10 pt body
(or 13 pt over 9 pt), and the outline paragraphs of a Hangul sample were 바탕글 paragraphs with an outline head. No heading came
out of any of them.
"""

import struct

from opendockit import convert
from opendockit.extractors._hangul import _size_heading_text
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

    def test_dates_signatures_quotes_and_contacts_are_no_headings(self):
        """Lines of forms and letters printed large: a date (filled in or blank), a signature or stamp line, a quoted sentence or
        saying, a polite `…요` sentence, a note or contact line, an address; items of a typed bullet list (`ㅇ`, `□`, `-`); and
        the caption of a figure or table."""
        large = ["2024년 12월 31일", "2024년    월    일", "3월 5일(화)", "신청인 :          (서명 또는 인)", "대표이사  홍 길 동  (인)",
                 "“오늘은 좋은 하루였다.”", "(이하 생략한다)", '"나는 할 수 있다"', "함께 해요", "※ 문의: 02-123-4567",
                 "담당자 연락처 02-123-4567", "www.example.go.kr", "help@example.go.kr", "ㅇ 예산 확보 필요",
                 "▶ 향후 일정", "- 세부 내용", "〈그림 1〉 추진 체계도", "[표 2] 예산 현황", "그림 3. 조직도", "〈표 Ⅲ-1〉 연도별 예산", "[표-1] 예산",
                 "〔그림 1〕 체계", "1588-1234 안내"]
        data = _hwp(*(_paragraph(line, (0, 2)) for line in large), *[_paragraph(_BODY)] * 3)
        hwpx = _hwpx(*(_hwpx_paragraph(line, (2, line)) for line in large), *[_hwpx_paragraph(_BODY)] * 3)

        assert not any(line.startswith("#") for line in convert(data, "a.hwp").text.splitlines())
        assert not any(line.startswith("#") for line in convert(hwpx, "a.hwpx").text.splitlines())
        assert not _size_heading_text("<표 Ⅲ-1> 연도별 예산")   # (an ASCII `<` cannot be written into the XML built here)

    def test_titles_that_end_like_a_date_or_a_polite_word_stay_headings(self):
        titles = ["1. 사업 개요", "추진 필요", "주요 내용", "2024년 사업 계획", "Ⅱ. 추진 경과(안)", "「개인정보 보호법」 개정 사항",
                  "표준 계약서 작성 방법", "그림책 지원 사업", "□ 추진 배경", "Ⅰ. 총칙(개정 2024. 1. 1.)", "제3장 2023-2024-2025 계획",
                  "서울.kr 소개", "2024년", "사업 대요", "『소년이 온다』"]
        data = _hwp(*(_paragraph(title, (0, 2)) for title in titles), *[_paragraph(_BODY)] * 4)

        assert [line for line in convert(data, "a.hwp").text.splitlines() if line.startswith("#")] == [f"# {t}" for t in titles]

    def test_heading_with_a_tab_before_its_words_stays_a_heading(self):
        data = _hwp(_paragraph(("1.", _TAB, "사업 개요"), (0, 2)), _paragraph(_BODY))

        assert convert(data, "a.hwp").text.startswith("# 1. 사업 개요")

    def test_cell_text_in_a_larger_size_is_no_heading(self):
        """In a table of two columns and two rows: a table of one column is a layout box (`TestTitlesInBoxes`), and one of
        one row a line of text (`test_hwp_one_row_tables.py`)."""
        record = builder.hwp_record
        cells = b"".join(record(TAG_LIST_HEADER, 2, builder.hwp_cell(index // 2, index % 2)) + record(TAG_PARA_HEADER, 2, _para_header(0))
                         + record(TAG_PARA_TEXT, 3, builder.hwp_text(text)) + record(TAG_PARA_CHAR_SHAPE, 3, struct.pack("<II", 0, 2))
                         for index, text in enumerate(("표 제목", "단위", "매출 합계", "천원")))
        table = (record(TAG_PARA_HEADER, 0, _para_header(0)) + record(TAG_PARA_TEXT, 1, builder.hwp_text((11, "tbl ")))
                 + record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("tbl ")) + record(TAG_TABLE, 2, struct.pack("<IHH", 0, 2, 2) + b"\x00" * 10)
                 + cells)
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

    def test_body_size_is_counted_on_body_paragraphs_only(self):
        """A report written in 11 pt with a long table in 9 pt: the table's text set the body size to 9 pt, and every 11 pt body
        line became a heading (`### 담당 부서 : 기획예산과`)."""
        def cell(row: int, col: int, text: str) -> str:
            return (f"<hp:tc><hp:subList>{_hwpx_paragraph(text, (4, text))}</hp:subList>"
                    f'<hp:cellAddr colAddr="{col}" rowAddr="{row}"/><hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>')
        rows = "".join(f"<hp:tr>{cell(row, 0, f'항목 {row}')}{cell(row, 1, _BODY)}</hp:tr>" for row in range(12))
        table = f'<hp:p paraPrIDRef="0" styleIDRef="0"><hp:run><hp:tbl rowCnt="12" colCnt="2">{rows}</hp:tbl></hp:run></hp:p>'
        body = [_hwpx_paragraph(line, (3, line)) for line in ("담당 부서 : 기획예산과", "총사업비 120억원", "붙임 자료 참조")]

        lines = convert(_hwpx(_hwpx_paragraph("사업 개요", (2, "사업 개요")), *body, table), "a.hwpx").text.split("\n\n")

        assert lines[:4] == ["## 사업 개요", "담당 부서 : 기획예산과", "총사업비 120억원", "붙임 자료 참조"]

    def test_form_whose_body_is_in_tables_keeps_its_title(self):
        """A form has its title on top and its body in a table: counted on top-level paragraphs alone, the body size was the
        title's own, and the title was never larger than it."""
        def cell(row: int, col: int, text: str) -> str:
            return (f"<hp:tc><hp:subList>{_hwpx_paragraph(text, (0, text))}</hp:subList>"
                    f'<hp:cellAddr colAddr="{col}" rowAddr="{row}"/><hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>')
        rows = "".join(f"<hp:tr>{cell(row, 0, f'항목 {row}')}{cell(row, 1, _BODY)}</hp:tr>" for row in range(6))
        table = f'<hp:p paraPrIDRef="0" styleIDRef="0"><hp:run><hp:tbl rowCnt="6" colCnt="2">{rows}</hp:tbl></hp:run></hp:p>'

        text = convert(_hwpx(_hwpx_paragraph("개인정보 수집 동의서", (2, "개인정보 수집 동의서")), table), "a.hwpx").text

        assert text.startswith("# 개인정보 수집 동의서")

    def test_short_body_lines_over_a_table_stay_body_text(self):
        """A memo's routing lines in 11 pt beside its 16 pt title, over a table in 9 pt: the top level is not its title alone,
        so its own sizes set the body, and the routing lines are body text, not headings measured against the table."""
        def cell(row: int, col: int, text: str) -> str:
            return (f"<hp:tc><hp:subList>{_hwpx_paragraph(text, (4, text))}</hp:subList>"
                    f'<hp:cellAddr colAddr="{col}" rowAddr="{row}"/><hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>')
        rows = "".join(f"<hp:tr>{cell(row, 0, f'항목 {row}')}{cell(row, 1, _BODY)}</hp:tr>" for row in range(6))
        table = f'<hp:p paraPrIDRef="0" styleIDRef="0"><hp:run><hp:tbl rowCnt="6" colCnt="2">{rows}</hp:tbl></hp:run></hp:p>'
        lines = [_hwpx_paragraph(line, (3, line)) for line in ("수신 각 부서", "참조 기획팀")]

        text = convert(_hwpx(_hwpx_paragraph("회의 결과 보고", (2, "회의 결과 보고")), *lines, table), "a.hwpx").text

        assert text.split("\n\n")[:3] == ["## 회의 결과 보고", "수신 각 부서", "참조 기획팀"]

    def test_short_lines_all_in_one_size_stay_body_text(self):
        """A memo of short lines all in one size over a smaller table, and a notice in large print with one small note: the
        largest size is taken for titles only for a title or two, and these lines are the body."""
        def cell(row: int, col: int, text: str) -> str:
            return (f"<hp:tc><hp:subList>{_hwpx_paragraph(text, (4, text))}</hp:subList>"
                    f'<hp:cellAddr colAddr="{col}" rowAddr="{row}"/><hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>')
        rows = "".join(f"<hp:tr>{cell(row, 0, f'항목 {row}')}{cell(row, 1, _BODY)}</hp:tr>" for row in range(6))
        table = f'<hp:p paraPrIDRef="0" styleIDRef="0"><hp:run><hp:tbl rowCnt="6" colCnt="2">{rows}</hp:tbl></hp:run></hp:p>'
        memo = [_hwpx_paragraph(line, (0, line)) for line in ("수신 각 부서", "참조 기획팀", "제목 회의 결과", "장소 대회의실")]
        notice = [_hwpx_paragraph(line, (1, line)) for line in ("주차장 이용 안내", "공사 기간 중 출입 금지", "지하 2층 이용 가능")]

        for document in (_hwpx(*memo, table), _hwpx(*notice, _hwpx_paragraph("※ 문의 총무과", (4, "※ 문의 총무과")))):
            assert not any(line.startswith("#") for line in convert(document, "a.hwpx").text.splitlines())

    def test_title_beside_a_shorter_line_keeps_its_heading(self):
        """A form's title with a short line in another size on top (`(앞쪽)`, `[별지 제1호서식]`) over its tables: the title's
        size was the most frequent at the top level and so the body size, and the title was lost."""
        def cell(row: int, col: int, text: str) -> str:
            return (f"<hp:tc><hp:subList>{_hwpx_paragraph(text, (0, text))}</hp:subList>"
                    f'<hp:cellAddr colAddr="{col}" rowAddr="{row}"/><hp:cellSpan colSpan="1" rowSpan="1"/></hp:tc>')
        rows = "".join(f"<hp:tr>{cell(row, 0, f'항목 {row}')}{cell(row, 1, _BODY)}</hp:tr>" for row in range(6))
        table = f'<hp:p paraPrIDRef="0" styleIDRef="0"><hp:run><hp:tbl rowCnt="6" colCnt="2">{rows}</hp:tbl></hp:run></hp:p>'
        for line in ("(앞쪽)", "[별지 제1호서식]", "처리기간 7일"):
            data = _hwpx(_hwpx_paragraph(line, (4, line)), _hwpx_paragraph("개인정보 수집 동의서", (2, "개인정보 수집 동의서")), table)

            assert "# 개인정보 수집 동의서" in convert(data, "a.hwpx").text.split("\n\n"), line

    def test_hwp_and_hwpx_count_the_same_characters(self):
        """HWP counted the 8 WCHARs of a tab and the paragraph end with the text: twenty short 13 pt lines with a tab outweighed
        a 10 pt body as HWP and not as HWPX, so the same document lost its heading as HWP."""
        body = "본문은 열 포인트 글자로 길게 씁니다. " * 7
        hwp = _hwp(_paragraph("1. 사업 개요", (0, 1)), _paragraph(body), *[_paragraph(("가", _TAB, "나다."), (0, 1))] * 20)
        hwpx = _hwpx(_hwpx_paragraph("1. 사업 개요", (1, "1. 사업 개요")), _hwpx_paragraph(body),
                     *['<hp:p paraPrIDRef="0" styleIDRef="0"><hp:run charPrIDRef="1"><hp:t>가<hp:tab/>나다.</hp:t></hp:run></hp:p>'] * 20)

        assert convert(hwp, "a.hwp").text.splitlines()[0] == convert(hwpx, "a.hwpx").text.splitlines()[0] == "## 1. 사업 개요"


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
                + _hwpx_paragraph("다만, 천재지변의 경우에는 그러하지 아니하다.", shape=2)
                + "".join(_hwpx_paragraph(clause, shape=2) for clause in self._CLAUSES) + _hwpx_paragraph(_BODY))

        lines = convert(_hwpx(body, head=head), "a.hwpx").text.split("\n\n")

        assert lines[0] == "# 용역 계약서"
        assert not any(line.startswith("#") for line in lines[1:])
        assert lines[1].startswith("1. 갑은 을에게 계약금을 계약 체결일로부터 7일 이내에 지급한다.\n"
                                   "  - 가. 다만, 천재지변의 경우에는 그러하지 아니하다.")

    # Clause endings without a full stop: the ㄴ다 of the plain style after any syllable, and the stems of `…하다`, `…같다`
    _CLAUSES = ("그 밖의 사항은 관계 법령에 따른다", "기한 내 제출하지 아니한 경우 포기한 것으로 본다", "을은 비밀 유지 의무를 진다",
                "다만, 천재지변의 경우에는 그러하지 아니하다", "지원 대상은 다음과 같다", "필요한 사항은 따로 정할 수 있다",
                "위원회가 이를 의결했다", "제도가 개선됐다", "다음 각 호의 사항을 포함한다:", "제3조를 준용한다.", "삭제한다.")

    def test_titles_with_outline_heads_stay_headings(self):
        """An outline head is the author's title whatever it says; only the sentence of a numbered clause is left a numbered
        paragraph. With every guard of the size headings, `1. 캐나다` (it ends in `다`), a title with its revision date and a
        year were lost."""
        head = ('<hh:numberings itemCnt="1"><hh:numbering id="1" start="1">'
                '<hh:paraHead level="1" numFormat="DIGIT">^1.</hh:paraHead></hh:numbering></hh:numberings>'
                '<hh:paraProperties itemCnt="2"><hh:paraPr id="0"><hh:heading type="NONE" idRef="0" level="0"/></hh:paraPr>'
                '<hh:paraPr id="1"><hh:heading type="OUTLINE" idRef="0" level="0"/></hh:paraPr></hh:paraProperties>')
        titles = ["캐나다", "플로리다", "총칙(개정 2024. 1. 1.)", "2024년 12월", "Introduction", "왜 바다인가?",
                  "Ⅳ. 사례: 미국, 영국, 캐나다", "우간다", "동아프리카 르완다", "미래가 온다", "Appendix A.", "조직도 ...",
                  "아름답다"]
        body = ('<hp:p paraPrIDRef="0" styleIDRef="0"><hp:run><hp:secPr outlineShapeIDRef="1"/><hp:t>보고서</hp:t></hp:run></hp:p>'
                + "".join(_hwpx_paragraph(title, shape=1) for title in titles) + _hwpx_paragraph(_BODY))

        lines = convert(_hwpx(body, head=head), "a.hwpx").text.split("\n\n")

        assert [line for line in lines if line.startswith("#")] == [f"# {n}. {t}" for n, t in enumerate(titles, 1)]


def _hwp_box(*cells: list[tuple[str, int]]) -> bytes:
    """A top-level table of one column; each cell is its paragraphs as (text, character shape id)."""
    record = builder.hwp_record
    body = b"".join(
        record(TAG_LIST_HEADER, 2, builder.hwp_cell(row, 0))
        + b"".join(record(TAG_PARA_HEADER, 2, _para_header(0)) + record(TAG_PARA_TEXT, 3, builder.hwp_text(text))
                   + record(TAG_PARA_CHAR_SHAPE, 3, struct.pack("<II", 0, shape)) for text, shape in cell)
        for row, cell in enumerate(cells))
    return (record(TAG_PARA_HEADER, 0, _para_header(0)) + record(TAG_PARA_TEXT, 1, builder.hwp_text((11, "tbl ")))
            + record(TAG_CTRL_HEADER, 1, builder.hwp_ctrl("tbl ")) + record(TAG_TABLE, 2, struct.pack("<IHH", 0, len(cells), 1) + b"\x00" * 10)
            + body)


def _hwpx_box(*cells: list[tuple[str, int]]) -> str:
    rows = "".join("<hp:tr><hp:tc><hp:subList>" + "".join(_hwpx_paragraph(text, (shape, text)) for text, shape in cell)
                   + f'</hp:subList><hp:cellAddr colAddr="0" rowAddr="{row}"/></hp:tc></hp:tr>' for row, cell in enumerate(cells))
    return f'<hp:p paraPrIDRef="0" styleIDRef="0"><hp:run charPrIDRef="0"><hp:tbl rowCnt="{len(cells)}" colCnt="1">{rows}</hp:tbl></hp:run></hp:p>'


class TestTitlesInBoxes:
    """A table of one column in the top-level body is a layout box, written as paragraphs; a paragraph of it set as a title is
    a heading by its size (`SizeHeadings.box`). The title of a Korean press release stands in such a box: of 60 releases of
    November 2026, 47 had no heading at all as HWPX while their PDF had the title as one (`evals/README.md`)."""

    _TITLE = [("산림청, 전국 산지관리", 2), ("담당자 워크숍 개최", 2)]
    _SUMMARY = [("- 산지관리 제도 개선 방향을 논의했다", 3), ("- 우수 사례를 공유했다", 3)]

    def _both(self, *cells: list[tuple[str, int]]) -> tuple[str, str]:
        return (convert(_hwp(_hwp_box(*cells), _paragraph(_BODY), _paragraph(_BODY)), "a.hwp").text,
                convert(_hwpx(_hwpx_box(*cells), _hwpx_paragraph(_BODY), _hwpx_paragraph(_BODY)), "a.hwpx").text)

    def test_the_title_of_a_box_is_a_heading_and_its_lines_are_one(self):
        for text in self._both(self._TITLE, self._SUMMARY):
            blocks = text.split("\n\n")
            assert blocks[0] == "# 산림청, 전국 산지관리 담당자 워크숍 개최"
            assert blocks[1] == "- 산지관리 제도 개선 방향을 논의했다\n- 우수 사례를 공유했다"
            assert blocks[2].startswith("본문은")

    def test_text_around_the_title_in_its_cell_is_written_as_before(self):
        for text in self._both([("보도자료", 0), *self._TITLE, ("배포 즉시 보도", 0)]):
            assert text.split("\n\n")[:3] == ["보도자료", "# 산림청, 전국 산지관리 담당자 워크숍 개최", "배포 즉시 보도"]

    def test_a_box_without_a_title_is_written_as_before(self):
        plain = [("참고 사항", 0), ("문의는 담당 부서로 하십시오", 0)]
        for text in self._both(plain, self._SUMMARY):
            assert "#" not in text
            assert text.startswith("참고 사항\n문의는 담당 부서로 하십시오\n\n- 산지관리 제도 개선 방향을 논의했다\n- 우수 사례를 공유했다\n\n본문은")

    def test_a_line_only_a_little_larger_is_no_heading_in_a_box(self):
        """1.1 times the body: a heading of level 3 at the top level needs 1.15, one in a box 1.3."""
        for text in self._both([("추진 배경", 3)], self._SUMMARY):
            assert "#" not in text

    def test_a_box_is_measured_against_the_size_most_of_the_document_has(self):
        """The text of the document stands in the box, in 13 pt; the one line outside it is 9 pt and is not its body."""
        box = [("추진 배경", 1)] + [("본문을 상자 안에 열세 포인트로 길게 적은 문서입니다 " * 3, 1)] * 4
        hwp = convert(_hwp(_hwp_box(box), _paragraph("- 1 -", (0, 4))), "a.hwp").text
        hwpx = convert(_hwpx(_hwpx_box(box), _hwpx_paragraph("- 1 -", (4, "- 1 -"))), "a.hwpx").text

        assert "#" not in hwp and "#" not in hwpx

    def test_a_sentence_or_a_date_in_a_box_is_no_heading(self):
        """A sentence with its full stop, one in the polite style, a question and a date, each set as large as a title."""
        for line in ("오늘 행사를 개최했다.", "오늘 행사를 개최했습니다", "왜 필요한가?", "2026년 11월 20일"):
            for text in self._both([(line, 2)], self._SUMMARY):
                assert "#" not in text

    def test_a_headline_in_a_box_is_a_heading_though_it_ends_as_a_sentence_does(self):
        """A press release is titled in the plain style without a full stop ('… 공략한다', '… 나눈다', '… 올렸다'): of 59
        releases 5 lost their title to the sentence guard. Outside a box the same line is a sentence set large, as before."""
        for headline in ("K-스마트농업, 중국 시장 공략한다", "‘글로벌 대도약’ 깃발 올렸다"):
            for text in self._both([(headline, 2)], self._SUMMARY):
                assert text.split("\n\n")[0] == f"# {headline}"
        for text in self._both([("국제교류 경험·성과", 2), ("7개국 청소년과 나눈다", 2)], self._SUMMARY):
            assert text.split("\n\n")[0] == "# 국제교류 경험·성과 7개국 청소년과 나눈다"
        top_level = convert(_hwp(_paragraph("K-스마트농업, 중국 시장 공략한다", (0, 2)), _paragraph(_BODY), _paragraph(_BODY)), "a.hwp").text
        assert "#" not in top_level

    def test_a_table_inside_a_box_with_a_title_is_kept(self):
        """The box is unfolded by the table itself, which holds the tables of its cells: unfolded from the text of the
        cell, the table inside it was lost and its placeholder left a `0` in the text."""
        inner = ('<hp:p paraPrIDRef="0" styleIDRef="0"><hp:run charPrIDRef="0"><hp:tbl rowCnt="2" colCnt="2">'
                 + "".join("<hp:tr>" + "".join(
                     f'<hp:tc><hp:subList>{_hwpx_paragraph(f"r{row}c{col}")}</hp:subList><hp:cellAddr colAddr="{col}" rowAddr="{row}"/></hp:tc>'
                     for col in range(2)) + "</hp:tr>" for row in range(2))
                 + "</hp:tbl></hp:run></hp:p>")
        box = ('<hp:p paraPrIDRef="0" styleIDRef="0"><hp:run charPrIDRef="0"><hp:tbl rowCnt="1" colCnt="1"><hp:tr><hp:tc><hp:subList>'
               + _hwpx_paragraph("큰 제목", (2, "큰 제목")) + _hwpx_paragraph("설명 줄") + inner + _hwpx_paragraph("표 아래 줄")
               + '</hp:subList><hp:cellAddr colAddr="0" rowAddr="0"/></hp:tc></hp:tr></hp:tbl></hp:run></hp:p>')

        text = convert(_hwpx(box, _hwpx_paragraph(_BODY), _hwpx_paragraph(_BODY)), "a.hwpx").text

        assert text.split("\n\n")[:4] == ["# 큰 제목", "설명 줄", "| r0c0 | r0c1 |\n| --- | --- |\n| r1c0 | r1c1 |", "표 아래 줄"]
